# 维保需求单和领用单地址功能修复交接报告

交接日期：2026-10-11。接手人：用户本地 Codex。目标：核实并补齐维保需求单和领用单的地址导入、保存、更新和展示，兼容旧文件及另一台电脑上的在途改动。

本提交仅保存上下文和调研结果，没有修改业务代码、数据库、Excel 模板或运行中的服务，没有执行历史补录、生产部署或功能测试。

## 1 甲方需求与任务边界

已提出的功能需求：

1. 维保备件需求单导入 Excel 时，若有地址列，应读取并保存；后续导入能够更新地址。
2. 领用单 Excel 导入及页面手工填写均支持地址，保存后能查看。
3. Excel 地址列按表头名称定位，可以放在任意列；例如插在 A 与 B 之间或 D 与 E 之间，而非依赖固定列号。
4. 兼容过去没有地址列的文件，不要求业务人员把所有旧文件重新补齐。

供货分类暂不讨论、不修改。需求单删除、行删除和相关级联由用户另一台电脑处理，不属于本次开发范围。另一台电脑有部分已做、部分未做，尚未上传 GitHub；其具体差异仍未知，不能推断这些工作已经在远端。

## 2 调查基线与证据强度

| 基线 | SHA | 用途 |
|---|---|---|
| main | `02bd8ed6a95f5ac723985c6e29870c75d293c11e` | 最初源码调查，生产发布线 |
| test | `c9fe89b1b6664179c29cbbbcd2db76e7b46725f7` | 文档提交父基线，已复核关键地址文件，后续开发集成线 |

实施时重新 fetch 并复核最新代码。以上 SHA 是本次调查快照，不是要求回退的目标，也不证明当前部署正运行该版本。

明确区分三类结论：

- **代码事实**：模型、迁移、字段映射、接口、前端组件和领用 Excel 解析已查阅；关键字段与布局限制在 test 上也已复核。
- **历史档案事实**：仓库真实 Excel 核查记录列出了带「收货地址」的需求单导出。
- **实际数据待核实**：当前交接环境没有可读取的客户历史 Excel 原件及生产数据库连接，未查询真实地址非空数量、实际部署迁移头或某张生产单据的地址。不得把合成测试或旧发布记录描述为本次生产验证。

## 3 已确认的代码现状

| 对象 | 当前事实 | 对后续工作的含义 |
|---|---|---|
| 需求单地址 | `f_maintenance_order.receiver_address` 为 nullable Text；已有迁移 | 语义一致时复用该列，无需重复新建地址字段；先核实实际库迁移 |
| 需求单源映射 | 「收货地址」映射到 `receiver_address`；另有「选择收货地址」映射到 `delivery_address_option` | 两个源字段不同，不能用“包含地址”模糊匹配合并 |
| 需求单读取与页面 | `order_contact` 返回地址、联系人、电话；两个需求单入口使用 `OrderContactInfo` | 无需把已经存在的展示重写；核对真实缺口 |
| 地址可见性 | 沿用客户信息权限；受限时字段为 null，组件显示无权限 | 必须区分“无地址”“无权限”和“接口/部署未更新” |
| 需求单 Excel | 通用 reader 按列名解析；专用 WBDD 上传仍强制 90/91 列及头、明细位置区间 | 仅增加地址别名不能保证任意插列成功，必须一起调整布局门禁 |
| 领用单 | `maintenance_site_issue.site_location` 为 nullable String(256) | 已有位置字段，但需确认是否等同于甲方要的地址 |
| 领用手填 | 人工领用和仓库发货领用页面均有「现场位置」；人工领用创建 API 与服务要求非空、最长 256 | 不能宣称现有功能完全没有地址相关入口；不得顺手改变必填契约 |
| 领用 Excel | `V2_SITE_HEADERS`、`V2_SITE_BASE_FIELDS` 和读写逻辑没有地址或 `site_location` | 主要缺口在项目总表 `06_领用返还` 的双向读写与协议兼容 |

历史核查：[真实需求单 Excel 布局档案](../maintenance/archive/data-facts-and-linkage-2026-08.md) §1。该档案记录两份 90/91 列真实导出的分析，91 列清单明确包含「选择收货地址」「收货人」「收货人电话」「收货地址」。这证明已分析样本有该表头，不证明每张旧单都有地址值。

已有功能说明：[v1.32 需求单收货信息说明](../releases/v1.32-maintenance-contact-analytics.md)。该文档的合成测试和历史发布描述不能替代本次生产核验。

## 4 开发前先核实真实数据

先只读调查，再决定补功能还是修现有链路：

1. 检查当前使用的数据库、部署 SHA 与 Alembic revision；确认实际表存在地址相关列。
2. 统计需求单地址和领用现场位置的非空情况，记录数据库环境和查询时间。统计全部源单与有效单时分别注明口径，不能把作废单混入业务覆盖率。
3. 沿 `sys_raw_file` 和导入批次定位可读取的历史归档；确认真实文件名、表头行、列名、列序、纵向合并/续行情况以及地址是否有内容。不要打印数据库口令、原件里的完整客户地址或联系方式到公共报告。
4. 对同一单据按“历史原件 → 数据库 → 后端接口 → 当前页面”逐项核对，并测试有权限与无权限账号。
5. 无法取得数据库或原件时明确写“未核实”，继续做不依赖这些数据的代码核对，不伪造非空率或生产验证。

可以先用以下只读 SQL 核对结构与粗略非空数；交接期间未执行。查询运行在本地现有授权连接上，先确认环境：

```sql
SELECT table_name, column_name, data_type, character_maximum_length
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (
    (table_name = 'f_maintenance_order'
     AND column_name IN ('receiver_address', 'delivery_address_option'))
    OR
    (table_name = 'maintenance_site_issue'
     AND column_name = 'site_location')
  )
ORDER BY table_name, column_name;

-- 全部源单的粗略计数，不等同于有效单地址覆盖率。
SELECT COUNT(*) AS total,
       COUNT(*) FILTER (
         WHERE NULLIF(BTRIM(receiver_address), '') IS NOT NULL
       ) AS with_address
FROM f_maintenance_order;

SELECT COUNT(*) AS total,
       COUNT(*) FILTER (
         WHERE NULLIF(BTRIM(site_location), '') IS NOT NULL
       ) AS with_site_location
FROM maintenance_site_issue;
```

## 5 推荐实现路径

### 5.1 需求单地址继续复用现有字段

使用现有 `receiver_address`，将地址视为单头展示信息，不参与数量、成本、返还率、单据身份或项目归属。

在识别为 WBDD 后，按文件类型设置明确的表头别名，例如「地址」「收货地址」「收货地址（选填）」。别名集合是建议，需核对真实样本。不要在全局 canonicalize 中对所有含“地址”的表头做替换，以免影响采购、销售等其他导入。

同步修改专用上传的布局校验，使地址列移动、插入及兼容别名后仍可读。保留核心身份、重复表头、头/明细语义和文件类型校验，不能简单删除所有防御或只放宽列数。已有双表头和标准 90/91 列必须继续兼容；未知扩展列的处理规则要明确，不能默默误读。

建议把“本次源文件是否提供地址字段”带到更新层，区分字段缺失与显式值，避免缺列被 transform 变成 None 后由 upsert 清空旧地址。公共 loader 修改只涉及必要的地址更新语义，并覆盖其他导入不受影响的契约测试。

### 5.2 统一更新及同单冲突规则

以下是待确认建议，不是已经拍板的决策：

- 没有地址列：创建时可为空，更新时保留数据库现值。
- 地址列有值：保存或更新，保留中文、换行和完整文本；不截断。
- 地址列为空：建议保留旧值，清空走明确动作；若甲方要求空白即清空，需明确来源文件与往返工作簿的差异并写决策。
- 同单多明细：单头地址可以重复或仅在首行填写，续行只在同一单据组内继承，不能跨单串地址。
- 同单出现两个不同非空地址：明确报冲突并指出单号/行号；不能按处理顺序“最后一行覆盖”。
- 同时出现「地址」「收货地址」等候选列：明确检测同义列重复/矛盾，不以列序猜测哪列权威。
- 不用“缺地址列”“空地址”触发删除、作废、恢复或更换项目归属。

### 5.3 领用地址先确定语义再补齐链路

先确认领用“地址”是否就是现有“现场位置”：

- 同义：复用 `site_location`，页面可统一叫「地址（现场位置）」；补上项目总表 06 的导出、导入、新增、修改及详情显示。
- 不同义：若同时需要收货地址与实际使用地点，独立字段保存，不能将旧 `site_location` 原值搬成另一个含义；按需要设计 nullable 列和迁移。

不要根据需求单的收货地址自动覆盖领用地址。若提供预填，应由用户确认，并保存领用单自己的地址。

旧 Excel 没有该列时仍能上传并保留旧值。同一张领用单的地址是单头信息；同单多行的值一致性、只改一行的传播方式应沿用现有单头规范化机制，不能一行一行覆盖。

项目总表有签名、基线令牌、行级三路合并、重放及自动编号协议。增加字段时必须覆盖 `V2_SITE_HEADERS`、`V2_SITE_BASE_FIELDS`、行值构造、解析计划、apply、审计、版本更新和令牌兼容。旧工作簿基线没有地址字段，不能把“未提供地址”当成用户清空；新文件应把地址纳入相应冲突/基线机制。不要把地址纳入稳定实体身份或因为修改地址生成重复领用单。

如复用 256 字符列，前后端都明确验证超长输入，不静默截断；若要支持更长地址，再统一调整数据库、API、服务、模板与前端。不要因标签改名而改变原先手填“现场位置”必填规则。

### 5.4 优先核验现有展示

需求单“需求单与数据同步”及项目“备件与需求单”已有联系信息组件。先修正确认的缺口，保持长地址换行、复制、空值标记和现有客户信息权限。

领用地址必须在实际使用的手填入口、列表/详情及导出中一致。工作簿录入的领用单也应能看到地址。地址脱敏范围需沿用仓库现行权限或明确决策，不能只在前端隐藏而接口仍返回不该见的数据。

### 5.5 历史补录单独处理

仅当原件确有地址、数据库缺失且能唯一定位现有单据时，准备地址专用补录预览和差异报告。复用稳定源 ID，不能按项目名称或模糊单号猜匹配。

补录只改地址，保留来源与审计，不通过全量重导顺便覆盖数量、成本、手工 override、墓碑或归属。存在非空现值冲突时列为待核对。本次需求授权不包含自动生产补录或部署；先完成可审核的预览和实现，再按既有发布规则处理。

## 6 代码入口

路径与行号以调查基线为准，实施前用 rg 重新定位。

| 链路 | 重点文件 |
|---|---|
| 需求单模型和迁移 | `backend/app/models/maintenance.py`；`backend/alembic/versions/b4c8d2e6f1a3_wbdd_display_columns.py` |
| 表头与单头分组 | `backend/app/etl/mapping.py` 的 `MAINTENANCE_HEAD_DISPLAY`、`canonicalize_columns`；`backend/app/etl/reader.py` 的 `_inspect_frame`、`_ffill_head_columns` |
| 地址转换和更新 | `backend/app/etl/transform.py` 的 `_WBDD_HEAD_DISPLAY_SPEC`；`backend/app/etl/loader.py` 的需求单 upsert |
| 专用布局门禁 | `backend/app/services/maintenance_wbdd_import.py` 的 `validate_wbdd_layout`、`precheck_wbdd_file` |
| 需求单读取和展示 | `backend/app/services/maintenance_order_contact.py`；`frontend/src/components/maintenance/OrderContactInfo.tsx`；`MaintenanceDemandsPage.tsx`；`panel/PartsOrdersTab.tsx` |
| 领用模型和迁移 | `backend/app/models/maintenance_project_operations.py`；`backend/alembic/versions/f4b8d2e6a1c3_site_issue_v2_drafts.py` |
| 领用手填 API 和服务 | `backend/app/api/maintenance_project_operations.py`；`backend/app/services/maintenance_manual_site_issue.py`；`backend/app/services/maintenance_project_operations.py` |
| 领用表单和详情 | `frontend/src/components/maintenance/ManualSiteIssueSection.tsx`；`SiteIssueWorkflowPanel.tsx`；`frontend/src/pages/maintenance/panel/SiteReturnTab.tsx`；`frontend/src/api/maintenanceOperations.ts` |
| 领用 Excel | `backend/app/services/maintenance_project_master_workbook.py` 的 `V2_SITE_HEADERS`、`V2_SITE_BASE_FIELDS`、`_v2_site_row_values`、`_v2_parse_site` 和 apply |
| 历史原件归档 | `backend/app/models/system.py` 的 `SysRawFile`；`backend/app/etl/pipeline.py`；配置 `raw_file_dir` |

## 7 与另一台电脑的协作

1. 开始前读取 `AGENTS.md`、`docs/decisions/`、`CONTEXT.md`、本报告、`docs/agents/` 和 `docs/BRANCH_POLICY.md`。业务规则未覆盖的部分按仓库要求先记录新决策，不能把本报告的待确认建议当成已批准口径。
2. 先记录本地分支、HEAD、git status、暂存和未暂存差异，保留已有代码。不要 hard reset、clean、强推或用远端整文件覆盖本地成果。
3. fetch 后核对本地未推送提交及删除工作的实际差异。若另一台电脑尚有未共享改动，把受影响模块列明，先做可独立的地址工作，不编造共同基线已对齐。
4. 从合适的最新 test 基线创建地址功能分支，拆成需求单 Excel 兼容和领用地址两个小范围提交。文档提交可单独取入；无需合并整条旧开发分支来取得交接。
5. `etl/loader.py` 和 `maintenance_project_master_workbook.py` 是地址与删除可能共同修改的高危模块。即使目标只改地址，也可能产生文本及行为冲突，必须逐段核对并补契约测试，保留另一侧已确认的删除实现。
6. PR 默认指向 test。main 是生产发布线，任何合并须遵守管理员对精确候选 SHA 的人工 GitHub APPROVED review；本交接不授权绕过分支保护、main 合并或生产部署。

## 8 验收与测试

按功能改动选择实际测试；这次文档交接没有运行以下测试。

- 标准 90/91 列、单双表头、地址列移动及插入后仍正确读取；新增、改名候选地址列不会使 ID、PN、数量错位。
- 支持约定别名；不同含义的「选择收货地址」仍独立；重复/冲突候选列给出可定位提示。
- 没地址列的旧文件能导入；更新不丢旧地址；空白和明确清空符合最终决策。
- 同单多明细、首行有值续行空白、下一单空白、纵向合并及不同地址冲突均不串单、不静默覆盖。
- 中文、换行、长地址完整保存；超长校验一致。
- 需求单和领用单的页面、接口、数据库、Excel 回传和导出相互一致。
- 有权限、无客户信息权限及不同项目可见范围正确，接口不泄露地址。
- 新旧工作簿基线、并发修改、幂等重放、自动编号和单头传播保持正确；修改地址不新建重复单、不丢其他行修改。
- 既有数量、成本、人工 override、归属、作废优先和删除行为不因地址变化而改变，且保留另一台电脑已确认的修复。

优先扩展既有用例：`backend/tests/test_maintenance_wbdd_import_layouts.py`、`test_maintenance_order_contact.py`、`test_manual_site_issue_api.py`、`test_site_issue_v2_api.py`，以及项目总表工作簿契约测试、`ManualSiteIssueSection.test.tsx`、`SiteIssueWorkflowPanel.test.tsx` 和联系信息组件测试。运行必要后端回归、迁移检查及前端测试/类型检查/构建；若未改库结构，不制造无必要迁移。

按 AGENTS 要求，UI 自动检查通过后报告“待验收”，由用户在 `https://test.yunci.ink` 点按验收。不得把浏览器调试或合成测试宣称为甲方已验收。

## 9 接手后应交付

- 真实数据核查结果，逐项区分已核实和未核实。
- 地址语义、表头别名、空值/清空、冲突及旧模板兼容的最终决定与对应决策编号。
- 小范围实现提交、测试结果、迁移影响与实际验收状态。
- 如需历史补录，提供地址专用差异预览；没有生产授权不执行补录。
- 交接与 CONTEXT 同步更新为最终实现状态，避免后续代理继续把建议当成现状。
