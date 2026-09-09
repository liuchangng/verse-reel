"""热点来源绑定 守护测试（2026-09-09 文案污染事故）

事故：生成文案时实时重抓全平台热榜 Top3 注入 prompt，与任务无关的热搜词
（"苹果折叠屏""摊芒果"）被硬揉进古诗文案。修复（用户定夺的正推逻辑）：
- 热点路径：创建任务时把"选诗依据的那个热点"（标题/关键词）随任务落库，
  文案阶段只注入本任务关联的热点；
- 诗词路径：不传来源 → 任务无热点字段 → 文案阶段不注入任何热词。
"""
import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
import app.services.pipeline as pmod
from app.models.task import Task
from app.models.poem import Poem
from app.services.critic import ScoreResult
from app.services.pipeline import pipeline_engine


def _mk_poem(**over):
    fields = dict(id=1, title="静夜思", author="李白", dynasty="唐",
                  content="床前明月光，疑是地上霜。")
    fields.update(over)
    return Poem(**fields)


@pytest_asyncio.fixture()
async def script_env(monkeypatch):
    """内存库 + mock 掉文案生成的全部外部依赖，捕获注入 prompt 的 keywords。"""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    captured = {}

    async def fake_generate_script(**kwargs):
        captured["keywords"] = kwargs.get("keywords")
        return "文案正文「金句」。"

    async def fake_score(script_text):
        return ScoreResult(score=8.0, passed=True, feedback="ok")

    async def fake_voice(poem):
        return ""

    monkeypatch.setattr(pmod.critic_service, "generate_script", fake_generate_script)
    monkeypatch.setattr(pmod.critic_service, "score_script", fake_score)
    monkeypatch.setattr("app.services.voice_selector.recommend", fake_voice)
    yield factory, captured
    await engine.dispose()


# ---------------------------------------------------------------- #
# 模型层：source_keywords_list 解析
# ---------------------------------------------------------------- #

def test_source_keywords_list_parse():
    """空/坏数据 → []；合法 JSON → list（防脏数据崩文案阶段）"""
    t = Task(poem_id=1)
    assert t.source_keywords_list == []
    t.source_keywords = "not-json"
    assert t.source_keywords_list == []
    t.source_keywords = json.dumps(["教师节", "感恩老师"], ensure_ascii=False)
    assert t.source_keywords_list == ["教师节", "感恩老师"]


# ---------------------------------------------------------------- #
# 创建层：热点来源随任务落库
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_create_task_persists_hotspot_source(monkeypatch):
    """热点路径：来源标题/关键词落库，style 按来源热点主题创建时即定"""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(pmod.hotspot_service, "match_themes", lambda hotspots: ["爱情"])

    async with factory() as db:
        db.add(_mk_poem())
        await db.commit()
        task = await pipeline_engine.create_task(
            db, 1, "douyin",
            source_hotspot_title="七夕情人节礼物攻略",
            source_keywords=["七夕", "情人节"],
        )
        assert task.source_hotspot_title == "七夕情人节礼物攻略"
        assert task.source_keywords_list == ["七夕", "情人节"]
        assert task.style == "情感治愈", "来源热点主题应在创建时定风格"

        # 诗词库路径：不传来源 → 两列为空 = 非热点任务
        task2 = await pipeline_engine.create_task(db, 1, "douyin")
        assert task2.source_hotspot_title is None
        assert task2.source_keywords_list == []
    await engine.dispose()


# ---------------------------------------------------------------- #
# 文案阶段：只注入任务自己的热点
# ---------------------------------------------------------------- #

@pytest.mark.asyncio()
async def test_script_stage_injects_only_task_source(script_env):
    """热点任务：注入的 keywords 必须等于任务落库的来源关键词（而非实时热榜）"""
    factory, captured = script_env
    async with factory() as db:
        db.add(_mk_poem())
        db.add(Task(id=1, poem_id=1, platform="douyin", status="pending",
                    source_hotspot_title="教师节送礼指南",
                    source_keywords=json.dumps(["教师节", "感恩老师"], ensure_ascii=False)))
        await db.commit()
        await pipeline_engine.run_stage(db, 1, "script")
    assert captured["keywords"] == ["教师节", "感恩老师"]


@pytest.mark.asyncio()
async def test_script_stage_no_source_injects_nothing(script_env):
    """诗词库路径/存量任务：来源为空 → 不注入任何热词（旧版这里抓全榜注入）"""
    factory, captured = script_env
    async with factory() as db:
        db.add(_mk_poem())
        db.add(Task(id=2, poem_id=1, platform="douyin", status="pending"))
        await db.commit()
        await pipeline_engine.run_stage(db, 2, "script")
    assert captured["keywords"] == []
