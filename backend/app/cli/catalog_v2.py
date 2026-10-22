"""Catalog v2 operator entry point (does not enable HTTP credentials)."""
import argparse
import json

from app.db import engine
from app.services.catalog_v2.publisher import cleanup, publish_once, seed, status


def main():
    parser = argparse.ArgumentParser(description="主数据库 catalog v2 发布管理")
    parser.add_argument("command", choices=("seed", "publish-once", "status", "cleanup"))
    parser.add_argument("--retain-days", type=int, default=30,
                        help="cleanup 保留天数，不能小于30；不会自动运行")
    args = parser.parse_args()
    if args.command == "seed":
        result = {"publication_epoch": seed(engine)}
    elif args.command == "publish-once":
        batch = publish_once(engine)
        result = {"batch_id": batch["batch_id"] if batch else None, **status(engine)}
    elif args.command == "cleanup":
        result = {"removed_batches": cleanup(engine, retain_days=args.retain_days)}
    else:
        result = status(engine)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
