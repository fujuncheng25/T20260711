from __future__ import annotations

import datetime as dt
import os
import random
import re
import uuid
from functools import wraps
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from PIL import Image, ImageOps
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    desc,
    func,
    or_,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, scoped_session, sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

UI_BUILD_ID = "20260711_200500_94731"
UI_CSS_FILE = f"ui_{UI_BUILD_ID}.css"
UI_JS_FILE = f"ui_{UI_BUILD_ID}.js"

DEFAULT_NOTIFY_MESSAGE = "您的快递到了"
EMPTY_VALUES = {"", "NAN", "NONE", "NULL", "/", "\\", "-"}
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
ORDER_TOKEN_PATTERN = re.compile(r"[A-Z0-9]{8,32}")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/logistics_alert",
)

LOGISTICS_COLUMN_ALIASES = {
    "category": ("类型",),
    "item_desc": ("物品简述", "物品描述"),
    "quantity": ("数量",),
    "purchase_time": ("购买时间",),
    "purchase_status": ("购买状态",),
    "purchaser": ("购买人",),
    "amount": ("金额",),
    "payment_and_logistics_status": ("支付凭证+物流状态", "支付凭证物流状态"),
    "arrival_time": ("到达时间",),
    "order_no": ("订单号", "订单编号", "订单ID"),
    "tracking_no": ("快递单号", "物流单号"),
    "reimbursement_status": ("报销状态",),
}


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, nullable=False)

    memberships: Mapped[list["GroupMember"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    created_groups: Mapped[list["UserGroup"]] = relationship(back_populates="owner")
    created_reminders: Mapped[list["Reminder"]] = relationship(back_populates="creator")
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class UserGroup(Base):
    __tablename__ = "user_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, nullable=False)

    owner: Mapped[User] = relationship(back_populates="created_groups")
    members: Mapped[list["GroupMember"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), default="member", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, nullable=False)

    group: Mapped[UserGroup] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id"), nullable=False, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    order_suffix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    custom_message: Mapped[str] = mapped_column(String(240), default=DEFAULT_NOTIFY_MESSAGE, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, nullable=False)

    group: Mapped[UserGroup] = relationship(back_populates="reminders")
    creator: Mapped[User] = relationship(back_populates="created_reminders")


class Parcel(Base):
    __tablename__ = "parcels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    item_desc: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchase_time: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    purchase_status: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    purchaser: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    amount: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    payment_and_logistics_status: Mapped[str] = mapped_column(Text, default="", nullable=False)
    arrival_time: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    order_no: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    tracking_no: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    reimbursement_status: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime,
        default=dt.datetime.utcnow,
        onupdate=dt.datetime.utcnow,
        nullable=False,
    )


class PickupEvent(Base):
    __tablename__ = "pickup_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scanner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    image_path: Mapped[str] = mapped_column(String(260), nullable=False)
    extracted_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    detected_order_no: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    shown_group_id: Mapped[int | None] = mapped_column(ForeignKey("user_groups.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, nullable=False)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id"), nullable=False, index=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminders.id"), nullable=False, index=True)
    pickup_event_id: Mapped[int] = mapped_column(ForeignKey("pickup_events.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="notifications")


engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
SessionLocal = scoped_session(SessionFactory)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me-before-production")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024


def init_db() -> None:
    Base.metadata.create_all(engine)


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.upper() in EMPTY_VALUES:
        return ""
    return text


def normalize_token(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", clean_text(value).upper())


def parse_quantity(value: object) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def pick_first_value(row: pd.Series, aliases: Iterable[str]) -> str:
    for alias in aliases:
        if alias in row.index:
            value = clean_text(row.get(alias, ""))
            if value:
                return value
    return ""


def load_table_from_upload(uploaded_file) -> pd.DataFrame:
    filename = (uploaded_file.filename or "").lower()
    if filename.endswith(".csv"):
        return pd.read_csv(uploaded_file, dtype=str, keep_default_na=False)
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        return pd.read_excel(uploaded_file, dtype=str, keep_default_na=False)
    raise ValueError("只支持 CSV/XLS/XLSX 文件。")


def load_table_from_path(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str, keep_default_na=False)
    raise ValueError("输入文件必须是 CSV/XLS/XLSX。")


def import_parcel_dataframe(db_session, df: pd.DataFrame) -> tuple[int, int, int]:
    parsed_rows: list[dict[str, object]] = []
    skipped = 0

    for _, row in df.iterrows():
        order_no = normalize_token(pick_first_value(row, LOGISTICS_COLUMN_ALIASES["order_no"]))
        if not order_no:
            skipped += 1
            continue

        parsed_rows.append(
            {
                "category": pick_first_value(row, LOGISTICS_COLUMN_ALIASES["category"]),
                "item_desc": pick_first_value(row, LOGISTICS_COLUMN_ALIASES["item_desc"]),
                "quantity": parse_quantity(pick_first_value(row, LOGISTICS_COLUMN_ALIASES["quantity"])),
                "purchase_time": pick_first_value(row, LOGISTICS_COLUMN_ALIASES["purchase_time"]),
                "purchase_status": pick_first_value(row, LOGISTICS_COLUMN_ALIASES["purchase_status"]),
                "purchaser": pick_first_value(row, LOGISTICS_COLUMN_ALIASES["purchaser"]),
                "amount": pick_first_value(row, LOGISTICS_COLUMN_ALIASES["amount"]),
                "payment_and_logistics_status": pick_first_value(
                    row,
                    LOGISTICS_COLUMN_ALIASES["payment_and_logistics_status"],
                ),
                "arrival_time": pick_first_value(row, LOGISTICS_COLUMN_ALIASES["arrival_time"]),
                "order_no": order_no,
                "tracking_no": normalize_token(pick_first_value(row, LOGISTICS_COLUMN_ALIASES["tracking_no"])),
                "reimbursement_status": pick_first_value(row, LOGISTICS_COLUMN_ALIASES["reimbursement_status"]),
            }
        )

    if not parsed_rows:
        return 0, 0, skipped

    order_nos = [item["order_no"] for item in parsed_rows]
    existing = db_session.scalars(select(Parcel).where(Parcel.order_no.in_(order_nos))).all()
    existing_by_order = {item.order_no: item for item in existing}

    inserted = 0
    updated = 0

    for payload in parsed_rows:
        order_no = str(payload["order_no"])
        current = existing_by_order.get(order_no)
        if current is None:
            db_session.add(Parcel(**payload))
            inserted += 1
            continue

        for field_name, field_value in payload.items():
            setattr(current, field_name, field_value)
        updated += 1

    return inserted, updated, skipped


def decode_barcodes(image: np.ndarray) -> list[str]:
    try:
        import zxingcpp
    except Exception:
        return []

    values: list[str] = []
    try:
        results = zxingcpp.read_barcodes(image)
    except Exception:
        return []

    for result in results:
        text = clean_text(getattr(result, "text", ""))
        if text:
            values.append(text)
    return list(dict.fromkeys(values))


def decode_ocr_lines(image: Image.Image) -> list[str]:
    try:
        import pytesseract
    except Exception:
        return []

    # Use grayscale + autocontrast to increase OCR robustness for courier labels.
    prepared = ImageOps.autocontrast(ImageOps.grayscale(image))
    configs = [
        {"lang": "chi_sim+eng", "config": "--oem 3 --psm 6"},
        {"lang": "eng", "config": "--oem 3 --psm 6"},
    ]

    text = ""
    for kwargs in configs:
        try:
            text = clean_text(pytesseract.image_to_string(prepared, **kwargs))
        except Exception:
            text = ""
        if text:
            break

    if not text:
        return []

    return [line.strip() for line in text.splitlines() if line.strip()]


def extract_order_candidates(recognized_parts: Iterable[str]) -> list[str]:
    candidates: dict[str, bool] = {}
    for part in recognized_parts:
        text = clean_text(part).upper()
        if not text:
            continue
        for token in ORDER_TOKEN_PATTERN.findall(text):
            normalized = normalize_token(token)
            if len(normalized) < 8:
                continue
            digit_count = sum(char.isdigit() for char in normalized)
            if digit_count < 6:
                continue
            candidates[normalized] = True
    return sorted(candidates.keys(), key=lambda item: (-len(item), item))


def analyze_pickup_image(image_path: Path) -> tuple[list[str], list[str], list[str], str]:
    try:
        image = Image.open(image_path).convert("RGB")
    except Exception as exc:
        raise ValueError("图片解析失败，请上传清晰的 JPG/PNG 图片。") from exc

    barcode_values = decode_barcodes(np.array(image))
    ocr_lines = decode_ocr_lines(image)
    recognized_parts = barcode_values + ocr_lines
    candidates = extract_order_candidates(recognized_parts)
    extracted_text = "\n".join(recognized_parts)
    return candidates, barcode_values, ocr_lines, extracted_text


def match_reminders(candidates: list[str], reminders: list[Reminder]) -> list[tuple[str, Reminder]]:
    matched: list[tuple[str, Reminder]] = []
    for candidate in candidates:
        for reminder in reminders:
            if candidate.endswith(reminder.order_suffix):
                matched.append((candidate, reminder))
    return matched


def create_notifications_for_matches(
    db_session,
    scanner: User,
    pickup_event: PickupEvent,
    matches: list[tuple[str, Reminder]],
) -> int:
    chosen_by_reminder: dict[int, tuple[str, Reminder]] = {}
    for candidate, reminder in matches:
        chosen_by_reminder.setdefault(reminder.id, (candidate, reminder))

    created = 0
    for candidate, reminder in chosen_by_reminder.values():
        custom_message = clean_text(reminder.custom_message) or DEFAULT_NOTIFY_MESSAGE
        member_rows = db_session.scalars(
            select(GroupMember).where(GroupMember.group_id == reminder.group_id)
        ).all()

        for member in member_rows:
            title = f"{reminder.group.name} 取件通知"
            body = (
                f"{custom_message}\n"
                f"命中尾号: {reminder.order_suffix}\n"
                f"识别单号: {candidate}\n"
                f"取件人: {scanner.username}"
            )
            db_session.add(
                Notification(
                    user_id=member.user_id,
                    group_id=reminder.group_id,
                    reminder_id=reminder.id,
                    pickup_event_id=pickup_event.id,
                    title=title,
                    body=body,
                )
            )
            created += 1
    return created


def get_user_groups(db_session, user_id: int) -> list[UserGroup]:
    stmt = (
        select(UserGroup)
        .join(GroupMember, GroupMember.group_id == UserGroup.id)
        .where(GroupMember.user_id == user_id)
        .order_by(desc(UserGroup.created_at))
    )
    return db_session.scalars(stmt).unique().all()


def get_membership(db_session, group_id: int, user_id: int) -> GroupMember | None:
    return db_session.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    )


def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if g.current_user is None:
            flash("请先登录。", "warning")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapper


@app.context_processor
def inject_template_globals():
    return {
        "current_user": getattr(g, "current_user", None),
        "ui_build_id": UI_BUILD_ID,
        "ui_css_file": UI_CSS_FILE,
        "ui_js_file": UI_JS_FILE,
    }


@app.before_request
def open_db_session():
    g.db = SessionLocal()
    g.current_user = None
    user_id = session.get("user_id")
    if user_id:
        g.current_user = g.db.get(User, user_id)


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
def home():
    if g.current_user:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.current_user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = clean_text(request.form.get("username"))
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        db_session = g.db

        if len(username) < 3:
            flash("用户名至少 3 个字符。", "danger")
            return render_template("register.html")
        if len(password) < 6:
            flash("密码至少 6 个字符。", "danger")
            return render_template("register.html")
        if password != password_confirm:
            flash("两次密码输入不一致。", "danger")
            return render_template("register.html")
        if db_session.scalar(select(User).where(User.username == username)):
            flash("用户名已存在，请换一个。", "danger")
            return render_template("register.html")

        db_session.add(User(username=username, password_hash=generate_password_hash(password)))
        db_session.commit()
        flash("注册成功，请登录。", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.current_user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = clean_text(request.form.get("username"))
        password = request.form.get("password", "")
        db_session = g.db
        user = db_session.scalar(select(User).where(User.username == username))

        if user is None or not check_password_hash(user.password_hash, password):
            flash("用户名或密码错误。", "danger")
            return render_template("login.html")

        session["user_id"] = user.id
        flash("登录成功。", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.get("/logout")
def logout():
    session.clear()
    flash("已退出登录。", "info")
    return redirect(url_for("login"))


@app.get("/dashboard")
@login_required
def dashboard():
    db_session = g.db
    user = g.current_user
    groups = get_user_groups(db_session, user.id)
    group_ids = [group.id for group in groups]

    unread_count = db_session.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == user.id,
            Notification.is_read.is_(False),
        )
    ) or 0

    recent_notifications = db_session.scalars(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(desc(Notification.created_at))
        .limit(8)
    ).all()

    parcel_count = db_session.scalar(select(func.count(Parcel.id))) or 0
    active_reminders = 0
    if group_ids:
        active_reminders = db_session.scalar(
            select(func.count(Reminder.id)).where(
                Reminder.group_id.in_(group_ids),
                Reminder.is_active.is_(True),
            )
        ) or 0

    return render_template(
        "dashboard.html",
        groups=groups,
        parcel_count=parcel_count,
        active_reminders=active_reminders,
        unread_count=unread_count,
        recent_notifications=recent_notifications,
        default_notify_message=DEFAULT_NOTIFY_MESSAGE,
    )


@app.post("/import-logistics")
@login_required
def import_logistics():
    uploaded = request.files.get("sheet_file")
    if not uploaded or not uploaded.filename:
        flash("请先上传物流表（CSV/XLS/XLSX）。", "danger")
        return redirect(url_for("dashboard"))

    try:
        df = load_table_from_upload(uploaded)
    except Exception as exc:
        flash(f"文件读取失败：{exc}", "danger")
        return redirect(url_for("dashboard"))

    db_session = g.db
    try:
        inserted, updated, skipped = import_parcel_dataframe(db_session, df)
        db_session.commit()
    except Exception as exc:
        db_session.rollback()
        flash(f"导入失败：{exc}", "danger")
        return redirect(url_for("dashboard"))

    flash(
        f"导入完成：新增 {inserted} 条，更新 {updated} 条，跳过 {skipped} 条（无订单号）。",
        "success",
    )
    return redirect(url_for("dashboard"))


@app.post("/groups/create")
@login_required
def create_group():
    group_name = clean_text(request.form.get("group_name"))
    if not group_name:
        flash("组名不能为空。", "danger")
        return redirect(url_for("dashboard"))

    db_session = g.db
    group = UserGroup(name=group_name, created_by=g.current_user.id)
    db_session.add(group)
    db_session.flush()
    db_session.add(GroupMember(group_id=group.id, user_id=g.current_user.id, role="owner"))
    db_session.commit()

    flash(f"已创建用户组：{group_name}", "success")
    return redirect(url_for("dashboard"))


@app.post("/groups/<int:group_id>/members/add")
@login_required
def add_group_member(group_id: int):
    db_session = g.db
    membership = get_membership(db_session, group_id, g.current_user.id)
    if membership is None or membership.role != "owner":
        flash("只有组主可以添加成员。", "danger")
        return redirect(url_for("dashboard"))

    username = clean_text(request.form.get("username"))
    if not username:
        flash("成员用户名不能为空。", "danger")
        return redirect(url_for("dashboard"))

    user_to_add = db_session.scalar(select(User).where(User.username == username))
    if user_to_add is None:
        flash("找不到这个用户，请先注册账号。", "danger")
        return redirect(url_for("dashboard"))

    if get_membership(db_session, group_id, user_to_add.id):
        flash("该用户已经在组里了。", "info")
        return redirect(url_for("dashboard"))

    db_session.add(GroupMember(group_id=group_id, user_id=user_to_add.id, role="member"))
    db_session.commit()
    flash(f"已添加成员：{username}", "success")
    return redirect(url_for("dashboard"))


@app.post("/groups/<int:group_id>/reminders/create")
@login_required
def create_reminder(group_id: int):
    db_session = g.db
    membership = get_membership(db_session, group_id, g.current_user.id)
    if membership is None:
        flash("你不是该组成员，不能设置提醒。", "danger")
        return redirect(url_for("dashboard"))

    suffix = normalize_token(request.form.get("order_suffix"))
    custom_message = clean_text(request.form.get("custom_message")) or DEFAULT_NOTIFY_MESSAGE

    if len(suffix) < 4:
        flash("订单尾号至少输入 4 位。", "danger")
        return redirect(url_for("dashboard"))

    db_session.add(
        Reminder(
            group_id=group_id,
            created_by=g.current_user.id,
            order_suffix=suffix,
            custom_message=custom_message,
            is_active=True,
        )
    )
    db_session.commit()
    flash(f"提醒已创建：尾号 {suffix}", "success")
    return redirect(url_for("dashboard"))


@app.post("/reminders/<int:reminder_id>/toggle")
@login_required
def toggle_reminder(reminder_id: int):
    db_session = g.db
    reminder = db_session.get(Reminder, reminder_id)
    if reminder is None:
        flash("提醒不存在。", "danger")
        return redirect(url_for("dashboard"))

    membership = get_membership(db_session, reminder.group_id, g.current_user.id)
    if membership is None:
        flash("你不是该组成员，不能修改提醒。", "danger")
        return redirect(url_for("dashboard"))

    reminder.is_active = not reminder.is_active
    db_session.commit()
    status_text = "启用" if reminder.is_active else "停用"
    flash(f"提醒已{status_text}：尾号 {reminder.order_suffix}", "success")
    return redirect(url_for("dashboard"))


@app.route("/scan", methods=["GET", "POST"])
@login_required
def scan():
    scan_result = None
    if request.method == "POST":
        uploaded_image = request.files.get("pickup_image")
        if not uploaded_image or not uploaded_image.filename:
            flash("请上传拍照图片。", "danger")
            return redirect(url_for("scan"))

        suffix = Path(uploaded_image.filename).suffix.lower()
        if suffix not in ALLOWED_IMAGE_SUFFIXES:
            flash("图片格式不支持，请上传 JPG/PNG/WEBP/BMP/TIF。", "danger")
            return redirect(url_for("scan"))

        unique_name = (
            f"{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            f"_{uuid.uuid4().hex[:8]}_{secure_filename(uploaded_image.filename)}"
        )
        target_path = UPLOAD_DIR / unique_name
        uploaded_image.save(target_path)

        try:
            candidates, barcode_values, ocr_lines, extracted_text = analyze_pickup_image(target_path)
        except Exception as exc:
            flash(f"识别失败：{exc}", "danger")
            return redirect(url_for("scan"))

        db_session = g.db
        reminders = db_session.scalars(
            select(Reminder).where(Reminder.is_active.is_(True)).order_by(Reminder.created_at.asc())
        ).all()
        matches = match_reminders(candidates, reminders)

        matched_groups: dict[int, UserGroup] = {}
        for _, reminder in matches:
            matched_groups[reminder.group_id] = reminder.group
        shown_group = random.choice(list(matched_groups.values())) if matched_groups else None

        pickup_event = PickupEvent(
            scanner_user_id=g.current_user.id,
            image_path=str(target_path.relative_to(APP_DIR)),
            extracted_text=extracted_text,
            detected_order_no=candidates[0] if candidates else "",
            shown_group_id=shown_group.id if shown_group else None,
        )
        db_session.add(pickup_event)
        db_session.flush()

        notifications_created = create_notifications_for_matches(
            db_session=db_session,
            scanner=g.current_user,
            pickup_event=pickup_event,
            matches=matches,
        )
        db_session.commit()

        row_by_reminder: dict[int, dict[str, object]] = {}
        for candidate, reminder in matches:
            if reminder.id not in row_by_reminder:
                member_count = db_session.scalar(
                    select(func.count(GroupMember.id)).where(GroupMember.group_id == reminder.group_id)
                ) or 0
                row_by_reminder[reminder.id] = {
                    "group_name": reminder.group.name,
                    "suffix": reminder.order_suffix,
                    "candidate": candidate,
                    "message": reminder.custom_message,
                    "member_count": member_count,
                }

        scan_result = {
            "barcode_values": barcode_values,
            "ocr_lines": ocr_lines,
            "candidates": candidates,
            "shown_group": shown_group.name if shown_group else "未匹配到组别",
            "notifications_created": notifications_created,
            "match_rows": list(row_by_reminder.values()),
        }

    return render_template("scan.html", scan_result=scan_result)


@app.get("/notifications")
@login_required
def notifications():
    db_session = g.db
    rows = db_session.scalars(
        select(Notification)
        .where(Notification.user_id == g.current_user.id)
        .order_by(desc(Notification.created_at))
        .limit(300)
    ).all()
    return render_template("notifications.html", notifications=rows)


@app.post("/notifications/read-all")
@login_required
def read_all_notifications():
    db_session = g.db
    unread_rows = db_session.scalars(
        select(Notification).where(
            Notification.user_id == g.current_user.id,
            Notification.is_read.is_(False),
        )
    ).all()
    for row in unread_rows:
        row.is_read = True
    db_session.commit()
    flash("已标记全部通知为已读。", "success")
    return redirect(url_for("notifications"))


@app.get("/parcels")
@login_required
def parcels():
    db_session = g.db
    keyword = clean_text(request.args.get("q", ""))
    query = select(Parcel).order_by(desc(Parcel.updated_at), desc(Parcel.created_at))
    if keyword:
        like_term = f"%{keyword.upper()}%"
        query = query.where(
            or_(
                func.upper(Parcel.order_no).like(like_term),
                func.upper(Parcel.tracking_no).like(like_term),
                func.upper(Parcel.item_desc).like(like_term),
            )
        )

    rows = db_session.scalars(query.limit(300)).all()
    reminders = db_session.scalars(select(Reminder).where(Reminder.is_active.is_(True))).all()

    destination_by_parcel: dict[int, str] = {}
    for parcel in rows:
        order_no = normalize_token(parcel.order_no)
        group_candidates = [reminder.group.name for reminder in reminders if order_no.endswith(reminder.order_suffix)]
        destination_by_parcel[parcel.id] = random.choice(group_candidates) if group_candidates else "未分组"

    return render_template(
        "parcels.html",
        parcels=rows,
        keyword=request.args.get("q", ""),
        destination_by_parcel=destination_by_parcel,
    )


def main() -> None:
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
