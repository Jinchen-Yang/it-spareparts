"""主数据合并/撤销合并后的统一后置重算。

合并会把源 PN 的采购/销售/维保事实挪到目标 PN；若不立刻重算利润与成本，
itsp 的统计会停留在旧 part_id 口径。治理人工入口与 shop 集成写接口必须
执行完全相同的重算序列与失败处理，故集中在服务层。

口径（沿用 governance 既有行为）：
- 重算在业务写入提交之后执行，各服务自管事务；
- 失败只回滚重算阶段，不回滚已提交的合并，响应附 ``recompute_failed``；
- 幂等重放由调用方在提交回执后直接回放，不再次进入本函数。
"""
import logging

from sqlalchemy.orm import Session

from app.services import inventory as inventory_svc
from app.services import maintenance_cost as maintenance_svc
from app.services import profit as profit_svc

_log = logging.getLogger("master_recompute")

RECOMPUTE_FAILED = "治理操作已完成，但后置重算失败，请联系管理员处理"


def recompute_after_master_write(
    db: Session,
    result: dict,
    *,
    operation: str,
    profit_service=None,
    inventory_service=None,
    maintenance_service=None,
) -> dict:
    """执行 利润 → 库存成本 → 维保成本 重算，结果合并进 ``result``。

    ``operation`` 只用于日志（merge/unmerge）；三个 service 参数允许调用方
    传入自己的模块引用（governance 既有测试按模块名 monkeypatch）。
    """
    profit_service = profit_service or profit_svc
    inventory_service = inventory_service or inventory_svc
    maintenance_service = maintenance_service or maintenance_svc
    try:
        result["recompute"] = profit_service.recompute(db)
        result["inventory_costs"] = inventory_service.backfill_costs(db)
        result["maintenance_costs"] = maintenance_service.recompute(db)
    except Exception:  # noqa: BLE001 —— 重算失败不回滚已提交的业务写入
        _log.exception("post-%s recompute failed", operation)
        db.rollback()
        result["recompute_failed"] = RECOMPUTE_FAILED
    return result
