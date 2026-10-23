# 授权引擎本机实施可行性：固定版本实测

2026-10-23；仓库基线 `7a9fd20`。本次只在 `/tmp/itsp-authz-engine-feasibility-20261023` 创建独立 Python 环境，下载官方发布物、启动临时 loopback PDP 和使用内存 SQLite；未改业务代码、依赖锁或生产。以下是实际运行结果，不是性能压测，也不代表全系统接入完成。

## 推荐给实施阶段的选择

**以本系统当前的有限 capability 目录及具名 `own / project / all` 范围为前提，优先选 PyCasbin 进程内执行，配合项目自己的受限 SQL scope 与字段投影适配层。** 原因是两种方案均需完成接口、数据字段、查询用途、文件和任务改造，PyCasbin 能减少一个常驻服务及网络故障处理面；其新增依赖体量也明显更小。

**若实施要求变成“管理员可组合复杂属性条件，并由同一策略自动生成 SQL 条件”，则 Cerbos 更合适。** 它的发布版 SDK 和 SQLAlchemy adapter 已在本机真实跑通，可以少写一套通用条件规划器。不能既选 PyCasbin 的轻量部署，又默认得到 Cerbos 的查询计划能力。此建议依据实测和本项目范围；不是声称 PyCasbin 在所有场景更优。

前一份官方示例复核建议先试 Cerbos，本次已经完成这个试验：它技术上可行；纳入用户新增的部署体量、成本与实现范围权重后，有限范围模型优先 PyCasbin。最终应只选一个权限判断内核；不要让两套引擎并行成为业务真值。

## 1. 固定版本与环境

本机 Linux x86_64；使用已安装的 Python **3.11.15**，与后端 Docker 的 Python 3.11 主次版本一致；SQLAlchemy 固定为当前 `backend/uv.lock` 的 **2.0.49**。

| 组件 | 本次实测版本 | 官方来源 / 状态 |
| --- | --- | --- |
| Cerbos PDP | 0.57.0 | [官方 release](https://github.com/cerbos/cerbos/releases/tag/v0.57.0)，下载 Linux x86_64 archive，并核对同 release 的 SHA-256 |
| Cerbos Python SDK | 0.16.1 | [PyPI](https://pypi.org/project/cerbos/0.16.1/)，HTTP client 实测 |
| Cerbos SQLAlchemy adapter | 0.4.0 | [PyPI](https://pypi.org/project/cerbos-sqlalchemy/0.4.0/)，实测的是发布 wheel，不是 GitHub main |
| PyCasbin | 1.43.0 | [PyPI](https://pypi.org/project/casbin/1.43.0/) |
| simpleeval | 1.0.8 | PyCasbin 本次解析出的依赖；[PyPI](https://pypi.org/project/simpleeval/1.0.8/) |

两套引擎均能与当前项目依赖共同解析：从 `backend/uv.lock` 生成原有版本约束，再向项目依赖加入上述固定引擎版本，`uv pip compile --python-version 3.11 --constraint ...` 成功。此项证明依赖解析可满足，不等于已用整个应用跑过回归。实测 venv 的完整版本另有冻结清单；未将解析出的新依赖写入项目。

## 2. 实测体量，明确计量边界

单位为字节。wheel / archive 均实际下载，核对 SHA-256；安装体量为运行探针后 site-packages 内普通文件的字节和，包含已存在于项目中的 SQLAlchemy 等依赖及生成文件，不是净新增镜像大小。

| 对象 | 实测大小 / 数量 |
| --- | ---: |
| Cerbos Linux x86_64 archive | 49,559,605 B（约 47.26 MiB） |
| archive 内 PDP 可执行文件 | 105,574,560 B（约 100.68 MiB） |
| archive 内额外 cerbosctl | 46,440,608 B；运行 PDP 不需要把它一同常驻部署 |
| Cerbos SDK wheel | 187,468 B |
| Cerbos SQLAlchemy adapter wheel | 13,094 B |
| Cerbos 隔离 site-packages | 63,406,956 B，33 个 distribution；其中 22 个包名不在基线 uv.lock 中 |
| Casbin wheel | 475,059 B |
| simpleeval wheel | 18,925 B |
| Casbin 两个新增包的 wheel 合计 | 493,984 B（约 482.41 KiB） |
| Casbin 隔离 site-packages，含 SQLAlchemy 及其依赖 | 21,425,149 B，5 个 distribution；相对基线新增包名只有 casbin / simpleeval |

不能把 Cerbos 的 33 个隔离环境包都算成项目新增包；也不能把上述 site-packages 差额直接当成生产镜像差额。uv 复用了缓存，本次没有测量所有传递依赖的净网络传输量。没有测量生产吞吐、延迟、CPU 或常驻内存；没有估算或宣称全系统总代码行数。

## 3. 最小调用实际结果

使用三条合成订单：Alice 金额 100、Bob 金额 200、无 owner 金额 300。四种合成身份没有使用任何真实账户、姓名或业务数据。

| 身份 | Cerbos 单条 check / SQL 允许 ID | SQL COUNT / SUM | Casbin 单条允许 ID |
| --- | --- | --- | --- |
| 仅 Alice 本人范围 | `[1] / [1]` | `1 / 100` | `[1]` |
| 全量范围 | `[1,2,3] / [1,2,3]` | `3 / 600` | `[1,2,3]` |
| 本人范围但无人员绑定 | `[] / []` | `0 / 0` | `[]` |
| 没有对应 grant | `[] / []` | `0 / 0` | `[]` |

Cerbos：PDP + HTTP SDK 的 `is_allowed`、`plan_resources`、发布版 `get_query` 与内存 SQLite 均实际调用。未定义的 write 动作被拒绝。SQL 在聚合之前应用范围；无权限计划生成 `WHERE false`。这验证的是简单 equality / all / deny，不包括真实 PostgreSQL、多表 join、复杂表达式、字段 outputs、导出或任务。

Casbin：同一角色和本人范围模型的四种身份检查全部通过；未定义动作拒绝；撤销角色后同一进程立即拒绝。未试验跨进程撤权传播，也没有把“检查每条再过滤”当成 SQL 范围实现：后者仍须由应用提供，并验证与单条检查一致。

**发现的具体接入要求：** PDP 停止后，Cerbos HTTP SDK 0.16.1 的 `is_allowed` 即使传入 `raise_on_error=False`，仍抛出 `httpx.ConnectError`；该参数不能当成网络故障兜底。应用必须把不可用异常转换为明确的拒绝或服务不可用响应，且不得退回无范围查询。此行为不会自动放行，但未经处理会影响业务请求。

临时 PDP 的 HTTP、gRPC、debug listener 均通过 `ss` 确认为 **127.0.0.1**；匿名 telemetry 已关闭，未接 Hub。探针结束后正常退出，三个端口均确认关闭。

## 4. 自写代码与服务成本比较

| 工作 | Cerbos | PyCasbin |
| --- | --- | --- |
| 权限判定内核 | 使用 PDP / SDK；需管理进程健康、版本、超时及策略分发 | 应用内调用；需按授权版本加载/失效，处理多 worker 一致性 |
| 通用策略条件转 SQL | 已有真实可用 PlanResources + adapter，但属性/关系语义需映射和验证 | 没有同等通用 adapter；若要任意条件，需自建编译器，成本高 |
| 具名业务 scope | 编写 policy 与 ORM 映射；复杂关系仍需业务适配 | 可以直接维护有限 scope 的 ORM 谓词；必须与对象检查对照测试 |
| 页面 / 操作目录及授权面板 | 均需项目实现 | 均需项目实现 |
| 字段、过滤排序、聚合、导出、文件、Agent / 任务 | 均需逐个执行点接入；Cerbos 可表达字段决定，但不会替应用执行全部约束 | 均需逐个执行点接入 |
| 部署增量 | 至少一个 PDP 进程；本轮不需要专门数据库或 Hub | 无新增常驻服务；两个新增 Python 包 |

对本系统，建议将自写部分限定在：可信 principal、模块 capability 目录、明确 scope 构造器、字段用途规则、统一执行适配器及版本失效。不要开放任意 Python / SQL 表达式给授权面板；不要重复实现一个通用策略语言。若采用 PyCasbin，应把它留在模块边界内，未来可换引擎，业务入口只使用统一授权接口。

## 5. 证据与复现入口

- [Cerbos 探针脚本](engine-feasibility/probe.py)、[结果](engine-feasibility/cerbos-probe.json)：脚本自行启动/关闭临时 PDP。复跑须在隔离目录准备对应 `binary/cerbos`，使用已冻结 Python 环境；不直接在业务进程里执行。
- [Casbin 探针脚本](engine-feasibility/casbin_probe.py)、[结果](engine-feasibility/casbin-probe.json)。
- [版本 / SHA-256 / 体量](engine-feasibility/size-and-versions.json)、[结束后端口检查](engine-feasibility/shutdown-check.json)。
- [Cerbos 实测环境冻结版本](engine-feasibility/cerbos-requirements-frozen.txt)、[Casbin 实测环境冻结版本](engine-feasibility/casbin-requirements-frozen.txt)。
- [Cerbos 与当前锁版本共同解析](engine-feasibility/cerbos-project-with-current-lock.txt)、[Casbin 与当前锁版本共同解析](engine-feasibility/casbin-project-with-current-lock.txt)。这些是可行性附件，不替代项目 uv.lock。

二进制、wheel、venv 只保留在上述 `/tmp` 隔离目录，不写入仓库。此次成功证明“能运行且简单授权路径可接通”，不能用来宣称全量端点、字段范围或生产权限已验收。
