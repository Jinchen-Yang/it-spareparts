"""Stable, value-free Query Broker errors."""


_PUBLIC_MESSAGES = {
    "QUERY_BROKER_DISABLED": "只读查询能力未启用",
    "QUERY_BROKER_UNAVAILABLE": "只读查询能力当前不可用",
    "AGENT_DSN_MISSING": "只读查询数据库未配置",
    "AGENT_DSN_INVALID": "只读查询数据库配置无效",
    "AGENT_DSN_REUSES_APP_IDENTITY": "只读查询数据库身份未隔离",
    "AGENT_READER_IDENTITY_INVALID": "只读查询数据库身份无效",
    "DATASET_NOT_VISIBLE": "当前账号无权使用该数据集",
    "FIELD_NOT_VISIBLE": "当前账号无权使用该字段",
    "UNKNOWN_FIELD": "查询包含未知字段",
    "FIELD_KIND_MISMATCH": "字段用途与查询位置不匹配",
    "FILTER_OPERATOR_NOT_ALLOWED": "字段不支持该筛选操作",
    "FILTER_TYPE_INVALID": "筛选值类型不匹配",
    "TIME_RANGE_REQUIRED": "该数据集必须指定日期范围",
    "TIME_RANGE_INVALID": "日期范围无效",
    "TIME_RANGE_IN_FUTURE": "日期范围不能晚于今天",
    "TIME_RANGE_TOO_WIDE": "日期范围超过允许上限",
    "REQUIRED_DIMENSION_MISSING": "指标缺少必要维度",
    "SALES_ORDER_COUNT_GRAIN_INVALID": "销售单数仅允许固定月度型号粒度",
    "ROW_SUBJECT_REQUIRED": "当前行级范围缺少权威主体",
    "COMPILED_SQL_REJECTED": "服务端查询编译结果未通过安全校验",
    "AUTHORITY_UNAVAILABLE": "当前权限状态无法确认",
    "AUTHORIZATION_CHANGED": "权限或数据范围已变化，请重新规划",
    "PROVIDER_EGRESS_UNAVAILABLE": "当前数据出站策略无法确认",
    "PROVIDER_EGRESS_CHANGED": "数据出站策略已变化，请重新规划",
    "PROVIDER_EGRESS_DENIED": "当前数据出站策略不允许该查询",
    "QUERY_PLAN_COST_EXCEEDED": "查询预计成本超过预算",
    "QUERY_PLAN_ROWS_EXCEEDED": "查询预计扫描行数超过预算",
    "QUERY_PLAN_BYTES_EXCEEDED": "查询预计扫描数据量超过预算",
    "QUERY_EXECUTION_FAILED": "只读查询执行失败",
    "QUERY_RESULT_INVALID": "只读查询结果未通过安全校验",
    "EVIDENCE_SEAL_FAILED": "查询证据封存失败",
}


class QueryBrokerError(RuntimeError):
    """A stable error code with no SQL, parameter, driver, or business value."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_PUBLIC_MESSAGES.get(code, "只读查询被安全策略拒绝"))
