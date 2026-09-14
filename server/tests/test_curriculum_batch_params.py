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
import asyncio
import importlib.util
import json
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


# ---------------------------------------------------------------- #
# 断点续跑（change-id=curriculum-resume-tags）
# 旧版静默失效：page_size=1000 → HTTP 422，且列表项缺 source_hotspot_title
# ---------------------------------------------------------------- #

class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _FakeClient:
    """记录请求参数、按 page 返回对应 items 的假 httpx 客户端。"""

    def __init__(self, pages, status=200):
        self.pages = pages
        self.status = status
        self.calls = []

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        if self.status != 200:
            return _Resp(self.status, {"detail": "err"})
        page = int((params or {}).get("page", 1))
        idx = page - 1
        payload = self.pages[idx] if 0 <= idx < len(self.pages) else {"items": [], "total": 0}
        return _Resp(200, payload)


def test_resume_collects_tags_across_pages(script):
    """多页任务必须全部收集，且请求用合法 page_size（接口上限 100）。"""
    pages = [
        {"items": [{"source_hotspot_title": f"curriculum:primary:{i}"} for i in range(1, 101)],
         "total": 150},
        {"items": [{"source_hotspot_title": f"curriculum:primary:{i}"} for i in range(101, 151)],
         "total": 150},
    ]
    client = _FakeClient(pages)
    tags = asyncio.run(script.fetch_existing_curriculum_tags(client, {}))

    assert len(tags) == 150
    assert "curriculum:primary:1" in tags and "curriculum:primary:150" in tags
    assert [c["params"]["page_size"] for c in client.calls] == [100, 100], \
        "page_size 必须 ≤100（旧版 1000 直接 422）"
    assert [c["params"]["page"] for c in client.calls] == [1, 2]


def test_resume_ignores_non_curriculum_tags(script):
    """只认 curriculum:primary: 前缀；热点任务/空值不得参与跳过判定。"""
    pages = [{"items": [
        {"source_hotspot_title": "curriculum:primary:5"},
        {"source_hotspot_title": "某热点标题"},
        {"source_hotspot_title": None},
    ], "total": 3}]
    tags = asyncio.run(script.fetch_existing_curriculum_tags(_FakeClient(pages), {}))
    assert tags == {"curriculum:primary:5"}


def test_resume_fails_loud_on_bad_status(script):
    """列表接口非 200（如旧版的 422）必须抛错，不得静默当作“无已建”。"""
    client = _FakeClient([], status=422)
    with pytest.raises(RuntimeError):
        asyncio.run(script.fetch_existing_curriculum_tags(client, {}))


def test_source_has_no_oversized_page_size():
    """源码级护栏：不得再出现 page_size 写死 1000（接口 le=100，超限 422）。"""
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    assert '"page_size": 1000' not in src
    assert "PAGE_SIZE_MAX" in src


def test_tasks_list_api_exposes_source_hotspot_title():
    """GET /api/tasks/ 列表项必须暴露 source_hotspot_title（断点续跑的唯一依据）。

    说明：此处用源码级护栏而非打接口——``tests/test_api.py`` 的 client 走的是
    真实 poems.db，跑一次就会真建一条任务，会污染课标 75 条的现场。
    功能侧由真实接口探测另行验证（列表项含该字段）。
    """
    api_src = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "tasks.py"
    ).read_text(encoding="utf-8")
    assert '"source_hotspot_title": task.source_hotspot_title' in api_src


