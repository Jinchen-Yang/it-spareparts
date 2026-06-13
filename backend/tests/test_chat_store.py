"""对话持久化（平台化 P1）：归属隔离 / 自动标题 / 窗口截取 / 级联删除。"""
from app.services import chat_store


def test_create_list_and_owner_isolation(db):
    a1 = chat_store.create_session(db, "alice")
    chat_store.create_session(db, "alice", title="报价讨论")
    chat_store.create_session(db, "bob")

    alice = chat_store.list_sessions(db, "alice")
    assert len(alice) == 2
    assert {s["title"] for s in alice} == {"新对话", "报价讨论"}
    # bob 看不到 alice 的；get_owned 跨人取 → None（横向越权防线）
    assert len(chat_store.list_sessions(db, "bob")) == 1
    assert chat_store.get_owned(db, "bob", a1.id) is None
    assert chat_store.get_owned(db, "alice", a1.id) is not None


def test_first_user_message_sets_title_and_strips_file_prefix(db):
    s = chat_store.create_session(db, "u")
    chat_store.append_message(
        db, s, "user",
        "[已上传文件「询价单.xlsx」 file_id=abc123def456，sheets: S1(5行x3列)]\n\n帮我批量查价")
    assert s.title == "帮我批量查价"

    s2 = chat_store.create_session(db, "u")
    chat_store.append_message(db, s2, "user", "ST8000NM000A 报 2200 行不行？这是一条很长的提问内容")
    assert s2.title == "ST8000NM000A 报 2200 行"[:20] or len(s2.title) <= 20

    # 后续消息不再改标题
    chat_store.append_message(db, s2, "assistant", "可以")
    chat_store.append_message(db, s2, "user", "另一个问题")
    assert s2.title.startswith("ST8000NM000A")


def test_history_window_order_and_cap(db):
    s = chat_store.create_session(db, "u")
    for i in range(25):
        role = "user" if i % 2 == 0 else "assistant"
        chat_store.append_message(db, s, role, f"msg-{i}")
    hist = chat_store.history_for_llm(db, s.id)
    assert len(hist) == chat_store.HISTORY_WINDOW
    # 取最近 20 条且按时间正序：第一条是 msg-5，最后是 msg-24
    assert hist[0]["content"] == "msg-5"
    assert hist[-1]["content"] == "msg-24"
    assert all(m["role"] in ("user", "assistant") for m in hist)


def test_history_caps_overlong_content(db):
    s = chat_store.create_session(db, "u")
    chat_store.append_message(db, s, "user", "x" * 20000)
    hist = chat_store.history_for_llm(db, s.id)
    assert len(hist[0]["content"]) == chat_store.HISTORY_CHAR_CAP


def test_delete_session_removes_messages(db):
    from app.models.chat import ChatMessage
    from sqlalchemy import func, select

    s = chat_store.create_session(db, "u")
    chat_store.append_message(db, s, "user", "hi")
    chat_store.append_message(db, s, "assistant", "hello", tools=[{"name": "search_parts", "args": {}}])
    sid = s.id
    chat_store.delete_session(db, s)
    assert db.scalar(select(func.count()).where(ChatMessage.session_id == sid)) == 0
    assert chat_store.get_owned(db, "u", sid) is None


def test_save_assistant_progress_insert_then_update(db):
    """流式 checkpoint：首次插入、后续按 id 原地更新、定稿改 stopped。"""
    s = chat_store.create_session(db, "u")
    db.commit()  # checkpoint 用独立会话，需先可见

    mid = chat_store.save_assistant_progress(s.id, None, "部分内容", [], stopped=True)
    mid2 = chat_store.save_assistant_progress(
        s.id, mid, "完整内容", [{"name": "search_parts", "args": {}}], stopped=False)
    assert mid2 == mid

    db.expire_all()
    msgs = chat_store.list_messages(db, s.id)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "完整内容"
    assert msgs[0]["stopped"] is False
    assert msgs[0]["tools"][0]["name"] == "search_parts"


def test_assistant_tools_and_stopped_persisted(db):
    s = chat_store.create_session(db, "u")
    chat_store.append_message(db, s, "assistant", "部分回答",
                              tools=[{"name": "lookup_prices_bulk", "args": {"queries": ["A"]}}],
                              stopped=True)
    msgs = chat_store.list_messages(db, s.id)
    assert msgs[0]["stopped"] is True
    assert msgs[0]["tools"][0]["name"] == "lookup_prices_bulk"
