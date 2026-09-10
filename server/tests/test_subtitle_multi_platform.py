"""subtitle 阶段多平台成片守护测试（2026-09-10 任务005）。

根因：run_stage("subtitle")（队列主路径）只渲染 task.platform 单平台，
设置页 output_platforms 勾 3 平台也只出 1 个成片——与 run_pipeline 直跑
路径的多平台比例组循环漂移。修复后两处共用 _render_platform_outputs。

守护点：
1. subtitle 阶段按 settings.output_platforms 渲染全部比例组，
   platform_outputs 写入多平台映射（同比例只烧一份，如 douyin/kuaishou 共 9:16）；
2. 单平台任务不写 platform_outputs（保持旧行为）；
3. 所有比例组失败 → fail-loud 抛错（不静默出半成品）；
4. run_pipeline 与 run_stage 共用同一渲染方法（防再次漂移）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _make_task(platform="douyin", platforms=None):
    """构造不落库的轻量 Task 替身（只需 pipeline 渲染读到的字段）。"""
    class _T:
        pass
    t = _T()
    t.id = 99
    t.platform = platform
    t.platforms = platforms
    t.storyboard = "[]"
    t.image_urls = "[]"
    return t


def _patch_render(monkeypatch, build_ok=True, burn_fail=()):
    """mock _build_segments/_burn_subtitles，记录调用轨迹。"""
    from app.services.pipeline import pipeline_engine
    calls = {"build": [], "burn": []}

    async def fake_build(task, sb, tts, W, H, platform="douyin"):
        calls["build"].append(platform)
        if not build_ok:
            return {"ok": False}
        return {"ok": True, "base": f"base_{platform}.mp4",
                "timeline": [(0, 1, "x")], "duration": 1.0}

    async def fake_burn(task, base, sb, segs, W, H, platform="douyin", poem_content=None):
        calls["burn"].append(platform)
        if platform in burn_fail:
            return None
        return f"http://x/outputs/task_99/final_{platform}.mp4"

    monkeypatch.setattr(pipeline_engine, "_build_segments", fake_build)
    monkeypatch.setattr(pipeline_engine, "_burn_subtitles", fake_burn)
    return calls


@pytest.mark.asyncio
async def test_subtitle_stage_renders_all_platform_groups(monkeypatch):
    """platforms 为空回退 settings.output_platforms（3 平台）→ 2 个比例组、3 平台成片。"""
    from app.services.pipeline import pipeline_engine

    calls = _patch_render(monkeypatch)
    # 系统设置页默认：douyin/kuaishou(9:16 同组) + xiaohongshu(3:4 独立)
    monkeypatch.setattr("app.services.pipeline.settings.output_platforms",
                        ["douyin", "xiaohongshu", "kuaishou"])

    task = _make_task(platform="douyin", platforms=None)
    urls = await pipeline_engine._render_platform_outputs(task, [], {"segments": []})

    # 9:16 组 base 只建一次（rep=douyin），组内烧 douyin + kuaishou；3:4 组烧 xiaohongshu
    assert calls["build"] == ["douyin", "xiaohongshu"]
    assert sorted(calls["burn"]) == ["douyin", "kuaishou", "xiaohongshu"]
    assert set(urls) == {"douyin", "xiaohongshu", "kuaishou"}
    assert urls["douyin"].endswith("final_douyin.mp4")


@pytest.mark.asyncio
async def test_subtitle_stage_single_platform_no_outputs_json(monkeypatch):
    """单平台（含回退只剩 douyin）时不写 platform_outputs 的判定依据：
    len(platform_urls) > 1 才写。渲染层返回单条，写库逻辑由调用方分支。"""
    from app.services.pipeline import pipeline_engine

    calls = _patch_render(monkeypatch)
    monkeypatch.setattr("app.services.pipeline.settings.output_platforms", ["douyin"])

    task = _make_task(platform="douyin", platforms=None)
    urls = await pipeline_engine._render_platform_outputs(task, [], {"segments": []})

    assert calls["build"] == ["douyin"]
    assert set(urls) == {"douyin"}


@pytest.mark.asyncio
async def test_render_fail_loud_when_all_groups_fail(monkeypatch):
    """所有比例组 base 合成失败 → 空 dict，调用方 fail-loud 抛错。"""
    from app.services.pipeline import pipeline_engine

    _patch_render(monkeypatch, build_ok=False)
    monkeypatch.setattr("app.services.pipeline.settings.output_platforms",
                        ["douyin", "xiaohongshu"])

    task = _make_task(platform="douyin", platforms=None)
    urls = await pipeline_engine._render_platform_outputs(task, [], {"segments": []})
    assert urls == {}


def test_run_stage_and_run_pipeline_share_renderer():
    """防再漂移：subtitle 阶段与 run_pipeline 必须共用 _render_platform_outputs。"""
    import inspect
    from app.services.pipeline import PipelineEngine
    src_stage = inspect.getsource(PipelineEngine.run_stage)
    src_full = inspect.getsource(PipelineEngine.run_pipeline)
    assert src_stage.count("_render_platform_outputs") >= 1
    assert src_full.count("_render_platform_outputs") >= 1
    # 渲染循环本体只存在于共享方法，不得在任一调用方内联展开
    for src in (src_stage, src_full):
        assert "ar_groups" not in src
