from __future__ import annotations

import base64
import binascii
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
from typing import Iterable, cast

import numpy as np
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
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

UI_BUILD_ID = "20260712_081500_swipefix"
UI_CSS_FILE = "ui_20260711_200500_94731.css"
UI_JS_FILE = "ui_20260711_200500_94731.js"
SW_JS_FILE = "sw_20260712_050000_localcache.js"

DEFAULT_REMINDER_MESSAGE = "您的快递到了"
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
ORDER_TOKEN_PATTERN = re.compile(r"[A-Z0-9]{8,32}")
THUMBNAIL_MIME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_THUMBNAIL_BYTES = 120 * 1024
LOCAL_STATIC_RETENTION_SECONDS = 90 * 24 * 60 * 60
BANDWIDTH_SAVER_MODE = False

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
    receive_notifications: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=dt.datetime.utcnow)


class ReminderRule(Base):
    __tablename__ = "reminder_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watcher_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    order_suffix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    item_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
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


def to_utc_iso(value: object) -> str:
    if not isinstance(value, dt.datetime):
        return ""

    moment = value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    else:
        moment = moment.astimezone(dt.timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


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
    ensure_column_exists("reminder_rules", "item_name", "VARCHAR(120) NOT NULL DEFAULT ''")
    ensure_column_exists("group_members", "receive_notifications", "BOOLEAN NOT NULL DEFAULT 1")


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


def merge_order_candidates(*candidate_groups: Iterable[str]) -> list[str]:
    merged: dict[str, bool] = {}
    for group in candidate_groups:
        for raw_candidate in group:
            candidate = normalize_token(raw_candidate)
            if len(candidate) < 8:
                continue
            if sum(char.isdigit() for char in candidate) < 6:
                continue
            merged[candidate] = True
    return sorted(merged.keys(), key=lambda item: (-len(item), item))


def serialize_order_candidates(candidates: Iterable[str], max_items: int = 8, max_chars: int = 80) -> str:
    serialized: list[str] = []
    for raw_candidate in candidates:
        candidate = normalize_token(raw_candidate)
        if len(candidate) < 8:
            continue
        if sum(char.isdigit() for char in candidate) < 6:
            continue
        if candidate in serialized:
            continue
        if len(serialized) >= max_items:
            break

        preview = " | ".join(serialized + [candidate])
        if serialized and len(preview) > max_chars:
            break
        serialized.append(candidate)

    return " | ".join(serialized)


def suffix_matches(candidate: str, suffix: str) -> bool:
    if candidate.endswith(suffix):
        return True

    suffix_digits = digits_only(suffix)
    if len(suffix_digits) < 4:
        return False
    return digits_only(candidate).endswith(suffix_digits)


def lcs_length(left: str, right: str) -> int:
    left = normalize_token(left)
    right = normalize_token(right)
    if not left or not right:
        return 0

    if len(left) > len(right):
        left, right = right, left

    previous_row = [0] * (len(left) + 1)
    for right_char in right:
        current_row = [0]
        for index, left_char in enumerate(left, start=1):
            if left_char == right_char:
                current_row.append(previous_row[index - 1] + 1)
            else:
                current_row.append(max(previous_row[index], current_row[index - 1]))
        previous_row = current_row
    return previous_row[-1]


def rank_pickup_logs_by_lcs(logs: Iterable[PickupLog], query: str, limit: int = 20) -> list[dict[str, object]]:
    normalized_query = normalize_token(query)
    if not normalized_query:
        return []

    scored: list[dict[str, object]] = []
    for log in logs:
        stored_order_text = clean_text(getattr(log, "recognized_order_no", ""))
        log_candidates = extract_order_candidates([stored_order_text])
        if not log_candidates and stored_order_text:
            fallback = normalize_token(stored_order_text)
            if fallback:
                log_candidates = [fallback]

        best_order_no = ""
        best_lcs = 0
        best_score = 0.0

        for order_no in log_candidates:
            normalized_order_no = normalize_token(order_no)
            if not normalized_order_no:
                continue

            lcs = lcs_length(normalized_query, normalized_order_no)
            if lcs <= 0:
                continue

            score = lcs / max(len(normalized_query), len(normalized_order_no))
            if (
                lcs > best_lcs
                or (lcs == best_lcs and score > best_score)
                or (lcs == best_lcs and score == best_score and len(order_no) > len(best_order_no))
            ):
                best_order_no = order_no
                best_lcs = lcs
                best_score = score

        if not best_order_no:
            continue

        scored.append(
            {
                "log": log,
                "order_no": best_order_no,
                "lcs": int(best_lcs),
                "score": float(best_score),
            }
        )

    scored.sort(
        key=lambda item: (
            int(item["lcs"]),
            float(item["score"]),
            cast(PickupLog, item["log"]).created_at,
        ),
        reverse=True,
    )
    return scored[:limit]


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


def build_unique_upload_filename(original_filename: str, prefix: str = "") -> str:
    safe_original = secure_filename(clean_text(original_filename)) or "upload.jpg"
    suffix = Path(safe_original).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        safe_original = "upload.jpg"

    return (
        f"{prefix}{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}_"
        f"{safe_original}"
    )


def save_thumbnail_data_url(data_url: str) -> str:
    matched = re.match(
        r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$",
        clean_text(data_url),
        re.IGNORECASE | re.DOTALL,
    )
    if not matched:
        raise ValueError("缩略图格式错误。")

    mime_type = matched.group(1).lower()
    encoded = re.sub(r"\s+", "", matched.group(2))
    suffix = THUMBNAIL_MIME_SUFFIXES.get(mime_type)
    if not suffix:
        raise ValueError("缩略图类型不支持。")

    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("缩略图解码失败。") from exc

    if not image_bytes:
        raise ValueError("缩略图内容为空。")
    if len(image_bytes) > MAX_THUMBNAIL_BYTES:
        raise ValueError("缩略图过大，请压缩后重试。")

    unique_filename = build_unique_upload_filename(f"thumb{suffix}", prefix="thumb_")
    image_path = UPLOAD_DIR / unique_filename
    with open(image_path, "wb") as file_obj:
        file_obj.write(image_bytes)
    return unique_filename


def sanitize_client_candidates(raw_candidates: object, fallback_candidate: object = "") -> list[str]:
    recognized_parts: list[str] = []

    if isinstance(raw_candidates, (list, tuple)):
        for item in raw_candidates[:30]:
            text = clean_text(item)
            if text:
                recognized_parts.append(text[:120])
    else:
        text = clean_text(raw_candidates)
        if text:
            recognized_parts.append(text[:120])

    fallback_text = clean_text(fallback_candidate)
    if fallback_text:
        recognized_parts.append(fallback_text[:120])

    return extract_order_candidates(recognized_parts)


def build_local_extracted_text(local_lines: object, candidates: list[str]) -> str:
    merged_lines: list[str] = []

    if isinstance(local_lines, (list, tuple)):
        for row in local_lines[:80]:
            text = clean_text(row)
            if text:
                merged_lines.append(text[:240])

    for candidate in candidates:
        if candidate not in merged_lines:
            merged_lines.append(candidate)

    return "\n".join(merge_unique_lines(merged_lines))


def create_notifications_for_candidates(
    db_session,
    pickup_log: PickupLog,
    uploader_name: str,
    candidates: list[str],
) -> tuple[int, int]:
    reminder_rules = db_session.scalars(
        select(ReminderRule)
        .where(ReminderRule.is_active.is_(True))
        .order_by(ReminderRule.created_at.asc())
    ).all()

    watcher_names = sorted({rule.watcher_name for rule in reminder_rules if clean_text(rule.watcher_name)})
    watcher_rows = []
    if watcher_names:
        watcher_rows = db_session.execute(
            select(User.id, User.username).where(User.username.in_(watcher_names))
        ).all()

    watcher_id_by_name = {str(row[1]): int(row[0]) for row in watcher_rows}
    watcher_ids = [int(row[0]) for row in watcher_rows]

    creator_groups_map: dict[int, set[int]] = {}
    if watcher_ids:
        creator_group_rows = db_session.execute(
            select(GroupMember.user_id, GroupMember.group_id).where(GroupMember.user_id.in_(watcher_ids))
        ).all()
        for row in creator_group_rows:
            creator_groups_map.setdefault(int(row[0]), set()).add(int(row[1]))

    all_group_ids = sorted({group_id for groups in creator_groups_map.values() for group_id in groups})
    group_name_map: dict[int, str] = {}
    group_recipient_map: dict[int, list[str]] = {}
    if all_group_ids:
        group_name_rows = db_session.execute(
            select(UserGroup.id, UserGroup.name).where(UserGroup.id.in_(all_group_ids))
        ).all()
        group_name_map = {int(row[0]): str(row[1]) for row in group_name_rows}

        recipient_rows = db_session.execute(
            select(GroupMember.group_id, User.username, GroupMember.receive_notifications)
            .join(User, User.id == GroupMember.user_id)
            .where(GroupMember.group_id.in_(all_group_ids))
            .order_by(GroupMember.group_id.asc(), User.username.asc())
        ).all()
        for row in recipient_rows:
            if not bool(row[2]):
                continue
            group_recipient_map.setdefault(int(row[0]), []).append(str(row[1]))

    existing_notification_rows = db_session.execute(
        select(Notification.reminder_id, Notification.watcher_name, Notification.matched_order_no)
        .where(Notification.pickup_log_id == pickup_log.id)
    ).all()
    existing_notification_keys = {
        (
            int(row[0]),
            clean_text(row[1]),
            normalize_token(row[2]),
        )
        for row in existing_notification_rows
    }

    matched_rules = 0
    notification_count = 0

    for rule in reminder_rules:
        matched_candidate = next((candidate for candidate in candidates if suffix_matches(candidate, rule.order_suffix)), None)
        if not matched_candidate:
            continue

        matched_rules += 1
        recipients = {rule.watcher_name}
        scope_text = "个人提醒"
        creator_group_names: list[str] = []

        creator_id = watcher_id_by_name.get(rule.watcher_name)
        if creator_id is not None:
            for group_id in sorted(creator_groups_map.get(creator_id, set())):
                members = group_recipient_map.get(group_id, [])
                if not members:
                    continue
                recipients.update(members)
                creator_group_names.append(group_name_map.get(group_id, f"组#{group_id}"))

        if creator_group_names:
            scope_text = f"个人 + 分组: {'、'.join(creator_group_names)}"

        if not recipients:
            continue

        item_name = clean_text(getattr(rule, "item_name", ""))
        custom_message = clean_text(rule.custom_message) or DEFAULT_REMINDER_MESSAGE
        headline = f"{item_name} 到了" if item_name else (custom_message or "快递到了")

        for recipient in sorted(recipients):
            dedupe_key = (int(rule.id), recipient, normalize_token(matched_candidate))
            if dedupe_key in existing_notification_keys:
                continue

            title = headline
            body = (
                f"{headline}\n"
                f"提醒创建人: {rule.watcher_name}\n"
                f"拍照人: {uploader_name}\n"
                f"命中尾号: {rule.order_suffix}\n"
                f"识别单号: {matched_candidate}\n"
                f"通知范围: {scope_text}"
            )
            db_session.add(
                Notification(
                    reminder_id=rule.id,
                    pickup_log_id=pickup_log.id,
                    watcher_name=recipient,
                    title=title,
                    body=body,
                    matched_order_no=matched_candidate,
                    order_suffix=rule.order_suffix,
                    uploader_name=uploader_name,
                    image_filename=pickup_log.image_filename,
                    is_read=False,
                )
            )
            existing_notification_keys.add(dedupe_key)
            notification_count += 1

    return matched_rules, notification_count


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if getattr(g, "current_user", None) is None:
            flash("请先登录。", "warning")
            next_url = clean_text(request.path)
            return redirect(url_for("login", next=next_url))
        return view_func(*args, **kwargs)

    return wrapped


def super_admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if getattr(g, "current_user", None) is None:
            flash("请先登录。", "warning")
            return redirect(url_for("login", next=clean_text(request.path)))
        if not is_loopback_request():
            abort(403)
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
        "to_utc_iso": to_utc_iso,
        "bandwidth_saver_mode": BANDWIDTH_SAVER_MODE,
        "is_super_admin": is_loopback_request() if request else False,
        "current_user": getattr(g, "current_user", None),
    }


@app.after_request
def apply_cache_headers(response):
    if request.method != "GET":
        return response

    path = clean_text(request.path)

    if path == "/sw.js":
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        return response

    if path.startswith("/static/") or path.startswith("/uploads/"):
        response.headers["Cache-Control"] = (
            f"private, max-age={LOCAL_STATIC_RETENTION_SECONDS}, immutable"
        )
        return response

    if response.status_code == 200 and clean_text(response.mimetype).startswith("text/html"):
        response.headers["Cache-Control"] = (
            f"private, max-age={LOCAL_STATIC_RETENTION_SECONDS}"
        )

    return response


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

    unread = [item for item in notifications if not item.is_read]
    recent_notifications = unread[:8] if unread else notifications[:8]
    latest_notification_id = int(notifications[0].id) if notifications else 0

    return render_template(
        "index.html",
        reminders=reminders,
        notifications=notifications,
        recent_notifications=recent_notifications,
        latest_notification_id=latest_notification_id,
        default_reminder_message=DEFAULT_REMINDER_MESSAGE,
    )


@app.post("/reminders/create")
@login_required
def create_reminder():
    order_suffix = digits_only(clean_text(request.form.get("order_suffix")))
    item_name = clean_text(request.form.get("item_name"))

    if len(order_suffix) < 4:
        flash("快递尾号至少输入 4 位数字。", "danger")
        return redirect(url_for("index"))

    reminder = ReminderRule(
        watcher_name=g.current_user.username,
        order_suffix=order_suffix,
        item_name=item_name,
        custom_message=(item_name or DEFAULT_REMINDER_MESSAGE),
        target_group_id=None,
        is_active=True,
    )
    g.db.add(reminder)
    g.db.commit()

    if item_name:
        flash(f"提醒已设置：尾号 {order_suffix} / 物品 {item_name}", "success")
    else:
        flash(f"提醒已设置：尾号 {order_suffix}", "success")
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

    unique_filename = build_unique_upload_filename(uploaded.filename)
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
        recognized_order_no=serialize_order_candidates(candidates),
    )
    g.db.add(pickup_log)
    g.db.flush()
    matched_rules, notification_count = create_notifications_for_candidates(
        g.db,
        pickup_log,
        uploader_name,
        candidates,
    )

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


@app.post("/api/scan/staged/init")
@login_required
def staged_scan_init():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "请求格式错误。"}), 400

    thumbnail_data_url = clean_text(payload.get("thumbnail_data_url"))
    if not thumbnail_data_url:
        return jsonify({"ok": False, "error": "缺少缩略图。"}), 400

    try:
        thumbnail_filename = save_thumbnail_data_url(thumbnail_data_url)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    candidates = sanitize_client_candidates(
        payload.get("recognized_candidates"),
        payload.get("recognized_order_no"),
    )
    extracted_text = build_local_extracted_text(payload.get("recognized_text_lines"), candidates)
    uploader_name = g.current_user.username

    try:
        pickup_log = PickupLog(
            uploader_name=uploader_name,
            image_filename=thumbnail_filename,
            extracted_text=extracted_text,
            recognized_order_no=serialize_order_candidates(candidates),
        )
        g.db.add(pickup_log)
        g.db.flush()

        matched_rules, notification_count = create_notifications_for_candidates(
            g.db,
            pickup_log,
            uploader_name,
            candidates,
        )
        g.db.commit()
    except Exception:
        g.db.rollback()
        (UPLOAD_DIR / thumbnail_filename).unlink(missing_ok=True)
        return jsonify({"ok": False, "error": "初始化上传失败，请稍后重试。"}), 500

    return jsonify(
        {
            "ok": True,
            "pickup_log_id": int(pickup_log.id),
            "recognized_order_no": pickup_log.recognized_order_no,
            "recognized_order_candidates": candidates,
            "candidate_count": len(candidates),
            "matched_rules": matched_rules,
            "notifications_created": notification_count,
        }
    )


@app.post("/api/scan/staged/finalize")
@login_required
def staged_scan_finalize():
    pickup_log_id_raw = clean_text(request.form.get("pickup_log_id"))
    if not pickup_log_id_raw:
        return jsonify({"ok": False, "error": "缺少日志 ID。"}), 400

    try:
        pickup_log_id = int(pickup_log_id_raw)
    except ValueError:
        return jsonify({"ok": False, "error": "日志 ID 无效。"}), 400

    pickup_log = g.db.get(PickupLog, pickup_log_id)
    if pickup_log is None or pickup_log.uploader_name != g.current_user.username:
        return jsonify({"ok": False, "error": "日志不存在或无权操作。"}), 404

    uploaded = request.files.get("pickup_image")
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "error": "请上传原图文件。"}), 400

    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        return jsonify({"ok": False, "error": "图片格式不支持。"}), 400

    unique_filename = build_unique_upload_filename(uploaded.filename, prefix="full_")
    image_path = UPLOAD_DIR / unique_filename
    old_filename = clean_text(pickup_log.image_filename)
    merged_candidates: list[str] = []
    matched_rules = 0
    notification_count = 0

    try:
        uploaded.save(image_path)
        pickup_log.image_filename = unique_filename
        notification_rows = g.db.scalars(
            select(Notification).where(Notification.pickup_log_id == pickup_log.id)
        ).all()
        for row in notification_rows:
            row.image_filename = unique_filename

        server_candidates: list[str] = []
        server_extracted_text = ""
        try:
            server_candidates, _, _, server_extracted_text = analyze_pickup_image(image_path)
        except Exception:
            server_candidates = []
            server_extracted_text = ""

        local_candidates = extract_order_candidates(
            [
                clean_text(pickup_log.recognized_order_no),
                clean_text(pickup_log.extracted_text),
            ]
        )
        merged_candidates = merge_order_candidates(local_candidates, server_candidates)
        if merged_candidates:
            pickup_log.recognized_order_no = serialize_order_candidates(merged_candidates)

        merged_extracted_lines = merge_unique_lines(
            clean_text(pickup_log.extracted_text).splitlines()
            + clean_text(server_extracted_text).splitlines()
            + merged_candidates
        )
        pickup_log.extracted_text = "\n".join(merged_extracted_lines)

        if merged_candidates:
            matched_rules, notification_count = create_notifications_for_candidates(
                g.db,
                pickup_log,
                pickup_log.uploader_name,
                merged_candidates,
            )

        g.db.commit()
    except Exception:
        g.db.rollback()
        image_path.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": "原图上传失败，请稍后重试。"}), 500

    if old_filename.startswith("thumb_"):
        (UPLOAD_DIR / old_filename).unlink(missing_ok=True)

    return jsonify(
        {
            "ok": True,
            "pickup_log_id": int(pickup_log.id),
            "image_filename": unique_filename,
            "recognized_order_no": pickup_log.recognized_order_no,
            "recognized_order_candidates": merged_candidates,
            "candidate_count": len(merged_candidates),
            "fallback_matched_rules": matched_rules,
            "fallback_notifications_created": notification_count,
        }
    )


@app.get("/settings")
@login_required
def settings_page():
    user_id = int(g.current_user.id)

    my_memberships = g.db.execute(
        select(GroupMember.group_id, GroupMember.receive_notifications, UserGroup.name)
        .join(UserGroup, UserGroup.id == GroupMember.group_id)
        .where(GroupMember.user_id == user_id)
        .order_by(UserGroup.name.asc())
    ).all()

    my_group_settings = [
        {
            "group_id": int(row[0]),
            "receive_notifications": bool(row[1]),
            "group_name": str(row[2]),
        }
        for row in my_memberships
    ]

    admin_groups: list[dict[str, object]] = []
    if is_loopback_request():
        group_rows = g.db.scalars(select(UserGroup).order_by(UserGroup.name.asc())).all()
        group_ids = [int(group.id) for group in group_rows]

        member_map: dict[int, list[dict[str, object]]] = {}
        if group_ids:
            member_rows = g.db.execute(
                select(
                    GroupMember.group_id,
                    User.username,
                    GroupMember.role,
                    GroupMember.receive_notifications,
                )
                .join(User, User.id == GroupMember.user_id)
                .where(GroupMember.group_id.in_(group_ids))
                .order_by(GroupMember.group_id.asc(), User.username.asc())
            ).all()
            for row in member_rows:
                member_map.setdefault(int(row[0]), []).append(
                    {
                        "username": str(row[1]),
                        "role": str(row[2]),
                        "receive_notifications": bool(row[3]),
                    }
                )

        admin_groups = [
            {
                "id": int(group.id),
                "name": str(group.name),
                "members": member_map.get(int(group.id), []),
            }
            for group in group_rows
        ]

    return render_template(
        "settings.html",
        my_group_settings=my_group_settings,
        admin_groups=admin_groups,
        is_super_admin=is_loopback_request(),
    )


@app.post("/settings/groups/create")
@super_admin_required
def create_group():
    group_name = clean_text(request.form.get("group_name"))
    if len(group_name) < 1:
        flash("分组名称不能为空。", "danger")
        return redirect(url_for("settings_page"))

    existing = g.db.scalar(
        select(UserGroup).where(UserGroup.name == group_name)
    )
    if existing is not None:
        flash("该分组已存在。", "warning")
        return redirect(url_for("settings_page"))

    group = UserGroup(name=group_name, created_by=g.current_user.id)
    g.db.add(group)
    g.db.flush()
    g.db.add(
        GroupMember(
            group_id=group.id,
            user_id=g.current_user.id,
            role="owner",
            receive_notifications=True,
        )
    )
    g.db.commit()

    flash(f"分组已创建：{group_name}", "success")
    return redirect(url_for("settings_page"))


@app.post("/settings/groups/<int:group_id>/members/add")
@super_admin_required
def add_group_member(group_id: int):
    group = g.db.get(UserGroup, group_id)
    if group is None:
        flash("分组不存在。", "danger")
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
        existing_member.receive_notifications = True
        g.db.commit()
        flash("该用户已在组内，已恢复为默认接收本组通知。", "info")
        return redirect(url_for("settings_page"))

    g.db.add(
        GroupMember(
            group_id=group_id,
            user_id=user_to_add.id,
            role="member",
            receive_notifications=True,
        )
    )
    g.db.commit()
    flash(f"已添加成员：{username}", "success")
    return redirect(url_for("settings_page"))


@app.post("/settings/groups/<int:group_id>/notifications/toggle")
@login_required
def toggle_group_notification(group_id: int):
    membership = g.db.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == g.current_user.id,
        )
    )
    if membership is None:
        flash("你不在该分组中。", "danger")
        return redirect(url_for("settings_page"))

    membership.receive_notifications = not membership.receive_notifications
    g.db.commit()
    state_text = "开启" if membership.receive_notifications else "关闭"
    flash(f"已{state_text}本组通知接收。", "success")
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


@app.post("/api/notifications/<int:notification_id>/read")
@login_required
def api_mark_notification_read(notification_id: int):
    notification = g.db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.watcher_name == g.current_user.username,
        )
    )
    if notification is None:
        return jsonify({"ok": False, "error": "提醒不存在。"}), 404

    if not notification.is_read:
        notification.is_read = True
        g.db.commit()

    return jsonify({"ok": True, "id": int(notification.id), "is_read": True})


@app.get("/api/notifications/poll")
@login_required
def poll_notifications():
    after_id_raw = clean_text(request.args.get("after_id"))
    try:
        after_id = max(int(after_id_raw or "0"), 0)
    except ValueError:
        after_id = 0

    rows = g.db.scalars(
        select(Notification)
        .where(
            Notification.watcher_name == g.current_user.username,
            Notification.id > after_id,
        )
        .order_by(Notification.id.asc())
        .limit(20)
    ).all()

    max_id = after_id
    items: list[dict[str, object]] = []
    for row in rows:
        max_id = max(max_id, int(row.id))
        first_line = clean_text(row.body).splitlines()
        preview = first_line[0] if first_line else ""
        items.append(
            {
                "id": int(row.id),
                "title": clean_text(row.title),
                "body": preview,
                "order_suffix": clean_text(row.order_suffix),
                "created_at": row.created_at.strftime("%H:%M:%S"),
                "created_at_utc": to_utc_iso(row.created_at),
            }
        )

    return jsonify({"items": items, "max_id": max_id})


@app.get("/logs")
@login_required
def logs_page():
    search_query = clean_text(request.args.get("q"))
    logs = g.db.scalars(
        select(PickupLog)
        .order_by(desc(PickupLog.created_at))
        .limit(400)
    ).all()

    search_results = rank_pickup_logs_by_lcs(logs, search_query, limit=20) if search_query else []

    return render_template(
        "logs.html",
        logs=logs,
        search_query=search_query,
        search_results=search_results,
    )


@app.get("/api/orders/search")
@login_required
def api_search_orders():
    query = clean_text(request.args.get("q"))
    normalized_query = normalize_token(query)
    if len(normalized_query) < 2:
        return jsonify({"query": query, "normalized_query": normalized_query, "items": []})

    logs = g.db.scalars(
        select(PickupLog)
        .where(PickupLog.recognized_order_no != "")
        .order_by(desc(PickupLog.created_at))
        .limit(600)
    ).all()
    ranked = rank_pickup_logs_by_lcs(logs, normalized_query, limit=20)

    items = [
        {
            "log_id": int(cast(PickupLog, item["log"]).id),
            "order_no": str(item["order_no"]),
            "lcs": int(item["lcs"]),
            "score": round(float(item["score"]), 4),
            "uploader_name": cast(PickupLog, item["log"]).uploader_name,
            "created_at": cast(PickupLog, item["log"]).created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "created_at_utc": to_utc_iso(cast(PickupLog, item["log"]).created_at),
            "image_url": url_for("uploaded_file", filename=cast(PickupLog, item["log"]).image_filename),
        }
        for item in ranked
    ]

    return jsonify({"query": query, "normalized_query": normalized_query, "items": items})


@app.get("/uploads/<path:filename>")
@login_required
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/sw.js")
def service_worker_script():
    response = send_from_directory(
        app.static_folder,
        SW_JS_FILE,
        mimetype="application/javascript",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


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
