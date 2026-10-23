"""Maintenance read-only tools: schemas and handlers have one registration site.

Business reads, row scope and redaction remain in the existing services.
Adding a tool here does not require editing runtime.py or tools.dispatch.
"""
from sqlalchemy.orm import Session

from app import security
from app.agent.tool_catalog import ToolCatalog
from app.services import maintenance_cost

catalog = ToolCatalog()

_MAINT_PAGE_ERR = {"error": "无权限：你的账号未开通「项目成本（维保出库）」页面权限，无法查询维保成本数据。"}


@catalog.register({
    "type": "function",
    "function": {
        "name": "get_maintenance_board",
        "description": (
            "维保合同预算消耗参考：备件先分实际采购参考/估算参考/成本缺失，再与生效报销费用组成"
            "已知支出参考。incomplete_cost=成本不完整；"
            "expense_data_unavailable=项目追踪工作簿报销明细尚未建立费用全量数据水位；两者的 "
            "remaining/remaining_pct 都为空，严禁自行推断红黄绿或盈亏；"
            "仅成本与费用数据都完整时才给 red=预算已用完或超预算、"
            "yellow=预算余量≤20%、green=预算余量>20%、no_budget=无正预算。"
            "同时返回合同级含税/未税收入、归一备件成本、备件毛利及毛利率；两套口径"
            "独立 fail closed。parts_profit_status_inc/ex 中 complete_estimated 必须标注"
            "含估算；missing_revenue/missing_tax_rate/invalid_tax_rate/"
            "ambiguous_revenue/incomplete_cost/filtered_scope 均不得把 null 当 0。"
            "合同级贡献毛利使用独立 contribution_profit/margin/status 字段；"
            "contribution_status_inc/ex 为 expense_data_unavailable 或"
            "expense_tax_unknown 时必须保持为空，不得包装成正式财务毛利。"
            "合同额、预算与余量仅按利润权限返回；无利润权限时不返回这些金额、决策状态、"
            "双口径毛利及其状态、状态计数或筛选结果，改按最近出库日期排列。"
            "需要项目成本页面权限。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "incomplete_cost",
                        "expense_data_unavailable",
                        "red",
                        "yellow",
                        "green",
                        "no_budget",
                    ],
                    "description": "按预算消耗参考状态过滤，可省略=全部",
                },
            },
        },
    },
})
def _get_maintenance_board(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not security.page_allowed(ctx, "page_maintenance"):
        return _MAINT_PAGE_ERR
    if security.is_scoped_sales(ctx):
        return {
            "error": "无权限：受限销售账号不能查看合同级维保数据。"
            "可以查看本人范围的项目事实。"
        }
    st = args.get("status")
    data = maintenance_cost.board(
        db, None, None,
        st if st in (
            "incomplete_cost",
            "expense_data_unavailable",
            "red",
            "yellow",
            "green",
            "no_budget",
        ) else None,
        user_ctx=ctx,
    )
    return security.apply_field_visibility(data, ctx)


@catalog.register({
    "type": "function",
    "function": {
        "name": "get_maintenance_projects",
        "description": (
            "维保项目成本汇总（项目维度）：实际采购参考/估算参考按含税与不含税分列、"
            "缺失成本行、成本完整性、含税/未税归一备件成本、已知成本混合原值兼容参考、出库行数/数量/覆盖率/"
            "成本来源分布(direct=专属采购直配、window=±7天最近价、month_avg=当月均价、"
            "trace_avg=估算追溯均价、sales_ref=估算销售参考、"
            "pool_purchase/pool_sales=互通池同伴历史均价、"
            "purchase_history/sales_history=本PN历史参考、none=成本缺失)/关联销售订单与合同额参考。"
            "合同额以 contract_amount_inc_tax 明确返回含税事实，contract_amount_basis 固定为 inc_tax；"
            "contract_amount 仅为兼容别名。contract_incomplete=true 时金额为空，不得按 0 使用。"
            "合同额参考仅按利润权限返回，空值不等于源数据缺失。"
            "无成本权限时按项目名排序后再截取 top，不按隐藏成本排名。"
            "需要项目成本页面权限。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "项目名关键词过滤，可省略"},
                "top": {"type": "integer", "description": "返回项目数，默认 20，最大 50"},
            },
        },
    },
})
def _get_maintenance_projects(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not security.page_allowed(ctx, "page_maintenance"):
        return _MAINT_PAGE_ERR
    top = max(1, min(int(args.get("top") or 20), 50))
    data = maintenance_cost.projects_aggregate(db, None, None, args.get("q"), user_ctx=ctx)
    rows = data.get("rows", [])
    if len(rows) > top:
        sort_label = "按项目名" if data.get("ranking_restricted") else "成本最高"
        data = {**data, "rows": rows[:top],
                "note": f"共 {len(rows)} 个项目，仅返回{sort_label}的前 {top} 个；可用 q 过滤"}
    return security.apply_field_visibility(data, ctx)


@catalog.register({
    "type": "function",
    "function": {
        "name": "get_maintenance_lines",
        "description": (
            "单个维保项目的出库明细（追单据用）：维保单号/日期/需求类型/仓库/PN/"
            "需求数量/退货数量/已返数量/待返数量/"
            "单价/金额/cost_tier(actual|estimated|missing)/原始成本来源与税口径/"
            "置信度(high|medium|low)/取价月/距采购天数/关联采购单/异常标记。"
            "return_qty（退货数量）才是净量/成本冲抵字段；returned_qty（已返数量）和"
            "pending_return_qty（待返数量）仅为源表流转状态展示，不参与净量或成本核减，"
            "禁止混用。"
            "cost_tier 是权威事实层级；missing 时单价和金额为空，原始来源/税口径仅供诊断，"
            "不得据此自行恢复金额或重判 actual/estimated。"
            "project 必须是 get_maintenance_projects 返回的准确项目名。需要项目成本页面权限。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名，精确值"},
                "month": {"type": "string", "description": "按月过滤 YYYY-MM，可省略"},
                "page": {"type": "integer", "description": "页码，默认 1（每页 50 行）"},
            },
            "required": ["project"],
        },
    },
})
def _get_maintenance_lines(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not security.page_allowed(ctx, "page_maintenance"):
        return _MAINT_PAGE_ERR
    project = str(args.get("project", "")).strip()
    if not project:
        return {"error": "project 不能为空（用 get_maintenance_projects 拿准确项目名）"}
    month = args.get("month")
    data = maintenance_cost.project_lines(db, project, month if month else None,
                                          None, None, max(int(args.get("page") or 1), 1), 50,
                                          user_ctx=ctx)
    return security.apply_field_visibility(data, ctx)
