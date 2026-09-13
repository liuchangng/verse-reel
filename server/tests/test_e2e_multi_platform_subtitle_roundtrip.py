"""端到端多平台成片守护（Wave 6 / change: multi-platform-final-videos-platform-outputs）。

服务 REQ-M1（多平台并行渲染 → entry 对象落库）、REQ-M3（部分平台失败不阻断整任务）、
REQ-M5（GET 详情解析为 object）。

做法：用内存 SQLite + 覆写 get_db 跑真实 run_stage("subtitle") 路径，仅 mock
TTS / base 合成 / 字幕烧录（无需真实视频素材），验证 platform_outputs 真正以 entry
对象序列化落库，并被 API 解析回 object。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fastapi.testclient import TestClient

from app.database import Base, get_db
from app.main import app
from app.models.task import Task
from app.models.poem import Poem
from app.services.pipeline import pipeline_engine
from app.services.platform_outputs import parse_platform_outputs


@pytest_asyncio.fixture()
async def env():
    """内存 SQLite + 覆写 get_db 依赖，供 run_stage 与 TestClient 走真实路由。"""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    yield factory
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest_asyncio.fixture()
async def seeded(env):
    """预置 poem + 3 平台任务（douyin/kuaishou 9:16、bilibili 16:9）；放行鉴权。"""
    from app.config import settings
    settings.app_token = "test-token"
    async with env() as s:
        s.add(Poem(id=1, title="定风波", author="苏轼", dynasty="宋",
                   content="莫听穿林打叶声，何妨吟啸且徐行。"))
        s.add(Task(id=1, poem_id=1, platform="douyin",
                   platforms=json.dumps(["douyin", "kuaishou", "bilibili"], ensure_ascii=False),
                   status="subtitle", script="文案正文",
                   storyboard=json.dumps([{"text": "一蓑烟雨任平生", "img": "x"}],
                                          ensure_ascii=False)))
        await s.commit()
    yield env


def _patch_render(monkeypatch, build_ok=True, burn_fail=()):
    """mock TTS / _build_segments / _burn_subtitles，记录调用轨迹。"""
    calls = {"build": [], "burn": []}

    async def fake_tts(task, storyboard, db, force=False):
        return {"success": True, "segments": [{"text": "x", "audio": "y", "duration": 1.0}]}

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
        return f"http://x/outputs/task_1/final_{platform}.mp4"

    monkeypatch.setattr(pipeline_engine, "_generate_tts_segments", fake_tts)
    monkeypatch.setattr(pipeline_engine, "_build_segments", fake_build)
    monkeypatch.setattr(pipeline_engine, "_burn_subtitles", fake_burn)
    return calls


@pytest.mark.asyncio
async def test_e2e_subtitle_writes_entry_objects_to_db(seeded, monkeypatch):
    """REQ-M1：3 平台并行渲染 → platform_outputs 落库为 entry 对象（url/ratio/duration/status）。"""
    _patch_render(monkeypatch)
    async with seeded() as s:
        await pipeline_engine.run_stage(s, 1, "subtitle")
        t = await s.get(Task, 1)
        raw = t.platform_outputs
        assert raw, "platform_outputs 必须落库（REQ-M1）"
        po = parse_platform_outputs(raw)
        assert set(po) == {"douyin", "kuaishou", "bilibili"}
        for plat, entry in po.items():
            assert entry["status"] == "ok"
            assert entry["url"].endswith(f"final_{plat}.mp4")
            assert "duration" in entry
        # 同比例组（douyin/kuaishou）共 9:16，bilibili 独立 16:9
        assert po["douyin"]["ratio"] == "9:16"
        assert po["kuaishou"]["ratio"] == "9:16"
        assert po["bilibili"]["ratio"] == "16:9"
        assert t.video_url.endswith("final_douyin.mp4"), "主平台成片回退到 video_url"


@pytest.mark.asyncio
async def test_e2e_subtitle_partial_failure_marks_failed_not_blocking(seeded, monkeypatch):
    """REQ-M3：bilibili 烧录失败 → 该平台 entry=failed，其余正常，整任务不抛错。"""
    _patch_render(monkeypatch, burn_fail=("bilibili",))
    async with seeded() as s:
        # 部分失败不应 raise，run_stage 正常结束（失败平台单独标记，不阻断整任务）
        await pipeline_engine.run_stage(s, 1, "subtitle")
        t = await s.get(Task, 1)
        po = parse_platform_outputs(t.platform_outputs)
        assert po["douyin"]["status"] == "ok"
        assert po["kuaishou"]["status"] == "ok"
        assert po["bilibili"]["status"] == "failed"
        assert "url" not in po["bilibili"]
        assert po["bilibili"]["error"]
        # 主平台（douyin）成功 → video_url 仍有值，任务可继续进入发布审核
        assert t.video_url.endswith("final_douyin.mp4")


@pytest.mark.asyncio
async def test_e2e_subtitle_get_task_parses_object(seeded, monkeypatch):
    """REQ-M5：run_stage 落库后，GET /api/tasks/1 返回 object（非裸字符串）。"""
    _patch_render(monkeypatch)
    async with seeded() as s:
        await pipeline_engine.run_stage(s, 1, "subtitle")
    c = TestClient(app, headers={"Authorization": "Bearer test-token"})
    resp = c.get("/api/tasks/1")
    assert resp.status_code == 200
    po = resp.json()["platform_outputs"]
    assert isinstance(po, dict), "REQ-M5: platform_outputs 必须是 object，不再是裸字符串"
    assert po["douyin"]["status"] == "ok"
    assert po["douyin"]["url"].endswith("final_douyin.mp4")
    assert po["bilibili"]["ratio"] == "16:9"
