# Project Context — IT 备件智能管理系统

> 最后更新：2026-08-10
> 维护者：云间辞 (Jinchen-Yang)
> 仓库：https://github.com/Jinchen-Yang/it-spareparts

## Project Overview

**产品名称：** IT 备件智能管理系统（IT Spareparts Intelligent Management System）

**产品目标：**
- 为 IT 运维团队提供备件全生命周期管理：采购、库存、领用、返还、报废
- 为维保项目提供合同管理、成本核算、双税口径、现场领用与项目工作台
- 二期引入 AI 助手辅助定价、询价、备件识别和维保健康检查

**用户群体：**
- 备件管理员：主数据维护、入库出库、库存盘点、型号查询
- 采购/销售：价格查询、利润分析、补库申请
- 项目经理：维保项目管控、月报工作簿、成本核验
- 管理员：账号权限、系统设置、Beta 功能放行

**核心价值：**
- 备件型号标准化（PN 归一化、替代组、品牌/规格搜索）
- 维保项目从合同到成本的完整闭环（四表：合同/回款/领用/报销）
- 成本取价链冻结证据（防篡改审计）
- AI 辅助但不自动决策（所有 AI 输出为建议，人类最终确认）

**商业目标：**
- 替代手工 Excel 台账
- 减少备件采购错误率
- 维保项目利润率可视化
- 为氚云/钉钉生态提供数据底座

## Current Status

**当前开发阶段：** 维保 Beta 集成候选 + AI 管线设计阶段

**当前基线分支：** `codex/maintenance-manager-combined` (HEAD `3dbc9dc`)，已推送 GitHub

**已完成模块（已合入 main）：**
- ✅ 备件主数据管理（型号 CRUD、搜索、导入、替代组）
- ✅ 入库/出库/库存/盘点
- ✅ 采购记录与采购分析面板
- ✅ 利润分析（双税含税/不含税）
- ✅ 互通池管理与分析
- ✅ 老板看板（晨会摘要、订单排名、池状态）
- ✅ 权限中心 v2（RBAC + 角色模板）
- ✅ 数据质量治理（校准面板、异常检测）
- ✅ AI 助手 P0-P1（Chat 对话 + 10 个工具 + 4 个 Skill Playbook）
- ✅ 维保项目主档与合同基础（#196, #198, #200）
- ✅ 发布控制系统（v1.20/v1.21 Beta deployment controls）

**已完成模块（在基线分支，未合入 main）：**
- 🔶 维保项目工作台（四表 + 提醒 + 导出）
- 🔶 项目经理任务板（账号映射 + 我的项目）
- 🔶 月报工作簿 v3（全量下载 + 受控回填）
- 🔶 验收报告闭环
- 🔶 现场领用 v2（发货约束 + 系统编号）
- 🔶 坏件返还（义务追踪 + 硬盘免返 + 双口径返还率）
- 🔶 WBDD 安全逻辑删除
- 🔶 成本/库存迁移控制（dry-run + 签名 manifest）
- 🔶 补库购物车 Beta（PN 搜索 + 审核回传）
- 🔶 来源单人工归属

**AI 管线（Draft PR，不在基线上）：**
- 🔴 Capability Kernel (#235) — 只读执行器
- 🔴 Artifact Delivery v2 (#236) — 可信文件管线
- 🔴 Query Broker (#238) — Text2SQL 只读网关
- 🔴 Durable Task (#234) — 已冻结
- 🔴 补库审核内核 (#239) — 新 SHA 缺失
- 🔴 表格清洗内核 (#228) — BLOCK（2 个 P1）

**未完成验收：**
- ❌ 真实项目/合同数据映射
- ❌ 现场收货确认闭环
- ❌ 坏件返还率正式分子
- ❌ 报销正式金额列
- ❌ 成本/库存生产切换
- ❌ 生产备份恢复演练
- ❌ 真实 canary/白名单验收

## Important Constraints

**技术限制：**
- 后端 Python 3.11+ / FastAPI / SQLAlchemy 2.0+ / PostgreSQL 15
- 前端 React 18 / TypeScript 5.6+ / Vite 7 / AntD 5
- 所有 Beta 功能必须默认关闭（`maintenance_beta_enabled=false`, `replenishment_beta_enabled=false`, `enable_agent=false`）
- 迁移链必须保持单 head（Alembic 线性历史，禁止分叉后直接合并）

**性能要求：**
- 导入预检支持 10 万行 Excel
- 搜索响应 < 500ms
- 前端构建 chunk 大小警告可接受（ECharts 等大型库）
- Docker 日志限制单文件 10MB×5（防磁盘写满）

**安全要求：**
- 生产密钥仅通过 `.env` 注入，禁止提交到 Git
- 管理员密码/数据库密码/SECRET_KEY 必须 prod 模式强随机（默认值拒绝启动）
- API 全面 token 鉴权 + RBAC 权限 + 页面准入（`require_page`）
- SQL 使用 ORM 参数化，禁止裸字符串拼接
- 文件上传：agent 文件 ACL 校验 + 大小限制
- Beta 功能：白名单 + 二次审批 + 总闸控制
- 部署：只绑 `127.0.0.1`，生产经 Caddy HTTPS 反向代理

**业务规则：**
- 价格互通池人工维护，不得演变为自动定价
- AI 助手仅辅助，不自动审批、报价或阻断交易
- 系统呈现历史事实，不判断人员动机
- 成本取价链一旦冻结不可修改（审计 append-only）
- 坏件返还率分子/分母由业务确认，技术侧不做口径裁决

## Non-Goals（当前不做的）

- ❌ 自动定价引擎
- ❌ 实时库存预警推送
- ❌ 移动端 App
- ❌ 多租户 SaaS 化
- ❌ 钉钉/氚云深度集成（只读参考）
- ❌ LangGraph / GPU 模型服务（Durable 冻结中）
- ❌ AI 自动审批或自动执行业务操作
- ❌ 替代现有 ERP/财务系统
