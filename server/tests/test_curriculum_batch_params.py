"""课标批量建任务脚本 platforms 传参守护（REQ：逗号串污染产物文件名）。

背景（2026-09-14，change-id=curriculum-platforms-param）
-------------------------------------------------------
``scripts/batch_curriculum.py`` 旧版用
``platforms=",".join(["douyin", "bilibili"])`` 调 ``POST /api/tasks``。
后端签名是 ``platforms: list[str] = Query(None)``，收到单个 ``"douyin,bilibili"``
时解析为**单元素列表** ``["douyin,bilibili"]``——整串被当成「一个平台名」落库：

    platform  = 'douyin,bilibili'
    platforms = '["douyin,bilibili"]'   # 畸形

渲染阶段把 ``platform`` 直接拼进文件名，于是产出

    seg_douyin,bilibili_0.mp4 / base_douyin,bilibili.mp4 / final_douyin,bilibili.mp4

批量建的 75 个课标任务全部中招。正确做法是传 list，由 httpx 序列化为重复参数
（``platforms=douyin&platforms=bilibili``），FastAPI 方能解析为真实列表。

本模块锁定该契约，防止回归。
"""
import importlib.util
from pathlib import Path

import pytest

# tests/ → server/ → 仓库根 → scripts/
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "batch_curriculum.py"

_ITEM = {"no": 1, "title": "江南", "poem_id": 3890}


@pytest.fixture(scope="module")
def script():
    """按路径加载脚本模块（scripts/ 非包，直接 exec 更稳）。"""
    assert SCRIPT_PATH.exists(), f"脚本不存在: {SCRIPT_PATH}"
    spec = importlib.util.spec_from_file_location("batch_curriculum_under_test", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_platforms_passed_as_list_not_comma_string(script):
    """platforms 必须是 list，且每个元素为不含逗号的单平台名。"""
    params = script.build_create_params(dict(_ITEM))
    assert isinstance(params["platforms"], list)
    assert params["platforms"] == ["douyin", "bilibili"]
    for plat in params["platforms"]:
        assert "," not in plat, f"平台名不得含逗号（会拼进产物文件名）: {plat!r}"


def test_main_platform_is_single_platform(script):
    """platform 须为单平台名——逗号串会让产物变成 final_douyin,bilibili.mp4。"""
    params = script.build_create_params(dict(_ITEM))
    assert params["platform"] == "douyin"
    assert "," not in params["platform"]


def test_source_tag_marker_unchanged(script):
    """断点续跑标记格式不变（存量跳过逻辑依赖 curriculum:primary:N）。"""
    params = script.build_create_params({"no": 7, "title": "静夜思", "poem_id": 1})
    assert params["source_hotspot_title"] == "curriculum:primary:7"


def test_style_only_passed_when_explicit(script):
    """未显式指定风格时不传 style（由后端按 curriculum 默认「历史解读」推断）。"""
    assert "style" not in script.build_create_params(dict(_ITEM))
    assert script.build_create_params(dict(_ITEM), "人生感悟")["style"] == "人生感悟"


def test_source_has_no_comma_join_regression():
    """源码级护栏：不得再出现 `",".join(...)` 作为 platforms 取值。"""
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    assert '"platforms": ",".join' not in src, "platforms 不得用逗号串拼接"
    assert '"platforms": list(PLATFORMS)' in src, "platforms 必须以 list 传参"
