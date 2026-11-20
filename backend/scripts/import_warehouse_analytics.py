"""离线校验仓单导出，按源修改时间增量同步库存分析投影。"""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app.services.warehouse_analytics_import import apply_warehouse_workbook, parse_warehouse_workbook


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path, help="完整单据的 .xlsx 导出")
    parser.add_argument("--apply", action="store_true", help="将解析结果写入 DATABASE_URL 指定的分析投影")
    args = parser.parse_args()
    if args.apply and not os.environ.get("DATABASE_URL"):
        parser.error("--apply 需要显式设置 DATABASE_URL")
    try:
        parsed = parse_warehouse_workbook(args.workbook)
        result = {name: {"documents": len(records), "lines": sum(len(r.lines) for r in records)}
                  for name, records in parsed.items()}
        if args.apply:
            from app.db import SessionLocal
            with SessionLocal.begin() as db:
                result["applied"] = apply_warehouse_workbook(db, parsed)
        print(json.dumps(result, ensure_ascii=False))
    except SQLAlchemyError:
        parser.exit(1, "仓单导入失败，数据库事务已撤销。请核对连接、迁移版本和投影约束。\n")
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"仓单导入失败：{exc}\n")


if __name__ == "__main__":
    main()
