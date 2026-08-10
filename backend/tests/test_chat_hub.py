"""对话生成中枢 RunHub：缓冲回放 + 多订阅者扇出 + 取消 + 注册表（可重连直播的核心）。"""
from app.api.chat_sessions import (
    _MAX_SUBSCRIBERS_PER_RUN,
    _RunHub,
    _SUBSCRIBER_QUEUE_CAP,
    acquire_session,
    get_run,
    is_generating,
    release_session,
    request_cancel,
)


def test_hub_replays_buffer_then_streams_live():
    hub = _RunHub()
    hub.publish({"type": "delta", "text": "A"})
    hub.publish({"type": "delta", "text": "B"})
    # 晚到订阅者（切回会话）先拿到已生成部分的回放
    replay, q = hub.subscribe()
    assert [e["text"] for e in replay] == ["A", "B"]
    assert q is not None
    # 之后的实时事件进该订阅者队列
    hub.publish({"type": "delta", "text": "C"})
    assert q.get_nowait()["text"] == "C"
    hub.finish()
    assert q.get_nowait() is None          # 结束哨兵


def test_hub_fans_out_to_all_subscribers():
    hub = _RunHub()
    _, q1 = hub.subscribe()
    _, q2 = hub.subscribe()
    hub.publish({"type": "delta", "text": "X"})
    assert q1.get_nowait()["text"] == "X"
    assert q2.get_nowait()["text"] == "X"


def test_subscribe_after_finish_gets_replay_no_queue():
    hub = _RunHub()
    hub.publish({"type": "done"})
    hub.finish()
    replay, q = hub.subscribe()
    assert [e["type"] for e in replay] == ["done"]
    assert q is None                       # 已结束：回放完即收，不再等实时


def test_unsubscribe_stops_delivery():
    hub = _RunHub()
    _, q = hub.subscribe()
    hub.unsubscribe(q)
    hub.publish({"type": "delta", "text": "Z"})
    assert q.empty()


def test_registry_acquire_release_cancel():
    sid = 987654
    release_session(sid)                   # 清干净
    assert not is_generating(sid)
    hub = acquire_session(sid)
    assert hub is not None and is_generating(sid)
    assert acquire_session(sid) is None    # 同会话二次开始 → None（409 语义）
    assert get_run(sid) is hub
    assert request_cancel(sid) is True and hub.cancel.is_set()
    release_session(sid)
    assert not is_generating(sid)
    assert request_cancel(sid) is False    # 已无生成


def test_slow_subscriber_is_bounded_evicted_and_signalled_without_blocking_publish():
    hub = _RunHub()
    _, q = hub.subscribe()
    assert q is not None and q.maxsize == _SUBSCRIBER_QUEUE_CAP

    for index in range(_SUBSCRIBER_QUEUE_CAP + 1):
        hub.publish({"type": "delta", "text": str(index)})

    assert q.get_nowait() == {"type": "subscriber_evicted", "retry_attach": True}
    assert q.get_nowait() is None
    assert q not in hub._subs


def test_run_hub_caps_subscriber_fanout_and_finish_handles_full_queues():
    hub = _RunHub()
    queues = [hub.subscribe()[1] for _ in range(_MAX_SUBSCRIBERS_PER_RUN)]
    replay, denied = hub.subscribe()
    assert replay == [{"type": "subscriber_evicted", "retry_attach": True}]
    assert denied is None

    for index in range(_SUBSCRIBER_QUEUE_CAP):
        hub.publish({"type": "delta", "text": str(index)})
    hub.finish()

    for q in queues:
        assert q is not None
        assert q.get_nowait() == {"type": "subscriber_evicted", "retry_attach": True}
        assert q.get_nowait() is None
    assert hub._subs == []
