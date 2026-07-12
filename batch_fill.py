import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from app import DEFAULT_REMINDER_MESSAGE, ReminderRule, SessionLocal, clean_text, init_db, normalize_token


REQUIRED_COLUMNS = {"watcher_name", "order_suffix"}
OPTIONAL_COLUMNS = {"custom_message", "is_active"}


def load_rules_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".xls", ".xlsx"}:
        return pd.read_excel(path)
    raise ValueError("Only CSV/XLS/XLSX files are supported.")


def parse_active_flag(value: object) -> bool:
    text = clean_text(value).lower()
    if text in {"", "1", "true", "yes", "y", "on", "启用", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "停用", "否"}:
        return False
    return True


def import_reminder_rules(db_session, df: pd.DataFrame) -> tuple[int, int, int]:
    normalized_columns = {str(col).strip().lower(): col for col in df.columns}
    missing = REQUIRED_COLUMNS - set(normalized_columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    inserted = 0
    updated = 0
    skipped = 0

    for _, row in df.iterrows():
        watcher_name = clean_text(row[normalized_columns["watcher_name"]])
        order_suffix = normalize_token(row[normalized_columns["order_suffix"]])
        custom_message = DEFAULT_REMINDER_MESSAGE
        is_active = True

        if "custom_message" in normalized_columns:
            custom_message = clean_text(row[normalized_columns["custom_message"]]) or DEFAULT_REMINDER_MESSAGE
        if "is_active" in normalized_columns:
            is_active = parse_active_flag(row[normalized_columns["is_active"]])

        if not watcher_name or len(order_suffix) < 4:
            skipped += 1
            continue

        existing = db_session.scalar(
            select(ReminderRule).where(
                ReminderRule.watcher_name == watcher_name,
                ReminderRule.order_suffix == order_suffix,
            )
        )

        if existing is None:
            db_session.add(
                ReminderRule(
                    watcher_name=watcher_name,
                    order_suffix=order_suffix,
                    custom_message=custom_message,
                    is_active=is_active,
                )
            )
            inserted += 1
        else:
            existing.custom_message = custom_message
            existing.is_active = is_active
            updated += 1

    return inserted, updated, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Import reminder rules into SQLite")
    parser.add_argument("--input", required=True, help="Path to CSV/XLS/XLSX reminder file")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"File not found: {input_path}")

    init_db()
    df = load_rules_table(input_path)

    db_session = SessionLocal()
    try:
        inserted, updated, skipped = import_reminder_rules(db_session, df)
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()
        SessionLocal.remove()

    print(f"Import completed: inserted={inserted}, updated={updated}, skipped={skipped}")


if __name__ == "__main__":
    main()
