import configparser
import io
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import browser_cookie3
import pandas as pd
import requests
from bs4 import BeautifulSoup
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
ARRIVAL_PATTERN = re.compile(
    r"(?:\d{1,2}[/-]\d{1,2}(?:[日号]|(?:\s*[上下]午\s*\d{1,2}(?::\d{1,2})?)?)?[^\n]{0,12}(?:送达|签收)|"
    r"(?:预计|已于)?\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}[^\n]{0,12}(?:送达|签收))"
)
PAYMENT_DATETIME_PATTERN = re.compile(
  r"(20\d{2}[年\-./\s]\d{1,2}[月\-./\s]\d{1,2}(?:日)?\s*\d{1,2}:\d{2}(?::\d{2})?)"
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
RESULT_STORE: dict[str, dict[str, object]] = {}


@dataclass
class OrderInfo:
    item_name: str = ""
    tracking_no: str = ""
    arrival_time: str = ""
  payment_time: str = ""


def is_empty_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    text = str(value).strip()
    if not text:
        return True
    return text.lower() in EMPTY_LIKE


def clean_text_lines(raw_text: str) -> list[str]:
    lines: list[str] = []
    for line in raw_text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


def extract_item_name(lines: list[str], fallback_from_dom: str) -> str:
    if fallback_from_dom:
        return fallback_from_dom.strip()

    ignored = {
        "订单详情",
        "订单信息",
        "付款详情",
        "查看物流",
        "确认收货",
        "加入购物车",
        "退款",
        "包裹",
        "付款",
    }

    for line in lines:
        if len(line) < 4 or len(line) > 90:
            continue
        if any(k in line for k in ignored):
            continue
        if any(k in line for k in ["¥", "￥", "x1", "x2", "订单号", "交易"]):
            continue
        if re.search(r"[\u4e00-\u9fff]", line):
            return line
    return ""


def extract_tracking_no(lines: list[str], page_text: str) -> str:
    for line in lines:
        if any(k in line for k in ["运单", "物流", "快递", "包裹"]):
            match = TRACKING_PATTERN.search(line.replace(" ", ""))
            if match:
                return match.group(0)

    candidates = TRACKING_PATTERN.findall(page_text.replace(" ", ""))
    for candidate in candidates:
        if 10 <= len(candidate) <= 20:
            return candidate
    return ""


def extract_arrival_time(lines: list[str], page_text: str) -> str:
    for line in lines:
        if any(k in line for k in ["送达", "签收", "收货"]):
            m = ARRIVAL_PATTERN.search(line)
            if m:
                return m.group(0)
            return line

    m = ARRIVAL_PATTERN.search(page_text)
    return m.group(0) if m else ""


  def _extract_datetime_candidate(text: str) -> str:
    m = PAYMENT_DATETIME_PATTERN.search(text)
    if not m:
      return ""
    value = m.group(1)
    return re.sub(r"\s+", " ", value).strip()


  def extract_payment_time(lines: list[str]) -> str:
    keywords = ("付款时间", "支付时间")

    for i, line in enumerate(lines):
      if any(k in line for k in keywords):
        candidate = _extract_datetime_candidate(line)
        if candidate:
          return candidate

        for offset in (1, 2):
          j = i + offset
          if j < len(lines):
            candidate = _extract_datetime_candidate(lines[j])
            if candidate:
              return candidate

    return ""


def detect_firefox_profile() -> str:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return ""

    profiles_ini = Path(appdata) / "Mozilla" / "Firefox" / "profiles.ini"
    if not profiles_ini.exists():
        return ""

    parser = configparser.ConfigParser()
    try:
        parser.read(profiles_ini, encoding="utf-8")
    except Exception:
        return ""

    install_sections = [s for s in parser.sections() if s.startswith("Install")]
    for section in install_sections:
        default_profile = parser.get(section, "Default", fallback="").strip()
        if default_profile:
            candidate = Path(appdata) / "Mozilla" / "Firefox" / default_profile
            if candidate.exists():
                return str(candidate)

    for section in parser.sections():
        if not section.startswith("Profile"):
            continue
        is_default = parser.get(section, "Default", fallback="0") == "1"
        path_value = parser.get(section, "Path", fallback="").strip()
        is_relative = parser.get(section, "IsRelative", fallback="1") == "1"
        if is_default and path_value:
            candidate = (
                Path(appdata) / "Mozilla" / "Firefox" / path_value
                if is_relative
                else Path(path_value)
            )
            if candidate.exists():
                return str(candidate)

    return ""


def _load_cookie_jar(profile_path: str):
    cookie_file = ""
    if profile_path.strip():
        candidate = Path(profile_path.strip()) / "cookies.sqlite"
        if candidate.exists():
            cookie_file = str(candidate)

    if cookie_file:
        return browser_cookie3.firefox(cookie_file=cookie_file)
    return browser_cookie3.firefox()


def build_session(profile_path: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )

    jar = _load_cookie_jar(profile_path)
    session.cookies.update(jar)
    return session


def scrape_order_detail(order_id: str, session: requests.Session) -> OrderInfo:
    url = BASE_URL.format(order_id=order_id)
    resp = session.get(url, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text

    if "login.taobao.com" in resp.url or "请登录" in html[:3000]:
        raise RuntimeError("登录态失效：请先在当前 Firefox 中登录淘宝/天猫，再重试")

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = clean_text_lines(text)

    dom_item_name = ""
    nodes = soup.select("a[href*='item.htm'], .item-title, .item-name, div[class*='item'] a")
    for node in nodes:
        node_text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if node_text and len(node_text) >= 3:
            dom_item_name = node_text
            break

    return OrderInfo(
        item_name=extract_item_name(lines, dom_item_name),
        tracking_no=extract_tracking_no(lines, text),
        arrival_time=extract_arrival_time(lines, text),
      payment_time=extract_payment_time(lines),
    )


def fill_csv(
    df: pd.DataFrame,
    order_col: str,
    item_col: str,
    tracking_col: str,
    arrival_col: str,
  purchase_time_col: str,
    profile_path: str,
    status_cb: Optional[Callable[[str, str], None]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> pd.DataFrame:
    df_out = df.copy()

    def report(level: str, message: str) -> None:
        if status_cb:
            status_cb(level, message)

    def report_progress(done: int, total: int) -> None:
        if progress_cb:
            progress_cb(done, total)

    if order_col not in df_out.columns:
        raise ValueError(f"CSV 中找不到订单列: {order_col}")

    target_indices: list[int] = []
    for idx, row in df_out.iterrows():
        order_id = str(row.get(order_col, "")).strip()
        if is_empty_value(order_id) or order_id in {"/", "\\"}:
            continue

        need_fill = False
        for col in [item_col, tracking_col, arrival_col, purchase_time_col]:
            if col in df_out.columns and is_empty_value(row.get(col, None)):
                need_fill = True
                break
        if need_fill:
            target_indices.append(idx)

    total = len(target_indices)
    if total == 0:
        report("info", "没有发现需要补全的空白单元格。")
        report_progress(1, 1)
        return df_out

    effective_profile = profile_path.strip() or detect_firefox_profile()
    session = build_session(effective_profile)

    if effective_profile:
        report("info", f"已复用 Firefox Profile: {effective_profile}")
    else:
        report("warning", "未检测到 Firefox Profile，将尝试系统默认 Cookie")

    for i, idx in enumerate(target_indices, start=1):
        row = df_out.loc[idx]
        order_id = str(row[order_col]).strip()
        report("info", f"({i}/{total}) 正在抓取订单: {order_id}")

        try:
            info = scrape_order_detail(order_id, session)
        except Exception as exc:
            report("warning", f"订单 {order_id} 抓取失败: {exc}")
            report_progress(i, total)
            continue

        if item_col in df_out.columns and is_empty_value(df_out.at[idx, item_col]) and info.item_name:
            df_out.at[idx, item_col] = info.item_name

        if tracking_col in df_out.columns and is_empty_value(df_out.at[idx, tracking_col]) and info.tracking_no:
            df_out.at[idx, tracking_col] = info.tracking_no

        if arrival_col in df_out.columns and is_empty_value(df_out.at[idx, arrival_col]) and info.arrival_time:
            df_out.at[idx, arrival_col] = info.arrival_time

        if (
          purchase_time_col in df_out.columns
          and is_empty_value(df_out.at[idx, purchase_time_col])
          and info.payment_time
        ):
          df_out.at[idx, purchase_time_col] = info.payment_time

        report_progress(i, total)

    report("success", "补全完成。你可以下载结果 CSV。")
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
    :root {
      --bg1: #fdf6ec;
      --bg2: #e9f5ff;
      --card: #ffffff;
      --ink: #1c2230;
      --muted: #5f6b7a;
      --brand: #e85d2a;
      --brand-2: #2a7be8;
      --ok: #127a44;
      --warn: #9c5d00;
      --bad: #ad1725;
      --line: #d8dee8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at 20% 0%, var(--bg2), transparent 35%),
                  radial-gradient(circle at 80% 100%, #ffe8db, transparent 35%),
                  var(--bg1);
      min-height: 100vh;
    }
    .shell {
      max-width: 980px;
      margin: 34px auto;
      padding: 0 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: 0 14px 35px rgba(31, 48, 77, 0.08);
      overflow: hidden;
    }
    .head {
      padding: 22px 24px;
      background: linear-gradient(120deg, #fff, #fff7f2);
      border-bottom: 1px solid var(--line);
    }
    .head h1 {
      margin: 0;
      font-size: 24px;
      color: var(--brand);
      letter-spacing: 0.3px;
    }
    .head p {
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 14px;
    }
    form {
      padding: 24px;
      display: grid;
      gap: 14px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    @media (max-width: 760px) {
      .grid { grid-template-columns: 1fr; }
    }
    label {
      display: block;
      font-size: 13px;
      margin-bottom: 6px;
      color: var(--muted);
    }
    input[type="text"], input[type="file"] {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 14px;
      background: #fff;
    }
    .hint {
      font-size: 12px;
      color: var(--muted);
      margin-top: -4px;
    }
    .alert {
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 14px;
      border: 1px solid;
    }
    .alert.error {
      color: var(--bad);
      background: #ffecef;
      border-color: #ffc1c9;
    }
    .submit {
      border: 0;
      border-radius: 12px;
      background: linear-gradient(120deg, var(--brand), var(--brand-2));
      color: #fff;
      padding: 11px 16px;
      font-size: 15px;
      cursor: pointer;
      width: fit-content;
    }
    .footer-note {
      padding: 0 24px 22px;
      font-size: 13px;
      color: var(--muted);
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="card">
      <div class="head">
        <h1>Tmall 订单 CSV 自动补全</h1>
        <p>仅补空白单元格，不覆盖已有内容。不会新开 Firefox，直接复用你当前登录态。</p>
      </div>
      <form method="post" action="{{ url_for('process_csv') }}" enctype="multipart/form-data">
        {% if error %}
          <div class="alert error">{{ error }}</div>
        {% endif %}

        <div>
          <label>上传 CSV 文件</label>
          <input type="file" name="csv_file" accept=".csv" required>
        </div>

        <div class="grid">
          <div>
            <label>订单号列名</label>
            <input type="text" name="order_col" value="订单号">
          </div>
          <div>
            <label>物品名称列名</label>
            <input type="text" name="item_col" value="物品简述">
          </div>
          <div>
            <label>快递单号列名</label>
            <input type="text" name="tracking_col" value="快递单号">
          </div>
          <div>
            <label>到达时间列名</label>
            <input type="text" name="arrival_col" value="到达时间">
          </div>
          <div>
            <label>购买时间列名（将填入付款时间）</label>
            <input type="text" name="purchase_time_col" value="购买时间">
          </div>
        </div>

        <div>
          <label>Firefox Profile 路径（可选）</label>
          <input type="text" name="profile_path" value="{{ default_profile }}">
          <div class="hint">留空会自动检测默认 Profile。示例：C:/Users/用户名/AppData/Roaming/Mozilla/Firefox/Profiles/xxxx.default-release</div>
        </div>

        <button class="submit" type="submit">开始补全并生成下载</button>
      </form>
      <div class="footer-note">如果某条订单抓取失败，该行将保持原值，不会破坏已有数据。</div>
    </div>
  </div>
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
    body {
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: #f4f7fb;
      color: #1c2230;
    }
    .wrap {
      max-width: 980px;
      margin: 34px auto;
      padding: 0 16px;
    }
    .card {
      background: #fff;
      border: 1px solid #d8dee8;
      border-radius: 16px;
      padding: 20px;
      box-shadow: 0 12px 30px rgba(31, 48, 77, 0.08);
    }
    h1 {
      margin-top: 0;
      color: #127a44;
    }
    .meta {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }
    .meta .item {
      border: 1px solid #d8dee8;
      border-radius: 10px;
      padding: 10px;
      background: #f9fbff;
      font-size: 14px;
    }
    @media (max-width: 760px) {
      .meta { grid-template-columns: 1fr; }
    }
    a.btn {
      display: inline-block;
      text-decoration: none;
      background: linear-gradient(120deg, #e85d2a, #2a7be8);
      color: #fff;
      padding: 10px 16px;
      border-radius: 10px;
      font-weight: 600;
    }
    .logs {
      margin-top: 18px;
      background: #111827;
      color: #e5e7eb;
      border-radius: 10px;
      padding: 12px;
      max-height: 300px;
      overflow: auto;
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      white-space: pre-wrap;
      line-height: 1.5;
    }
    .back {
      margin-top: 14px;
      display: inline-block;
      color: #2a7be8;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>补全完成</h1>
      <div class="meta">
        <div class="item">总行数：{{ total_rows }}</div>
        <div class="item">变化单元格：{{ changed_cells }}</div>
        <div class="item">任务 ID：{{ token }}</div>
      </div>
      <a class="btn" href="{{ url_for('download_result', token=token) }}">下载补全后的 CSV</a>
      <a class="back" href="{{ url_for('index') }}">返回上传页</a>
      <div class="logs">{{ logs_text }}</div>
    </div>
  </div>
</body>
</html>
"""


def _to_text(value: Optional[str], fallback: str) -> str:
    value = (value or "").strip()
    return value if value else fallback


def _count_changed_cells(before_df: pd.DataFrame, after_df: pd.DataFrame) -> int:
    before = before_df.fillna("").astype(str)
    after = after_df.fillna("").astype(str)
    return int((before != after).sum().sum())


@app.get("/")
def index():
    return render_template_string(
        INDEX_HTML,
        error=request.args.get("error", ""),
        default_profile=detect_firefox_profile(),
    )


@app.post("/process")
def process_csv():
    uploaded = request.files.get("csv_file")
    if not uploaded or not uploaded.filename:
        return redirect(url_for("index", error="请先上传 CSV 文件"))

    order_col = _to_text(request.form.get("order_col"), "订单号")
    item_col = _to_text(request.form.get("item_col"), "物品简述")
    tracking_col = _to_text(request.form.get("tracking_col"), "快递单号")
    arrival_col = _to_text(request.form.get("arrival_col"), "到达时间")
    purchase_time_col = _to_text(request.form.get("purchase_time_col"), "购买时间")
    profile_path = _to_text(request.form.get("profile_path"), "")

    try:
        df = pd.read_csv(uploaded, dtype=str, keep_default_na=False)
    except Exception as exc:
        return redirect(url_for("index", error=f"CSV 读取失败: {exc}"))

    logs: list[str] = []

    def status_cb(level: str, message: str) -> None:
        logs.append(f"[{level}] {message}")

    def progress_cb(done: int, total: int) -> None:
        logs.append(f"[progress] {done}/{total}")

    try:
        completed = fill_csv(
            df=df,
            order_col=order_col,
            item_col=item_col,
            tracking_col=tracking_col,
            arrival_col=arrival_col,
            purchase_time_col=purchase_time_col,
            profile_path=profile_path,
            status_cb=status_cb,
            progress_cb=progress_cb,
        )
    except Exception as exc:
        return redirect(url_for("index", error=f"补全过程失败: {exc}"))

    output_bytes = build_download_bytes(completed)
    token = secrets.token_urlsafe(12)
    RESULT_STORE[token] = {
        "bytes": output_bytes,
        "total_rows": len(df),
        "changed_cells": _count_changed_cells(df, completed),
        "logs": logs[-180:],
    }

    return redirect(url_for("result_page", token=token))


@app.get("/result/<token>")
def result_page(token: str):
    data = RESULT_STORE.get(token)
    if data is None:
        abort(404)

    logs_text = "\n".join(data["logs"]) if data.get("logs") else "无日志输出"
    return render_template_string(
        RESULT_HTML,
        token=token,
        total_rows=data["total_rows"],
        changed_cells=data["changed_cells"],
        logs_text=logs_text,
    )


@app.get("/download/<token>")
def download_result(token: str):
    data = RESULT_STORE.get(token)
    if data is None:
        abort(404)

    return send_file(
        io.BytesIO(data["bytes"]),
        as_attachment=True,
        download_name="completed_orders.csv",
        mimetype="text/csv",
    )


def main() -> None:
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
