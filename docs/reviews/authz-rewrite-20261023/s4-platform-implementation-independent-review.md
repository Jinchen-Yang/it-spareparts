# S4 平台实现独立复核

审查者为 Import/shared 作者，未修改 root 的平台实现。对象：control_access、principal、management 的新接缝、accounts/role_templates/authorization APIs、boundaries、machine_access、service快照，以及 domain_contexts 抽取。此处是本地独立复核，不是远端CI或浏览器验收。

结果：普通资料与管理员恢复的已冻结状态矩阵闭合，未发现该范围新增阻断。8个独立真实HTTP/PG/PDP反例与root作者平台/机器/抽取测试原样组合 **58 passed**，日志 `/tmp/itsp-s4-root-independent-first.log`。

持久独立测试为 `backend/tests/test_authz_s4_platform_independent.py`：

- 全局canonical管理而目录/资料两资源never-cutover，旧有效资格仍可用，但旧布尔写DTO整体422；仅启用directory后admin无Grant403，独立never-cutover的profile仍可用，finder不借目录权。
- 将四类boundary ID（含空actions）通过真实 modules writer 注入，整体422，零Grant、epoch及audit变化。
- 三种损坏来源形状，finder与普通目录均保留真实目标，summary=null/status=invalid，模块详情409；不猜空授权、不带last_login或旧权限blob。

此文件是本审查者的 8 例；Astra 原有的 2 个独立平台反例已恢复到唯一文件 `backend/tests/test_authz_s4_platform_astra_independent.py`。两位审查者的证据分别保留，不用其中一组代替另一组。

原样执行的作者测试文件：test_authz_s4_platform.py、test_authz_s4_machine_boundaries.py、test_authz_s4_domain_contexts.py。作者证据不冒充独立新向量。静态同时确认：普通profile预检在独立RR结束后才取排序用户锁，最终同锁重验；目录不附送活动，activity SQL不物化任意日志资源/审计JSON；PDP故障/me503不由admin绕过，named-admin控制API独立可用；机器credential和动作闭集在业务SQL前检查。

公共 domain_contexts 的跨entry/branch错误resolver反例由Astra发现、root另修；上述58项是在该修复前的冻结版本运行，不能据此宣称该毒针已闭合。应引用其独立红绿及最终整合回归。完整398入口闭合也只是路由漂移证据，不能取代这些行为检查。

PDP整体故障时前端仍可能无法进入；本轮未增加故障专用UI。精确提交全量CI、前端真实消费、统一浏览器验收和生产授权均不在本结论内。
