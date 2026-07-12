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
from functools import wraps
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
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, desc, inspect, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, scoped_session, sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash
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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=dt.datetime.utcnow)


class UserGroup(Base):
    __tablename__ = "user_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=dt.datetime.utcnow)


class GroupMember(Base):
    __tablename__ = "group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=dt.datetime.utcnow)


class ReminderRule(Base):
    __tablename__ = "reminder_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watcher_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    order_suffix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    custom_message: Mapped[str] = mapped_column(String(240), nullable=False, default=DEFAULT_REMINDER_MESSAGE)
    target_group_id: Mapped[int | None] = mapped_column(ForeignKey("user_groups.id"), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=dt.datetime.utcnow)


class PickupLog(Base):
    __tablename__ = "pickup_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uploader_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    image_filename: Mapped[str] = mapped_column(String(260), nullable=False)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recognized_order_no: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=dt.datetime.utcnow, index=True)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminder_rules.id"), nullable=False, index=True)
    pickup_log_id: Mapped[int] = mapped_column(ForeignKey("pickup_logs.id"), nullable=False, index=True)
    watcher_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    matched_order_no: Mapped[str] = mapped_column(String(80), nullable=False)
    order_suffix: Mapped[str] = mapped_column(String(32), nullable=False)
    uploader_name: Mapped[str] = mapped_column(String(80), nullable=False)
    image_filename: Mapped[str] = mapped_column(String(260), nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=dt.datetime.utcnow)


EXPECTED_REBUILD_COLUMNS: dict[str, tuple[str, ...]] = {
    "pickup_logs": (
        "id",
        "uploader_name",
        "image_filename",
        "extracted_text",
        "recognized_order_no",
        "created_at",
    ),
    "notifications": (
        "id",
        "reminder_id",
        "pickup_log_id",
        "watcher_name",
        "title",
        "body",
        "matched_order_no",
        "order_suffix",
        "uploader_name",
        "image_filename",
        "is_read",
        "created_at",
    ),
}

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


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def rebuild_legacy_tables_if_needed() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    rebuild_plan: list[tuple[str, tuple[str, ...], set[str], list[str]]] = []

    for table_name, expected_columns in EXPECTED_REBUILD_COLUMNS.items():
        if table_name not in existing_tables:
            continue

        current_columns = {row["name"] for row in inspector.get_columns(table_name)}
        missing_columns = [col for col in expected_columns if col not in current_columns]
        if missing_columns:
            rebuild_plan.append((table_name, expected_columns, current_columns, missing_columns))

    if not rebuild_plan:
        return

    stamp = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    renamed_tables: list[tuple[str, str, tuple[str, ...], set[str], list[str]]] = []

    with engine.begin() as conn:
        for table_name, expected_columns, current_columns, missing_columns in rebuild_plan:
            index_names = [
                idx["name"]
                for idx in inspector.get_indexes(table_name)
                if clean_text(idx.get("name"))
            ]
            legacy_name = f"{table_name}_legacy_{stamp}"
            conn.exec_driver_sql(
                f"ALTER TABLE {quote_identifier(table_name)} RENAME TO {quote_identifier(legacy_name)}"
            )
            for index_name in index_names:
                conn.exec_driver_sql(f"DROP INDEX IF EXISTS {quote_identifier(index_name)}")
            renamed_tables.append((table_name, legacy_name, expected_columns, current_columns, missing_columns))

    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        for table_name, legacy_name, expected_columns, current_columns, _ in renamed_tables:
            if table_name == "notifications":
                continue

            copy_columns = [col for col in expected_columns if col in current_columns]
            if not copy_columns:
                continue

            quoted = ", ".join(quote_identifier(col) for col in copy_columns)
            conn.exec_driver_sql(
                f"INSERT INTO {quote_identifier(table_name)} ({quoted}) "
                f"SELECT {quoted} FROM {quote_identifier(legacy_name)}"
            )

    migration_report = "; ".join(
        f"{table_name} missing [{', '.join(missing_columns)}]"
        for table_name, _, _, _, missing_columns in renamed_tables
    )
    print(f"[schema] Rebuilt incompatible legacy tables: {migration_report}")


def ensure_column_exists(table_name: str, column_name: str, column_ddl: str) -> None:
    with engine.begin() as conn:
        rows = conn.exec_driver_sql(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
        current_names = {str(row[1]) for row in rows}
        if column_name in current_names:
            return
        conn.exec_driver_sql(
            f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN {quote_identifier(column_name)} {column_ddl}"
        )


def init_db() -> None:
    Base.metadata.create_all(engine)
    rebuild_legacy_tables_if_needed()
    Base.metadata.create_all(engine)
    ensure_column_exists("reminder_rules", "target_group_id", "INTEGER")


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


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if getattr(g, "current_user", None) is None:
            flash("请先登录。", "warning")
            next_url = clean_text(request.path)
            return redirect(url_for("login", next=next_url))
        return view_func(*args, **kwargs)

    return wrapped


def get_group_membership(db_session, user_id: int, group_id: int) -> GroupMember | None:
    return db_session.scalar(
        select(GroupMember).where(GroupMember.user_id == user_id, GroupMember.group_id == group_id)
    )


def list_groups_for_user(db_session, user_id: int) -> list[dict[str, object]]:
    rows = db_session.execute(
        select(UserGroup.id, UserGroup.name)
        .join(GroupMember, GroupMember.group_id == UserGroup.id)
        .where(GroupMember.user_id == user_id)
        .order_by(UserGroup.name.asc())
    ).all()
    return [{"id": int(row[0]), "name": str(row[1])} for row in rows]


@app.before_request
def open_db_session():
    g.db = SessionLocal()
    g.current_user = None

    raw_user_id = session.get("user_id")
    if raw_user_id is None:
        return

    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        session.pop("user_id", None)
        return

    user = g.db.get(User, user_id)
    if user is None:
        session.pop("user_id", None)
        return

    g.current_user = user


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


@app.context_processor
def inject_template_context() -> dict[str, object]:
    return {
        "ui_build_id": UI_BUILD_ID,
        "ui_css_file": UI_CSS_FILE,
        "ui_js_file": UI_JS_FILE,
        "is_super_admin": is_loopback_request() if request else False,
        "current_user": getattr(g, "current_user", None),
    }


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.current_user is not None:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = clean_text(request.form.get("username"))
        password = clean_text(request.form.get("password"))

        if len(username) < 2:
            flash("用户名至少 2 个字符。", "danger")
            return redirect(url_for("register"))
        if len(password) < 4:
            flash("密码至少 4 位。", "danger")
            return redirect(url_for("register"))

        db_session = g.db
        existing = db_session.scalar(select(User).where(User.username == username))
        if existing is not None:
            flash("用户名已存在。", "danger")
            return redirect(url_for("register"))

        user = User(username=username, password_hash=generate_password_hash(password))
        db_session.add(user)
        db_session.commit()

        session["user_id"] = user.id
        flash("注册成功，已自动登录。", "success")
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.current_user is not None:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = clean_text(request.form.get("username"))
        password = clean_text(request.form.get("password"))

        db_session = g.db
        user = db_session.scalar(select(User).where(User.username == username))
        if user is None or not check_password_hash(user.password_hash, password):
            flash("用户名或密码错误。", "danger")
            return redirect(url_for("login"))

        session["user_id"] = user.id
        next_url = clean_text(request.args.get("next"))
        if not next_url.startswith("/"):
            next_url = url_for("index")

        flash(f"欢迎回来，{user.username}。", "success")
        return redirect(next_url)

    return render_template("login.html")


@app.get("/logout")
def logout():
    session.pop("user_id", None)
    flash("你已退出登录。", "info")
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    db_session = g.db
    current_username = g.current_user.username

    reminders = db_session.scalars(
        select(ReminderRule)
        .where(ReminderRule.watcher_name == current_username)
        .order_by(desc(ReminderRule.created_at))
        .limit(200)
    ).all()

    notifications = db_session.scalars(
        select(Notification)
        .where(Notification.watcher_name == current_username)
        .order_by(desc(Notification.created_at))
        .limit(300)
    ).all()

    today = dt.date.today()
    start = dt.datetime.combine(today, dt.time.min)
    end = start + dt.timedelta(days=1)
    today_logs = db_session.scalars(
        select(PickupLog)
        .where(PickupLog.created_at >= start, PickupLog.created_at < end)
        .order_by(desc(PickupLog.created_at))
        .limit(300)
    ).all()

    user_groups = list_groups_for_user(db_session, g.current_user.id)
    group_name_by_id = {item["id"]: item["name"] for item in user_groups}

    missing_group_ids = {
        int(rule.target_group_id)
        for rule in reminders
        if rule.target_group_id is not None and int(rule.target_group_id) not in group_name_by_id
    }
    if missing_group_ids:
        rows = db_session.execute(
            select(UserGroup.id, UserGroup.name).where(UserGroup.id.in_(sorted(missing_group_ids)))
        ).all()
        for row in rows:
            group_name_by_id[int(row[0])] = str(row[1])

    return render_template(
        "index.html",
        reminders=reminders,
        notifications=notifications,
        today_logs=today_logs,
        today=today,
        user_groups=user_groups,
        group_name_by_id=group_name_by_id,
        default_reminder_message=DEFAULT_REMINDER_MESSAGE,
    )


@app.post("/reminders/create")
@login_required
def create_reminder():
    order_suffix = normalize_token(request.form.get("order_suffix"))
    custom_message = clean_text(request.form.get("custom_message")) or DEFAULT_REMINDER_MESSAGE
    target_group_raw = clean_text(request.form.get("target_group_id"))

    if len(order_suffix) < 4:
        flash("快递尾号至少输入 4 位。", "danger")
        return redirect(url_for("index"))

    target_group_id: int | None = None
    if target_group_raw:
        try:
            target_group_id = int(target_group_raw)
        except ValueError:
            flash("分组参数错误。", "danger")
            return redirect(url_for("index"))

        membership = get_group_membership(g.db, g.current_user.id, target_group_id)
        if membership is None:
            flash("你不在这个分组里，不能把提醒发给该组。", "danger")
            return redirect(url_for("index"))

    reminder = ReminderRule(
        watcher_name=g.current_user.username,
        order_suffix=order_suffix,
        custom_message=custom_message,
        target_group_id=target_group_id,
        is_active=True,
    )
    g.db.add(reminder)
    g.db.commit()

    if target_group_id is None:
        flash(f"提醒已创建（个人）：{g.current_user.username} / 尾号 {order_suffix}", "success")
    else:
        flash(f"提醒已创建（分组）：尾号 {order_suffix}", "success")
    return redirect(url_for("index"))


@app.post("/reminders/<int:reminder_id>/toggle")
@login_required
def toggle_reminder(reminder_id: int):
    reminder = g.db.scalar(
        select(ReminderRule).where(
            ReminderRule.id == reminder_id,
            ReminderRule.watcher_name == g.current_user.username,
        )
    )
    if reminder is None:
        flash("提醒不存在，或你无权操作。", "danger")
        return redirect(url_for("index"))

    reminder.is_active = not reminder.is_active
    g.db.commit()
    state_text = "启用" if reminder.is_active else "停用"
    flash(f"提醒已{state_text}：尾号 {reminder.order_suffix}", "info")
    return redirect(url_for("index"))


@app.post("/scan")
@login_required
def scan_and_record():
    uploaded = request.files.get("pickup_image")
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

    uploader_name = g.current_user.username
    pickup_log = PickupLog(
        uploader_name=uploader_name,
        image_filename=unique_filename,
        extracted_text=extracted_text,
        recognized_order_no=(candidates[0] if candidates else ""),
    )
    g.db.add(pickup_log)
    g.db.flush()

    reminder_rules = g.db.scalars(
        select(ReminderRule)
        .where(ReminderRule.is_active.is_(True))
        .order_by(ReminderRule.created_at.asc())
    ).all()

    group_ids = sorted(
        {
            int(rule.target_group_id)
            for rule in reminder_rules
            if rule.target_group_id is not None
        }
    )

    group_name_map: dict[int, str] = {}
    group_member_map: dict[int, list[str]] = {}
    if group_ids:
        name_rows = g.db.execute(
            select(UserGroup.id, UserGroup.name).where(UserGroup.id.in_(group_ids))
        ).all()
        group_name_map = {int(row[0]): str(row[1]) for row in name_rows}

        member_rows = g.db.execute(
            select(GroupMember.group_id, User.username)
            .join(User, User.id == GroupMember.user_id)
            .where(GroupMember.group_id.in_(group_ids))
            .order_by(GroupMember.group_id.asc(), User.username.asc())
        ).all()
        for row in member_rows:
            group_id = int(row[0])
            group_member_map.setdefault(group_id, []).append(str(row[1]))

    matched_rules = 0
    notification_count = 0

    for rule in reminder_rules:
        matched_candidate = next((candidate for candidate in candidates if suffix_matches(candidate, rule.order_suffix)), None)
        if not matched_candidate:
            continue

        matched_rules += 1
        recipients: list[str]
        scope_text: str

        if rule.target_group_id is None:
            recipients = [rule.watcher_name]
            scope_text = "个人提醒"
        else:
            target_group_id = int(rule.target_group_id)
            recipients = sorted(set(group_member_map.get(target_group_id, [])))
            group_name = group_name_map.get(target_group_id, f"组#{target_group_id}")
            scope_text = f"分组提醒: {group_name}"

        if not recipients:
            continue

        custom_message = clean_text(rule.custom_message) or DEFAULT_REMINDER_MESSAGE
        for recipient in recipients:
            title = f"{recipient} 的快递提醒"
            body = (
                f"{custom_message}\n"
                f"拍照人: {uploader_name}\n"
                f"命中尾号: {rule.order_suffix}\n"
                f"识别单号: {matched_candidate}\n"
                f"通知范围: {scope_text}"
            )
            g.db.add(
                Notification(
                    reminder_id=rule.id,
                    pickup_log_id=pickup_log.id,
                    watcher_name=recipient,
                    title=title,
                    body=body,
                    matched_order_no=matched_candidate,
                    order_suffix=rule.order_suffix,
                    uploader_name=uploader_name,
                    image_filename=unique_filename,
                    is_read=False,
                )
            )
            notification_count += 1

    g.db.commit()

    flash(
        (
            f"上传已写入日志（拍照人：{uploader_name}）。"
            f"条码识别 {len(barcode_lines)} 条，OCR 识别 {len(ocr_lines)} 条，"
            f"候选单号 {len(candidates)} 个，命中提醒规则 {matched_rules} 条，生成通知 {notification_count} 条。"
        ),
        "success",
    )
    return redirect(url_for("index"))


@app.get("/settings")
@login_required
def settings_page():
    user_id = g.current_user.id

    my_group_ids = [
        int(row[0])
        for row in g.db.execute(
            select(GroupMember.group_id).where(GroupMember.user_id == user_id)
        ).all()
    ]

    groups: list[dict[str, object]] = []
    if my_group_ids:
        group_rows = g.db.scalars(
            select(UserGroup).where(UserGroup.id.in_(my_group_ids)).order_by(UserGroup.name.asc())
        ).all()
        member_rows = g.db.execute(
            select(GroupMember.group_id, User.username, GroupMember.role)
            .join(User, User.id == GroupMember.user_id)
            .where(GroupMember.group_id.in_(my_group_ids))
            .order_by(GroupMember.group_id.asc(), User.username.asc())
        ).all()

        member_map: dict[int, list[dict[str, str]]] = {}
        for row in member_rows:
            member_map.setdefault(int(row[0]), []).append(
                {"username": str(row[1]), "role": str(row[2])}
            )

        groups = [
            {
                "id": group.id,
                "name": group.name,
                "is_owner": int(group.created_by) == int(user_id),
                "members": member_map.get(int(group.id), []),
            }
            for group in group_rows
        ]

    return render_template("settings.html", groups=groups)


@app.post("/settings/groups/create")
@login_required
def create_group():
    group_name = clean_text(request.form.get("group_name"))
    if len(group_name) < 1:
        flash("分组名称不能为空。", "danger")
        return redirect(url_for("settings_page"))

    existing = g.db.scalar(
        select(UserGroup).where(UserGroup.name == group_name, UserGroup.created_by == g.current_user.id)
    )
    if existing is not None:
        flash("你已创建过同名分组。", "warning")
        return redirect(url_for("settings_page"))

    group = UserGroup(name=group_name, created_by=g.current_user.id)
    g.db.add(group)
    g.db.flush()
    g.db.add(GroupMember(group_id=group.id, user_id=g.current_user.id, role="owner"))
    g.db.commit()

    flash(f"分组已创建：{group_name}", "success")
    return redirect(url_for("settings_page"))


@app.post("/settings/groups/<int:group_id>/members/add")
@login_required
def add_group_member(group_id: int):
    group = g.db.get(UserGroup, group_id)
    if group is None:
        flash("分组不存在。", "danger")
        return redirect(url_for("settings_page"))

    if int(group.created_by) != int(g.current_user.id):
        flash("只有组主可以添加成员。", "danger")
        return redirect(url_for("settings_page"))

    username = clean_text(request.form.get("username"))
    if not username:
        flash("成员用户名不能为空。", "danger")
        return redirect(url_for("settings_page"))

    user_to_add = g.db.scalar(select(User).where(User.username == username))
    if user_to_add is None:
        flash("找不到该用户，请先注册。", "danger")
        return redirect(url_for("settings_page"))

    existing_member = g.db.scalar(
        select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_to_add.id)
    )
    if existing_member is not None:
        flash("该用户已在组内。", "info")
        return redirect(url_for("settings_page"))

    g.db.add(GroupMember(group_id=group_id, user_id=user_to_add.id, role="member"))
    g.db.commit()
    flash(f"已添加成员：{username}", "success")
    return redirect(url_for("settings_page"))


@app.post("/notifications/<int:notification_id>/read")
@login_required
def mark_notification_read(notification_id: int):
    notification = g.db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.watcher_name == g.current_user.username,
        )
    )
    if notification is None:
        flash("提醒不存在。", "danger")
        return redirect(url_for("index"))

    if not notification.is_read:
        notification.is_read = True
        g.db.commit()
    return redirect(url_for("index"))


@app.get("/uploads/<path:filename>")
@login_required
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
    print(f"公开入口（需登录）： http://0.0.0.0:{PORT}/")
    print(f"管理入口（仅 127.0.0.1 可见）： http://127.0.0.1:{PORT}/admin/")
    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
