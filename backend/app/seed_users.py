"""用户初始化（幂等）。部署/测试用：

    uv run python -m app.seed_users

默认建：admin（管理员）、boss（老板，看全量）。销售账号在 _SALES 里维护，
username 与 salesperson_name 对齐真实销售数据（防恶性竞争靠这个名字做行级匿名化）。
口令：admin/boss 用 .env 的 ADMIN_PASSWORD；销售默认 'sales123'（首次登录应改）。
真实上线请按企业花名册增删 _SALES，并改默认口令。
"""
from sqlalchemy import select

from app.auth import hash_password
from app.config import get_settings
from app.db import SessionLocal
from app.models.system import SysUser

# (username, role, display_name, salesperson_name) —— 销售示例账号，按真实花名册维护
_SALES = [
    ("sales_liu", "sales", "刘青青", "刘青青"),
    ("sales_cui", "sales", "崔丽娜", "崔丽娜"),
]
_SALES_DEFAULT_PW = "sales123"

# 维保负责人示例（2026-08-21 客户反馈）：整套维保页面 + 行级收敛（负责人∪销售），
# 成本金额默认不可见。salesperson_name 对齐需求单「销售人员」列——行级隔离靠它。
_MAINTENANCE_MANAGERS = [
    ("maint_wang", "王维保", "王维保"),
]
_MAINT_DEFAULT_PW = "maint123"


def _upsert(db, username, role, display_name, salesperson_name, password):
    u = db.scalar(select(SysUser).where(SysUser.username == username))
    if u is None:
        db.add(SysUser(username=username, role=role, display_name=display_name,
                       salesperson_name=salesperson_name, password_hash=hash_password(password)))
        return "created"
    return "exists"


def main() -> None:
    s = get_settings()
    db = SessionLocal()
    try:
        out = []
        out.append(("admin", _upsert(db, "admin", "admin", "管理员", None, s.admin_password)))
        out.append(("boss", _upsert(db, "boss", "boss", "老板", None, s.admin_password)))
        for username, role, dn, sp in _SALES:
            out.append((username, _upsert(db, username, role, dn, sp, _SALES_DEFAULT_PW)))
        for username, dn, sp in _MAINTENANCE_MANAGERS:
            out.append((username, _upsert(
                db, username, "maintenance_manager", dn, sp, _MAINT_DEFAULT_PW)))
        db.commit()
        for name, st in out:
            print(f"  {name}: {st}")
        print("销售默认口令:", _SALES_DEFAULT_PW, "（admin/boss 用 ADMIN_PASSWORD）")
    finally:
        db.close()


if __name__ == "__main__":
    main()
