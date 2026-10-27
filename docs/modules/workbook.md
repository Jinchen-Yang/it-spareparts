# M4 通用工作簿往返引擎

调用链：业务声明 `WorkbookSchema` → `export_workbook` → 用户编辑 `.xlsx` → `parse_workbook` → `plan` → `preview` → `apply` → 首次回执。引擎不读取任何询价或维保业务表；业务通过注册回调接入。

## Schema

`ColumnSpec(key, header, type, edit, hint, choices=None, width=None, max_len=None, decimals=None, field_group=None)` 是唯一的列定义。`SheetSpec` 组织列并声明是否允许新增、删除、自定义列；`WorkbookSchema` 组织工作表、名称、版本和填写说明。页面列选择、模板格式、解析和校验共享这一份定义。

- `type`：`text`、`code`、`int`、`decimal`、`date`、`enum`、`bool`。
- `edit`：`locked`、`required`、`optional`、`reference`。
- `field_group`：由权限中心决定整列可见性。导出整列排除无权限字段；上传时若出现该列，忽略并给警告。
- `visible_columns` 只影响本次导出选择；隐藏的行令牌和锁定的 `code` 列始终保留。
- `custom_columns=True` 时，表头 `+备注` 表示名为「备注」的自定义文本字段。引擎返回字段名和值，字段池的创建与权限归业务回调处理。

普通表头须预先去掉首尾空白和不可见字符，不能重复或占用系统令牌名。工作表名按 Excel 的大小写规则排除系统保留名（包括 `_META` 等大小写变体）。

## 外观和 Office 兼容性

| 颜色 | 列类型 | 可编辑性 |
|---|---|---|
| 灰底 | `locked`、隐藏行令牌 | 锁定 |
| 黄底 | `required` | 可编辑，非空 |
| 绿底 | `optional` | 可编辑 |
| 蓝底 | `reference` | 只读 |
| 白底，表头以 `+` 开头 | 自定义列 | 可编辑 |

第一个工作表固定为「填写说明」，包含颜色图例、每列格式、增删行规则、常见错误和业务补充说明。表头批注包含格式要求与选项。工作表开启无密码保护、冻结首行，列宽来自 schema。下拉验证使用内联列表，超过 255 字符时只写说明。

`text` 和 `code` 一律以字符串写入并使用 `@` 格式，防止编码被转成数字。隐藏的 `_meta` 保存 schema 身份、导出编号、导出人、时间、导出行基线和 HMAC；行令牌同样经过签名，用户调整顺序不改变身份。

仅支持 `.xlsx`，不生成宏、ListObject、条件格式公式或合并单元格。工作表保护是编辑辅助，服务端仍会独立校验身份、锁定字段、权限、类型与版本。

## 类型和行识别

- 编码出现数字单元格（前导零丢失、科学计数法、长数字、`.0` 浮点尾巴）时拒绝该行，提示「请将该列设为文本后重新粘贴」，不猜原始值。
- 数值允许千分位、`¥`、`￥`、首尾空格和全角数字。小数位超过 `decimals` 时报错，不做四舍五入；空字符串为 `None`。
- 超过 15 位的整数、高精度或指数形式的 Decimal 在导出时使用十进制文本，避免办公软件截断；解析后仍恢复为 schema 声明的数值类型。
- 日期接受 Excel 日期单元格、`YYYY-MM-DD`、`YYYY/MM/DD`、Excel 序列号。默认 1900 日期系统；签名元数据声明 1904 时使用 1904 系统。
- 布尔支持「是/否、Y/N、1/0、TRUE/FALSE、对/错」；枚举同时支持代码值和中文标签，忽略大小写。
- 文本清理首尾空白、零宽字符、NBSP、BOM。非法控制字符和其 OOXML 转义形式在入口拒绝，复用 `excel_text_safety`。
- 公式只接受缓存结果；无缓存或含外部链接时报错。任何合并区域都拒绝并指出区域。
- 有合法行令牌的行按实体 ID 和基线版本识别；无令牌且有可编辑内容为新增；空行忽略。重复、伪造、跨导出的令牌整份拒绝。
- 缺失导出行形成删除候选，仅在 schema 允许时有效，而且每一行都须在应用时确认。
- 锁定或参考列的修改被忽略并警告；服务端版本与基线不一致时形成冲突。

上传限制为 10 MB、解压总量 64 MiB、ZIP 条目数 2048。单表最多 20000 行、256 列；解析只扫描实际存在的单元格，稀疏表不会被补成巨大矩形。缺失 `_meta`、签名篡改、schema 名称或版本不符时提示使用系统导出的模板。

## 回调与两步提交

通过 `registry.register(schema.name, WorkbookSpec(schema, load_current, apply_fn, can_access, validate_fn=None))` 注册。注册名必须等于 schema 名称，重复注册拒绝；测试结束可调用 `unregister(name)`。声明 schema 的最小示例：

```python
schema = WorkbookSchema("demo", 1, [
    SheetSpec("items", "明细", [
        ColumnSpec("pn", "PN", "code", "locked", "按文本保存"),
        ColumnSpec("qty", "数量", "int", "required", "填写整数"),
        ColumnSpec("note", "备注", "text", "optional", "自由填写", max_len=200),
    ], allow_insert=True, allow_delete=True, custom_columns=True),
], instructions=["新增行请留空行令牌"])
```

| 名称 | 类型 | 用途 | 定义原因 |
|---|---|---|---|
| `schema` | `WorkbookSchema` | 同时驱动文件与页面 | 避免多份规则不一致 |
| `items` | sheet key | 服务回调定位逻辑表 | 与可展示的中文表名分开 |
| `pn`、`qty`、`note` | column key | 回调值字典的键 | 独立于展示表头 |

注册回调契约：

- `load_current(db, sheet, ids, ctx) -> dict[id, (version, values)]`：只返回当前用户可访问的实体；应用阶段须在当前事务中通过 `SELECT FOR UPDATE` 锁定对应记录，持续到外层提交，封住版本复查与实际写入之间的竞态。
- `apply_fn(db, plan, ctx) -> dict`：只接收 `added`、`modified`、已确认的 `deleted` 行；`unchanged` 不传入。行的 `values` 只包含实际上传且获授权的可编辑字段，`custom_fields` 单独返回；业务不得把缺列当作清空。
- `can_access(db, ctx, subject_type, subject_id) -> bool`：预览、应用和重放均调用，业务自行判断可见性及操作权限。可选业务标识只能成对传入，保存在 `Plan.subject_type` / `Plan.subject_id` 并参与摘要；`apply_fn` 必须验证所有实体属于该业务对象，新增行也据此确定目标。
- `validate_fn(plan, session) -> list[RowError]`（可选）：也支持沿用现有回调顺序的 `(db, plan, ctx)`，引擎在调用前检查签名，不会在业务抛出 TypeError 后重复调用。`RowError(sheet, message, row_key=None, row_number=None, column=None)` 通过签名行的 `key` 或 Excel 行号定位，`column` 使用 schema 列键。引擎在独立 SAVEPOINT 内调用，结束时立即回滚，禁止回调提交；可以复用应用服务试运行。错误合并到 `Plan.errors` 和对应行的 `errors`，前端沿用红框并禁用应用。冲突行保留原有 conflict 计数，并补充行内错误。

应用前再次执行相同业务校验，并重新锁定和检查版本。失败时 `response.row_errors` 保留同样的定位结构；HTTP `detail` 包含 `message`、完整操作结果（`plan`、`plan_digest`、`status=failed`）和失败回执。版本变化维持 HTTP 409，其余应用错误为 422。调用方可以直接使用返回的 `plan` 更新预览。未提供校验回调的 schema 保持原有契约。

低层 `plan(parse_result, load_current)` 采用 `(sheet, ids)` 形式的只读回调；注册服务负责把 `db` 和 `ctx` 绑定进去。`export_workbook` 的 `data` 为 `{sheet_key: [{entity_id, base_version, values, custom_fields?}]}`，`custom_fields={sheet_key: [字段名]}` 选择自定义列；身份不是从展示行号推断。

`WorkbookContext(field_groups=set(), actor=None)` 可供纯文件调用和测试使用，也支持系统 `UserContext`。注册操作服务必须使用有效实名账号，`actor` 接受账号 ID 或 `UserContext`。`schema.to_dict(ctx=ctx)` 返回已经按权限过滤的 JSON 结构；通用 `GET /workbooks/{schema_name}/schema` 也可获取。

`operations.preview(db, schema_name, file_bytes, *, ctx, subject_type=None, subject_id=None)` 保存计划；`operations.apply(db, operation_id, actor, expected_plan_digest, *, confirm_deletes=[])` 确认计划。删除确认使用预览每行的 `delete_key`，未确认的候选不传给业务回调。

服务不提交外层事务；业务回调必须使用传入 `db`，不得自行 `commit`、`rollback` 或另建会话写入。任何一行抛错都会回滚全部业务写入，操作状态记录为 `failed`；直接调用服务的上层捕获 `ApplyFailed` 后须提交以保留失败记录，通用 API 已处理。预览不改业务数据。应用绑定 `operation_id`、操作者和 `expected_plan_digest`；有错误或版本冲突时保守拒绝整份应用，预览后版本变化也会阻止应用。

`ParseResult` 为每格返回 `value`、`raw`、可选 `error` / `warning`，并保留工作表、Excel 行号、列名。`Plan.rows` 为行状态及 `changes=[{key, header, before, after}]`；只列出实际变化的单元格，另有分类计数、定位后的错误/警告及删除候选的 `delete_key`。JSON 输出将 Decimal 保留为十进制字符串，日期转 ISO 文本。

`workbook_operation` 以 `(export_id, file_sha256, actor)` 唯一约束保证幂等。已应用文件再次上传或重复应用，返回首次保存的 `response` 并标记 `replayed=true`，不再次调用业务写入。模板升级后仍可重放精确原文件；权限被收回或历史字段已从 schema 删除时，保守拒绝展示旧计划。通用接口返回 `operation_id`、`plan_digest`、`plan`、`status`、`replayed` 及可选 `response`：

- `POST /workbooks/{schema_name}/preview`：multipart 上传 `.xlsx`，返回操作编号、计划及摘要。
- `POST /workbooks/operations/{id}/apply`：提交 `expected_plan_digest` 与逐个确认的 `confirm_deletes`。

导出接口由业务方暴露。所有写操作走访问审计。通用前端 `WorkbookExportButton` 接收 schema 获取和导出回调；`WorkbookImport` 管理上传、预览、逐项删除确认、应用和回执，不使用浏览器原生 alert/confirm。

## WPS / Microsoft Excel 人工验收清单

下列步骤须在 WPS 与 Microsoft Excel 各执行一次，记录软件版本、操作系统和结果。自动化的 openpyxl/LibreOffice 检查不能代替这份实际点按验收。

- [ ] 打开系统导出的 `.xlsx`，没有文件损坏、修复或宏安全提示；首表为「填写说明」。
- [ ] 核对灰/黄/绿/蓝/白五种颜色、表头批注、列宽、冻结首行；行令牌列和 `_meta` 隐藏。
- [ ] 黄绿及自定义单元格可填写，灰蓝单元格受保护；无密码可取消保护后再次验证服务端忽略锁定修改并提示。
- [ ] 枚举和是否列能下拉选择；超长选项清单出现在说明中，未生成失效下拉。
- [ ] 将 `0231`、`2310000`、`12345678901234567890` 按文本粘入编码列，另存后上传仍精确保留。故意改成数字格式并粘入数值，预览拒绝该行并显示修复提示。
- [ ] 输入全角数字、带千分位及人民币符号的数字；输入超限小数，分别确认归一化成功或定位错误且未自动四舍五入。
- [ ] 输入日期单元格、`YYYY-MM-DD`、`YYYY/MM/DD`，确认日期一致；非法日期显示行号和列名。
- [ ] 修改可编辑值后上传，逐格预览 before/after，新增绿、修改黄、删除红、冲突灰、错误红框。
- [ ] 在允许新增的表插入无令牌新行；在不允许的表插入新行，分别确认新增和错误。
- [ ] 删除一条导出行，上传后显示删除候选；不勾确认时保留，单独勾确认后才删除。
- [ ] 调换行顺序、中间插空行后另存上传，实体定位正确，空行不产生新增。
- [ ] 添加 `+特殊要求` 自定义列，值能进入预览；重复字段名按错误提示处理；不允许自定义的表将该列忽略并警告。
- [ ] 另存为 `.xlsx` 后上传，令牌、签名和单元格类型仍有效；文件原件留作复测。
- [ ] 确认应用后再次上传同一文件，出现「该文件已应用过，显示首次结果」，业务数据只变更一次。
