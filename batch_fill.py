import argparse
from pathlib import Path

from app import SessionLocal, import_parcel_dataframe, init_db, load_table_from_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Import logistics table into PostgreSQL")
    parser.add_argument("--input", required=True, help="Path to CSV/XLS/XLSX logistics file")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"File not found: {input_path}")

    init_db()
    df = load_table_from_path(input_path)

    db_session = SessionLocal()
    try:
        inserted, updated, skipped = import_parcel_dataframe(db_session, df)
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
