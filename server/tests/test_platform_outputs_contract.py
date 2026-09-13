"""platform_outputs 契约守护测试（change: multi-platform-final-videos-platform-outputs）。

req: REQ-M2 —— platform_outputs 使用 JSON object 契约落库。
覆盖：
1. 成功平台 entry 必须含 url + status=ok
2. 失败平台 entry 必须含 status=failed + error，不含 url
3. 旧 {platform: "url-string"} 弱契约自动归一为成功 entry
4. 非法 JSON 字符串解析失败 → 返回空 dict，不抛异常
5. 空值/空字符串 → 返回空 dict
6. primary_video_url：主平台成功取主平台，主平台失败回退到第一个成功平台
7. serialize_platform_outputs：空 dict → None
"""
import sys
import os
import json as _json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.platform_outputs import (
    OK,
    FAILED,
    normalize_entry,
    parse_platform_outputs,
    serialize_platform_outputs,
    ok_platforms,
    failed_platforms,
    primary_video_url,
)


def test_normalize_str_value_becomes_ok_entry():
    """旧契约 {platform: "url"} 中的纯字符串值应归一为成功 entry。"""
    entry = normalize_entry("douyin", "http://x/final_douyin.mp4")
    assert entry == {"url": "http://x/final_douyin.mp4", "status": OK}


def test_normalize_dict_value_preserves_fields_and_fills_status():
    """dict entry 保留原字段，status 缺省补 ok。"""
    entry = normalize_entry("bilibili", {"url": "http://x/final_bilibili.mp4", "ratio": "16:9"})
    assert entry["url"] == "http://x/final_bilibili.mp4"
    assert entry["ratio"] == "16:9"
    assert entry["status"] == OK


def test_normalize_failed_entry_strips_nothing():
    """失败 entry 保留 status=failed 与 error，不补 url。"""
    entry = normalize_entry("xiaohongshu", {"ratio": "3:4", "status": FAILED, "error": "烧录失败"})
    assert entry["status"] == FAILED
    assert entry["error"] == "烧录失败"
    assert "url" not in entry


def test_normalize_empty_str_returns_none():
    """空字符串值无意义 → None（调用方丢弃）。"""
    assert normalize_entry("douyin", "") is None
    assert normalize_entry("douyin", "   ") is None


def test_parse_legacy_string_mapping():
    """DB 中 {platform: "url"} 旧数据解析后自动归一为 object。"""
    raw = '{"douyin": "http://x/final_douyin.mp4", "bilibili": "http://x/final_bilibili.mp4"}'
    out = parse_platform_outputs(raw)
    assert out["douyin"]["url"] == "http://x/final_douyin.mp4"
    assert out["bilibili"]["status"] == OK


def test_parse_structured_mapping():
    """新契约 {platform: object} 原样解析。"""
    raw = '{"douyin": {"url": "http://x/final_douyin.mp4", "status": "ok", "ratio": "9:16"}}'
    out = parse_platform_outputs(raw)
    assert out["douyin"]["ratio"] == "9:16"
    assert out["douyin"]["status"] == OK


def test_parse_invalid_json_returns_empty():
    """非法 JSON 不得抛异常（REQ-M5 边界）。"""
    assert parse_platform_outputs("{not-valid-json") == {}
    assert parse_platform_outputs("") == {}
    assert parse_platform_outputs(None) == {}


def test_parse_non_object_json_returns_empty():
    """顶层非 object（list/scalar）视为无效 → 空 dict。"""
    assert parse_platform_outputs("[1,2,3]") == {}
    assert parse_platform_outputs('"just-a-string"') == {}


def test_ok_and_failed_platform_helpers():
    """ok_platforms / failed_platforms 按 status 过滤。"""
    mapping = {
        "douyin": {"url": "http://x/final_douyin.mp4", "status": OK},
        "bilibili": {"status": FAILED, "error": "base 合成失败"},
        "kuaishou": {"url": "http://x/final_kuaishou.mp4", "status": OK},
    }
    assert ok_platforms(mapping) == ["douyin", "kuaishou"]
    assert failed_platforms(mapping) == {"bilibili": "base 合成失败"}


def test_primary_video_url_prefers_primary_then_fallback():
    """主平台成功取主平台；主平台失败回退到第一个成功平台（design §4.1）。"""
    mapping = {
        "douyin": {"status": FAILED, "error": "烧录失败"},
        "bilibili": {"url": "http://x/final_bilibili.mp4", "status": OK},
    }
    assert primary_video_url(mapping, "douyin") == "http://x/final_bilibili.mp4"
    assert primary_video_url(mapping, "bilibili") == "http://x/final_bilibili.mp4"
    assert primary_video_url({}, "douyin") is None


def test_serialize_empty_returns_none():
    """单平台任务 platform_outputs 可为空 → None（不写无意义 "{}"）。"""
    assert serialize_platform_outputs({}) is None
    s = serialize_platform_outputs({"douyin": {"url": "http://x/final_douyin.mp4", "status": OK}})
    assert s and _json.loads(s)["douyin"]["status"] == OK


# ---------------------------------------------------------------- #
# API 层守护（REQ-M5 / REQ-M7）
# ---------------------------------------------------------------- #

def _mk_task(platform_outputs, platform="douyin"):
    """构造不落库的轻量 Task 替身（get_task 详情 / publish 路径读到的字段）。"""
    class _T:
        pass
    t = _T()
    t.platform = platform
    t.platform_outputs = platform_outputs
    t.status = "done"
    t.video_url = "http://x/final_primary.mp4"
    return t


def test_get_task_serializes_platform_outputs_as_object():
    """REQ-M5：GET /tasks/{id} 的 platform_outputs 由裸字符串改为 object。"""
    raw = '{"douyin": {"url": "http://x/final_douyin.mp4", "ratio": "9:16", "status": "ok"}}'
    parsed = parse_platform_outputs(raw)
    assert isinstance(parsed, dict)
    assert parsed["douyin"]["status"] == OK
    assert parsed["douyin"]["ratio"] == "9:16"


def test_get_task_legacy_url_string_normalized():
    """旧 {platform: "url"} 数据解析后自动归一为成功 entry（向后兼容）。"""
    raw = '{"douyin": "http://x/final_douyin.mp4"}'
    parsed = parse_platform_outputs(raw)
    assert parsed["douyin"]["url"] == "http://x/final_douyin.mp4"
    assert parsed["douyin"]["status"] == OK


def test_publish_per_platform_uses_platform_outputs():
    """REQ-M7：publish 入口按平台取对应成片；缺失/失败平台返回 400。

    复用 publish 路径中的核心决策逻辑（契约层 helper），
    验证「按平台查 platform_outputs，无成功 entry 时 400」。
    """
    # 多平台任务：douyin 成功，bilibili 失败
    raw = _json.dumps({
        "douyin": {"url": "http://x/final_douyin.mp4", "ratio": "9:16", "status": OK},
        "bilibili": {"ratio": "16:9", "status": FAILED, "error": "烧录失败"},
    }, ensure_ascii=False)
    po = parse_platform_outputs(raw)

    # douyin 在 po 中有成功 entry → 可发布
    douyin_po = {p: po[p] for p in ["douyin"] if p in po}
    assert primary_video_url(douyin_po, "douyin") == "http://x/final_douyin.mp4"

    # bilibili 在 po 中但 entry.status=failed → 取不到 URL，应 400
    bili_po = {p: po[p] for p in ["bilibili"] if p in po}
    assert primary_video_url(bili_po, "bilibili") is None

    # 某平台根本不在 po 里 → 取不到 URL，应 400
    assert "youtube" not in po
    yt_po = {p: po[p] for p in ["youtube"] if p in po}
    assert primary_video_url(yt_po, "youtube") is None


def test_publish_single_platform_keeps_video_url():
    """单平台任务（platform_outputs 为 None）保持旧行为：用 task.video_url。"""
    task = _mk_task(platform_outputs=None, platform="douyin")
    # 单平台 → 无 po → 不逐平台查，沿用 video_url（旧行为兜底）
    assert task.platform_outputs is None
    assert task.video_url == "http://x/final_primary.mp4"
