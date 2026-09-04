"""_rule_candidates 回归守护：主路召回 + golden 金句加权（真实内存 SQLite）。

背景（2026-09-04 端到端验收发现的两个失效点）：

1. **主路静默失效（P0）**：P2 加权代码使用了 sqlalchemy 的 `case(...)`，
   但模块顶部只导入了 `select, func, text`，漏了 `case` → 主路查询每次抛
   NameError，被 `try/except` 捕获后仅 logger.warning，primary_ids 变空，
   推荐整体退化为"纯名望兜底"，主题相关性全丢。且因异常被吞，无测试可发现。
   → 本文件用真实 SQL 执行守护"主路必须召回"。

2. **golden 加权恒不生效**：golden term 存的是整句（"长风破浪会有时，直挂云帆济沧海"），
   而 `_keywords_to_terms` 输出 jieba 碎片（"有时"/"长风破浪"/"沧海"），
   两者永不相等 → golden_hits 恒为 0。加权 SQL 逻辑正确但上游喂不进整句。
   → 本文件用人工构造的整句检索词守护"加权算法本身正确"（数据侧缺陷另案修复）。
"""
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models import Poem, PoemTerm
from app.services import hotspot
from app.services.hotspot import hotspot_service


@pytest.fixture
async def session():
    """内存库：建表 → 返回 session → 销毁。poets 表留空（fame 兜底自动降级）。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


def test_case_is_imported():
    """守护：`case` 必须在模块命名空间中可见。

    缺失时主路查询抛 NameError 并被 except 吞掉，表现为"推荐结果变差"而非报错，
    极难定位。这条断言把静默失效变成显式的红灯。
    """
    assert hasattr(hotspot, "case"), (
        "hotspot 模块缺少 sqlalchemy `case` 导入 → _rule_candidates 主路查询会抛 "
        "NameError 并被 try/except 静默吞掉，导致主路召回失效、推荐退化为名望兜底"
    )


@pytest.mark.asyncio
async def test_main_path_returns_inverted_hits(session):
    """主路倒排必须召回命中诗（守护 case 链路完整）。"""
    session.add(Poem(id=1, title="秋日登高", author="张三", dynasty="唐", content="秋日登高"))
    session.add(Poem(id=2, title="无关诗", author="李四", dynasty="唐", content="春花秋月"))
    # poem1 命中 2 词；poem2 命中 1 词（"秋月"不匹配，故仅作对照）
    session.add_all([
        PoemTerm(poem_id=1, term="登高", position=""),
        PoemTerm(poem_id=1, term="望远", position=""),
        PoemTerm(poem_id=2, term="望远", position=""),
    ])
    await session.commit()

    ids = await hotspot_service._rule_candidates(session, ["登高", "望远"], limit=200)

    # 主路必须召回（若 case 缺失，这里会因 except 吞异常而只剩兜底/空）
    assert 1 in ids, "主路倒排未召回 poem1 —— 检查 case 导入与 SQL 是否抛异常被吞"
    assert 2 in ids


@pytest.mark.asyncio
async def test_golden_term_weighted_3x(session):
    """golden 命中加权 3x：score = hits + golden_hits*3。

    构造：
      poem1 命中 2 个普通词          → hits=2, golden=0 → score=2
      poem2 命中 1 普通 + 1 golden   → hits=2, golden=1 → score=5
    期望 poem2 排在 poem1 之前。
    """
    session.add(Poem(id=1, title="普通诗", author="张三", dynasty="唐", content="x"))
    session.add(Poem(id=2, title="金句诗", author="李四", dynasty="宋", content="y"))
    session.add_all([
        PoemTerm(poem_id=1, term="登高", position=""),
        PoemTerm(poem_id=1, term="望远", position=""),
        PoemTerm(poem_id=2, term="登高", position=""),
        # 整句金句：真实链路需热点标题原样引用才命中（见模块 docstring 失效点 2）
        PoemTerm(poem_id=2, term="长风破浪会有时，直挂云帆济沧海", position="g"),
    ])
    await session.commit()

    ids = await hotspot_service._rule_candidates(
        session, ["登高", "望远", "长风破浪会有时，直挂云帆济沧海"], limit=200
    )

    assert 2 in ids and 1 in ids
    assert ids.index(2) < ids.index(1), (
        f"golden 加权未生效：金句诗(2) 应排在普通诗(1) 之前，实际 {ids}"
    )


@pytest.mark.asyncio
async def test_empty_keywords_returns_empty(session):
    """检索词为空 → 直接返回空（不查库）。"""
    assert await hotspot_service._rule_candidates(session, [], limit=200) == []
