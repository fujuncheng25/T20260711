import argparse
from pathlib import Path

import pandas as pd

from app import build_download_bytes, fill_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill empty CSV cells using Tmall order details")
    parser.add_argument("--input", required=True, help="Path to input CSV")
    parser.add_argument("--output", required=True, help="Path to output CSV")
    parser.add_argument("--order-col", default="订单号")
    parser.add_argument("--item-col", default="物品简述")
    parser.add_argument("--tracking-col", default="快递单号")
    parser.add_argument("--arrival-col", default="到达时间")
    parser.add_argument("--purchase-time-col", default="购买时间")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(input_path, dtype=str, keep_default_na=False)

    def status_cb(_level: str, message: str) -> None:
        print(message)

    def progress_cb(done: int, total: int) -> None:
        print(f"进度: {done}/{total}")

    completed = fill_csv(
        df=df,
        order_col=args.order_col,
        item_col=args.item_col,
        tracking_col=args.tracking_col,
        arrival_col=args.arrival_col,
        purchase_time_col=args.purchase_time_col,
        status_cb=status_cb,
        progress_cb=progress_cb,
    )

    output_path.write_bytes(build_download_bytes(completed))
    print(f"输出已保存: {output_path}")


if __name__ == "__main__":
    main()
