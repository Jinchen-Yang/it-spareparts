# S3 全局成本文件与发布边界独立复核

结论：本次独立范围未发现尚未关闭的授权阻断。此结论只覆盖本地已审代码与下列实际测试，不代替 S3 全量 CI、用户验收或生产授权。

复核 `maintenance_global_workbook_authorization.py`：来源订单导出使用真实 PDP SQL 谓词与本资源字段；合同成本工作簿保留完整 all_sources 合同，固定往返模板独立 all_contracts + C/F/K；在同一 RR READ ONLY 中完成全部 spool，释放 DB 后使用当前读者 provenance 发布。重算保留原全局业务命令，完整来源/项目/领用闭包先锁定，暂存业务效果后才取 actor SHARE，当前 Grant/PDP 失败全回滚。

独立新增 `test_authz_s3_global_independent.py` 两项：实际 workbook 已物化后，用第二连接改变来源成交人关系或撤去当前 Grant 字段并推进 version；发布均 403，实际 spool 关闭。作者 8 项与独立 2 项及 collection-plan 11 项组合 21 passed：`/tmp/itsp-s3-shared-download-final.log`。最终文件头兼容调整后，global 10 项又随 return-import 13 项通过，`/tmp/itsp-s3-return-global-independent-final.log`，共 23 passed。这些集合有重叠，不合计成唯一测试数。

静态发现 protected service 反向 import API 私有响应封装。按 root 授权已移至 `authorization_downloads.stream_materialized_file`，`stream_materialized_bytes` 使用同一底层，保留 64 KiB 分块、异常/断开关闭、发送前及逐块当前读者重验；全局 facade 不再 import API。既有 legacy API 辅助函数未作无关重构。

collection-plan 发布独立扩展还证明：初始 RR 成功后删除真实不可变 sidecar 必须阻止原件返回。先红（`/tmp/itsp-s3-collection-plan-binding-release-red.log`），reference 重建时增加实际 sidecar 检查后通过。root 独立 epoch 红例原样保留；这两项针对不同失效来源，不能仅以 owner 或此前 allow 替代。
