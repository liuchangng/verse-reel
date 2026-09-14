"""script 阶段必须生成分镜 守护测试（2026-09-09 事故）

事故：run_stage("script") 只生成文案并清空下游产物，分镜（storyboard）生成
逻辑只存在于旧 run_pipeline 直跑路径。创建任务统一入队后 storyboard 永远
为空，image/tts/subtitle 三阶段必然"缺少前置 storyboard"失败。

守护点：
- script 阶段完成时 task.storyboard 必须就绪（由文案生成，tier 按任务平台解析）；
- 文案评分未达标提前返回时，不得生成分镜（保持清空态）。
"""
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.task import Task
from app.models.poem import Poem
from app.services.critic import ScoreResult
from app.services import pipeline as pipeline_mod
from app.services.pipeline import pipeline_engine


@pytest_asyncio.fixture()
async def sb_env(monkeypatch):
    """内存库 + mock 掉 critic_service（逐档生成在 critic 边界被 mock），捕获分镜入参。"""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        s.add(Poem(id=1, title="静夜思", author="李白", dynasty="唐",
                   content="床前明月光，疑是地上霜。"))
        s.add(Task(id=1, poem_id=1, platform="douyin",
                   platforms=json.dumps(["douyin"])))  # 仅 douyin → S 单档
        await s.commit()

    captured = {}

    class _FakeCritic:
        async def generate_script(self, poem_title, poem_content, author, dynasty,
                                  custom_prompt="", keywords=None):
            return "文案正文「金句」。"

        async def score_script(self, script, tier="S"):
            return ScoreResult(score=8.0, passed=True, feedback="ok")

        async def generate_storyboard(self, script, tier="S"):
            captured["script"] = script
            captured["tier"] = tier
            return json.dumps([{"time": "0-3s", "description": "月夜", "narration": "床前明月光"}],
                              ensure_ascii=False)

    async def fake_recommend(_poem):
        return "preset-test"

    monkeypatch.setattr(pipeline_mod, "critic_service", _FakeCritic())
    from app.services import voice_selector as _vs
    monkeypatch.setattr(_vs, "recommend", fake_recommend)
    yield factory, captured
    await engine.dispose()


# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_script_stage_generates_storyboard(sb_env):
    """script 阶段完成时 storyboard 必须就绪（tts/image/subtitle 的前置）"""
    factory, captured = sb_env
    async with factory() as db:
        await pipeline_engine.run_stage(db, 1, "script")
        task = await db.get(Task, 1)
        assert task.storyboard, "script 阶段完成后 storyboard 不得为空"
        sb = json.loads(task.storyboard)
        assert isinstance(sb, list) and len(sb) == 1
        assert sb[0]["narration"] == "床前明月光"
    # 分镜由本阶段生成的文案驱动
    assert captured["script"] == "文案正文「金句」。"


@pytest.mark.asyncio()
async def test_script_score_fail_skips_storyboard(sb_env, monkeypatch):
    """文案评分未达标 → 提前返回，不生成分镜（保持清空态）"""
    factory, _ = sb_env

    class _FailCritic:
        async def generate_script(self, *a, **k):
            return "不合格文案。"
        async def score_script(self, *a, **k):
            return ScoreResult(score=3.0, passed=False, feedback="不合格")
        async def generate_storyboard(self, *a, **k):
            return "[]"

    monkeypatch.setattr(pipeline_mod, "critic_service", _FailCritic())

    async with factory() as db:
        await pipeline_engine.run_stage(db, 1, "script")
        task = await db.get(Task, 1)
        assert task.storyboard is None, "评分未达标时不得生成分镜"
        assert task.status == "failed"
