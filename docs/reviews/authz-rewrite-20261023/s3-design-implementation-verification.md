# S3 设计基线与实施清单的两层核对

冻结设计的 48 个来源 SHA 对应真实 S2 精确提交 `68e99445c144eb5df8c6c93b2d0980b94b340619`。S3 实施必然修改这些文件，不能刷新原 SHA 后继续宣称旧语义来源未变。`verify_s3_execution_spec.py --source-ref <S2 SHA>` 直接从 Git blob 校验原冻结来源及旧权限解释器；不传 source-ref 仍严格检查工作树，保留原设计冻结守卫。

本次只修实施过程中已批准但未同步的审计镜像：migration-preview operation_input、return submit/retry/cancel 真实项目 policy slots、closed roundtrip batch type、demand source-order 字符串 selector、optional customer field 的依赖形状，以及 runtime 明确 replay/unassigned/discovery 分支的审计约束。没有把 frozen source hashes 替换为当前实现散列，也没有以模板字符串声称上下文已闭合。

最终静态检查通过（`/tmp/itsp-s3-frozen-design-check-final.log`）：73 资源、232 数据/写动作、228 活跃/11 退役入口、48 个 frozen source blobs、612 个持久 legacy 输入向量、44,676 资源向量求值、106 前端 surfaces。另 `--self-test --source-ref <S2 SHA>` 的 12 个结构/来源毒针全部拒绝。该检查不证明 SQL、事务、文件生命周期或 HTTP 行为。

`verify_s3_implementation_inventory.py` 独立导入真实 app（不启动 lifespan、不连接业务 DB），将 project94 + workflow145 的唯一入口账与 actual mounted handler、实际依赖树、可解析 facade 调用和当前 registry 逐项连接，写入 `s3-implementation-inventory.json`。当前记录真实 239 个 method/path 全匹配、0 结构错误；semantic status 原样来自作者/独立复核账。`--require-complete` 遇到任何 pending 必须失败，不把 registry 数量或 AST 调用当作授权支配证明。

集中 metadata 修订与跨作者复核完成后，owner 将 AI 3 项、工作簿 4 项恢复为有证据的 verified；最终 `--require-complete` 已通过：228 live + 11 retired = 239 个真实 method/path 全匹配，pending=[]、structural_errors=[]（`/tmp/itsp-s3-mounted-inventory-metadata-final.log`）。仓库/补库 31 项、工作簿 48 项的独立证据及清单存储输入篡改红→绿见 `s3-bootstrap-metadata-cross-review.md`。此前失败日志保留，不以新结论抹去发现过程。

同一冻结工作树上，S2 `--source-ref` 校验、`generate_authz_frontend.py --check`、`generate_authz_policies.py --check` 全部通过，分别记录在 `/tmp/itsp-s3-frozen-design-metadata-final.log`、`/tmp/itsp-s3-frontend-generator-metadata-final.log`、`/tmp/itsp-s3-policy-generator-metadata-final.log`。这里仍只是来源、生成物与逐入口本地复核账闭合；不能替代精确提交全量 CI、用户验收或生产发布。
