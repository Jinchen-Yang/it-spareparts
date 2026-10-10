# 生产源码基线与分线记录

核验日期：2026-10-10（Asia/Shanghai）。本次仅调整 Git 仓库与 GitHub 保护，不部署生产。

- 原 main：`6dc7e7b1b33faebdd606c295fef3d26a0d31dec9`，迁入 `test`，历史完整保留。
- 生产 Git 基线：`c32c6fa16d2f4030db17cd39c443da6a66c12051`，前端版本 `1.36.3`。
- 前端镜像：`it-spareparts-frontend:release-v1363-c32c6fa16d2f`。
- 前端镜像 ID：`sha256:49763676fd81b2d9d65156c70dfd91599f28feb96a60c705264e4cd11e262f2b`。
- 后端镜像：`it-shop-lookup-upstream:shop-lookup-20261009T030526Z`。
- 后端镜像 ID：`sha256:69c5a0a161c8ce096ca0b493dae7e5cb04f14f90768607e6cc1cb13781ac9ff8`。
- 线上后端是在生产基线上叠加的只读商城接口。将容器中的源码归档并逐字节比对，共 377 个文件一致，纳入 app、alembic、scripts、锁文件与构建描述；未复制生产数据、.env 或凭据。
- 源码归档 SHA256：`1146b8296bfaa77e5eb6944480965b00bd01be431ddc6fb59d5aa28ee559051a`。
- 相对 c32c6fa 的运行源码变化仅六个文件：`backend/app/config.py`、`backend/app/main.py`、`backend/app/api/resale_export.py`、`backend/app/api/resale_catalog_export.py`、`backend/app/api/resale_catalog_search.py`、`backend/app/services/resale_catalog_matching.py`。
- 前端源码保留 c32c6fa；本次新增的协作规范、CODEOWNERS、PR 模板和 CI 分支触发配置不改变运行代码。

这是实际运行源码的归档核对，不是本次重新通过全量 CI 或生产验收的声明。
生产容器继续运行原镜像，不把本次仓库治理提交描述成已重新部署。
以后进入 main 必须按 `docs/BRANCH_POLICY.md` 由 @Jinchen-Yang 人工审批。
