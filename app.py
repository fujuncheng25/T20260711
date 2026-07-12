from __future__ import annotations

import datetime as dt
import hmac
import os
import re
import secrets
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Iterable

import numpy as np
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from PIL import Image, ImageOps
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, desc, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, scoped_session, sessionmaker
from werkzeug.utils import secure_filename

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

DB_PATH = Path(os.getenv("SQLITE_DB_PATH", str(APP_DIR / "logistics_alert.db"))).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

DB_BACKUP_DIR = APP_DIR / "db_backups"
MAX_DB_BACKUPS = 10
SQLITE_HEADER = b"SQLite format 3\x00"

PORT = int(os.getenv("PORT", "5000"))
MAX_CONTENT_LENGTH_BYTES = 300 * 1024 * 1024

UI_BUILD_ID = "20260711_200500_94731"
UI_CSS_FILE = f"ui_{UI_BUILD_ID}.css"
UI_JS_FILE = f"ui_{UI_BUILD_ID}.js"

DEFAULT_REMINDER_MESSAGE = "您的快递到了"
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
ORDER_TOKEN_PATTERN = re.compile(r"[A-Z0-9]{8,32}")

# OCR commonly confuses these glyphs on courier labels; keeping a mapped variant
# improves suffix matching stability without requiring perfect full-string OCR.
OCR_CONFUSION_MAP = {
    "O": "0",
    "D": "0",
    "Q": "0",
    "I": "1",
    "L": "1",
    "Z": "2",
    "S": "5",
    "B": "8",
}

_RAPID_OCR = None
_RAPID_OCR_LOCK = threading.Lock()

_EASY_OCR_READER = None
_EASY_OCR_LOCK = threading.Lock()


class Base(DeclarativeBase):
    pass


class ReminderRule(Base):
    __tablename__ = "reminder_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watcher_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    order_suffix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    custom_message: Mapped[str] = mapped_column(String(240), nullable=False, default=DEFAULT_REMINDER_MESSAGE)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=dt.datetime.utcnow)

    notifications: Mapped[list["Notification"]] = relationship(back_populates="reminder")


class PickupLog(Base):
    __tablename__ = "pickup_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uploader_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    image_filename: Mapped[str] = mapped_column(String(260), nullable=False)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recognized_order_no: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=dt.datetime.utcnow, index=True)

    notifications: Mapped[list["Notification"]] = relationship(back_populates="pickup_log")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminder_rules.id"), nullable=False, index=True)
    pickup_log_id: Mapped[int] = mapped_column(ForeignKey("pickup_logs.id"), nullable=False, index=True)
    watcher_name: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    matched_order_no: Mapped[str] = mapped_column(String(80), nullable=False)
    order_suffix: Mapped[str] = mapped_column(String(32), nullable=False)
    uploader_name: Mapped[str] = mapped_column(String(80), nullable=False)
    image_filename: Mapped[str] = mapped_column(String(260), nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=dt.datetime.utcnow)

    reminder: Mapped[ReminderRule] = relationship(back_populates="notifications")
    pickup_log: Mapped[PickupLog] = relationship(back_populates="notifications")


engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False},
)
SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
SessionLocal = scoped_session(SessionFactory)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me-before-production")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH_BYTES


def init_db() -> None:
    Base.metadata.create_all(engine)


@app.context_processor
def inject_template_context() -> dict[str, object]:
    is_super_admin = False
    if request:
        is_super_admin = is_loopback_request()
    return {
        "ui_build_id": UI_BUILD_ID,
        "ui_css_file": UI_CSS_FILE,
        "ui_js_file": UI_JS_FILE,
        "is_super_admin": is_super_admin,
    }


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_token(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", clean_text(value).upper())


def digits_only(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


def merge_unique_lines(lines: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for raw_line in lines:
        text = clean_text(raw_line)
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged


def token_variants(token: str) -> list[str]:
    mapped = "".join(OCR_CONFUSION_MAP.get(char, char) for char in token)
    if mapped != token:
        return [token, mapped]
    return [token]


def extract_order_candidates(recognized_parts: Iterable[str]) -> list[str]:
    candidates: dict[str, bool] = {}
    for part in recognized_parts:
        normalized_part = clean_text(part).upper()
        if not normalized_part:
            continue
        for token in ORDER_TOKEN_PATTERN.findall(normalized_part):
            base = normalize_token(token)
            for variant in token_variants(base):
                if len(variant) < 8:
                    continue
                if sum(char.isdigit() for char in variant) < 6:
                    continue
                candidates[variant] = True
    return sorted(candidates.keys(), key=lambda item: (-len(item), item))


def suffix_matches(candidate: str, suffix: str) -> bool:
    if candidate.endswith(suffix):
        return True

    suffix_digits = digits_only(suffix)
    if len(suffix_digits) < 4:
        return False
    return digits_only(candidate).endswith(suffix_digits)


def is_loopback_request() -> bool:
    """Allow admin actions only from the same machine (127.0.0.1/::1)."""
    remote_addr = (request.remote_addr or "").strip()
    return remote_addr in LOOPBACK_ADDRESSES or remote_addr.startswith("127.")


def get_rapid_ocr():
    global _RAPID_OCR
    if _RAPID_OCR is not None:
        return _RAPID_OCR

    with _RAPID_OCR_LOCK:
        if _RAPID_OCR is None:
            from rapidocr_onnxruntime import RapidOCR

            _RAPID_OCR = RapidOCR()
    return _RAPID_OCR


def decode_rapidocr_lines(np_image: np.ndarray) -> list[str]:
    try:
        reader = get_rapid_ocr()
    except Exception:
        return []

    try:
        results, _ = reader(np_image)
    except Exception:
        return []

    lines: list[str] = []
    for row in results or []:
        if not isinstance(row, (list, tuple)):
            continue
        text = clean_text(row[1] if len(row) > 1 else "")
        if text:
            lines.append(text)
    return lines


def get_easyocr_reader():
    global _EASY_OCR_READER
    if _EASY_OCR_READER is not None:
        return _EASY_OCR_READER

    with _EASY_OCR_LOCK:
        if _EASY_OCR_READER is None:
            import easyocr
            import torch

            _EASY_OCR_READER = easyocr.Reader(
                ["ch_sim", "en"],
                gpu=torch.cuda.is_available(),
                verbose=False,
            )
    return _EASY_OCR_READER


def decode_easyocr_lines(np_image: np.ndarray) -> list[str]:
    try:
        reader = get_easyocr_reader()
    except Exception:
        return []

    try:
        results = reader.readtext(np_image, detail=1, paragraph=False)
    except Exception:
        return []

    lines: list[str] = []
    for row in results:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        text = clean_text(row[1])
        if text:
            lines.append(text)
    return lines


def decode_barcodes(np_image: np.ndarray) -> list[str]:
    try:
        import zxingcpp
    except Exception:
        return []

    try:
        rows = zxingcpp.read_barcodes(np_image)
    except Exception:
        return []

    lines: list[str] = []
    for row in rows:
        text = clean_text(getattr(row, "text", ""))
        if text:
            lines.append(text)
    return lines


def analyze_pickup_image(image_path: Path) -> tuple[list[str], list[str], list[str], str]:
    try:
        image = Image.open(image_path).convert("RGB")
    except Exception as exc:
        raise ValueError("图片解析失败，请上传清晰的 JPG/PNG 图片。") from exc

    rgb_array = np.array(image)
    gray_array = np.array(ImageOps.autocontrast(ImageOps.grayscale(image)))

    barcode_lines = decode_barcodes(rgb_array)
    rapidocr_lines = decode_rapidocr_lines(gray_array)
    easyocr_lines = decode_easyocr_lines(gray_array)

    ocr_lines = merge_unique_lines(rapidocr_lines + easyocr_lines)
    recognized_parts = merge_unique_lines(barcode_lines + ocr_lines)
    candidates = extract_order_candidates(recognized_parts)
    extracted_text = "\n".join(recognized_parts)
    return candidates, barcode_lines, ocr_lines, extracted_text


def is_valid_sqlite_file(path: Path) -> bool:
    try:
        with open(path, "rb") as file_obj:
            if file_obj.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                return False

        conn = sqlite3.connect(str(path))
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(result) and str(result[0]).lower() == "ok"
        finally:
            conn.close()
    except Exception:
        return False


def create_db_backup() -> Path | None:
    if not DB_PATH.exists():
        return None

    DB_BACKUP_DIR.mkdir(exist_ok=True)
    backup_name = f"{DB_PATH.stem}_{dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db"
    backup_path = DB_BACKUP_DIR / backup_name
    shutil.copy2(DB_PATH, backup_path)

    backups = sorted(DB_BACKUP_DIR.glob(f"{DB_PATH.stem}_*.db"))
    while len(backups) > MAX_DB_BACKUPS:
        oldest = backups.pop(0)
        oldest.unlink(missing_ok=True)
    return backup_path


@app.before_request
def open_db_session():
    g.db = SessionLocal()


@app.teardown_request
def close_db_session(error):
    db_session = g.pop("db", None)
    if db_session is None:
        SessionLocal.remove()
        return
    try:
        if error is not None:
            db_session.rollback()
    finally:
        db_session.close()
        SessionLocal.remove()


@app.get("/")
def index():
    db_session = g.db

    reminders = db_session.scalars(
        select(ReminderRule).order_by(desc(ReminderRule.created_at)).limit(300)
    ).all()
    notifications = db_session.scalars(
        select(Notification).order_by(desc(Notification.created_at)).limit(300)
    ).all()

    today = dt.date.today()
    start = dt.datetime.combine(today, dt.time.min)
    end = start + dt.timedelta(days=1)
    today_logs = db_session.scalars(
        select(PickupLog)
        .where(PickupLog.created_at >= start, PickupLog.created_at < end)
        .order_by(desc(PickupLog.created_at))
    ).all()

    return render_template(
        "index.html",
        reminders=reminders,
        notifications=notifications,
        today_logs=today_logs,
        today=today,
        can_admin=is_loopback_request(),
        default_reminder_message=DEFAULT_REMINDER_MESSAGE,
    )


@app.post("/reminders/create")
def create_reminder():
    watcher_name = clean_text(request.form.get("watcher_name"))
    order_suffix = normalize_token(request.form.get("order_suffix"))
    custom_message = clean_text(request.form.get("custom_message")) or DEFAULT_REMINDER_MESSAGE

    if len(watcher_name) < 1:
        flash("提醒接收人不能为空。", "danger")
        return redirect(url_for("index"))
    if len(order_suffix) < 4:
        flash("快递尾号至少输入 4 位。", "danger")
        return redirect(url_for("index"))

    db_session = g.db
    db_session.add(
        ReminderRule(
            watcher_name=watcher_name,
            order_suffix=order_suffix,
            custom_message=custom_message,
            is_active=True,
        )
    )
    db_session.commit()
    flash(f"提醒已创建：{watcher_name} / 尾号 {order_suffix}", "success")
    return redirect(url_for("index"))


@app.post("/reminders/<int:reminder_id>/toggle")
def toggle_reminder(reminder_id: int):
    db_session = g.db
    reminder = db_session.get(ReminderRule, reminder_id)
    if reminder is None:
        flash("提醒不存在。", "danger")
        return redirect(url_for("index"))

    reminder.is_active = not reminder.is_active
    db_session.commit()
    state_text = "启用" if reminder.is_active else "停用"
    flash(f"提醒已{state_text}：{reminder.watcher_name} / 尾号 {reminder.order_suffix}", "info")
    return redirect(url_for("index"))


@app.post("/scan")
def scan_and_record():
    uploader_name = clean_text(request.form.get("uploader_name"))
    uploaded = request.files.get("pickup_image")

    if len(uploader_name) < 1:
        flash("请填写拍照上传人。", "danger")
        return redirect(url_for("index"))
    if not uploaded or not uploaded.filename:
        flash("请上传图片文件。", "danger")
        return redirect(url_for("index"))

    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        flash("图片格式不支持，请上传 JPG/PNG/WEBP/BMP/TIF。", "danger")
        return redirect(url_for("index"))

    unique_filename = (
        f"{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}_"
        f"{secure_filename(uploaded.filename)}"
    )
    image_path = UPLOAD_DIR / unique_filename
    uploaded.save(image_path)

    try:
        candidates, barcode_lines, ocr_lines, extracted_text = analyze_pickup_image(image_path)
    except Exception as exc:
        image_path.unlink(missing_ok=True)
        flash(f"OCR 识别失败：{exc}", "danger")
        return redirect(url_for("index"))

    db_session = g.db
    pickup_log = PickupLog(
        uploader_name=uploader_name,
        image_filename=unique_filename,
        extracted_text=extracted_text,
        recognized_order_no=(candidates[0] if candidates else ""),
    )
    db_session.add(pickup_log)
    db_session.flush()

    reminder_rules = db_session.scalars(
        select(ReminderRule).where(ReminderRule.is_active.is_(True)).order_by(ReminderRule.created_at.asc())
    ).all()

    reminder_count = 0
    for rule in reminder_rules:
        matched_candidate = next((candidate for candidate in candidates if suffix_matches(candidate, rule.order_suffix)), None)
        if not matched_candidate:
            continue

        title = f"{rule.watcher_name} 的快递提醒"
        custom_message = clean_text(rule.custom_message) or DEFAULT_REMINDER_MESSAGE
        body = (
            f"{custom_message}\n"
            f"拍照人: {uploader_name}\n"
            f"命中尾号: {rule.order_suffix}\n"
            f"识别单号: {matched_candidate}"
        )
        db_session.add(
            Notification(
                reminder_id=rule.id,
                pickup_log_id=pickup_log.id,
                watcher_name=rule.watcher_name,
                title=title,
                body=body,
                matched_order_no=matched_candidate,
                order_suffix=rule.order_suffix,
                uploader_name=uploader_name,
                image_filename=unique_filename,
                is_read=False,
            )
        )
        reminder_count += 1

    db_session.commit()

    flash(
        (
            f"上传已记录到今日日志：拍照人 {uploader_name}。"
            f"条码识别 {len(barcode_lines)} 条，OCR 识别 {len(ocr_lines)} 条，"
            f"订单候选 {len(candidates)} 个，触发提醒 {reminder_count} 条。"
        ),
        "success",
    )
    return redirect(url_for("index"))


@app.post("/notifications/<int:notification_id>/read")
def mark_notification_read(notification_id: int):
    db_session = g.db
    notification = db_session.get(Notification, notification_id)
    if notification is None:
        flash("提醒不存在。", "danger")
        return redirect(url_for("index"))

    if not notification.is_read:
        notification.is_read = True
        db_session.commit()
    return redirect(url_for("index"))


@app.get("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/admin/")
def admin_dashboard():
    if not is_loopback_request():
        abort(404)

    DB_BACKUP_DIR.mkdir(exist_ok=True)
    backups = sorted(DB_BACKUP_DIR.glob("*.db"), reverse=True)
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    return render_template(
        "admin_dashboard.html",
        db_path=str(DB_PATH),
        db_exists=DB_PATH.exists(),
        db_size=(DB_PATH.stat().st_size if DB_PATH.exists() else 0),
        backups=backups,
        max_db_backups=MAX_DB_BACKUPS,
        csrf_token=session["csrf_token"],
    )


@app.get("/admin/database/download")
def admin_download_database():
    if not is_loopback_request():
        abort(404)
    if not DB_PATH.exists():
        abort(404)

    return send_file(
        DB_PATH,
        as_attachment=True,
        download_name=DB_PATH.name,
        mimetype="application/vnd.sqlite3",
    )


@app.post("/admin/database/upload")
def admin_upload_database():
    if not is_loopback_request():
        abort(404)

    submitted_token = clean_text(request.form.get("csrf_token"))
    session_token = clean_text(session.get("csrf_token"))
    if not session_token or not hmac.compare_digest(submitted_token, session_token):
        flash("安全校验失败，请重新提交。", "danger")
        return redirect(url_for("admin_dashboard"))

    uploaded = request.files.get("db_file")
    if not uploaded or not uploaded.filename:
        flash("请先选择要上传的 SQLite 数据库文件。", "danger")
        return redirect(url_for("admin_dashboard"))

    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in {".db", ".sqlite", ".sqlite3"}:
        flash("仅允许上传 .db/.sqlite/.sqlite3 文件。", "danger")
        return redirect(url_for("admin_dashboard"))

    temp_path = DB_PATH.with_name(DB_PATH.name + ".upload_tmp")
    uploaded.save(temp_path)

    if not is_valid_sqlite_file(temp_path):
        temp_path.unlink(missing_ok=True)
        flash("上传文件不是有效的 SQLite 数据库，已拒绝覆盖。", "danger")
        return redirect(url_for("admin_dashboard"))

    try:
        engine.dispose()
        backup_path = create_db_backup()
        shutil.move(str(temp_path), str(DB_PATH))
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        flash(f"覆盖数据库失败：{exc}", "danger")
        return redirect(url_for("admin_dashboard"))

    backup_note = f"，覆盖前已自动备份为 {backup_path.name}" if backup_path else "（覆盖前没有可备份的旧数据库）"
    flash(f"数据库已成功覆盖{backup_note}。", "success")
    return redirect(url_for("admin_dashboard"))


def main() -> None:
    init_db()
    print(f"公开入口（普通用户）： http://0.0.0.0:{PORT}/")
    print(f"管理入口（仅 127.0.0.1 自动识别为超级管理员，无需登录）： http://127.0.0.1:{PORT}/admin/")
    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
