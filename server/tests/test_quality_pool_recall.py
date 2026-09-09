"""推荐质量池消费守护测试（2026-09-09 问题 4b-2）

_rule_candidates 三阶段召回按 poems.quality_score >= settings.poem_quality_threshold
过滤（源头打标）；严格召回不足 _QUALITY_RELAX_MIN(10) 时降级全库补齐保可用性。
"""
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.database import Base
from app.models import Poem, PoemTerm
from app.services.hotspot import hotspot_service


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _seed(session, n_quality: int):
    """n_quality 首高质量诗 + 1 首垃圾诗，全部命中词"月亮"（垃圾诗命中更多次）。"""
    for i in range(n_quality):
        session.add(Poem(id=i + 1, title=f"名篇{i}", author="苏轼", dynasty="宋",
                         content="明月几时有", quality_score=90))
        session.add(PoemTerm(poem_id=i + 1, term="月亮", position=""))
    # 垃圾诗：当代打油诗，命中 3 次（字面更"相关"）
    session.add(Poem(id=999, title="月球移民指南", author="网红丙", dynasty="当代",
                     content="月亮月亮月亮", quality_score=0))
    session.add_all([
        PoemTerm(poem_id=999, term="月亮", position=""),
        PoemTerm(poem_id=999, term="月亮", position=""),
    ])
    await session.commit()


@pytest.mark.asyncio
async def test_quality_gate_excludes_junk_when_pool_sufficient(session, monkeypatch):
    """质量池召回充足（>=10）时，垃圾诗（quality=0）不得进候选池。"""
    monkeypatch.setattr(settings, "poem_quality_threshold", 55)
    await _seed(session, n_quality=12)

    ids = await hotspot_service._rule_candidates(session, ["月亮"], limit=200)

    assert 999 not in ids, "quality=0 的当代打油诗不应进入质量池候选"
    assert len(ids) >= 10


@pytest.mark.asyncio
async def test_quality_gate_relaxes_when_pool_insufficient(session, monkeypatch):
    """质量池召回不足（<10）时降级全库补齐：垃圾诗允许进池（保可用性）。"""
    monkeypatch.setattr(settings, "poem_quality_threshold", 55)
    await _seed(session, n_quality=2)

    ids = await hotspot_service._rule_candidates(session, ["月亮"], limit=200)

    assert 999 in ids, "质量池不足降级后应能补齐（否则冷门词零召回）"


@pytest.mark.asyncio
async def test_quality_gate_disabled_by_zero_threshold(session, monkeypatch):
    """threshold=0 关闭质量池，行为回退全库召回。"""
    monkeypatch.setattr(settings, "poem_quality_threshold", 0)
    await _seed(session, n_quality=12)

    ids = await hotspot_service._rule_candidates(session, ["月亮"], limit=200)

    assert 999 in ids
