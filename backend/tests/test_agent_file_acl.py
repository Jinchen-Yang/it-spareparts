"""Agent 文件归属/越权（PR-C）：上传与生成文件归创建者，他人不可读/下。"""
from app import permissions, security
from app.agent import tools
from app.auth import hash_password
from app.models.system import SysUser
from app.services import agent_files


def _ctx(user_id, role="sales"):
    return security.UserContext(
        user_id=user_id,
        role=role,
        salesperson_name=user_id,
        permissions=permissions.effective(role, None),
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
        token_version=0,
    )


def _owner(db, user_id, role="sales"):
    user = db.query(SysUser).filter_by(username=user_id).one_or_none()
    if user is None:
        db.add(SysUser(
            username=user_id,
            role=role,
            salesperson_name=user_id,
            permissions=permissions.effective(role, None),
            password_hash=hash_password("pw123456"),
        ))
        db.commit()
    return agent_files.verified_artifact_owner(db, _ctx(user_id, role))


def test_upload_records_owner_and_owns_logic(db):
    up = agent_files.save_upload(b"hello", "a.txt", _owner(db, "alice"))
    fid = up["file_id"]
    assert agent_files._owner_of_unchecked(fid) == "alice"
    assert agent_files.access_allowed(fid, _ctx("alice")) is True
    assert agent_files.access_allowed(fid, _ctx("bob")) is False
    assert agent_files.access_allowed(fid, _ctx("bob", role="admin")) is False


def test_read_tools_block_other_users_file(db):
    fid = agent_files.save_upload(b"secret quote", "q.txt", _owner(db, "alice"))["file_id"]
    _owner(db, "bob")
    # 他人通过读工具访问 → 拒
    assert tools._read_document(db, {"file_id": fid}, _ctx("bob")) == tools._NO_ACCESS
    assert tools._inspect_file(db, {"file_id": fid}, _ctx("bob")) == tools._NO_ACCESS
    assert tools._read_file_rows(db, {"file_id": fid}, _ctx("bob")) == tools._NO_ACCESS
    assert tools._write_excel(db, {"base_file_id": fid}, _ctx("bob")) == tools._NO_ACCESS
    # 本人读自己的 txt 正常（非越权拒绝）
    assert tools._read_document(db, {"file_id": fid}, _ctx("alice")) != tools._NO_ACCESS


def test_readonly_cannot_read_others_file(db):
    """收紧（2026-06-15）：readonly 不再是文件全量角色——共享口令回退把非 admin 一律发成
    readonly，若放行会让任何知道 ADMIN_PASSWORD 的人凭 file_id 读他人上传的报价/合同(IDOR)。"""
    fid = agent_files.save_upload(b"secret quote", "q.txt", _owner(db, "alice"))["file_id"]
    _owner(db, "bob", "readonly")
    assert agent_files.access_allowed(fid, _ctx("bob", role="readonly")) is False
    assert tools._read_document(db, {"file_id": fid}, _ctx("bob", role="readonly")) == tools._NO_ACCESS
    # admin / boss 也不能通过普通端点跨 owner；取证需未来独立 break-glass。
    assert agent_files.access_allowed(fid, _ctx("bob", role="admin")) is False
    assert agent_files.access_allowed(fid, _ctx("bob", role="boss")) is False


def test_generated_report_owned_by_creator(db):
    _owner(db, "carol")
    out = tools._write_report(db, {"headers": ["型号"], "rows": [["PN-A"]], "title": "报价"},
                              _ctx("carol"))
    fid = out["file_id"]
    assert agent_files._owner_of_unchecked(fid) == "carol"
    assert agent_files.access_allowed(fid, _ctx("carol")) is True
    assert agent_files.access_allowed(fid, _ctx("dave")) is False
