# 项目总表领用身份冲突回执修复

决策: 无

基线：`main@54efe9d`；候选分支 `fix/workbook-stale-replay-ux`。
遵循决策文件 0006（内部 `issue_no` 身份与展示号分离）、0004（地址补丁）及 D-01/D-02/D-03。

## 诊断与范围

本地诊断 `~/workspaces/reviews/upload-diag-20261019/codex-out.md` 指出：
追加的空实体ID行，其 WBDD+PN+SN 命中已有 deterministic 身份，
日期或是否应返还不同会触发 `stale_site_replay`。拒绝符合既有重放规则，
但首行即停止且只有 toast，无法一次定位所有冲突。真实工作簿含生产数据，
仅保留在原本地目录；实现及测试均使用合成数据。

三路分析结论：用户需要行号和逐字段差异；业务要求身份、重放、作废优先和
有实体ID的三方合并不变；技术上错误详情不能复用三方合并的 `conflicts`，
否则前端可能误展示强制接管。当前总表编辑门要求项目范围及整本字段可见权，
地址沿决策 0004 最新修订对维保页和项目范围可见；不返回成本或任意 ORM 字段。

## 实施契约

- 仅收集 generated/legacy_replay 命中已有有效行且内容不同的冲突；扫描结束后统一拒绝，
  不产生部分应用。仍用原七字段哈希判断；地址只报告实际参与判定的单头补丁，
  generated 分支的关联需求单差异也明确报告。
- `WorkbookError` 增加可选 keyword-only `details`，HTTP 保留 `code/message/issues`；
  新增 `rows`、`total_count`、`fix_hint`。每行包含 `row_no/entity_id/issue_no/pn/sn`，
  `differences` 为 `field/old/new`；值沿既有比较归一化为字符串（日期 ISO、空值空串）。
  行摘要 `issue_no` 使用现有 `display_no_effective` 可读展示口径；身份判定仍用内部 `issue_no`。
  只保存前 200 行明细，但总数覆盖所有碰撞。前端按错误码打开只读弹窗，缺明细回落原消息。
- 原文件已导入但新生成身份不存在、apply 并发守卫继续用原单条提示；它们不能可靠
  指向单个已有实体供逐字段比较。其它错误仍按原顺序拒绝，包括同文件重复身份、
  跨项目和非法输入；不改变错误优先级或去重规则。
- validate 的 WorkbookError 失败追加 `workbook_validate` 访问日志，仅写项目ID、错误码、
  冲突数量等 JSON 原始量；审计失败不影响原错误响应。成功路径保持原样。
- `00_使用说明`、`98_字段说明` 和 06 示例说明旧 WBDD+PN+SN 应保留实体ID原行修改，
  不得追加空ID副本；真正的新领用单号留空自动编号。仅补文案，不改列、签名或解析协议，
  模板版本保持 2.7.1，避免严格版本门拒绝在途工作簿。

## 验证与交付

测试边界为公开 workbook build/validate/apply、HTTP validate 和 WorkbookRoundTrip 用户交互。
契约覆盖多行/部分字段/地址清空差异、200行截断、完整重放、有ID三方合并、
HTTP 422兼容、范围保护、失败审计与审计故障隔离、说明文本。
后端使用本任务创建的临时 postgres:15，pytest 自建独立数据库；完成后只删除该容器。
运行全部 workbook/roundtrip/master_v2 相关文件及前端 tsc、全部 Vitest、Vite build。

无数据库迁移，无身份/去重逻辑改动。交付状态为候选待用户点按验收、待 main 人工审批；
不合并、不部署、不读取生产。回退为撤销本候选提交，旧客户端继续读取原 `message`。

## API 示例（合成数据）

`POST /api/maintenance/projects/stable/{project_id}/master-workbook/validate` 返回 HTTP 422：

```json
{
  "detail": {
    "code": "stale_site_replay",
    "message": "共 3 行与已入库领用记录冲突，首行：第 27 行……",
    "issues": [],
    "total_count": 3,
    "rows": [
      {
        "row_no": 27,
        "entity_id": "manual-site:synthetic-example",
        "issue_no": "WBDD-20261019-0001",
        "pn": "SYNTHETIC-PN",
        "sn": "SYNTHETIC-SN",
        "differences": [
          {"field": "领用日期", "old": "2026-10-18", "new": "2026-10-19"}
        ]
      }
    ],
    "fix_hint": "已入库记录请保留实体ID在原行修改；真正的新领用请将领用单号留空自动编号（或使用尚未占用的单号）"
  }
}
```

示例省略后两行；真实响应只在超过 200 行时截断。失败日志只记录
`{"project_id":"合成项目ID","error_code":"stale_site_replay","conflict_count":3}`，
不保存差异内容。上传媒体读取或鉴权阶段的 HTTP 拒绝沿用既有路径；新增失败日志覆盖
进入 workbook 校验器后抛出的 `WorkbookError`（含 V1/V2 普通校验错误）。
