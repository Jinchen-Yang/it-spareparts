"""Agent 文件归属/越权（PR-C）：上传与生成文件归创建者，他人不可读/下，admin 例外。"""
from app import security
from app.agent import tools
from app.services import agent_files


def _ctx(user_id, role="sales"):
    return security.UserContext(
        user_id=user_id,
        role=role,
        is_authenticated=True,
        authn="sys_user",
    )


def test_upload_records_owner_and_owns_logic(db):
    up = agent_files.save_upload(b"hello", "a.txt", "alice")
    fid = up["file_id"]
    assert agent_files.owner_of(fid) == "alice"
    assert tools._owns(_ctx("alice"), fid) is True            # 本人
    assert tools._owns(_ctx("bob"), fid) is False             # 他人
    assert tools._owns(_ctx("bob", role="admin"), fid) is False  # admin 也只能本人
    assert tools._owns(_ctx("bob"), None) is True             # 无 file_id 放行


def test_read_tools_block_other_users_file(db):
    fid = agent_files.save_upload(b"secret quote", "q.txt", "alice")["file_id"]
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
    fid = agent_files.save_upload(b"secret quote", "q.txt", "alice")["file_id"]
    assert tools._owns(_ctx("bob", role="readonly"), fid) is False        # readonly 也需本人
    assert tools._read_document(db, {"file_id": fid}, _ctx("bob", role="readonly")) == tools._NO_ACCESS
    # 所有普通角色统一 owner-only，避免角色特权形成 IDOR 旁路。
    assert tools._owns(_ctx("bob", role="admin"), fid) is False
    assert tools._owns(_ctx("bob", role="boss"), fid) is False


def test_unstable_subject_never_owns_named_file(db):
    fid = agent_files.save_upload(b"secret quote", "q.txt", "alice")["file_id"]
    shared = security.UserContext(
        user_id="alice",
        role="admin",
        is_authenticated=True,
        authn="shared",
    )

    assert tools._owns(shared, fid) is False


def test_generated_report_owned_by_creator(db):
    out = tools._write_report(db, {"headers": ["型号"], "rows": [["PN-A"]], "title": "报价"},
                              _ctx("carol"))
    fid = out["file_id"]
    assert agent_files.owner_of(fid) == "carol"      # 生成文件归创建者 → 本人能下
    assert tools._owns(_ctx("carol"), fid) is True
    assert tools._owns(_ctx("dave"), fid) is False   # 他人不可下
