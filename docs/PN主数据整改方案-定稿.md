# PN 主数据整改方案（定稿）

> 输入：《PN主数据模块数据库设计审核说明.md》+ 用户整改建议草案
> 过程：代码全量通读 + 活库数据核实 + 四视角对抗性评审（DBA/财务完整性/MVP/运维）→ 修订 → 实施
> 状态：已实施并通过 30 项回归测试

## 一、对原整改建议的评估结论

原建议方向正确（保留 dim_part.id 为商品主键、additive 迁移、P0 指标先行、
候选必须人工审、按 part_id 聚合），全部采纳。以下为修订项及依据：

### 1.1 原建议遗漏的关键事实（必须处理）

| 发现 | 处理 |
|---|---|
| 本地活库 alembic 版本已是 `bad10309d7ec`（来自未合并分支 thirsty-brahmagupta 的 pg_trgm + sys_user 迁移），main 缺这两个迁移文件，升级即断链 | cherry-pick 两个迁移文件 + 配套模型（与该分支字节一致，未来合并零冲突），而非整体合并 5000 行的二期/三期分支 |
| 94%（21750/23152）的型号存在恒等别名（pn_raw=pn_std），合并时"写入别名"用 INSERT 必撞唯一键 | 合并改用 UPSERT（ON CONFLICT (pn_raw) DO UPDATE） |
| `inventory.backfill_costs`（库存成本回填）按 pn_std 文本聚合，原建议的 P3 清单漏了它——合并后库存资产价值与利润 COGS 口径裂脑 | 纳入 P3，切 part_id |
| `profit.recompute` 的 is_excluded 排除集按 pn 文本判定，合并后部分行口径分裂 | 切 part_id |
| loader 的 dim_part upsert（ON CONFLICT pn_std DO UPDATE）会"复活"已合并墓碑：再导入同 pn 时改写墓碑属性、事实行指回墓碑；库存路径同样存在 | 重定向下沉到 pn→id 映射层（订单/库存两路共用），upsert 加 `WHERE status != 'merged'`，墓碑沿 merged_into 链重定向（合并时路径压缩，链长恒≤1） |
| 100 个 pn_compact 重复组约半数是垃圾碰撞（'3'/'CPU'/'15M'），且组内可能是真实不同 SKU（如 RH2288HV3 大/小盘位）；1078 个待审型号 90% 没有可用合并候选 | 垃圾 compact（<5 位或纯数字）不进合并队列；候选只取 score≥0.70；提供"批量确认独立型号"出口与按业务量排序，避免单管理员逐条点击 |
| 品牌"去尾部数字"会把「待定11111」洗成合法品牌 | 占位符黑名单，命中者 brand_id 置空并记质量问题 |

### 1.2 砍掉的过度设计（YAGNI，依据=无写入方或无消费方）

- `product_spec_keys` 字典表 → 键定义与抽取正则必须同步演进，作为常量放 `spec_extract.py`
- `product_specs.confidence`、各处 `reviewed_by`（登录体系无用户身份，恒为 'admin'，审计已有 `sys_audit_log.operated_by`）、别名的 `source_table/source_record_id`（pn_raw 全局唯一对应多条事实行，事实行自带 pn_raw 可回溯）、`normalized_value`（与 pg_trgm STORED 生成列 pn_compact 完全重复）
- `dim_part.standard_name`/`lifecycle_status`/`is_eol`/`risk_level` → 无人填、无模块消费；
  status 枚举去掉 'disabled'（停用语义已有 is_excluded，防双开关）
- `product_categories` 邻接树 → 扁平两级（实测仅 33 大类 + 154 小类）
- part_substitute 不删 a<b CHECK（删了之后 (A,B)/(B,A) 双向数据二义）→ 保留规范序，
  方向用相对枚举 both/a_to_b/b_to_a，一行表达三种方向

### 1.3 补上的真缺口（原建议低估）

- **规格查询通路**：原建议只抽规格不消费——`/parts/search` 增加
  part_type/interface/capacity 区间过滤 + 前端筛选控件，否则"按容量/接口查询"验收落空
- **审核工作流的机制设计**：候选按业务量排序、批量确认、垃圾组过滤——决定单管理员
  是 1-2 天清完还是两周死亡行军
- **发布编排**：alembic 单头 pytest 防护（thirsty 分支将来并行加迁移会被测试拦住）、
  env.py `transaction_per_migration`、部署手册升级顺序改为"备份→build→迁移→起容器"
  （新 ORM 列 + 旧顺序 = 迁移窗口内全量 500）

## 二、实施内容（按提交）

1. **P0 迁移链对齐**：cherry-pick c3a51f8d2e07（pg_trgm 生成列+GIN）/ bad10309d7ec（sys_user）
   + part_resolver（治理候选发现基础设施）+ §8.7 治理口径配置
2. **P1 表结构（additive，2 个迁移）**：
   - 新表：brands（normalized_name 唯一）、product_categories（NULLS NOT DISTINCT）、
     product_specs（EAV + numeric_value 范围查询）、product_match_candidates（部分唯一防重复待审）、
     product_merge_logs（前镜像+受影响行 id）、product_data_quality_issues（entity 定位+部分唯一+dismissed 不重开）
   - 存量表：dim_part + status/merged_into_id（自指/配对 CHECK）/brand_id/category_id/
     data_quality_score/reviewed_at + (id,pn_std) 冗余唯一；part_alias + part_id/status +
     复合外键 (part_id,pn_std)→dim_part(id,pn_std)（数据库级防"文本与身份漂移"）；
     part_substitute + direction/type/status；事实表 part_id 收紧 NOT NULL；询价补 part_id
3. **P3 查询主口径**：profit（事件流/兜底/排除集/聚合）、part_overview（含 merged 重定向+
   redirected_from）、governance、inventory.backfill_costs 全部按 part_id；
   事实行 pn_std/pn_raw 永远保留导入原文（追溯与回滚归属的前提）
4. **P2 回填（幂等，只填 NULL）**：品牌/品类字典、别名/询价 part_id、三类规格抽取、
   质量扫描（自动关已修复）、质量分；导入后自动执行
5. **P4 工作流**：merge（锁序 FOR UPDATE 防并发互合、恒等别名 UPSERT、路径压缩、
   合并后自动重算利润+库存成本）；候选 merge/reject/independent；批量确认；
   别名 approve/reject/reassign（reassign 重指既有事实行）；问题关闭；
   GET /governance/metrics 输出审核说明 §7 全部指标
6. **测试**：30 用例，含「part_id 分组 vs pn 分组逐行等价」零回归证明、
   合并后成本流归并、墓碑不复活（skip/upsert）、幂等性、审核结果不被重扫刷回
7. **前端**：治理工作台（指标/候选审核/质量问题/别名审核/刷新主数据）+ 搜索页规格筛选

## 三、口径定义（避免歧义）

- **商品身份 = dim_part.id**（业务语义上的 product_id）。pn_std 唯一约束保留，
  但事实表上的 pn_std/pn_raw 是"导入时归一痕迹"，禁止作过滤/聚合键。
- **合并语义**：事实行只 repoint part_id，不改写 pn 文本；库存行保留源 pn 行，
  part 级库存口径 = SUM（同 part 同仓可多行）。
- **回滚**：仅支持按 merge_log（前镜像+受影响行 id）人工 LIFO 回滚最近一次合并，
  不承诺任意历史自动回滚；回滚后必须重算利润+库存成本。
- **自动化边界**：任何分数的匹配都不自动合并；分数仅决定是否入队与排序。

## 四、给 thirsty-brahmagupta 分支的合并清单（PR 描述同步）

该分支（二期 AI 定价/三期 RBAC）合并本分支后必须：
1. `grep -rn "FSalesLine.pn_std ==\|FPurchaseLine.pn_std ==" backend/app` 应零命中——
   quick_pricing/_sales_velocity/agent tools 需改为 part_id + merged 重定向，
   否则合并历史会悄悄从 AI 定价答案中消失（git 文本合并不会报冲突）；
2. 跑 `tests/test_profit_part_id.py` 的等价性回归；
3. 若该分支新增 alembic 迁移，`tests/test_spec_and_misc.py::test_alembic_single_head`
   会拦住多头，需 rebase 到本分支迁移之后。

## 五、遗留事项

- 维保工单备件（maintenance_parts）无数据源，本期不建表；维保只体现为采购
  source_type=维保需求（已确认不计成本）。
- 别名 alias_type（supplier_pn/customer_pn）等氚云导出里没有的来源类型，待有数据源再加。
- dev 库 docker 容器仍按旧 compose 配置以 0.0.0.0:5432 暴露（compose 文件已改 127.0.0.1
  但容器未重建），建议 `docker compose up -d --force-recreate db`（数据在命名卷，不丢）。
