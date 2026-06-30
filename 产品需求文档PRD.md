# IT 备件智能管理系统 — 产品需求文档（PRD）

> 版本：v1.0 | 日期：2026-06-30 | 状态：现行

## 目录

1. [背景与立项依据](#一背景与立项依据)
2. [用户画像与需求](#二用户画像与需求)
3. [问题定义与核心挑战](#三问题定义与核心挑战)
4. [产品功能规格](#四产品功能规格)
5. [技术架构决策](#五技术架构决策)
6. [工程化路线图](#六工程化路线图)
7. [验收标准总览](#七验收标准总览)
8. [遗留与待定](#八遗留与待定)

---

## 一、背景与立项依据

### 1.1 客户背景

**甲方**：中石化完井研究中心（中国石油化工集团旗下专业研究机构）

中心 IT 部门负责管理数百台服务器、存储设备、网络设备及其配套备件，包括内存条、硬盘、网卡、CPU、HBA 卡、光模块等各类 IT 耗材。业务模式为：IT 部门统一采购备件后，向内部各研究室/项目组按需销售，形成内部结算。

**业务规模（现行数据）**：
- 型号总数：23,152 个 dim_part 记录
- 采购/销售明细：来源于氚云（泛微低代码平台）导出的 Excel 文件
- 业务人员：销售 3-5 人、采购 2-3 人、管理层 2 人

### 1.2 现有痛点

#### 痛点 1：型号数据混乱（核心问题）

同一个备件可能有十几种写法：
- 厂商官方型号（MPN）：`HMA82GR7AFR8N-VK`
- 供应商自定义编码：`SK-32G-DDR4-2666-REG-V2.0`
- 客户系统编码：`MEM-32G-2666`
- 氚云录入变体：大小写不一、带/不带连字符、带 V-code 版本号前缀

后果：库存数据离散，同款型号分散为多个独立 SKU，无法汇总真实库存量。实测 94%（21,750/23,152）的型号存在恒等别名问题（pn_raw = pn_std），合并重复导致唯一键冲突。

#### 痛点 2：成本核算失真

成本走移动加权平均法，但由于型号碎片化，同款型号分散为多个独立 SKU，每个文本变体各自维护独立的成本池，导致加权平均被污染，毛利分析数据不可信，无法作为经营决策依据。

#### 痛点 3：采购决策依赖人工经验

销售和采购人员需要手动查多个系统（氚云、Excel、历史邮件）才能报价，耗时且易出错。历史成交价散落在进出口记录里，无系统化提取，新员工上手慢。

#### 痛点 4：数据导入摩擦大

氚云系统导出的采购/销售明细为 Excel，双表头格式（第一行字段名、第二行包含 F\d{7} 格式氚云数据 ID），需要人工识别、处理后再录入，重复劳动且容易引入错误。同一份文件重复导入会产生重复数据。

#### 痛点 5：管理透明度不足

老板无法实时看到哪类备件最赚钱、哪个客户贡献最大、库存是否有积压，经营分析全靠月底人工整理报表，决策时效性差。

#### 痛点 6：AI 能力未被利用

团队有基本的 AI 使用意识，但 LLM 无法访问内部价格和库存数据，只能依赖通用知识，无法给出有意义的报价建议。

### 1.3 立项目标

用一套统一的信息管理系统替代 Excel + 氚云导出 + 人工查价的工作流程，实现：

| 目标 | 衡量标准 |
|---|---|
| 型号主数据统一治理 | 23,152 型号中重复率从 94% 降至 <5% |
| 成本核算口径统一 | COGS 与库存成本一致，利润数据可信 |
| AI 辅助报价 | 30 秒内返回历史成交价和建议售价 |
| 管理驾驶舱 | 实时利润/库存/客户多维分析，无需等月报 |
| 数据导入自动化 | 氚云 Excel 无人工干预自动解析，幂等不重复 |

---

## 二、用户画像与需求

### 2.1 用户角色

| 角色 | 人数 | 系统权限 | 核心诉求 |
|---|---|---|---|
| 管理员 (admin) | 1 | 全部权限 | 系统配置、账户管理、主数据治理 |
| 老板 (boss) | 1-2 | 全量数据查看，无管理操作 | 经营分析、利润报表、全局视图 |
| 销售 (sales) | 3-5 | 自己客户行级过滤，无成本字段 | AI 报价助手、客户历史价格查询 |
| 采购 (purchaser) | 2-3 | 采购相关权限，有成本字段 | 供应商比价、成本核算、采购记录 |
| 只读 (readonly) | N | 只读，字段组受限 | 型号查询、库存查询 |

### 2.2 核心用户故事

**销售场景 A — 单件报价**
> "客户问我华为 RH2288HV3 服务器内存 32G DDR4 的价格，我打开 AI 助手，直接问'华为 32G DDR4 报多少钱'，助手查库给我历史成交均价和建议售价，30 秒出结果。"

**销售场景 B — 批量询价**
> "客户发来一份 20 个型号的询价单 Excel，我把文件上传给 AI，助手自动批量查价，生成含报价的 Excel 文件，直接发给客户。"

**销售场景 C — 型号解释**
> "客户问 SFP+ 和 SFP28 能不能混用，我让 AI 解释型号差异和兼容性，AI 结合规格库给出专业回答。"

**采购场景 A — 历史价格查询**
> "我需要知道这批内存上次从哪个供应商买的、买了多少钱，直接在系统里搜型号就能看到历史采购记录。"

**采购场景 B — 主数据治理**
> "同一个型号被录成了三个不同的 PN，库存分散了，我在治理工作台合并它们，合并后历史成本自动重算，利润数据恢复准确。"

**管理场景 A — 经营分析**
> "我想知道上半年哪类产品毛利最高、哪个销售贡献最大，直接在分析面板看，不用等月报。"

**管理场景 B — 库存监控**
> "IT 采购这块的库存成本是多少、占用了多少资金，实时可查，年底审计有数据支撑。"

---

## 三、问题定义与核心挑战

### 3.1 核心问题：型号身份碎片化

**问题本质**：IT 备件没有像商品条码一样的全球唯一标识，同一个物理器件可以有多种文本表达。系统必须解决"多文本 → 单身份"的映射问题，且这个映射不能依赖人工一次性处理，因为新数据持续进来。

**问题定量**：
- 23,152 个 dim_part 记录中，94%（21,750 个）存在恒等别名（pn_raw = pn_std）
- 100 个 pn_compact 重复组中约半数是垃圾碰撞（'3'/'CPU'/'15M' 等短串）
- 1,078 个待审型号中 90% 没有可用合并候选

**解决方案**：

```
dim_part.id  ←  商品身份主键（不可变，与文本解耦）
pn_std       ←  当前标准写法（可改名，身份不变）
part_alias   ←  所有历史/别名写法，入库时通过别名表归一到 part_id
part_resolver←  pg_trgm 模糊召回 + Python rerank，处理新型号自动归一
```

**关键约束**：`商品身份 = dim_part.id`，禁止 pn 文本作过滤/聚合键（违者绕过合并重定向）。

### 3.2 核心问题：成本口径裂脑

**问题本质**：移动加权成本必须按同一身份的型号聚合，但历史数据按 pn 文本分散入库，导致每个文本变体各自维护一个成本池，实际成本失真。

**影响链**：
1. 库存成本（backfill_costs）按 pn_std 聚合 → 资产价值失真
2. 利润 COGS（profit.recompute）按 pn 判定排除集 → 利润口径分裂
3. moving-average 被碎片化污染 → 报价参考价不准确

**解决方案**：
- 事实表（销售/采购/库存行）加 `part_id NOT NULL` 外键
- 合并操作同时重指所有历史事实行的 `part_id`（历史数据全部归入主型号）
- `cost.replay()` 按 `part_id` 重放事件流，加权平均口径统一
- 合并/取消合并后自动触发成本重算

### 3.3 核心挑战：AI 与业务数据的安全集成

**挑战**：LLM 需要访问内部价格、成本、客户数据，但这些数据有权限管控——销售不能看成本，A 销售不能看 B 销售的客户数据，老板能看全量数据。

**不能做的**：
- 在 prompt 里写"不要告诉用户成本数据"（prompt 注入可绕过）
- 让 LLM 直接连接数据库（无法做行级过滤）

**解决方案**：
- **权限在数据层**：工具函数在返回数据前先过 `apply_field_visibility()`，递归置空受限字段
- **行级过滤**：`own_customers_only` 的账号，工具查询自动加 `WHERE customer_id IN (...)`
- **LLM 只能看工具返回的数据**，没有任何直接数据库访问通道

### 3.4 核心挑战：合并的副作用传播

**挑战**：合并两个型号时，不只是改一个标记——需要同步更新历史事实行、重算成本、更新库存、压缩指向链。任何遗漏都会导致数据不一致。

**解决方案**：
- 合并操作事务包裹（PostgreSQL ACID）
- `SELECT FOR UPDATE` 升序 id 锁（防并发互合死锁）
- 路径压缩：所有指向被合并型号的 `merged_into_id` → 主型号（链长恒 ≤ 1）
- 合并后自动触发：`recompute(keep_id)` + `backfill_costs(keep_id)`
- LIFO 回滚：`product_merge_logs` 记录前镜像 + 受影响行 id，支持手动回滚最近一次

---

## 四、产品功能规格

### 4.1 数据导入系统

**功能描述**：将氚云导出的 Excel 文件（采购/销售明细）自动化解析入库，无需人工处理。

**关键特性**：

| 特性 | 实现方式 |
|---|---|
| 双表头自动检测 | 扫描行，找第一个含 `F\d{7}` 格式氚云数据 ID 的行作为真实表头 |
| PN 标准化（Strategy B） | 去 V/v 版本前缀、大写、去特殊符号、保留连字符 |
| 幂等入库 | `SHA256(氚云数据ID + 文件哈希)` 唯一键，重复导入自动跳过 |
| Skip/Upsert 模式 | Skip：重复行跳过；Upsert：重复行覆盖更新 |
| 导入后自动处理 | 品牌/品类回填 + 质量扫描 + 质量分更新 |
| 型号归一（3层） | 别名精确匹配 → merge 重定向 → 新型号 upsert（加 `WHERE status != 'merged'` 防墓碑复活） |

**API**：
```
POST /api/imports/upload   multipart form，上传 Excel 文件，触发 ETL 流水线
GET  /api/imports          导入历史列表
GET  /api/imports/{id}     导入任务状态/结果摘要
```

**验收标准**：
- 上传氚云采购明细 Excel，系统自动识别双表头，解析所有行，返回导入摘要（成功/跳过/更新数量）
- 同一文件导入两次，第二次全部跳过（skip 模式）
- 含 V-code 的型号（如 `V2.0-SFP28`）正确标准化

### 4.2 型号主数据治理

**功能描述**：统一管理型号身份，消除重复和碎片化，维护数据质量。

#### 4.2.1 候选发现

| 机制 | 说明 |
|---|---|
| 召回路径 1 | pn_compact 重复组（≥5位且非纯数字，score≥0.70 进队列） |
| 召回路径 2 | needs_review 标记型号的 part_resolver 模糊匹配召回 |
| 垃圾过滤 | pn_compact <5位或纯数字（如 '3'/'CPU'/'15M'）不进队列 |
| 排序策略 | 按业务量（销售额/采购量）排序，高频型号先审 |
| 防重机制 | `product_match_candidates` 部分唯一 `(part_a_id, part_b_id) WHERE status='pending'` |

#### 4.2.2 合并工作台

**候选列表页**：
- 显示两个型号的相似度分数、各自销售量/库存量
- 操作按钮：合并（选主型号）/ 拒绝 / 标记为独立型号
- 批量确认独立型号（避免 1,078 条逐条点击的死亡行军）

**合并操作链**：
1. `SELECT FOR UPDATE` 升序 id 锁（防并发互合死锁）
2. 所有事实行 `part_id` → 主型号
3. 别名 `UPSERT`（ON CONFLICT (pn_raw) DO UPDATE，恒等别名不撞唯一键）
4. 路径压缩：所有指向被合并型号的链 → 主型号（链长恒 ≤ 1）
5. spec/substitute 迁移
6. 被合并型号 `status='merged'`，写 `product_merge_logs`（前镜像 + 受影响行 id）
7. 自动触发：`recompute(keep_id)` + `backfill_costs(keep_id)`

**回滚**：仅支持 LIFO 回滚最近一次合并，回滚后必须手动重算利润 + 库存成本。

#### 4.2.3 别名管理

- 审核：approve（确认别名有效）/ reject（拒绝）
- 重指：reassign（别名重指到正确型号，重定向既有事实行）
- 合并时恒等别名用 UPSERT，不产生唯一键冲突

#### 4.2.4 型号重命名

- 旧 pn_std 自动写入 `part_alias`（type=historical）
- dim_part.pn_std 更新为新值（复合 FK 延迟校验保证事务内一致性）
- 若旧 pn 已有候选 → 状态改为 occupied

#### 4.2.5 数据质量监控

| 质量检查项 | 说明 |
|---|---|
| 品牌占位符 | "待定xxxxx" 等占位符 → brand_id 置空，记质量问题 |
| 无规格型号 | 无任何 product_specs 记录的型号 |
| 疑似重复 | 高相似度但未进候选队列的型号对 |
| 孤立别名 | 指向已合并（墓碑）型号的别名 |

**质量分（0-100）**：由品牌完整度、规格完整度、无疑似重复等指标加权计算。

**验收标准**：
- 治理工作台可在 1-2 天内完成 1,078 个待审型号的全部审核
- 合并后成本口径统一，重算结果与合并前按 pn 分组求和等价（30 项回归测试）

**API**：
```
GET  /api/governance/metrics                    治理 KPI 指标（候选数、质量分、待审数等）
GET  /api/governance/candidates                 待审候选列表（分页、按业务量排序）
POST /api/governance/candidates/{id}/merge      确认合并
POST /api/governance/candidates/{id}/reject     拒绝
POST /api/governance/candidates/{id}/independent 标为独立型号
POST /api/governance/candidates/bulk-independent 批量确认独立
GET  /api/governance/quality-issues             质量问题列表
POST /api/governance/quality-issues/{id}/dismiss 关闭问题
POST /api/governance/refresh                    刷新候选 + 质量扫描
```

### 4.3 成本与利润分析

**功能描述**：多维度利润核算与分析，成本口径按 dim_part.id 聚合。

**成本方法**：移动加权平均（主） + FIFO（备）双轨并行，按 part_id 事件流重放。

| 功能点 | 说明 |
|---|---|
| 利润总览 | 按型号/销售员/客户 3 维聚合，支持日期范围过滤 |
| 利润排行榜 | top N 最赚钱型号（含毛利率、营收、数量） |
| 排除集 | 标记不计利润的型号（内部测试、维保件等） |
| 手动重算 | 大批量合并后手动触发 recompute |
| 字段展示 | 毛利率、营收、成本、数量、移动加权/FIFO 双口径 |

**验收标准**：
- part_id 分组 vs pn 分组逐行等价（零回归测试）
- 库存资产价值与利润 COGS 口径一致（backfill_costs 按 part_id）
- 合并后成本流归并正确（30 项回归测试）

**API**：
```
GET  /api/profit/overview          聚合利润（3D：part/salesperson/customer）
GET  /api/profit/ranking           利润排行榜（top N）
POST /api/profit/recompute         触发利润重算
GET  /api/profit/exclusions        排除集列表
POST /api/profit/exclusions        添加排除项
DELETE /api/profit/exclusions/{id} 移除排除项
```

### 4.4 库存管理

**功能描述**：实时库存量和成本价值查询，按 dim_part.id 聚合（合并后自动归并）。

| 功能点 | 说明 |
|---|---|
| 库存列表 | 型号、仓库、数量、单位成本、总价值 |
| 合并聚合 | 同一 part_id 的多行库存合并显示 |
| 成本回填 | backfill_costs：合并后自动更新库存成本到新 part_id 口径 |

**API**：
```
GET  /api/inventory                库存列表（含成本估值）
GET  /api/inventory/{part_id}      型号库存详情
POST /api/inventory/backfill-costs 触发成本回填
```

### 4.5 AI 智能体

**功能描述**：接入内部数据的 LLM 助手，支持多轮对话、工具调用、流式输出、服务端会话持久化。

#### 工具列表

| 工具 | 功能 |
|---|---|
| search_parts | pg_trgm 模糊搜索型号 + 规格过滤（part_type/interface/capacity 区间） |
| get_part_overview | 型号详情（含合并重定向、规格、别名、历史价格） |
| get_profit_ranking | 利润排行（含毛利率、营收、数量） |
| list_recent_purchases | 近期采购记录（默认 90 天） |
| inspect_file | Excel 文件预览（列名、行数、样本行） |
| read_file_rows | 分批读取文件行（分页防超大文件） |
| read_document | 读取文档内容（PDF/Word/TXT） |
| lookup_prices_bulk | 批量查价（part_id 列表 → 价格信息） |
| write_excel | 生成 Excel 报表，返回下载链接 |
| write_report | 生成 Word 报告，返回下载链接 |

#### 场景手册

| 场景 | 触发条件 | 核心能力 |
|---|---|---|
| A — 报价助手 | 用户询问某型号价格 | 查历史成交价、建议售价、库存量 |
| B — 压价助手 | 用户询问采购价/谈判策略 | 历史采购价、供应商对比、成本分析 |
| C — 型号解释 | 用户询问型号含义/兼容性 | 规格解析、接口协议、兼容性说明 |
| D — 批量询价 | 用户上传询价单 Excel | 批量查价、生成含报价的 Excel |
| D2 — 拆整机 | 用户上传整机清单 | 整机拆解为零部件清单 |
| D3 — 配整机 | 用户描述整机需求 | 配置整机方案 + 备件清单 |
| E — 经营分析 | 用户询问经营状况 | 利润趋势、客户/品类分析、报告生成 |

#### 会话特性

- **服务端持久化**：`chat_session` / `chat_message` 入库，历史不丢失
- **历史窗口**：20 条消息 / 单条 8,000 字符，服务端截取（不传全量历史给 LLM）
- **并发保护**：同一会话同时只允许一轮生成（409 Conflict）
- **中断语义**：点停止 → `stopped=True` 落库；断网 → worker 跑完整答案落库
- **权限隔离**：`fb=True`（共享口令回退登录）→ 禁用会话功能（403）
- **流式输出**：SSE 事件（delta/thinking/tool/tool_done/done），支持 DeepSeek-R1 思考链展示

**验收标准**：
- "华为 32G DDR4 报多少钱？" → 30 秒内返回历史成交均价和建议售价
- 20 行询价单 Excel 上传 → 批量查价 → 生成含报价 Excel，全程 < 2 分钟
- 销售账号无法在任何工具返回结果中看到成本/毛利字段

**API**：
```
POST /api/agent/chat          非流式单轮对话
POST /api/agent/chat/stream   SSE 流式对话
POST /api/agent/chat/cancel   取消当前生成
POST /api/agent/files/upload  上传文件供工具使用
GET  /api/agent/files         已上传文件列表
GET  /api/chat-sessions       会话列表
POST /api/chat-sessions       创建新会话
GET  /api/chat-sessions/{id}  会话详情 + 消息历史
DELETE /api/chat-sessions/{id} 删除会话
POST /api/chat-sessions/{id}/messages  发送消息（SSE 流式响应）
POST /api/chat-sessions/{id}/cancel    取消生成
```

### 4.6 型号搜索与规格查询

**功能描述**：支持模糊搜索 + 规格维度过滤，使"按容量/接口查询"可用。

| 参数 | 说明 |
|---|---|
| q | 型号关键词（pg_trgm 模糊匹配） |
| part_type | 品类（内存/硬盘/网卡/CPU 等） |
| interface | 接口类型（DDR4/SAS/SATA/SFP+ 等） |
| capacity_min / capacity_max | 容量区间（GB，利用 numeric_value 范围查询） |

**API**：
```
GET /api/parts                 搜索型号列表（支持上述过滤参数）
GET /api/parts/{id}            型号详情（含规格、别名、替代品）
PUT /api/parts/{id}            更新型号元数据
POST /api/parts/{id}/rename    重命名（含别名归档和候选重置）
```

### 4.7 替代品管理

**功能描述**：管理型号间的替代关系，支持有向和双向替代。

| 特性 | 说明 |
|---|---|
| 方向 | both（互相替代）/ a_to_b / b_to_a |
| 规范序 | `CHECK (part_a_id < part_b_id)` 防双向数据二义 |
| 状态 | active / pending / deprecated |

**API**：
```
GET    /api/parts/{id}/substitutes   型号替代品列表
POST   /api/parts/{id}/substitutes   添加替代关系
PUT    /api/substitutes/{id}         更新替代关系
DELETE /api/substitutes/{id}         删除替代关系
```

### 4.8 账户与权限管理

**功能描述**：多角色访问控制，支持字段级和行级权限隔离。

**权限矩阵**：

| 能力 | admin | boss | sales | purchaser | readonly |
|---|---|---|---|---|---|
| 成本字段 | ✓ | ✓ | ✗ | ✓ | ✗ |
| 价格字段 | ✓ | ✓ | ✓（自己客户） | ✓ | ✗ |
| 毛利字段 | ✓ | ✓ | ✗ | ✓ | ✗ |
| 供应商字段 | ✓ | ✓ | ✗ | ✓ | ✗ |
| 用户管理 | ✓ | ✗ | ✗ | ✗ | ✗ |
| 主数据治理 | ✓ | ✗ | ✗ | ✓（有限） | ✗ |
| 行级过滤 | 无 | 无 | own_customers_only | 无 | 无 |

**安全特性**：
- HMAC-SHA256 自签 token，`token_version` 数据库列即时吊销
- 登录失败锁定：5次失败 → `locked_until = now + 15min`
- 产品模式（PROD）拒绝默认 secret，强制自定义密钥

**API**：
```
POST   /api/auth/login              用户名密码登录，返回 token
POST   /api/auth/logout             吊销当前 token
GET    /api/auth/me                 当前用户信息
GET    /api/accounts                用户列表（admin only）
POST   /api/accounts                创建用户
PUT    /api/accounts/{id}           更新用户（角色/密码等）
DELETE /api/accounts/{id}           删除用户
POST   /api/accounts/{id}/reset-password  重置密码
```

---

## 五、技术架构决策

### 5.1 架构原则

1. **商品身份 = dim_part.id**：所有功能引用商品一律 part_id，禁止 pn 文本作键
2. **权限在数据层**：`apply_field_visibility()` 在工具/API 层，不在 prompt 层
3. **服务端为唯一事实源**：会话、工作流、治理定义全部入库，前端是纯视图
4. **幂等操作**：所有导入、回填、重算均幂等，可重复执行不产生副作用

### 5.2 技术选型理由

| 技术 | 选型理由 |
|---|---|
| FastAPI | 原生异步 IO，SSE 流式输出天然支持，pydantic v2 类型安全 |
| PostgreSQL 15 + pg_trgm | GIN 索引实现型号模糊搜索（核心功能）+ 事务保证合并原子性 |
| SQLAlchemy 2.0 ORM | async session，类型安全，复杂查询可 text() 兜底 |
| Alembic | 数据库迁移版本控制，single-head 测试保证迁移链完整 |
| React 18 + AntD 5 | 企业内部系统标准组件库，快速构建数据表格/表单/工作台 |
| OpenAI 兼容 API | 可切换 DeepSeek/Qwen/其他模型，不锁定单一厂商 |
| uv | Python 包管理，比 pip/poetry 快 10-100x，lock 文件确定性 |
| Docker Compose | 单机部署三服务（db/app/frontend），运维简单 |

### 5.3 关键设计决策

**为什么用 dim_part.id 而不是 pn_std 作主键？**
pn_std 是文本，可以改名。业务上"同一款产品"的概念必须独立于其文本表达——合并、重命名、别名都需要一个不变的身份锚点。

**为什么不直接用 pn_std 唯一约束就够了？**
pn_std 唯一约束保留（用于复合 FK 防文本漂移），但它不能作聚合键，因为合并后两个 pn_std 必须归一到同一个事实行集合。

**为什么 moving-average 和 FIFO 双轨并行？**
moving-average 是会计主口径（平滑价格波动）；FIFO 是审计辅助口径（精确追溯批次）。replay() 同时维护两个，对性能影响可忽略。

**为什么 pn 文本永远保留在事实表？**
`FSalesLine.pn_raw / pn_std` 保留导入原文，是审计追溯和合并回滚的前提条件，任何时候都可以回溯"这条记录原始导入时写的什么型号"。

---

## 六、工程化路线图

### 已完成（P0/P1）

- [x] 基础 CRUD：型号/采购/库存/替代品/用户
- [x] 数据导入 ETL 流水线：氚云 Excel 自动解析 + 幂等入库
- [x] 成本引擎：moving-avg + FIFO replay，按 part_id 聚合
- [x] 利润分析：3D 聚合 + 排除集 + 按 part_id 口径
- [x] AI 智能体：10 工具 + 7 场景 + 服务端会话 + SSE 流式
- [x] RBAC：5 角色 + 字段级 + 行级权限
- [x] 型号主数据整改：dim_part.id 身份锚 + 合并/拆分 + 路径压缩
- [x] 治理工作台：候选审核 + 别名管理 + 质量问题 + 批量操作
- [x] 规格查询：product_specs EAV + /parts/search 规格过滤
- [x] CI：GitHub Actions（pytest + tsc + vite build）+ alembic 单头测试

### P2：知识库（RAG）

**用途**：产品资料、兼容性矩阵、供应商档案、SOP 文档 — 模型按需检索引用。

**核心数据模型**：
```sql
kb_collection(id, name, description, owner_sub, visibility)  -- private/team/all
kb_document(id, collection_id, filename, source_type, file_hash, status)
kb_chunk(id, document_id, seq, content, embedding vector(1024), token_count, meta)
```

**关键设计**：
- pgvector 向量检索 + pg_trgm 关键词混合（failover 降级）
- `search_knowledge(query, collection?)` 工具接入智能体
- `collection.visibility` 过滤后才进 LLM 上下文（权限不变量）
- 入库管道复用现有多格式解析（docx/pdf/xlsx/图片→视觉）

**依赖**：PostgreSQL 镜像升级 `postgres:15` → `pgvector/pgvector:pg15`

**验收场景**："DDR4 和 DDR5 能混插吗？" → 检索兼容性文档 → 带出处回答

### P3：工作流引擎

**用途**：把"询价→查价→回填→发采购"等固定流程显式化、可视化，可断点续跑。

**核心数据模型**：
```sql
wf_definition(id, name, graph JSONB, version, enabled)  -- 节点+边 DAG
wf_run(id, definition_id, status, inputs, outputs, started_by)
wf_run_step(id, run_id, node_id, status, input, output, error)
```

**节点类型**：
- `tool`：调用现有工具（search_parts/write_excel 等）
- `llm`：一段提示词 + 上下文
- `branch`：条件分支
- `human`：挂起等待人工确认，复用治理审核的待办列表交互

**与智能体集成**：`run_workflow(name, inputs)` 作为复合工具暴露给 agent

### P4：Skill 可插拔

**用途**：新业务场景不改代码，在管理页配置即可，对标扣子(Coze)的 Skill 概念。

**核心数据模型**：
```sql
skill(id, name, manual_md, tool_names[], kb_collections[], enabled)
```

**现有 A-E 场景手册**迁移为表化 skill，系统提示词在运行时按启用的 skill 动态拼装。

**MCP 远期**：provider/tools 抽象已是 OpenAI function 格式，将来接 MCP server 只需把 MCP tool list 翻译进注册器，不动 runtime。

### 实施依赖关系

```
P1（已完成）──┬──> P2 知识库（依赖：pgvector 镜像、embed provider）
              ├──> P3 工作流（依赖：human 节点复用治理审核交互）
              └──> P4 Skill 表化（依赖：P2/P3 稳定后抽象才不会抖）
```

---

## 七、验收标准总览

| 功能域 | 关键验收指标 |
|---|---|
| 数据导入 | 氚云 Excel 无人工干预自动解析；重复导入幂等（全部跳过） |
| 型号治理 | 1,078 个待审型号 1-2 天审完；30 项合并回归测试全过 |
| 成本利润 | part_id 口径 vs pn 口径逐行等价；COGS 与库存成本一致 |
| AI 助手 | 单件报价 30 秒；20 行询价单批量处理 < 2 分钟 |
| 权限隔离 | 销售账号无法获取成本/毛利字段；行级过滤仅见自己客户 |
| 系统稳定性 | 同一会话无并发生成（409）；断网不丢消息；合并原子性 |

---

## 八、遗留与待定

| 事项 | 状态 | 说明 |
|---|---|---|
| 维保工单备件 | 待定 | `maintenance_parts` 暂无数据源；维保体现为采购 `source_type=维保需求`，不计成本 |
| 前端路由 | 待做 | 现为 state-based routing，页面不可直链/刷新；计划 P2 同期引入 react-router |
| 沙箱代码执行 | 挂账 | 独立容器，不在 API 进程内 exec；三期再做 |
| LLM 用量统计 | P2 | `chat_message` 加 `tokens` 字段 + provider 返回 usage |
| 评测框架 | P3 | 固定问题集 + 期望工具轨迹，防提示词劣化回归 |
| Docker 网络 | 待处理 | dev 库容器仍按旧 compose 配置以 `0.0.0.0:5432` 暴露（建议 `docker compose up -d --force-recreate db`，数据在命名卷不丢） |
| 别名来源类型 | 待定 | `alias_type`（supplier_pn/customer_pn）等氚云导出里没有的来源类型，待有数据源再加 |
