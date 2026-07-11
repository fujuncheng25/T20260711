from __future__ import annotations

import ctypes
import io
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd
import pyautogui
import pygetwindow as gw
import pyperclip
from flask import Flask, abort, redirect, render_template_string, request, send_file, url_for

BASE_URL = (
    "https://trade.tmall.com/detail/orderDetail.htm"
    "?spm=tbpc.boughtlist.order_detail.1.6c622e8dEevWzv"
    "&bizOrderId={order_id}"
)

EMPTY_LIKE = {"", "nan", "none", "null", "/", "\\", "-"}
TRACKING_PATTERN = re.compile(
    r"\b(?:SF|YT|JT|JDAP|DPK|LP|ZTO|STO|EMS|YUNDA|JD|DBK)?[A-Z0-9]{10,24}\b",
    re.IGNORECASE,
)
PAYMENT_DATETIME_PATTERN = re.compile(
    r"(20\d{2}[年\-./\s]\d{1,2}[月\-./\s]\d{1,2}(?:日)?\s*\d{1,2}:\d{2}(?::\d{2})?)"
)
ARRIVAL_PATTERN = re.compile(
    r"(?:预计|已于)?\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}[^\n]{0,16}(?:送达|签收)?"
    r"|(?:\d{1,2}[/-]\d{1,2}[^\n]{0,16}(?:送达|签收))"
)
COURIER_PREFIX_PATTERN = re.compile(
    r"^(?:SF|YT|JT|JDAP|DPK|LP|ZTO|STO|EMS|YUNDA|JD|DBK)", re.IGNORECASE
)
DEFAULT_PAGE_WAIT_SECONDS = 20.0
DEFAULT_ORDER_INTERVAL_SECONDS = 20.0
MIN_PAGE_WAIT_SECONDS = 10.0
MIN_ORDER_INTERVAL_SECONDS = 10.0

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
RESULT_STORE: dict[str, dict[str, object]] = {}
FIREFOX_LOCK = threading.Lock()


@dataclass
class OrderInfo:
    item_name: str = ""
    tracking_no: str = ""
    arrival_time: str = ""
    payment_time: str = ""


class FirefoxAccessLimitedError(RuntimeError):
    """Raised when Firefox shows an access-control or rate-limit page."""


def is_empty_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    value_text = str(value).strip()
    return not value_text or value_text.lower() in EMPTY_LIKE


def clean_text_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


def extract_tracking_no(lines: list[str], page_text: str) -> str:
    logistics_terms = ("运单", "物流", "快递", "包裹", "顺丰", "圆通", "中通", "京东", "邮政", "韵达")

    for line in lines:
        if not any(term in line for term in logistics_terms):
            continue
        candidates = TRACKING_PATTERN.findall(line.replace(" ", ""))
        for candidate in candidates:
            if COURIER_PREFIX_PATTERN.match(candidate):
                return candidate
        for candidate in candidates:
            if 10 <= len(candidate) <= 16 and not candidate.isalpha():
                return candidate

    candidates = TRACKING_PATTERN.findall(page_text.replace(" ", ""))
    for candidate in candidates:
        if COURIER_PREFIX_PATTERN.match(candidate):
            return candidate
    for candidate in candidates:
        # Tmall order IDs are commonly 19 digits; avoid using those as a tracking number.
        if 10 <= len(candidate) <= 16 and not candidate.isalpha():
            return candidate
    return ""


def extract_payment_time(lines: list[str]) -> str:
    for index, line in enumerate(lines):
        if "付款时间" not in line and "支付时间" not in line:
            continue

        for candidate_line in lines[index : index + 3]:
            matched = PAYMENT_DATETIME_PATTERN.search(candidate_line)
            if matched:
                return re.sub(r"\s+", " ", matched.group(1)).strip()
    return ""


def extract_arrival_time(lines: list[str], page_text: str) -> str:
    for line in lines:
        if not any(term in line for term in ("送达", "签收", "预计")):
            continue
        matched = ARRIVAL_PATTERN.search(line)
        if matched:
            return matched.group(0).strip()
        return line

    matched = ARRIVAL_PATTERN.search(page_text)
    return matched.group(0).strip() if matched else ""


def extract_item_name(lines: list[str], tracking_no: str) -> str:
    """Find a likely item title in copied, rendered order-page text."""
    ignored_terms = (
        "订单详情",
        "订单信息",
        "付款详情",
        "查看物流",
        "确认收货",
        "加入购物车",
        "退款",
        "付款",
        "卖家已",
        "物流服务",
        "订单号",
        "交易号",
        "支付方式",
        "收货地址",
        "订单服务",
        "猜你喜欢",
        "累计",
        "实付款",
        "商品总价",
        "运费",
        "展开全部商品",
        "申请售后",
    )

    tracking_index = -1
    if tracking_no:
        for index, line in enumerate(lines):
            if tracking_no in line:
                tracking_index = index
                break

    if tracking_index >= 0:
        # Product titles are normally displayed directly beneath the package/logistics line.
        search_lines = lines[tracking_index + 1 : tracking_index + 25] + lines[:tracking_index]
    else:
        search_lines = lines

    candidates: list[tuple[int, str]] = []
    for line in search_lines:
        if len(line) < 3 or len(line) > 180:
            continue
        if any(term in line for term in ignored_terms):
            continue
        if re.search(r"(?:¥|￥|订单号|交易号|x\d+\b)", line, re.IGNORECASE):
            continue
        if re.fullmatch(r"[\d\s.,:：/-]+", line):
            continue

        score = 0
        if re.search(r"[\u4e00-\u9fff]", line):
            score += 4
        if re.search(r"\d", line):
            score += 2
        if re.search(r"(?:M\d|\*|×|X\d|[【\[（(].+[】\]）)])", line, re.IGNORECASE):
            score += 2
        if tracking_index >= 0 and line in lines[tracking_index + 1 : tracking_index + 12]:
            score += 2
        if 4 <= len(line) <= 100:
            score += 1
        if score >= 4:
            candidates.append((score, line))

    return max(candidates, default=(0, ""), key=lambda item: item[0])[1]


def _find_firefox_window():
    """Return the active Firefox window, otherwise the largest visible Firefox window."""
    try:
        active = gw.getActiveWindow()
        if active and "firefox" in (active.title or "").lower():
            return active

        candidates = [
            window
            for window in gw.getAllWindows()
            if "firefox" in (window.title or "").lower()
            and window.width > 300
            and window.height > 200
        ]
    except Exception as exc:
        raise RuntimeError(f"无法读取 Windows 窗口列表: {exc}") from exc

    if not candidates:
        raise RuntimeError("未找到已打开的 Firefox 窗口。请先打开并登录天猫后再开始补全。")
    return max(candidates, key=lambda window: window.width * window.height)


def _window_handle(window) -> int:
    try:
        return int(getattr(window, "_hWnd", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _foreground_window_handle() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except (AttributeError, OSError):
        return 0


class ExistingFirefoxScraper:
    """Controls an existing visible Firefox window; it never launches a browser process."""

    def __init__(
        self,
        page_wait_seconds: float,
        status_cb: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._page_wait_seconds = page_wait_seconds
        self._status_cb = status_cb
        self._firefox_window = _find_firefox_window()
        self._firefox_handle = _window_handle(self._firefox_window)
        self._previous_window = None
        self._previous_clipboard = ""
        self._temporary_tab_opened = False

    def _report(self, level: str, message: str) -> None:
        if self._status_cb:
            self._status_cb(level, message)

    def _activate_firefox(self) -> None:
        try:
            if getattr(self._firefox_window, "isMinimized", False):
                self._firefox_window.restore()
            if self._firefox_handle:
                user32 = ctypes.windll.user32
                user32.BringWindowToTop(self._firefox_handle)
                user32.SetForegroundWindow(self._firefox_handle)
            self._firefox_window.activate()
        except Exception as exc:
            raise RuntimeError(f"无法激活现有 Firefox 窗口: {exc}") from exc
        time.sleep(0.8)

        active_window = gw.getActiveWindow()
        active_handle = _foreground_window_handle()
        is_expected_handle = bool(self._firefox_handle and active_handle == self._firefox_handle)
        is_firefox_title = bool(
            active_window and "firefox" in (active_window.title or "").lower()
        )
        is_foreground = is_expected_handle if self._firefox_handle else is_firefox_title
        if not is_foreground:
            raise RuntimeError(
                "Firefox 未成为前台窗口。为避免快捷键发到其他程序，本次不会打开或读取订单；"
                "请将 Firefox 放到最前面后重试。"
            )

    def __enter__(self) -> "ExistingFirefoxScraper":
        try:
            self._previous_window = gw.getActiveWindow()
        except Exception:
            self._previous_window = None

        try:
            self._previous_clipboard = pyperclip.paste()
        except Exception:
            self._previous_clipboard = ""

        self._activate_firefox()
        pyautogui.hotkey("ctrl", "t")
        time.sleep(0.8)
        self._temporary_tab_opened = True
        self._report(
            "info",
            "已确认当前有头 Firefox 在前台，并已在同一窗口请求新建临时标签页；不点击页面元素。",
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if self._temporary_tab_opened:
                try:
                    self._activate_firefox()
                except RuntimeError:
                    self._report("warning", "Firefox 未在前台，临时标签页未自动关闭。")
                else:
                    pyautogui.hotkey("ctrl", "w")
                    time.sleep(0.35)
        finally:
            try:
                pyperclip.copy(self._previous_clipboard)
            except Exception:
                pass
            try:
                if self._previous_window and self._previous_window != self._firefox_window:
                    self._previous_window.activate()
            except Exception:
                pass

    def _copy_rendered_page_text(self) -> str:
        self._activate_firefox()
        clipboard_marker = "__TMALL_CSV_COPY_PENDING__"
        try:
            pyperclip.copy(clipboard_marker)
        except Exception:
            clipboard_marker = ""
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.6)
        try:
            page_text = str(pyperclip.paste()).strip()
        except Exception as exc:
            raise RuntimeError(f"无法从 Firefox 复制已渲染页面内容: {exc}") from exc

        if clipboard_marker and page_text == clipboard_marker:
            raise RuntimeError("Firefox 未复制到页面正文；请确认浏览器窗口未被遮挡且未最小化。")
        return page_text

    @staticmethod
    def _is_order_detail_page(text: str) -> bool:
        return any(marker in text for marker in ("订单详情", "订单信息", "付款详情", "订单服务"))

    @staticmethod
    def _is_login_or_blocked_page(text: str) -> bool:
        markers = ("请登录", "扫码登录", "密码登录", "访问受限", "操作频繁", "系统繁忙")
        return any(marker in text for marker in markers)

    def get_order_page_text(self, order_id: str) -> str:
        url = BASE_URL.format(order_id=order_id)
        self._activate_firefox()
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.2)
        pyautogui.write(url, interval=0.01)
        pyautogui.press("enter")

        self._report(
            "info",
            f"订单 {order_id} 已打开；固定等待 {self._page_wait_seconds:g} 秒让页面自然加载，不点击订单页内容。",
        )
        time.sleep(self._page_wait_seconds)
        page_text = self._copy_rendered_page_text()

        if self._is_login_or_blocked_page(page_text):
            if any(marker in page_text for marker in ("访问受限", "操作频繁", "系统繁忙")):
                raise FirefoxAccessLimitedError(
                    "Firefox 显示访问受限或操作频繁提示，已停止后续订单，避免继续发起请求。"
                )
            raise RuntimeError("当前 Firefox 未保持天猫登录态；请先在该 Firefox 中完成登录后重试。")
        if not self._is_order_detail_page(page_text):
            raise RuntimeError("未读到订单详情页正文；本订单不会重试。")
        return page_text


def scrape_order_detail(order_id: str, firefox: ExistingFirefoxScraper) -> OrderInfo:
    page_text = firefox.get_order_page_text(order_id)
    lines = clean_text_lines(page_text)
    tracking_no = extract_tracking_no(lines, page_text)
    return OrderInfo(
        item_name=extract_item_name(lines, tracking_no),
        tracking_no=tracking_no,
        arrival_time=extract_arrival_time(lines, page_text),
        payment_time=extract_payment_time(lines),
    )


def fill_csv(
    df: pd.DataFrame,
    order_col: str,
    item_col: str,
    tracking_col: str,
    arrival_col: str,
    purchase_time_col: str,
    page_wait_seconds: float = DEFAULT_PAGE_WAIT_SECONDS,
    order_interval_seconds: float = DEFAULT_ORDER_INTERVAL_SECONDS,
    status_cb: Optional[Callable[[str, str], None]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> pd.DataFrame:
    """Fill only blank supported cells by operating the already-open Firefox GUI."""
    df_out = df.copy()

    def report(level: str, message: str) -> None:
        if status_cb:
            status_cb(level, message)

    def report_progress(done: int, total: int) -> None:
        if progress_cb:
            progress_cb(done, total)

    if order_col not in df_out.columns:
        raise ValueError(f"CSV 中找不到订单列: {order_col}")
    if page_wait_seconds < MIN_PAGE_WAIT_SECONDS:
        raise ValueError(f"页面加载等待时间不能少于 {MIN_PAGE_WAIT_SECONDS:g} 秒。")
    if order_interval_seconds < MIN_ORDER_INTERVAL_SECONDS:
        raise ValueError(f"订单间隔不能少于 {MIN_ORDER_INTERVAL_SECONDS:g} 秒。")

    target_columns = [
        column
        for column in (item_col, tracking_col, arrival_col, purchase_time_col)
        if column in df_out.columns
    ]
    if not target_columns:
        raise ValueError("CSV 中找不到任何可补全列（物品简述、快递单号、到达时间、购买时间）。")

    target_indices: list[int] = []
    for index, row in df_out.iterrows():
        order_id = str(row.get(order_col, "")).strip()
        if is_empty_value(order_id) or order_id in {"/", "\\"}:
            continue
        if any(is_empty_value(row.get(column, None)) for column in target_columns):
            target_indices.append(index)

    total = len(target_indices)
    if total == 0:
        report("info", "没有发现需要补全的空白单元格。")
        report_progress(1, 1)
        return df_out

    changed_cells = 0
    stopped_for_access_limit = False
    with FIREFOX_LOCK:
        with ExistingFirefoxScraper(page_wait_seconds, status_cb) as firefox:
            for position, index in enumerate(target_indices, start=1):
                row = df_out.loc[index]
                order_id = str(row[order_col]).strip()
                report("info", f"({position}/{total}) 正在通过现有 Firefox 打开订单: {order_id}")

                try:
                    info = scrape_order_detail(order_id, firefox)
                except FirefoxAccessLimitedError as exc:
                    report("warning", f"订单 {order_id} 已停止：{exc}")
                    report_progress(position, total)
                    stopped_for_access_limit = True
                    break
                except Exception as exc:
                    report("warning", f"订单 {order_id} 抓取失败: {exc}")
                    report_progress(position, total)
                else:
                    filled_fields: list[str] = []
                    if item_col in df_out.columns and is_empty_value(df_out.at[index, item_col]) and info.item_name:
                        df_out.at[index, item_col] = info.item_name
                        filled_fields.append(item_col)
                        changed_cells += 1

                    if tracking_col in df_out.columns and is_empty_value(df_out.at[index, tracking_col]) and info.tracking_no:
                        df_out.at[index, tracking_col] = info.tracking_no
                        filled_fields.append(tracking_col)
                        changed_cells += 1

                    if arrival_col in df_out.columns and is_empty_value(df_out.at[index, arrival_col]) and info.arrival_time:
                        df_out.at[index, arrival_col] = info.arrival_time
                        filled_fields.append(arrival_col)
                        changed_cells += 1

                    if (
                        purchase_time_col in df_out.columns
                        and is_empty_value(df_out.at[index, purchase_time_col])
                        and info.payment_time
                    ):
                        df_out.at[index, purchase_time_col] = info.payment_time
                        filled_fields.append(purchase_time_col)
                        changed_cells += 1

                    if filled_fields:
                        report("info", f"订单 {order_id} 已补全: {', '.join(filled_fields)}")
                    else:
                        report("warning", f"订单 {order_id} 已打开，但未识别到可填入的空白字段。")
                    report_progress(position, total)

                if position < total:
                    report(
                        "info",
                        f"固定等待 {order_interval_seconds:g} 秒后再打开下一订单；期间不会点击页面元素。",
                    )
                    time.sleep(order_interval_seconds)

    if stopped_for_access_limit:
        report("warning", f"任务因访问限制提前停止，已写入 {changed_cells} 个空白单元格。")
    else:
        report("success", f"补全完成，共写入 {changed_cells} 个空白单元格。")
    return df_out


def build_download_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8-sig")


INDEX_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tmall 订单 CSV 自动补全</title>
  <style>
    :root { --ink:#172033; --muted:#64748b; --line:#dbe3ef; --orange:#f05a28; --blue:#276fed; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; font-family:"Segoe UI","Microsoft YaHei",sans-serif; color:var(--ink); background:linear-gradient(135deg,#fff6ef,#eef6ff); }
    main { max-width:900px; margin:36px auto; padding:0 16px; }
    section { background:#fff; border:1px solid var(--line); border-radius:18px; box-shadow:0 16px 42px rgba(20,43,86,.10); overflow:hidden; }
    header { padding:24px; background:linear-gradient(115deg,#fff,#fff4ec); border-bottom:1px solid var(--line); }
    h1 { margin:0; color:var(--orange); font-size:25px; }
    header p { color:var(--muted); margin:8px 0 0; }
    form { padding:24px; display:grid; gap:15px; }
    .fields { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
    label { display:block; color:var(--muted); font-size:13px; margin-bottom:6px; }
    input { width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:9px; font:inherit; }
    .notice { padding:12px; border-radius:10px; font-size:14px; background:#fff7e9; border:1px solid #ffd89e; color:#76500c; line-height:1.55; }
    .error { padding:11px 12px; border-radius:10px; color:#a41c2c; background:#fff0f2; border:1px solid #ffc8d0; }
    button { width:max-content; border:0; border-radius:10px; color:#fff; background:linear-gradient(120deg,var(--orange),var(--blue)); padding:11px 17px; font:600 15px inherit; cursor:pointer; }
    footer { padding:0 24px 23px; color:var(--muted); font-size:13px; }
    @media (max-width:640px) { .fields { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <main>
    <section>
      <header>
        <h1>Tmall 订单 CSV 自动补全</h1>
        <p>只补空白单元格；程序直接控制当前已打开的有头 Firefox，不会新启动浏览器进程。</p>
      </header>
      <form method="post" action="{{ url_for('process_csv') }}" enctype="multipart/form-data">
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <div>
          <label>上传 CSV 文件</label>
          <input type="file" name="csv_file" accept=".csv" required>
        </div>
        <div class="fields">
          <div><label>订单号列名</label><input type="text" name="order_col" value="订单号"></div>
          <div><label>物品名称列名</label><input type="text" name="item_col" value="物品简述"></div>
          <div><label>快递单号列名</label><input type="text" name="tracking_col" value="快递单号"></div>
          <div><label>到达时间列名</label><input type="text" name="arrival_col" value="到达时间"></div>
          <div><label>购买时间列名（填入付款时间）</label><input type="text" name="purchase_time_col" value="购买时间"></div>
                      <div><label>页面加载等待（秒，至少 10）</label><input type="number" name="page_wait_seconds" min="10" step="1" value="20"></div>
                      <div><label>订单间固定等待（秒，至少 10）</label><input type="number" name="order_interval_seconds" min="10" step="1" value="20"></div>
        </div>
                <div class="notice">开始前，请确认当前 Firefox 已登录淘宝/天猫且窗口可见。每个订单只执行：在临时标签页地址栏打开一次订单 URL → 固定等待 → 键盘复制一次已渲染正文。不会点击订单、物流、确认收货或任何页面按钮；不会轮询或重试同一个订单。若出现访问受限/操作频繁提示，任务会立即停止。</div>
        <button type="submit">开始补全并生成下载</button>
      </form>
      <footer>如果某个订单抓取失败，会保留原值；结果页日志会显示每个订单的成功、失败与实际写入列。</footer>
    </section>
  </main>
</body>
</html>
"""


RESULT_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>补全完成</title>
  <style>
    body { margin:0; font-family:"Segoe UI","Microsoft YaHei",sans-serif; color:#172033; background:#f4f7fb; }
    main { max-width:900px; margin:36px auto; padding:0 16px; }
    section { background:#fff; border:1px solid #dbe3ef; border-radius:18px; padding:24px; box-shadow:0 16px 42px rgba(20,43,86,.10); }
    h1 { margin:0 0 18px; color:#168246; }
    .stats { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:18px; }
    .stat { padding:12px; border:1px solid #dbe3ef; border-radius:10px; background:#f9fbff; }
    .btn { display:inline-block; padding:11px 17px; background:linear-gradient(120deg,#f05a28,#276fed); color:#fff; border-radius:10px; text-decoration:none; font-weight:600; }
    .back { display:inline-block; margin-left:15px; color:#276fed; }
    pre { margin:18px 0 0; max-height:340px; overflow:auto; padding:13px; border-radius:10px; background:#111827; color:#e5e7eb; white-space:pre-wrap; line-height:1.5; font:12px Consolas,monospace; }
    @media (max-width:640px) { .stats { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <main><section>
    <h1>补全完成</h1>
    <div class="stats">
      <div class="stat">总行数：{{ total_rows }}</div>
      <div class="stat">变化单元格：{{ changed_cells }}</div>
      <div class="stat">待补全订单：{{ target_rows }}</div>
    </div>
    <a class="btn" href="{{ url_for('download_result', token=token) }}">下载补全后的 CSV</a>
    <a class="back" href="{{ url_for('index') }}">返回上传页</a>
    <pre>{{ logs_text }}</pre>
  </section></main>
</body>
</html>
"""


def _form_text(name: str, fallback: str) -> str:
    return (request.form.get(name) or "").strip() or fallback


def _form_seconds(field_name: str, label: str, default: float, minimum: float) -> float:
    raw_value = (request.form.get(field_name) or "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须是数字。") from exc
    if value < minimum:
        raise ValueError(f"{label} 不能少于 {minimum:g} 秒。")
    return value


def _count_changed_cells(before: pd.DataFrame, after: pd.DataFrame) -> int:
    left = before.fillna("").astype(str)
    right = after.fillna("").astype(str)
    return int((left != right).sum().sum())


def _count_target_rows(
    df: pd.DataFrame,
    order_col: str,
    target_columns: tuple[str, ...],
) -> int:
    if order_col not in df.columns:
        return 0
    existing_columns = [column for column in target_columns if column in df.columns]
    return sum(
        not is_empty_value(row.get(order_col, ""))
        and any(is_empty_value(row.get(column, None)) for column in existing_columns)
        for _, row in df.iterrows()
    )


@app.get("/")
def index():
    return render_template_string(INDEX_HTML, error=request.args.get("error", ""))


@app.post("/process")
def process_csv():
    uploaded = request.files.get("csv_file")
    if not uploaded or not uploaded.filename:
        return redirect(url_for("index", error="请先上传 CSV 文件。"))

    order_col = _form_text("order_col", "订单号")
    item_col = _form_text("item_col", "物品简述")
    tracking_col = _form_text("tracking_col", "快递单号")
    arrival_col = _form_text("arrival_col", "到达时间")
    purchase_time_col = _form_text("purchase_time_col", "购买时间")
    try:
        page_wait_seconds = _form_seconds(
            "page_wait_seconds",
            "页面加载等待",
            DEFAULT_PAGE_WAIT_SECONDS,
            MIN_PAGE_WAIT_SECONDS,
        )
        order_interval_seconds = _form_seconds(
            "order_interval_seconds",
            "订单间固定等待",
            DEFAULT_ORDER_INTERVAL_SECONDS,
            MIN_ORDER_INTERVAL_SECONDS,
        )
    except ValueError as exc:
        return redirect(url_for("index", error=str(exc)))

    try:
        original = pd.read_csv(uploaded, dtype=str, keep_default_na=False)
    except Exception as exc:
        return redirect(url_for("index", error=f"CSV 读取失败: {exc}"))

    logs: list[str] = []

    def status_cb(level: str, message: str) -> None:
        logs.append(f"[{level}] {message}")

    def progress_cb(done: int, total: int) -> None:
        logs.append(f"[progress] {done}/{total}")

    target_rows = _count_target_rows(
        original,
        order_col,
        (item_col, tracking_col, arrival_col, purchase_time_col),
    )

    try:
        completed = fill_csv(
            df=original,
            order_col=order_col,
            item_col=item_col,
            tracking_col=tracking_col,
            arrival_col=arrival_col,
            purchase_time_col=purchase_time_col,
            page_wait_seconds=page_wait_seconds,
            order_interval_seconds=order_interval_seconds,
            status_cb=status_cb,
            progress_cb=progress_cb,
        )
    except Exception as exc:
        return redirect(url_for("index", error=f"补全过程失败: {exc}"))

    token = secrets.token_urlsafe(12)
    RESULT_STORE[token] = {
        "bytes": build_download_bytes(completed),
        "total_rows": len(original),
        "target_rows": target_rows,
        "changed_cells": _count_changed_cells(original, completed),
        "logs": logs[-250:],
    }
    return redirect(url_for("result_page", token=token))


@app.get("/result/<token>")
def result_page(token: str):
    result = RESULT_STORE.get(token)
    if result is None:
        abort(404)
    return render_template_string(
        RESULT_HTML,
        token=token,
        total_rows=result["total_rows"],
        target_rows=result["target_rows"],
        changed_cells=result["changed_cells"],
        logs_text="\n".join(result["logs"]) or "无日志输出",
    )


@app.get("/download/<token>")
def download_result(token: str):
    result = RESULT_STORE.get(token)
    if result is None:
        abort(404)
    return send_file(
        io.BytesIO(result["bytes"]),
        as_attachment=True,
        download_name="completed_orders.csv",
        mimetype="text/csv",
    )


def main() -> None:
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
