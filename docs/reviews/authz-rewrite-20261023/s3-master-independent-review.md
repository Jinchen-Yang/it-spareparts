# S3 Master 工作簿独立对抗审查

审查对象：`maintenance_master_apply_access.py` 与项目 Master V1/V2 受控 hooks。这是另一作者的交叉审查，未把作者自身测试当作独立证据。

发现并闭合的反例：历史 apply receipt 对实际来源单仍属原完整 parent closure 做了检查，但其授权 revision 没有包含当前 assignment generation。来源单在历史闭包内部重挂时，旧授权视图可能仍有效。作者现将 `source_id/assignment_id/version/project_id` 纳入授权 revision；不改变按批准业务规则保持不可变的历史 receipt 内容 revision。

独立真实 PostgreSQL/PDP 验证在 `backend/tests/test_authz_s3_master_independent.py`：

- 完成真实来源重挂及 Master apply，保存 receipt view；随后归档当前 assignment 并新建另一 generation，项目版本不变且目标仍在原历史闭包内。旧 precondition 返回 409；重新解析后的 view 和 replay 成功，仍返回同一 import batch。
- 真实 note 修改、artifact bytes 和 sidecar 发布后、事务提交前注入故障。项目备注、Operation、Audit、Artifact 计数全部回滚。

执行证据：`/tmp/itsp-s3-master-independent-final.log`，**2 passed**。另检查完整替代资源合同、全部 old/new parent subjects、domain→actor 锁顺序和稳定 actor ID；指出 V1 项目锁需 `populate_existing`，由该模块作者同步处理。

此结论只覆盖上述实现与两个独立运行向量。S3 其他入口和全量 CI 尚需各自闭合，不构成生产或阶段完成声明。
