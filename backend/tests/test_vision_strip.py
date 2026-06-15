"""视觉 OCR 去思考块：推理型视觉模型(minimax-m3)正文前的 <think>…</think> 要剥掉。"""
from app.agent.provider import _strip_think


def test_removes_think_block():
    assert _strip_think("<think>reasoning</think>\nABC123 OCR") == "ABC123 OCR"


def test_multiline_and_case_insensitive():
    assert _strip_think("<THINK>\nmulti\nline\n</THINK>  实际内容\n第二行") == "实际内容\n第二行"


def test_noop_when_no_think():
    assert _strip_think("纯文本无思考块") == "纯文本无思考块"


def test_keeps_body_angle_brackets():
    # 只去 think 块，正文里的 < 不受影响
    assert _strip_think("<think>x</think>结果: a < b") == "结果: a < b"
