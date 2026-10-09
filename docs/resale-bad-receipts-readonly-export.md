# 已收坏件跨系统只读导出合同

独立接收方：`IT-shop`。本接口不会写上游返还事实、库存或审计表；仅在服务器显式设置专用 SHA256 密钥摘要后可访问。

- `GET /api/integrations/resale/bad-receipts?limit=100&after=<receiptId>`：跨项目读取 `maintenance_rkd_return_line` 中 `source=manual`、件况 `坏品/废品` 且有逐件 `serial_numbers` 的记录。`receipt_id` 或最多 100 个 `receipt_ids` 可用于应用前版本/状态复核；既有有效与已作废记录均返回。
- 字段限 `receiptId,version,lineStatus,projectId,pn,qty,serialNumbers,condition,receiptKind,occurredAt,changedAt`；不包含客户/人员、物流、费用、备注或原始单据全文。RKD 导入行的 SN 为空，不凭数量伪造 SN。
- `RESALE_EXPORT_TOKEN_SHA256` 未配置时端点返回 404；配置时应存独立高熵随机令牌的 **SHA256 小写十六进制摘要**。调用方通过 `Authorization: Bearer` 发送原令牌，原值不能进入仓库、浏览器、URL、日志或聊天。公网必须由 HTTPS 代理接入。
- `backend/tests/test_resale_export.py` 验证关闭/错密钥、跨项目分页、作废、字段白名单与批量复核。上线无需迁移，不改变原有返还操作。
