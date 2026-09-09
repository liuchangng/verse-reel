"""情感标签打标脚本解析守护测试（2026-09-09 问题 4b-3）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.tag_poem_emotions import _parse_reply, EMOTION_TAGS  # noqa: E402


def test_parse_reply_valid():
    raw = '{"0": ["思乡", "喜悦"], "1": ["爱国", "编造标签", 5], "2": "不是列表"}'
    out = _parse_reply(raw)
    assert out == {0: ["思乡", "喜悦"], 1: ["爱国"]}, "非词表标签/非字符串/非列表值应剔除"


def test_parse_reply_markdown_wrapped_and_garbage():
    wrapped = '```json\n{"0": ["豪迈"]}\n```'
    assert _parse_reply(wrapped) == {0: ["豪迈"]}
    assert _parse_reply("我觉得都不错") == {}
    assert _parse_reply("") == {}
    # 行号越界不剔除（tag_batch 调用侧按 i >= len(poems) 过滤），解析器只管格式
    assert _parse_reply('{"99": ["思乡"]}') == {99: ["思乡"]}


def test_emotion_taxonomy_is_closed_and_reasonable():
    """固定词表必须包含用户点名的情感维度（压抑/积极/消极/爱国/不得志→壮志难酬）。"""
    for t in ("压抑", "积极", "消极", "爱国", "壮志难酬", "思乡"):
        assert t in EMOTION_TAGS
    assert len(EMOTION_TAGS) <= 25, "词表过大降低标注一致性"
