"""subtitle 阶段多平台成片守护测试（2026-09-10 任务005）。

根因：run_stage("subtitle")（队列主路径）只渲染 task.platform 单平台，
设置页 output_platforms 勾 3 平台也只出 1 个成片——与 run_pipeline 直跑
路径的多平台比例组循环漂移。修复后两处共用 _render_platform_outputs。

守护点：
1. subtitle 阶段按 settings.output_platforms 渲染全部比例组，
   platform_outputs 写入多平台映射（同比例只烧一份，如 douyin/kuaishou 共 9:16）；
2. 单平台任务不写 platform_outputs（保持旧行为）；
3. 所有比例组失败 → fail-loud 抛错（不静默出半成品）；
4. run_pipeline 与 run_stage 共用同一渲染方法（防再次漂移）；
5. 渲染层返回 entry 对象（2026-09-14 多平台成片 REQ-M2）：成功 entry 含
   url/ratio/duration/status=ok，base 失败组内平台 entry 为 status=failed+error。
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _make_task(platform="douyin", platforms=None, storyboards_json=None, script=None,
               image_urls="[]"):
    """构造不落库的轻量 Task 替身（只需 pipeline 渲染读到的字段）。"""
    class _T:
        pass
    t = _T()
    t.id = 99
    t.platform = platform
    t.platforms = platforms
    t.storyboard = "[]"
    t.image_urls = image_urls
    t.image_local_paths = None
    t.storyboards_json = storyboards_json
    t.script = script
    return t


def _patch_render(monkeypatch, build_ok=True, burn_fail=(), burn_returns=None):
    """mock _generate_tts_segments/_build_segments/_burn_subtitles，记录调用轨迹。

    burn_returns: 可选 dict {platform: url}，用于按平台控制烧录产物（默认成功时
    生成 http://x/outputs/task_99/final_{platform}.mp4）。

    渲染层已逐档化（选项 A）：每个档位各触发一次 TTS + 各比例组一次 base 构建，
    故 fake 必须接受 tier/image_urls/tier_script 等逐档参数。details 记录逐档细节
    供"各档用各自分镜"的守护断言使用。
    """
    from app.services.pipeline import pipeline_engine
    calls = {"build": [], "burn": [], "tts": [], "details": []}

    async def fake_tts(task, storyboard, db=None, force=False, tier=""):
        calls["tts"].append(tier)
        return {"success": True, "segments": [{"index": 0, "text": "x", "path": "y"}]}

    async def fake_build(task, sb, tts, W, H, platform="douyin", tier="", image_urls=None):
        calls["build"].append(platform)
        calls["details"].append({
            "kind": "build", "platform": platform, "tier": tier, "shots": len(sb or []),
        })
        if not build_ok:
            return {"ok": False}
        return {"ok": True, "base": f"base_{platform}.mp4",
                "timeline": [(0, 1, "x")], "duration": 1.0}

    async def fake_burn(task, base, sb, segs, W, H, platform="douyin",
                        poem_content=None, tier_script=None, tier=""):
        calls["burn"].append(platform)
        calls["details"].append({
            "kind": "burn", "platform": platform, "tier": tier,
            "shots": len(sb or []), "tier_script": tier_script,
        })
        if platform in burn_fail:
            return None
        if burn_returns is not None and platform in burn_returns:
            return burn_returns[platform]
        return f"http://x/outputs/task_99/final_{platform}.mp4"

    async def fake_persist(task, image_urls, tier=""):
        # 图片落盘在单测中打桩：避免真实 HTTP；逐档命名由 test_image_local_persist 覆盖
        return []

    monkeypatch.setattr(pipeline_engine, "_generate_tts_segments", fake_tts)
    monkeypatch.setattr(pipeline_engine, "_persist_images_local", fake_persist)
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
    urls = await pipeline_engine._render_platform_outputs(task)

    # 9:16 组 base 只建一次（rep=douyin），组内烧 douyin + kuaishou；3:4 组烧 xiaohongshu
    assert calls["build"] == ["douyin", "xiaohongshu"]
    assert sorted(calls["burn"]) == ["douyin", "kuaishou", "xiaohongshu"]
    assert set(urls) == {"douyin", "xiaohongshu", "kuaishou"}
    # entry 对象契约（REQ-M2）：成功 entry 含 url + status=ok
    assert urls["douyin"]["url"].endswith("final_douyin.mp4")
    assert urls["douyin"]["status"] == "ok"


@pytest.mark.asyncio
async def test_render_entry_carries_ratio_and_duration(monkeypatch):
    """成功 entry 携带比例与时长（design §4.1：ratio/duration 随成片记录）。"""
    from app.services.pipeline import pipeline_engine

    _patch_render(monkeypatch)
    monkeypatch.setattr("app.services.pipeline.settings.output_platforms",
                        ["douyin", "bilibili"])

    task = _make_task(platform="douyin", platforms=None)
    urls = await pipeline_engine._render_platform_outputs(task)

    assert urls["douyin"]["ratio"] == "9:16"
    assert urls["bilibili"]["ratio"] == "16:9"
    # _build_segments 返回 duration=1.0（mock），entry 记录同组同值
    assert urls["douyin"]["duration"] == urls["bilibili"]["duration"] == 1.0


@pytest.mark.asyncio
async def test_base_failure_marks_group_platforms_failed(monkeypatch):
    """比例组 base 合成失败 → 组内所有平台 entry status=failed + error（不静默丢弃）。"""
    from app.services.pipeline import pipeline_engine

    calls = _patch_render(monkeypatch, build_ok=False)
    monkeypatch.setattr("app.services.pipeline.settings.output_platforms",
                        ["douyin", "kuaishou", "xiaohongshu"])

    task = _make_task(platform="douyin", platforms=None)
    urls = await pipeline_engine._render_platform_outputs(task)

    # 9:16 组（douyin/kuaishou）与 3:4 组（xiaohongshu）均 base 失败
    assert set(urls) == {"douyin", "kuaishou", "xiaohongshu"}
    for plat in urls.values():
        assert plat["status"] == "failed"
        assert plat["error"]
        assert "url" not in plat
    # base 失败时不会触发烧录
    assert calls["burn"] == []


@pytest.mark.asyncio
async def test_burn_failure_marks_platform_failed(monkeypatch):
    """单平台烧录失败 → 该平台 entry status=failed + error，其余平台正常。"""
    from app.services.pipeline import pipeline_engine

    _patch_render(monkeypatch, burn_fail=("kuaishou",))
    monkeypatch.setattr("app.services.pipeline.settings.output_platforms",
                        ["douyin", "kuaishou"])

    task = _make_task(platform="douyin", platforms=None)
    urls = await pipeline_engine._render_platform_outputs(task)

    assert urls["douyin"]["status"] == "ok"
    assert urls["douyin"]["url"].endswith("final_douyin.mp4")
    assert urls["kuaishou"]["status"] == "failed"
    assert "url" not in urls["kuaishou"]
    assert urls["kuaishou"]["ratio"] == "9:16"


@pytest.mark.asyncio
async def test_subtitle_stage_single_platform_no_outputs_json(monkeypatch):
    """单平台（含回退只剩 douyin）时不写 platform_outputs 的判定依据：
    len(platform_urls) > 1 才写。渲染层返回单条，写库逻辑由调用方分支。"""
    from app.services.pipeline import pipeline_engine

    calls = _patch_render(monkeypatch)
    monkeypatch.setattr("app.services.pipeline.settings.output_platforms", ["douyin"])

    task = _make_task(platform="douyin", platforms=None)
    urls = await pipeline_engine._render_platform_outputs(task)

    assert calls["build"] == ["douyin"]
    assert set(urls) == {"douyin"}


@pytest.mark.asyncio
async def test_render_all_platforms_failed_yields_no_ok(monkeypatch):
    """所有比例组 base 失败 → 渲染层返回的 mapping 无任何成功 entry
    （调用方 run_stage 据 primary_video_url 为 None fail-loud）。"""
    from app.services.pipeline import pipeline_engine
    from app.services.platform_outputs import primary_video_url

    _patch_render(monkeypatch, build_ok=False)
    monkeypatch.setattr("app.services.pipeline.settings.output_platforms",
                        ["douyin", "xiaohongshu"])

    task = _make_task(platform="douyin", platforms=None)
    urls = await pipeline_engine._render_platform_outputs(task)
    assert primary_video_url(urls, "douyin") is None


@pytest.mark.asyncio
async def test_render_per_tier_uses_own_storyboard_and_prefix(monkeypatch):
    """选项 A 逐档渲染（2026-09-14）：混合任务（douyin S + bilibili L）每档用
    **各自** 的分镜/文案渲染，非主档走 tier_L 前缀，主档保持无前缀旧命名。

    旧行为：所有平台共用（主档）一份分镜 → B 站深档被渲染成 S 档的 ~30s 短片，
    与抖音成片无差别（用户实测"B 站成片和抖音一样"）。
    """
    from app.services.pipeline import pipeline_engine

    calls = _patch_render(monkeypatch)
    s_shots = [{"narration": f"S{i}"} for i in range(7)]   # S 档 7 镜
    l_shots = [{"narration": f"L{i}"} for i in range(18)]  # L 档 18 镜
    sb_json = json.dumps({
        "S": {"script": "S 档文案", "script_score": 8.0, "storyboard": s_shots,
              "image_urls": ["http://img/s.png"]},
        "L": {"script": "L 档文案", "script_score": 8.0, "storyboard": l_shots,
              "image_urls": ["http://img/l.png"]},
    }, ensure_ascii=False)

    task = _make_task(platform="douyin",
                      platforms=json.dumps(["douyin", "bilibili"]),
                      storyboards_json=sb_json)

    urls = await pipeline_engine._render_platform_outputs(task)

    assert set(urls) == {"douyin", "bilibili"}
    # 每档各触发一次本档 TTS：主档 tier=""、L 档 tier="L"
    assert sorted(calls["tts"]) == ["", "L"]
    builds = {d["platform"]: d for d in calls["details"] if d["kind"] == "build"}
    # 各档用各自镜数：主档 7 镜、L 档 18 镜（不再共用 S 档分镜）
    assert builds["douyin"]["tier"] == "" and builds["douyin"]["shots"] == 7
    assert builds["bilibili"]["tier"] == "L" and builds["bilibili"]["shots"] == 18
    # 每档烧字幕时传入本档文案（金句提取按档，不再用主档文案）
    burns = {d["platform"]: d for d in calls["details"] if d["kind"] == "burn"}
    assert burns["douyin"]["tier_script"] == "S 档文案"
    assert burns["bilibili"]["tier_script"] == "L 档文案"


@pytest.mark.asyncio
async def test_render_single_tier_keeps_legacy_main_naming(monkeypatch):
    """单档任务（纯 douyin）：主档 tier="" 无前缀，且只触发一次 TTS（不引入额外档）。"""
    from app.services.pipeline import pipeline_engine

    calls = _patch_render(monkeypatch)
    task = _make_task(platform="douyin", platforms=json.dumps(["douyin", "kuaishou"]))
    urls = await pipeline_engine._render_platform_outputs(task)

    assert set(urls) == {"douyin", "kuaishou"}
    assert calls["tts"] == [""]
    assert {d["tier"] for d in calls["details"]} == {""}


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


# ---------------------------------------------------------------- #
# _task_platforms 平台解析（REQ-M1 存量升级：旧数据逗号串兜底）
# ---------------------------------------------------------------- #

def _fake_task(platform="douyin", platforms=None):
    """轻量 task 替身，只需 _task_platforms 读到的字段。"""

    class _T:
        pass

    t = _T()
    t.platform = platform
    t.platforms = platforms
    return t


def test_task_platforms_json_array_preferred():
    """task.platforms 为 JSON 数组时优先采用，不被逗号串覆盖。"""
    from app.services.pipeline import PipelineEngine
    t = _fake_task(platform="douyin",
                   platforms='["douyin", "kuaishou", "xiaohongshu"]')
    assert PipelineEngine._task_platforms(t) == ["douyin", "kuaishou", "xiaohongshu"]


def test_task_platforms_legacy_comma_string():
    """旧数据 platform 逗号串 + platforms 空 → 解析出真实多平台（不走全局默认）。"""
    from app.services.pipeline import PipelineEngine
    t = _fake_task(platform="douyin,bilibili", platforms=None)
    assert PipelineEngine._task_platforms(t) == ["douyin", "bilibili"]


def test_task_platforms_single_platform_uses_global_default():
    """单平台 task.platform（无逗号）→ 回退全局默认，不改变既有单平台行为。"""
    from app.services.pipeline import PipelineEngine
    t = _fake_task(platform="douyin", platforms=None)
    # 依赖 settings.output_platforms（测试环境多为多平台默认），断言返回的是列表且含 douyin
    plats = PipelineEngine._task_platforms(t)
    assert isinstance(plats, list) and "douyin" in plats


def test_task_platforms_malformed_json_array_with_comma():
    """存量畸形：task.platforms 为单元素数组包整串逗号（'["douyin,bilibili"]'）→
    须拆成真实多平台，否则渲染只出 9:16 单成片、platform_outputs 因 len==1 不写
    （小批量 pilot 实测：75 个旧任务全中此坑，均产出 final_douyin,bilibili.mp4 而非
    分开的 final_douyin.mp4 + final_bilibili.mp4）。
    """
    from app.services.pipeline import PipelineEngine
    t = _fake_task(platform="douyin,bilibili",
                   platforms='["douyin,bilibili"]')
    assert PipelineEngine._task_platforms(t) == ["douyin", "bilibili"]
