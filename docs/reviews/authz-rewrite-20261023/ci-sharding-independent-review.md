# S0 CI 分片独立对抗复审

审查日期：2026-10-23。审查者：`backend_surface_inventory`（与实现者独立）。范围：CI 编排、收集分母、四片执行证据、失败/取消/跳过、旧 artifact 与 required 汇总；不含生产操作。审查只修改本文件，独立夹具和日志放在 `/tmp`。

## 审查结论与冻结范围

**结论：最终冻结版本通过本轮独立复审，未留下阻断问题。** 初版真实 CLI 根目录漂移已复现、修复并独立回归；35 项分片/证据检查、9 个真实生命周期场景、2 种外部 artifact 根目录场景均符合预期。实际仓库新清单与原命令收集结果的 7014 个节点逐项、顺序完全相等。可以提交进入精确 SHA 的远端 CI；本地没有执行四片业务全套，不能据此宣称远端 CI 或最终用户验收完成，更不构成生产授权。

审查起点是 `HEAD 1be345dd2e6396714de692ac5765a6920d7a272b` 加实现者确认冻结的六个工作树文件，不能将 HEAD 本身误称为包含分片实现的已提交版本：

| 文件 | 初版 SHA-256 |
| --- | --- |
| `.github/workflows/ci.yml` | `341cea12bef94c3a3a455e1d3d7fba62694d1ec2c196a606111bc57a6a4755bd` |
| `scripts/pytest_shard.py` | `f36bb68dedb1b120589b6c17a69713cd95a8aaa7df982ca8fd2237e93e292e0f` |
| `scripts/pytest_shard_plugin.py` | `019e88dd8c2588ebf3f7fdee4c37a1b8673c0960b80de67206d935278800f426` |
| `scripts/pytest_shard_skips.json` | `27efd1ba4eb25756df9b02464f0fd270e381ea4307914584f9141795f83e8ffd` |
| `backend/tests/test_ci_pytest_shards.py` | `02152072c48ab808a9399cc9be9bf74ecc9fb4bc6bb7587876f4b22cabd830b1` |
| `backend/tests/test_maintenance_collection_evidence.py` | `e81e3a1f816738136592714d8dee6bca0d850c3cfc9f2901108e8027c40933b7` |

修复后实现者重新冻结，本审查者在最终测试前后均重新计算并核对以下六个哈希一致；HEAD 仍为同一工作树基线：

| 文件 | 最终受审 SHA-256 |
| --- | --- |
| `.github/workflows/ci.yml` | `341cea12bef94c3a3a455e1d3d7fba62694d1ec2c196a606111bc57a6a4755bd` |
| `scripts/pytest_shard.py` | `611c799fcd4dd4dcffa9a2df0eb6da4eee1198fe8c325d975d3d6542181bf332` |
| `scripts/pytest_shard_plugin.py` | `517c0503696791d274573f835b8f758fb51879d5f2b99ee93dbb34a5cb3151c4` |
| `scripts/pytest_shard_skips.json` | `27efd1ba4eb25756df9b02464f0fd270e381ea4307914584f9141795f83e8ffd` |
| `backend/tests/test_ci_pytest_shards.py` | `5bc3ae0dd9360658e68c517ee7fee008fa96ab2b84703c85d318937dc02c255d` |
| `backend/tests/test_maintenance_collection_evidence.py` | `e81e3a1f816738136592714d8dee6bca0d850c3cfc9f2901108e8027c40933b7` |

## 已复现的问题与准确边界

### 1. 初版 CLI 的 plan/run 根目录不一致

初版 `invoke()` 没有显式固定 pytest 根目录/起始路径。独立临时 Git 仓库放置五个测试文件、每文件两个简单用例；`plan` 将清单写到 `backend` 外；`run` 读这个已存在清单并将 JUnit 写到外部目录。初版完整 CLI 的结果：

```text
plan: tests/test_a.py::test_one ...（10 条）
run:  backend/tests/test_a.py::test_one ...（10 条）
ERROR: complete collection differs from CI manifest
CI shard verification failed: shard 0: missing or invalid selected
```

四片都拒绝执行。`backend/pytest.ini` 存在也能复现；将配置放在临时仓库根部只能使独立夹具恰好稳定，不能修复实际 CLI 的隐式根目录推导。证据：`/tmp/ci-shard-independent-g60pbz3u/good/manifest.json` 与 `reports/shard-1.json`。这是分片新增的路径参数带来的可用性问题，表现为安全失败，不是漏跑后绿灯。

已通知实现者，并要求以真实 CLI 形状（外部 manifest、外部 JUnit，而非只直接调用 plugin）回归。最终修复显式指定 `--rootdir <repo>/backend .`。独立新建“无 pytest 配置”和“配置位于 backend”两种临时仓库，把 artifact 放在仓库之外；两者 plan、四片 run 和 verify 全部 exit 0，均执行 10 条。证据：`/tmp/ci-shard-independent-0vhhrny8/rootdir-results.json`。此项关闭。

### 2. 清单生成之前的筛选：继承语义，不误报为新增漏洞

在稳定根目录的独立夹具中，`test_e.py` 包含必失败毒针 `assert False`。下列五种配置/插件行为使初版 plan 收集到 8/10 条，四片和最终 verify 均成功：

| 筛选方式 | 初版分片结果 | 原 `python -m pytest -q` 对照 |
| --- | --- | --- |
| `collect_ignore = ['test_e.py']` | 8 条全部通过 | 同样 `8 passed` |
| `pytest_ignore_collect` 忽略该文件 | 8 条全部通过 | 同样 `8 passed` |
| `tryfirst` hook 静默删除 `items` 中该文件 | 8 条全部通过 | 同样 `8 passed` |
| `python_files = test_[abcd].py` | 8 条全部通过 | 同样 `8 passed` |
| ini `addopts` 附带四个测试文件位置参数 | 8 条全部通过 | 同样 `8 passed` |

因此不能称为分片相对于原全量命令新增的漏跑，也不能据此要求防御任意恶意 `conftest.py`。原命令等价性与“仓库任意代码都无法隐匿测试”是不同保证。初期消息将这五项称为新增 P1 过强，完成原命令对照后已更正；这里保留反例与更正，避免后续引用失真。

证据：`/tmp/ci-shard-independent-h244sihp/results.json`、同目录 `baseline-comparison.json`。实现者另选择补充默认发现配置、原始收集项和文件覆盖检查；这些属于更强的 CI 合同。最终版本独立重跑上述五条反例全部拒绝；实际仓库与原命令收集完全一致，未观察到当前仓库误拒绝。未来若有意改变 Python 发现规则，必须同步更新这份显式合同；它不保证抵抗任意恶意插件代码，也不能让文件扫描计数代替实际执行。

## 独立实际测试

独立脚本 `/tmp/ci_shard_independent_probe.py` 复制受审脚本到临时 Git 仓库，创建最小真实 pytest 文件，完整调用 `plan → run 0..3 → verify`。没有 monkeypatch 被审实现，也没有用作者构造的纯 JSON 测试作为唯一证据。第二个脚本 `/tmp/ci_shard_independent_lifecycle.py` 专门测试退出、清理和真实 skip 行为。

初版验证结果：稳定根目录后的 35 个检查中，30 项符合预期；其余五项是上节已和原命令对照的继承收集语义。最终冻结版本重新运行这 35 项全部符合预期；再运行九个真实生命周期场景全部符合预期：

| 攻击/正向场景 | 已观察结果 |
| --- | --- |
| 正常 10 条测试分四片、汇总 | `10 passed`，四片与 verify 全部 exit 0 |
| 缺片、重复片、缺执行事件，仍给 `needs=success` | verify 拒绝 |
| 上游任一 `failure/cancelled/skipped`、缺上游 key | verify 拒绝 |
| 旧 run attempt、旧 commit、报告 SHA/run key 不匹配 | verify 拒绝 |
| 重用已存在输出文件 | CLI 拒绝覆盖旧证据 |
| plan 后删除一个测试文件 | run 的完整收集对比拒绝 |
| collection 阶段模块级 skip、conftest 初始化 skip | 没有有效清单，失败 |
| fixture teardown 失败 | 失败事件和退出码阻断 |
| session cleanup hook 设置失败退出码 | 阻断 |
| 未批准的 setup/call skip | 阻断 |
| `pytest.exit(returncode=0)` 提前成功退出 | 缺执行项仍阻断 |
| 测试失败后 hook 把 `session.exitstatus` 改回 0 | failed pytest event 仍阻断 |
| 报告写出后 `atexit` 调用 `os._exit(23)` | `command_exit` 仍阻断 |
| 用例中 SIGTERM 杀死 pytest 进程，即使汇总输入 `needs=success` | 缺片/缺完整证据阻断 |
| 真实 pytest xfail 和非 strict xpass | 两者均因 `wasxfail` 阻断 |
| 临时夹具精确批准的 setup skip 与 call skip | 均通过，汇总明确列出 skipped 和 passed |
| 同一批准节点换成未批准 reason | 阻断 |

最终独立证据：`/tmp/ci-shard-independent-s3tiou7u/results.json`（35/35）、`/tmp/ci-shard-lifecycle-a6daci2s/results.json`（9/9）、`/tmp/ci-shard-independent-0vhhrny8/rootdir-results.json`（2/2）。生命周期夹具只修改自己的临时 skip policy 加一个固定节点/理由，未修改仓库七条白名单。测试期间没有修改受审实现。

## 工作流与既有验证的保留情况

静态比对旧 HEAD 的 `.github/workflows/ci.yml` 与候选版本：

| 原验证/约束 | 候选所在位置与判定 |
| --- | --- |
| 原 required 名称 | `后端测试（pytest + 迁移链验证）` 保持唯一；新的 `backend` 只做严格汇总 |
| 上游失败与取消 | `backend` 保持 `needs: [backend_prepare, backend_shards]` 和 `if: always()`；`verify_needs` 精确要求两个成功结果 |
| 四片完整性 | 固定 matrix `[0,1,2,3]`、`fail-fast: false`；四个不同索引报告、选中项/执行项/阶段顺序全部核对 |
| 数据库隔离 | 每个 job 独立 PostgreSQL service；每片是单进程 pytest，未引入 xdist；原 conftest 私有测试库/原文件目录创建和清理不变 |
| Alembic 迁移与 ORM 漂移 | prepare 继续 `alembic upgrade head`、`alembic check`、单头检查；命令改成 `uv run --frozen` |
| 真实 PDP | prepare 和每片安装相同钉版本、校验 SHA-256 的 Cerbos；缺 binary 时 fixture 会 skip，但新白名单不包含任何 authz 节点，不能因此绿灯 |
| 后端生产依赖漏洞 | prepare 保留 frozen export → pip-audit；审计之后才上传清单 |
| 前端构建/测试/生产依赖漏洞 | 原 frontend job 保持 build、test、audit:prod |
| 证据身份与重跑 | manifest/report 绑定 run ID、attempt、SHA；下载名也绑定 attempt；失败后需 Re-run all jobs，不能拼接不同 attempt 的成功片 |
| 失败吞掉 | 未新增 `continue-on-error` 或 `|| true`；缺 artifact 上传/下载失败不会变成成功 |

汇总解析和实际执行是双重证据。只提供成功的 `needs` 不能掩盖缺报告/缺事件；只提供全部成功的报告也不能掩盖失败的上游状态。显式白名单的七个节点与 reason 都在 manifest checksum 中；这不是对任意 skip 的豁免。

独立重读初版作者已有真实全仓库收集结果（不是执行结果）：

- `/tmp/itsp-ci-shard-manifest-stable.json` 有 7002 个唯一 node ID，四片为 1751/1751/1750/1750。
- 四个 `stable-collect-*.json` 的全量 `collected` 与 manifest 完全一致，`selected` 的 Counter 并集等于原集合且无重复。
- `backend/tests` 下 388 个实体 `test_*.py` 全部在清单中，未发现现行配置排除文件。
- 清单中有 authz engine 19 条、sales integration 17 条、service 23 条；原测试库隔离 85 条；七条 skip 白名单节点均被收集。
- 既有 `test_maintenance_collection_evidence.py` 与 HEAD 做 AST 比对，去除新增 `parametrize(ids=...)` 后 AST 完全相同，确认只固定六个参数 ID，没有改输入、断言或覆盖场景。

最终冻结版本另由本审查者独立执行实际仓库的 `plan` 和原命令收集：

```bash
# cwd: backend；本地测试基库 32780，conftest 自动隔离并清理子库。
DATABASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32780/spareparts_test \
CERBOS_BINARY=/tmp/itsp-authz-engine-feasibility-20261023/binary/cerbos \
.venv/bin/python ../scripts/pytest_shard.py plan \
  --output /tmp/ci-shard-independent-final-manifest.json \
  --run-key independent-review:1:1be345dd2e6396714de692ac5765a6920d7a272b

# 同样的两个环境变量；不加载分片插件。
.venv/bin/python -m pytest --collect-only -q
```

两命令均 exit 0。原命令 stdout 的 7014 个节点与最终 manifest 的 `nodeids` 列表完全相等；不存在只在某一边出现的节点，顺序也相同。四片为 1754/1754/1753/1753，388 个实体测试文件全部在集合中。与实现者最终四个 `rootfixed-collect-*.json` 再交叉核对：每片完整收集列表等于独立清单，selected Counter 并集恰好是该清单。7014 相对初版 7002 的差额是新增分片回归用例，不能拿两个版本总数不同推断漏跑。

最终实际仓库日志：`/tmp/ci-shard-independent-final-plan.log`、`/tmp/ci-shard-independent-original-collect.log`；清单：`/tmp/ci-shard-independent-final-manifest.json`。这些只证明完整收集与确定性划分，没有声称 7014 条全部执行通过。

本轮未在本地执行四片业务全套，未把 collect-only、作者局部测试、独立最小夹具当作全套执行成功。最终提交精确 SHA 的远端 CI、根代理第三角架构/业务复核、全模块完成后的用户统一验收仍是后续条件。没有部署、没有生产写入。
