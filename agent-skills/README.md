# it-spareparts Agent Skills

IT 备件智能管理系统（[Jinchen-Yang/it-spareparts](https://github.com/Jinchen-Yang/it-spareparts)）AI 智能体的**技能库**。
把主仓库 `backend/app/agent/prompts.py` 里的"场景手册"拆成按业务场景独立维护的 skill，
覆盖销售、采购、库存、经营分析、主数据治理、数据导入等公司业务全流程。

## 这个仓库是什么

主系统的智能体（DeepSeek/OpenAI 兼容模型 + 9 个数据库/文件工具）目前用一个整体 system prompt
描述所有场景。随着业务场景增多，单文件提示词会越来越难维护、难评审、难按角色裁剪。
本仓库把每个业务场景写成一个标准格式的 skill：

- **格式**：每个 skill 一个目录，目录内 `SKILL.md`（YAML frontmatter `name`/`description` + 正文），
  与 [Anthropic Agent Skills](https://docs.claude.com/en/docs/agents-and-tools/agent-skills) 规范一致，
  Claude Code / Agent SDK 可直接加载，也可由主系统 runtime 按意图注入。
- **内容**：触发条件、工具调用流程、数据口径、红线、边界情况与变通规则、正反示例。
- **依据**：全部来自主仓库已实现的工具层（`agent/tools.py`）、服务层口径
  （`part_overview` / `profit` / `merge` / `governance`）与《架构与交付报告》《PN主数据整改方案-定稿》。

## 目录

| Skill | 场景 | 主要使用者 |
|---|---|---|
| [skills/sales-quote](skills/sales-quote/SKILL.md) | 销售报价（"客户问 XX 报 2200 行不行？"） | 销售 |
| [skills/procurement-pricing](skills/procurement-pricing/SKILL.md) | 采购压价与进货决策（"要进 50 个 XX，目标价多少？"） | 采购 |
| [skills/inquiry-batch](skills/inquiry-batch/SKILL.md) | 询价单/清单批量处理（Excel 上传或粘贴一串型号） | 销售/采购 |
| [skills/bom-teardown](skills/bom-teardown/SKILL.md) | 整机/服务器配置拆解报价（最高价值场景） | 销售 |
| [skills/part-identify](skills/part-identify/SKILL.md) | 型号识别、消歧与新人解释（基础 skill，被其余引用） | 全员/新员工 |
| [skills/inventory-analysis](skills/inventory-analysis/SKILL.md) | 库存查询、周转与滞销分析 | 采购/管理层 |
| [skills/profit-analysis](skills/profit-analysis/SKILL.md) | 经营分析（型号/销售员/客户利润排名） | 管理层 |
| [skills/master-data-governance](skills/master-data-governance/SKILL.md) | PN 主数据治理（合并/回滚/别名/质量问题） | 管理员 |
| [skills/data-import](skills/data-import/SKILL.md) | 氚云数据导入与清洗答疑 | 管理员 |
| [skills/security-compliance](skills/security-compliance/SKILL.md) | 权限、防恶性竞争与数据红线（常驻 skill，任何场景生效） | 系统级 |

共享参考（多个 skill 引用，避免口径重复漂移）：

- [references/tools.md](references/tools.md) — 9 个智能体工具的使用手册（参数、返回、限制）
- [references/data-dictionary.md](references/data-dictionary.md) — 价格/成本/库存口径与术语字典

## Skill 编写约定

1. **frontmatter**：`name` 用 kebab-case；`description` 一句话说清"什么时候用我"——
   这是意图路由的依据，必须包含用户原话里会出现的关键词。
2. **结论先行、流程编号**：正文按 触发条件 → 流程 → 口径 → 红线 → 边界与变通 → 示例 组织。
3. **口径只写引用不写复制**：涉及 ref_sale_price、成本法等口径的，引用
   `references/data-dictionary.md`，数值参数（如 30 天窗口）以主仓库 `config.py` 为准。
4. **红线分级**：标 🔴 的条目是不可违反的硬规则（数据诚实/权限/成本泄露），
   评审时任何弱化 🔴 条目的改动需要管理员批准。
5. skill 之间用相对路径互相引用；公共流程（如型号消歧）只在 `part-identify` 维护一份。

## 与主系统的对接方式

当前主系统是单 system prompt（`agent/prompts.py::system_prompt()`）。两条接入路径：

1. **近期（无代码改动）**：把本仓库作为 prompts.py 的"源"，场景手册章节由对应 SKILL.md
   正文生成/同步，评审在本仓库做，主仓库只收编译产物。
2. **目标态（skill 路由）**：runtime 首轮先用轻量意图分类（或让模型自选 skill），
   只把命中的 1-2 个 SKILL.md + `security-compliance` + 两个 references 注入上下文——
   降低 token 成本，也让"销售看不到治理/导入类管理员流程"成为天然隔离。

无论哪条路径，`security-compliance` 与 `references/data-dictionary.md` 都必须无条件注入。

## 拆为独立仓库

本目录自包含、无外部相对引用，从主仓库拆出即可独立使用：

```bash
git subtree split -P agent-skills -b agent-skills-standalone
# 然后推到新建的空仓库
git push <new-repo-url> agent-skills-standalone:main
```
