"""选项 A 真·分平台生成 守护测试（Stage1：数据模型 + 逐档生成）

验证点：
- _generate_script 按去重档位集合逐档生成（混合=2 档 LLM 调用，单档=1 次）；
- task.storyboards_json 结构 {tier: {script, script_score, storyboard}}；
- task.storyboard / task.script / task.script_score = 主档（主平台档位）兼容值；
- 单平台任务行为不变（storyboards_json 仅 1 档，storyboard 即它本身）。

mock 在 critic_service 边界（_generate_script 已是逐档编排器，不能再直接
patch _generate_script / _generate_storyboard，否则逐档循环被短路）。
"""
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.task import Task
from app.models.poem import Poem
from app.services import pipeline as pipeline_mod
from app.services.pipeline import pipeline_engine
from app.services.critic import ScoreResult


@pytest_asyncio.fixture()
async def pt_env(monkeypatch):
    """内存库 + mock critic_service（按 tier 返回不同文案/分镜，并计数调用次数）。

    critic_service 是模块级单例，pipeline.py 通过属性访问调用；monkeypatch
    到该属性上可替换其方法。mock 对象需实现 critic_service 的 3 个方法 +
    voice_selector.recommend（音色自动选择）以阻断真实 LLM 调用。
    """
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        s.add(Poem(id=1, title="静夜思", author="李白", dynasty="唐",
                   content="床前明月光，疑是地上霜。"))
        s.add(Task(id=1, poem_id=1, platform="douyin"))
        await s.commit()

    state = {"script_calls": 0, "storyboard_calls": 0}

    class _FakeCritic:
        """仅实现 pipeline._generate_tier_script 用到的 3 个 critic 方法。"""

        async def generate_script(self, poem_title, poem_content, author,
                                  dynasty, custom_prompt="", keywords=None):
            tier = "L" if "L 深档" in (custom_prompt or "") else "S"
            state["script_calls"] += 1
            return f"{tier} 档文案正文「金句」。"

        async def score_script(self, script, tier="S"):
            return ScoreResult(
                score=8.0 if tier == "S" else 8.5,
                passed=True, feedback=f"{tier} 档通过",
            )

        async def generate_storyboard(self, script, tier="S"):
            # 返回 JSON 字符串（与真实 critic_service 一致；_generate_storyboard 内部解析）
            state["storyboard_calls"] += 1
            return json.dumps(
                [{"time": "0-3s", "description": f"{tier} 档镜", "narration": f"{tier} 档旁白"}],
                ensure_ascii=False,
            )

    async def fake_recommend(_poem):
        return "preset-test"

    monkeypatch.setattr(pipeline_mod, "critic_service", _FakeCritic())
    from app.services import voice_selector as _vs
    monkeypatch.setattr(_vs, "recommend", fake_recommend)
    yield factory, state
    await engine.dispose()


# ---------------------------------------------------------------- #


@pytest.mark.asyncio()
async def test_single_platform_single_tier(pt_env):
    """单平台（douyin=S）任务：仅 1 档，LLM 各 1 次；storyboards_json 仅 1 键。"""
    factory, state = pt_env
    async with factory() as s:
        poem = await s.get(Poem, 1)
        s.add(Task(id=2, poem_id=1, platform="douyin", platforms=json.dumps(["douyin"])))
        await s.commit()

    async with factory() as db:
        task = await db.get(Task, 2)
        script_text, score = await pipeline_engine._generate_script(db, task, poem, "人生感悟", None)
        task = await db.get(Task, 2)
        assert state["script_calls"] == 1
        assert state["storyboard_calls"] == 1
        sj = json.loads(task.storyboards_json)
        assert set(sj.keys()) == {"S"}
        # 主档（主平台 douyin → S）兼容字段
        assert task.storyboard and json.loads(task.storyboard) == sj["S"]["storyboard"]
        assert task.script == sj["S"]["script"] == "S 档文案正文「金句」。"


@pytest.mark.asyncio()
async def test_mixed_platforms_two_tiers(pt_env):
    """混合（douyin S + bilibili L）：2 档，LLM 各 2 次；storyboards_json 双键。"""
    factory, state = pt_env
    async with factory() as s:
        poem = await s.get(Poem, 1)
        s.add(Task(id=3, poem_id=1, platform="douyin",
                   platforms=json.dumps(["douyin", "bilibili"])))
        await s.commit()

    async with factory() as db:
        task = await db.get(Task, 3)
        script_text, score = await pipeline_engine._generate_script(db, task, poem, "人生感悟", None)
        task = await db.get(Task, 3)
        assert state["script_calls"] == 2
        assert state["storyboard_calls"] == 2
        sj = json.loads(task.storyboards_json)
        assert set(sj.keys()) == {"S", "L"}
        assert sj["L"]["script"].startswith("L 档文案")
        assert sj["S"]["script"].startswith("S 档文案")
        # 主档 = 主平台 douyin → S
        assert json.loads(task.storyboard) == sj["S"]["storyboard"]
        assert task.script == sj["S"]["script"]
        assert task.script_score == sj["S"]["script_score"]


@pytest.mark.asyncio()
async def test_main_tier_from_main_platform(pt_env):
    """主平台在任务平台内时，主档=主平台档位（bilibili → L），即便 S 也在集合中。"""
    factory, state = pt_env
    async with factory() as s:
        poem = await s.get(Poem, 1)
        s.add(Task(id=4, poem_id=1, platform="bilibili",
                   platforms=json.dumps(["douyin", "bilibili"])))
        await s.commit()

    async with factory() as db:
        task = await db.get(Task, 4)
        await pipeline_engine._generate_script(db, task, poem, "人生感悟", None)
        task = await db.get(Task, 4)
        sj = json.loads(task.storyboards_json)
        # 主平台 bilibili → L 为主档
        assert json.loads(task.storyboard) == sj["L"]["storyboard"]
        assert task.script == sj["L"]["script"]
        assert task.script_score == sj["L"]["script_score"] == 8.5
