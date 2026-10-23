# 方案 B：静态能力目录、不可变计划、显式事务协调器

> 综合稿说明：本文件保留独立备选方案，未来的 fresh 授权、Session 所有权和接口类型不是当前已实现状态。近期实施以[总体设计](../maintenance-refactor-design.md)及[批次计划](migration-and-acceptance.md)为准；尤其 V2 全面锁后重新鉴权尚需 R3 调查，不从本方案流程示意推断现状。

只做设计。基线 HEAD fa97e24（main基线7a9fd20）；没有修改业务代码。采用 design-an-interface 的“最大扩展性”视角，不推荐因为存在注册表就注册一切。

## 1. 问题与真实调用者

目标是同一项事实编辑供网页与工作簿复用、增加相同协议下的新表/新呈现时尽量不改事务核心；领域的新规则、锁类型、新权限永远不能承诺零核心修改。

已读证据（路径均相对仓库，行号为fa97e24）：
- services/maintenance_project_master_workbook.py:1817 MasterV2Plan 已是 frozen dataclass，但 site_issue_merges:tuple[dict,...]、site_issue_rows:dict[str,list[int]] 等深层仍可变，frozen不等于计划不可变。:5745 validate、:5813/:5823/:5865/:5869/:5967 按sheet分支；:6289公开apply拥有事务/约束回滚；:6327内部apply；:6357全局advisory，:6403附近XSDD identity，:6470状态/项目后，:6485应用幂等advisory；:7405统一bump，:7418原回执及:7441 commit。
- api/maintenance_project_master_workbook.py:886-940 先查询已完成回执，再validate，再查回执；合同额门、合并token及force_takeover仍由HTTP组织，不能让通用plan代替。
- services/maintenance_site_issue_line_correction.py:87 校验、当前授权、幂等锁、项目state/row、issue锁、重放、line锁、CAS、原语、bump、保存完整回执；:107锁幂等键，:108锁项目，:163调用事实原语，:169 bump；最后flush而不commit。
- api/maintenance_project_operations.py:1925 _site_issue_correction_result每次authorize刷新SysUser+token版本+page/action/cost+project scope；:1961 HTTP拥有commit。不能误称现状已统一service-own-tx。
- services/maintenance_site_issue_facts.py:202 apply_locked_site_issue_edits 明确不拥有commit/rollback/bump/回执；单头修改需要全部active明细锁，native delivery-backed受限；下游义务仍有自己的后继锁。
- services/maintenance_boss_board.py:1728 _card_contracts为台账优先+XSDD回退完整读模型；外部调用为cost.py:1245、master_workbook.py:347、workbook_v3.py:400、manager_workbook_adapter.py:41。此处不是单纯复制展示代码。
- services/maintenance_manual_site_issue.py:86 _resolve_demand_project只核active assignment，不能抽取时擅自追加order状态/墓碑检查（另有业务决定）。
- frontend/.../MaintenanceProjectPanelPage.tsx:383-401是已挂载Tab读回屏障；analysisNavigation.ts:13-21是URL白名单；后台能力目录不能自动决定前端刷新的完成语义。

## 2. 设计主干

```text
HTTP/CLI（受控调用者）
  ├── typed capability key ── 静态ReadRegistry ── 深读模型（只读）
  ├── 更正请求 ── SiteCorrectionTx ── 锁内领用事实原语
  └── 已签名文件 ── SheetCompilerRegistry ── ImmutableWorkbookPlan
                                          └── WorkbookApplyTx ── 锁内领域原语

两类事务协调器各自拥有当前授权、既有锁序、CAS、回执、revision和commit。
registry不拥有事务、不选择业务规则；sheet compiler只产生语义明确的意图，不能写库。
```

采用两个固定业务事务协议，不能把不同锁序强行压成一个sorted(table,id)全局锁序。capability descriptor只能绑定已经存在且明确审查的协议，不允许 before_commit/after_apply 等任意hook，也不通过字符串配置改税率、作废、取价、合并规则。

## 3. 提议 Interface（示意类型，不是可直接粘贴运行的框架）

```python
@dataclass(frozen=True)
class CapabilityKey(Generic[Request, Result]):
    name: str
    schema_version: int

@dataclass(frozen=True)
class WorkbookPlan:
    identity: WorkbookIdentity
    sheets: tuple[CompiledSheet, ...]
    affected: AffectedFacts
    guards: tuple[EntityGuard, ...]
    authorization: RequiredBusinessPermissions
    confirmation_digest: str

class MaintenanceReads:
    def query(self, key: CapabilityKey[Request, Result], *,
              actor: ActorIdentity, request: Request) -> Result: ...

class WorkbookCompiler:
    def preview(self, *, actor: ActorIdentity,
                submission: WorkbookSubmission) -> WorkbookPreview: ...

class WorkbookApplyTx:
    def apply(self, *, actor: ActorIdentity,
              submission: ConfirmedWorkbookSubmission) -> WorkbookReceipt: ...

class SiteCorrectionTx:
    def execute(self, key: CorrectionKey[Request], *,
                actor: ActorIdentity, request: Request) -> SiteCorrectionReceipt: ...
```

`CorrectionKey`只允许同一个已验证更正协议中的“单行更正/整单单头更正”，不是任意维护命令枚举。新增合单、仓库核销、批量跨项目不是给它注册handler即可；需要独立命令协议设计。

静态组装发生在 composition.py，普通模块导入无注册副作用：

```python
READS = ReadRegistry((
    ReadCapability(CONTRACT_REFERENCES, read_contract_references),
    ReadCapability(ASSIGNED_DEMAND_LINES, read_assigned_lines),
))
SHEETS = SheetCompilerRegistry((
    PartsSheetCompiler(), SiteIssueSheetCompiler(), ExpenseSheetCompiler(),
    ReceiptSheetCompiler(), MilestoneSheetCompiler(), OverviewSheetCompiler(),
))
CORRECTIONS = CorrectionRegistry((LINE_CORRECTION, HEADER_CORRECTION))
```

真实静态注册对象关联typed request/result和领域具体compiler；编译器可以做只读领域校验，不拿可提交Session。不用文件扫描、入口点插件、用户配置或动态import。类型/重复键/协议版本在启动时失败；这里不是安全沙箱，任意Python代码仍能绕过，靠模块依赖与测试约束，不能把registry宣称为防恶意插件机制。

### 逐变量表

| 名称 | 类型 | 用途 | 为何这样定义 |
|---|---|---|---|
| key | CapabilityKey[Request,Result] / CorrectionKey[Request] | 选择编译期已知能力 | 泛型关联请求与结果，避免任意字符串+dict |
| actor | ActorIdentity | 当前请求的实名与token身份 | 不把可见项目集合或旧权限缓存当授权；执行时仍查当前账号 |
| request | 具体frozen请求DTO | 单次读取/更正的业务输入 | 保留缺省/明确清空、CAS版本、原因、幂等键 |
| submission | WorkbookSubmission / ConfirmedWorkbookSubmission | 文件字节/哈希、项目、预检确认凭据 | apply绑定原文件和合并确认，不接受客户端自造计划 |
| identity | WorkbookIdentity | 项目/export/file hash/协议版本 | 明确同文件原回执身份，不靠对象内存地址 |
| sheets | tuple[CompiledSheet,...] | 已识别表的语义结果 | 深不可变，无ORM对象、dict/list或裸JSON可变叶子 |
| affected | AffectedFacts | 显式稳定ID/缺位身份集合 | 是锁包络输入，不是拿预检值直接写事实 |
| guards | tuple[EntityGuard,...] | 导出基线、用户实际改动及版本证据 | 锁内重建/复验，保留D-02作废优先与三路合并 |
| authorization | RequiredBusinessPermissions | 汇总此次实际触碰的门 | 只收紧；不能授予；回执额外保存首次合同额资格标记 |
| confirmation_digest | str | 绑定合并集合/冲突决议等用户确认 | 锁内重新编译不一致时要求重新预检，不扩大用户意图 |
| READS/SHEETS/CORRECTIONS | immutable registry | 显式组合目录 | 新同类adapter一处挂载，不改运行时核心 |

`CompiledSheet`使用各领域frozen DTO的明确和类型，而非通用 JSON actions。增加一个真正的新领域事实类型，类型集合、权限汇总和锁协议可能需要修改；这些修改是必须接受的业务成本。

## 4. 两个实际调用例

### 例一：网页更正一条领用事实（当前API:1887 /服务:30）

```python
receipt = corrections.execute(
    LINE_CORRECTION,
    actor=request_identity,
    request=CorrectSiteIssueLine(
        issue_id=issue_id, line_id=line_id,
        expected_issue_version=body.expected_issue_version,
        expected_line_version=body.expected_line_version,
        changes=LineChanges.from_explicit_fields(body),
        reason=body.reason, idempotency_key=body.idempotency_key,
    ),
)
return receipt.to_existing_http_shape()
```

变量表：receipt=SiteCorrectionReceipt，原响应与replay字段；corrections=SiteCorrectionTx，唯一commit拥有者；LINE_CORRECTION=CorrectionKey[CorrectSiteIssueLine]，固定更正协议；request_identity=ActorIdentity，账号/token身份；issue_id/line_id=str，稳定目标；body=现有验证DTO，保留expected版本、changes、reason、idempotency。这个迁移必须把现api:1925授权逻辑和:1961 commit一并收进协议，不可路由与协调器双commit。

单行与单头descriptor只决定合法意图和目标集合，不接收任意函数锁/写/提交；facts原语仍唯一写者。工作簿不能调用execute，因为execute自成事务；它在自己事务内调用相同facts原语。

### 例二：项目总表04费用+06领用一起应用（当前API:886–940）

```python
preview = workbooks.preview(actor=request_identity, submission=uploaded_file)
# HTTP返回原中文差异/冲突/签名确认；用户确认后发第二个请求。
receipt = workbook_tx.apply(
    actor=request_identity,
    submission=ConfirmedWorkbookSubmission(
        project_id=project_id, file_bytes=file_bytes,
        force_takeover=force_takeover, merge_token=merge_token,
    ),
)
return receipt.to_existing_http_shape()
```

变量表：preview=WorkbookPreview，对外兼容中文计划但内部deep immutable；workbooks=WorkbookCompiler，只有只读预检；uploaded_file=WorkbookSubmission，原文件+项目；workbook_tx=WorkbookApplyTx，整本唯一事务拥有者；file_bytes=bytes，不是客户端计划；force_takeover=bool，保留明确接管意图；merge_token=str，服务端签名的既有合并凭据；receipt=WorkbookReceipt，第一次完整回执或经当前授权允许的重放。

事务内依次：fresh授权→可用已完成原回执且复验首次amount门→既有global锁及expire_all→锁包络→复验当前账号/事实/签名→调用领域写原语→审计+原回执+一次revision→commit。状态漂移遵循既有D-02，不以“计划digest不同”一律拒绝合法三路合并；只有需用户明确确认的合并计划漂移按原协议拒绝。上述先后是业务阶段说明，具体幂等锁相对state锁严格按各当前协议保留。

## 5. 延迟发现锁对象：本方案最大风险与硬约束

现workbook:6380–6470不是只锁plan.project_id：改派旧owner、费用VOID原合同、缺位XSDD身份都需入包络。新sheet仅列自身行ID会漏锁，不能给每个adapter一个lock(db)方法让其自行补锁。

- preview给候选affected集合，不给授权/最终锁保证。
- WorkbookApplyTx先取既有global锁；在任何state/row锁前，按现有预读补齐缺位identity、旧owner与级联集合，再取identity、排序state等既有前缀；预读值在后续对应row锁下复验。
- 在state之后发现此前缺少的更早identity/owner，必须rollback并返回范围漂移/重新预检，不能向前补锁；不能静默重跑扩大用户确认集合。
- 原语及下游义务允许协议已定义的后继锁，不笼统宣称所有锁都必须第一条SQL前计算；其资源类别与允许阶段必须显式登记并测试。若新业务引入更早阶段资源，修改事务协议本身，不能只加注册表条目。
- 原生归属/成本/触发器也可能取锁，锁矩阵必须覆盖这些下游，不仅instrument Python显式FOR UPDATE。

Session由协调器创建/关闭，子adapter无commit/rollback；编译期间用只读数据port，不把ORM entity塞进计划。作为渐进迁移，尚未迁移的legacy apply仍完全拥有自己的Session/commit，外层facade只能委托，不能开启另一个事务包住它。

## 6. 扩展改哪些文件（目标态，非现状保证）

新增复用已有事实的工作簿sheet：新增sheet compiler+协议DTO/渲染器；composition.py注册；模板元数据/version/签名允许表清单；契约测试。若复用同种事实意图和锁阶段，WorkbookApplyTx不改。若增加新事实类型、预算/权限、缺位身份、新跨project引用，还必须改意图类型及事务协议；不能宣称“只改一个文件”。旧文件兼容/删除语义单独测试。

新增同协议网页更正命令：具体request DTO+合法意图转换descriptor、composition注册、HTTP route/response adapter、前端API/组件与契约测试。新命令若需要不同幂等身份、锁顺序、库存/跨项目写入，新增独立Tx module并经锁矩阵审查，不塞进CorrectionRegistry。

新增只读读模型：新read module+typed request/result、composition注册、HTTP或Agent Adapter及其鉴权；共用AuthorizedScope/字段脱敏，不依赖board私有函数。并不强制所有内部调用都经过registry，固定内部调用可直接函数调用。

新增前端Tab：新Tab、API、父页lazy/items、analysisNavigation白名单、Tab/父页测试（典型5–7文件）。后端registry不消除这些前端变更。可另做typed TabDescriptor集中URL/标题/默认排序，但不要把React refresh Promise<boolean>协议推到后端capability里。

## 7. 分批迁移和验收

B0：抽合同参考读模型及需求归属检查，保留旧导出别名和全部业务语义，零写链迁移。用现有权限/合同缺失与共享合同测试证明；此批不需要registry。
B1：把MasterV2Plan深可变叶子逐个替换为typed frozen值，并保持序列化/签名/回执完全相同；不要一并改变merge算法。加入嵌套不可变和roundtrip比较。
B2：选择两个真实sheet编译器（费用、回款计划）形成静态目录，只编译不写；输出与旧validate差异/警告/guards/顺序完全相同，未支持sheet继续原路径。新架构不得双运行产生写入。
B3：把网页更正整条授权/锁/CAS/原回执/commit抽为SiteCorrectionTx；API只映射HTTP错误；原事实primitive不动。用真实Postgres双Session测试同幂等并发、重放权限变化、CAS、token变更、rollback和合单后历史回执。
B4：工作簿事务协调器从旧完整apply移入，一次切换唯一事务拥有者，仍使用原写原语；不是按sheet一个个把commit迁走。新增sheet只在B4协议稳定后加入。

验收硬例：费用VOID的contract从原事实取而不是可改Excel；改派同时锁旧/新owner；锁内归属漂移零写；同文件ACK丢失原回执不重算；首次改合同额后重放时权限已撤销拒绝；新sheet审计/回执写失败全回滚；同领用单多行修正一次revision；日期/单号修改锁整单active lines；无PN/part_id半组更新；原生delivery-backed不可用通用编辑绕过；跨sheet只增加当前有授权的事实能力；读registry永远无DML/commit，Agent不获得业务写入口。

## 8. 何时不采用B，及与A/C对比

Interface复杂度：B公开方法少但调用者必须理解typed key、编译结果与两类事务协议，整体Interface明显大于A的三个领域函数。registry本身不创造Depth，只有将跨sheet共同约束集中而减少重复，才值得。

A对调用者最清晰，直接read/correct/apply。若预计只有少数新功能或新增内容总是新业务规则，A更好，registry只会增加导航层。A仍需面对未来apply内的sheet分支膨胀，可借B的deep immutable plan但不必引入目录。

C保留现有facade兼容入口，适合当前外部契约多、数据库锁协议脆弱的状态，最易渐进。但是如果facade只转发到原巨型模块，其可变性仍在内部；B把适配层变化集中在目录/编译器，业务新增速度提高时才有Leverage。

B不值得采用的边界：没有至少两个相同协议但不同输入Adapter；未来变化主要是税务/权限/锁语义而不是输入格式；团队无法承担跨协议并发测试；要求动态第三方插件；认为注册便能给任意业务安全写权限。这些情况下选择A或C，不交付空洞平台。

暂定建议：当前只选B0与B1作为A/C也可复用的基础；B2须以第二个真实新sheet需求驱动。完整B是最大扩展性备选，不是本轮默认实施方案。

## 9. A/C/根代理交叉挑战后的修订

1. **A：谁证明依赖清单完整？** 不能靠typed metadata。新增月份的缺位身份、expense VOID的raw事实关联、合单全部issue及refs，由可信领域compiler与事务内领域包络resolver明确查询和组合，不能从通用descriptor自动推断。每个compiler只表达已知事实意图；lock closure由协议所有者审查。A的新sheet可显式修改一个组合函数，评审直接看到业务顺序，这是实质优势。B只在多个输入表复用既有意图时减少组装变化，不追求把整本事务变成自组织插件。

2. **根：row guards依赖所有writer。** services/maintenance_project_master_workbook.py:5470–5494解释：05回款快照复验时尚未取其最终行锁，依靠global/state/project锁共同阻挡五类writer：operations.create_collection/update_collection、expense_collection_workbook.apply、project_identity归并、apply_exact_collection_dedupe。最后一项不取state。B不能只验证当前sheet declares_locks；必须建立外部writer×共享事实×阻挡锁矩阵。新增writer不遵这些协议时，即使计划完整也会静默破坏一致性，需真实并发测试与访问入口审查。

3. **根：ORM freshness不是重复refresh。** 保留workbook:6360后的锁后首次expire_all，且只在validate确实只读、Session无待flush修改时允许；写阶段不得通用populate_existing/expire_all，否则同一行改数量+改成本会丢未flush值。协议将“获取新鲜基线”与“写阶段”分开，不给adapter可随意调用的refresh hook。必有回归：同一行事实+人工成本同事务保存。

4. **C：旧指纹不能重新定义。** manual_site_issue.py:434–448先_normalize_manual_lines与原业务payload指纹，再取idempotency锁。B不能因引入统一compiler把它调序、换canonical JSON或改缺省/空值造成历史键冲突。manual命令不是当前CorrectionRegistry首批Adapter；后续接入要保留其原receipt/fingerprint版本，对旧回执有独立兼容协议。

5. **C：回执不授予权限。** api/maintenance_project_master_workbook.py:844–862的_completed_v2_upload每次读取requires_amount后复验当前合同额manage。plan.authorization只是待检查清单，绝不作为authorization token或复用旧权限；完整原回执、历史缺快照兼容仍各走原分支。

6. **C：事务所有权迁移必须一次完整。** 过渡facade只委托legacy apply，不begin、不追加commit；最终B3仅将这两个现有correction命令（单头/单行）的API授权+commit与服务锁/CAS一起迁入SiteCorrectionTx，而不是要求所有maintenance commands内部commit。仍未迁移的命令继续原拥有者；Workbook调用facts原语不能调用自提交网页命令。B4必须完整搬原整本事务，不按sheet拆commit。

7. **范围漂移的用户意图边界。** 锁内发现较早阶段新资源立即整次rollback并提示重新预检，不持锁扩集合；若未来希望安全自动重试，必须另审幂等/锁排序/确认范围协议，不能作为B的默认隐藏行为。下游后继锁是已明确协议的Implementation，不把“禁止晚补早期锁”误读为禁止所有后继FOR UPDATE。

交叉结论：A的显式领域函数最简单，C保留compat facade最稳健，B的完整组合目录暂不应直接成为近期实施计划；值得共同采用的是typed deep-immutable计划、清晰事务拥有者、真实writer锁矩阵和不改变业务规则的共享读模型。
