"""sys_access_log.action 列宽契约：静态扫描全部写入点，超长名在 CI 即失败。

背景（2026-10-16 修复）：action 原为 String(32)，而全仓 29 个动作名超 32
（最长 43，maintenance_warehouse_integration_reconcile），INSERT 被 PG 拒绝、
审计行经 best-effort 吞异常静默丢失。看守三个方向：

1. **字面量**：AST 扫描 app/、scripts/ 全部 .py（天然覆盖多行调用），收集
   record_access_log / record_security_event / SysAccessLog(action=…) / auth
   `_ev(…)`/`ev(…)` 闭包转发处的字符串字面量，断言不超过模型列宽（列宽从
   模型取，将来再扩列时断言自动跟随）。
2. **动态名**：非字面量实参（f-string/变量/三元）必须逐一登记在
   _KNOWN_DYNAMIC_SITES 且站点数精确匹配——多一个少一个都失败，新增动态
   写入点或旧站点消失/改名都会被逼着显式确认。可能取值从活代码计算——
   agent 工具注册表、FollowUpAction 字面量枚举——同样受列宽约束。
3. **不可解析调用**：sink 调用解析不到 action 实参（**kwargs 转发、缺位
   置参数、位置参数直造 SysAccessLog 等）直接失败——这类形态可携带任意
   长度 action 绕过前两条守卫，必须改成显式写法。

另含写入兜底用例（无上界外部输入——登录尝试名、模型自报工具名——在
helper 层截断到列宽，事件不丢）与 best-effort 失败告警只含异常类型的用例。
"""
import ast
import logging
import typing
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.models.system import SysAccessLog

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
# 扫描生产源码；tests/ 里的调用是测试辅助，不进审计契约。
_SCAN_DIRS = ("app", "scripts")

# 落库 sink：函数名 → action 实参的位置（None 表示只认 action= 关键字）。
_ACTION_SINKS = {
    "record_access_log": 1,      # (user_ctx, action, resource, ...)
    "record_security_event": 2,  # (username, role, action, ...)
    "SysAccessLog": None,
    "_ev": 0,   # auth.py 闭包：转发第 1 参给 record_security_event
    "ev": 0,    # auth._guard_password_attempt 的回调参数（实参即上述 _ev）
}

# 纯转发包装 sink：本身不产生新取值，其字面量调用点必须保持被扫描状态。
# 值 = 期望的字面量调用点数（防包装改名后字面量悄悄脱离枚举）。
_WRAPPER_SINK_LITERALS = {"_ev": 13, "ev": 3}

# 已确认可能取值的动态写入点：(相对路径, 实参源码原文) →
# (期望站点数, 取值说明)。变更/新增动态 action 时：先穷举可能值确认不超
# 过列宽，再登记并更新站点数。
_KNOWN_DYNAMIC_SITES: dict[tuple[str, str], tuple[int, str]] = {
    ("app/security.py", "action[:64]"): (
        2,
        "两个 helper 的落库点（各一处）；[:64] 兜底截断，值域=全部调用方 ∪ agent_tool:* ∪ auth._ev/ev 转发的 login_*/change_password*",
    ),
    ("app/agent/tools.py", 'f"agent_tool:{name}"'): (
        1,
        "注册表含 search_maintenance_movements；最大长度从实际注册表计算；注册表外的模型自报名无上界，由 helper 的 action[:64] 兜底",
    ),
    ("app/api/maintenance_collection_reminders.py",
     'f"collection_milestone_{body.action}"'): (
        1,
        "FollowUpAction 枚举 handle/reschedule/reopen",
    ),
    ("app/api/maintenance_project_master_workbook.py",
     '"workbook_apply" if not force_takeover else "workbook_apply_takeover"'): (
        1,
        "三元两分支字面量",
    ),
    ("app/auth.py", "action"): (
        3,
        "三处 _ev 闭包体内转发给 record_security_event 的变量；取值=本文件 _ev(...)/ev(...) 调用处的字面量（_WRAPPER_SINK_LITERALS 看守）",
    ),
    ("app/api/part_gallery.py", "action"): (
        1,
        "图库代理 _audit 包装；值域=GalleryAuditAction 的 10 个字面量（最长 "
        "gallery_upload_part_sign=24 < 64），由 test_dynamic_actions_fit_column_width 校验",
    ),
    ("app/api/work_items.py", 'f"work_item_{action}"'): (
        1,
        "_act 包装的 7 个动作：claim/start/release/transfer/reassign/complete/decline；"
        "加 work_item_ 前缀后最长 18，由 test_dynamic_actions_fit_column_width 校验",
    ),
    ("app/services/presales/common.py", "action"): (
        1,
        "audit 包装；值域=询价 _record 的 9 个 kind、建档 _apply 的 2 个 kind、"
        "报价 common.audit 的 6 个字面量（最长 presales.offer.draft_saved=26），"
        "由 test_dynamic_actions_fit_column_width 校验",
    ),
}


def _column_width() -> int:
    return SysAccessLog.__table__.columns["action"].type.length


def _iter_sites():
    """产出 (相对路径, sink 名, 行号, 实参节点或 None, 实参源码原文)。

    AST 精确定位，多行调用不漏；action 解析不到时节点为 None（调用本身
    仍被产出，交给调用方按"不可解析"失败，绝不静默跳过）。
    """
    for dirname in _SCAN_DIRS:
        for path in sorted((Path(_BACKEND_ROOT) / dirname).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else None)
                if name not in _ACTION_SINKS:
                    continue
                pos = _ACTION_SINKS[name]
                arg = (
                    node.args[pos] if pos is not None and len(node.args) > pos
                    else next((k.value for k in node.keywords
                               if k.arg == "action"), None)
                )
                rel = str(path.relative_to(_BACKEND_ROOT))
                segment = (ast.get_source_segment(source, arg) or ast.dump(arg)
                           ) if arg is not None else "<unresolvable>"
                yield rel, name, node.lineno, arg, segment


def _collect():
    """一次扫描产出 (字面量列表, 动态键列表, 不可解析站点列表, 包装字面量计数)。"""
    literals: list[str] = []
    dynamic: list[tuple[str, str]] = []
    unresolvable: list[str] = []
    wrapper_literals: Counter = Counter()
    for rel, name, lineno, arg, segment in _iter_sites():
        if arg is None:
            # **kwargs 转发 / 关键字改名 / 位置参数直造：一律视为不可解析。
            unresolvable.append(f"{rel}:{lineno} {name}(...)")
            continue
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            literals.append(arg.value)
            if name in _WRAPPER_SINK_LITERALS:
                wrapper_literals[name] += 1
        else:
            dynamic.append((rel, segment))
            # 三元等复合节点内嵌的字面量（如 workbook_apply 两分支）也算可能取值；
            # f-string 前缀片段（如 "agent_tool:"）不是完整取值，不算。
            if not isinstance(arg, ast.JoinedStr):
                literals.extend(
                    sub.value for sub in ast.walk(arg)
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                )
    return literals, dynamic, unresolvable, wrapper_literals


def _dynamic_values() -> list[str]:
    """动态写入点的可能取值：活代码计算，登记表外的动态站点直接失败。"""
    values: list[str] = []
    # agent 工具名：注册表现值 + 注册表外名字由 helper 截断兜底（另有用例）。
    from app.agent.tools import _REGISTRY
    values.extend(f"agent_tool:{key}" for key in _REGISTRY)
    # 回款提醒里程碑动作：Literal 枚举现值。
    from app.schemas.maintenance_collection_reminders import FollowUpAction
    values.extend(
        f"collection_milestone_{choice}"
        for choice in typing.get_args(FollowUpAction)
    )
    # 图库代理审计动作：Literal 枚举现值（_audit 包装的动态写入点）。
    from app.api.part_gallery import GalleryAuditAction
    values.extend(typing.get_args(GalleryAuditAction))
    # 待办和售前包装的字面量调用点；沿转发链取活代码，新增动作同样受列宽约束。
    for rel, callee, pos, prefix in (
        ("app/api/work_items.py", "_act", 3, "work_item_"),
        ("app/services/presales/inquiries.py", "_record", 3, ""),
        ("app/services/presales/binding.py", "_apply", 6, ""),
        ("app/services/presales/offers.py", "common.audit", 2, ""),
    ):
        tree = ast.parse((_BACKEND_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or ast.unparse(node.func) != callee:
                continue
            assert len(node.args) > pos, f"{rel}:{node.lineno} {callee} 动作实参须显式穷举"
            arg = node.args[pos]
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                f"{rel}:{node.lineno} {callee} 动态动作须同步穷举可能取值")
            values.append(prefix + arg.value)
    return values


def test_no_unresolvable_action_sites():
    """解析不到 action 的 sink 调用直接失败：**kwargs 转发等形态可绕过其余守卫。"""
    _, _, unresolvable, _ = _collect()
    assert not unresolvable, (
        "以下 sys_access_log 写入调用无法静态解析 action 实参（**kwargs 转发/"
        "位置参数直造等），改成显式字面量或 action= 关键字，或经登记的包装函数：\n"
        + "\n".join(unresolvable))


def test_literal_actions_fit_column_width():
    literals, _, _, _ = _collect()
    assert literals, "静态扫描应找到 action 写入点，扫描器本身失效"
    width = _column_width()
    too_long = sorted(
        {name for name in literals if len(name) > width}, key=len, reverse=True)
    assert not too_long, (
        f"action 名超过 sys_access_log.action 列宽 String({width})："
        f"{too_long}；要么改名，要么扩列并同步迁移/模型/本断言")


def test_dynamic_sites_registered_with_exact_counts():
    _, dynamic, _, _ = _collect()
    found = Counter(dynamic)
    expected = Counter({key: count for key, (count, _note) in
                        _KNOWN_DYNAMIC_SITES.items()})
    assert found == expected, (
        f"动态 action 站点与登记表不一致：\n实际={dict(found)}\n"
        f"登记={dict(expected)}\n新增站点先穷举可能取值确认不超列宽再登记；"
        "消失/改名的站点同步更新登记表")


def test_wrapper_sink_literals_still_scanned():
    """包装函数改名会让其调用点字面量脱离枚举：按精确计数看守活性。"""
    _, _, _, wrapper_literals = _collect()
    assert dict(wrapper_literals) == _WRAPPER_SINK_LITERALS, (
        f"包装 sink 字面量调用点数变化：实际={dict(wrapper_literals)} "
        f"期望={_WRAPPER_SINK_LITERALS}；若是包装改名/新增，请同步 "
        "_ACTION_SINKS 与 _WRAPPER_SINK_LITERALS，确认新包装的字面量仍被扫描")


def test_dynamic_actions_fit_column_width():
    values = _dynamic_values()
    assert values, "动态取值计算应非空（注册表/枚举至少各一项）"
    width = _column_width()
    too_long = sorted(
        {name for name in values if len(name) > width}, key=len, reverse=True)
    assert not too_long, f"动态 action 取值超过列宽 String({width})：{too_long}"


def test_helpers_truncate_unbounded_inputs_to_column_width(db):
    """username（登录尝试名，请求体无上限）与 action（agent 自报工具名）兜底截断。

    截断保留审计事件本身——比 422 拒绝（丢事件）或落库失败（丢行）更利于排查。
    """
    from app import security

    security.record_security_event(
        username="u" * 100, role="readonly", action="a" * 100,
        resource="r" * 600, ip="9.9.9.9", user_agent="ua-" + "x" * 400)
    security.record_access_log(
        security.UserContext(user_id="f" * 100, role="readonly"),
        "b" * 100, "res")

    rows = db.execute(
        select(SysAccessLog).order_by(SysAccessLog.id)).scalars().all()
    assert [r.action for r in rows] == ["a" * 64, "b" * 64]
    assert [r.username for r in rows] == ["u" * 64, "f" * 64]
    assert rows[0].resource == "r" * 500
    assert rows[0].ip_address == "9.9.9.9"
    assert rows[0].user_agent == "ua-" + "x" * 297


def test_best_effort_failure_warning_carries_exception_type_only(monkeypatch):
    """落库失败只报异常类型：消息/SQL 参数可能含敏感值，不得进日志。

    直接在 "access" logger 上挂临时捕获：不依赖 root 传播/其他用例的日志
    配置状态（前序 TestClient 用例会扰动传播，caplog 形态曾因此漏捕）。
    """
    import app.db as db_mod
    from app import security

    def _boom():
        raise RuntimeError("secret-ish message with password=123")

    monkeypatch.setattr(db_mod, "SessionLocal", _boom)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("access")
    old_level = logger.level
    old_disabled = logger.disabled
    # 测试进程内一旦走过 alembic（conftest 迁移/列槽位重建），env.py 的
    # fileConfig 会把已存在的本 logger 置 disabled=True——本用例须自洽，
    # 不受进程内迁移副作用影响。
    logger.setLevel(logging.WARNING)
    logger.disabled = False
    logger.addHandler(handler)
    try:
        security.record_access_log(
            security.UserContext(user_id="u", role="admin"), "search", "x")
        security.record_security_event("u", None, "login_failed")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.disabled = old_disabled
    warnings = [r for r in records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2
    for record in warnings:
        assert "RuntimeError" in record.getMessage()
        assert "secret-ish" not in record.getMessage()
