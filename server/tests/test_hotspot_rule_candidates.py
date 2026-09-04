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


@pytest.mark.asyncio
async def test_fallback_does_not_occupy_top_slot(session, monkeypatch):
    """兜底只保证"入选"，不保证"排前"（2026-09-04 修正）。

    旧逻辑把 fame_ids 放在候选池头部（占位优先），而 _rule_top_by_score 的 rel()
    按候选索引给相关性分 → 兜底诗恒得高分 → 无论什么热点 Top1 都是同一首兜底诗
    （实测《李监宅》杜甫在"秋日登高望远"与"中秋月圆夜"两个无关热点里均排 Top1）。
    """
    session.add(Poem(id=1, title="秋日登高", author="张三", dynasty="唐", content="秋日"))
    session.add(Poem(id=2, title="名家代表作", author="李白", dynasty="唐", content="无关联"))
    session.add(PoemTerm(poem_id=1, term="秋日", position=""))  # 仅 poem1 命中检索词
    await session.commit()

    # 构造名望表：李白 S 档 → 其作品进兜底池
    async def fake_fame(db):
        return {"李白": (95.0, "normal")}

    monkeypatch.setattr(hotspot, "_get_fame_table", fake_fame)

    ids = await hotspot_service._rule_candidates(session, ["秋日"], limit=200)

    assert 1 in ids, "主路命中的主题相关诗必须入选"
    assert 2 in ids, "兜底诗仍须入选（保住'检索词跑偏时名家兜底'的原意）"
    assert ids.index(1) < ids.index(2), (
        f"兜底诗不应排在主路命中诗之前，否则热点相关性被兜底淹没；实际顺序 {ids}"
    )


@pytest.mark.asyncio
async def test_expanded_terms_excluded_when_precise_pool_full(session):
    """2026-09-04 问题7（两阶段）守护：扩展主题词只做补位，不进主路池。
    精确词已填满候选池时，仅命中扩展词的无关诗被排除；精确词诗必在池中。

    构造（模拟 '苏轼《定风波》走红' 污染场景）：
      poem1 仅命中扩展词 '人生感悟'(0.3)
      poem2 命中精确词 '定风波'(1.0)
      limit=200，定风波池远未满 → poem1 不应入选（扩展词不污染主路）
    """
    session.add(Poem(id=1, title="无关杂记", author="张三", dynasty="宋", content="x"))
    session.add(Poem(id=2, title="定风波词", author="李四", dynasty="宋", content="y"))
    session.add(PoemTerm(poem_id=1, term="人生感悟", position=""))  # 扩展词
    session.add(PoemTerm(poem_id=2, term="定风波", position=""))    # 精确词
    await session.commit()

    # limit=1 模拟"精确词池已满"：定风波(精确) 占满唯一名额，扩展词不补位
    ids = await hotspot_service._rule_candidates(
        session, {"定风波": 1.0, "人生感悟": 0.3}, limit=1
    )
    assert 2 in ids, "精确词诗必须入选"
    assert 1 not in ids, "仅命中扩展词的无关诗在主路池已满时不应入选（扩展词仅补位）"


@pytest.mark.asyncio
async def test_expanded_terms_fill_gap_when_precise_short(session):
    """2026-09-04 问题7（两阶段）守护：精确词召回不足 limit 时，扩展词补位生效。

    构造：仅扩展词 '人生感悟'(0.3) 命中 poem1，无精确词 → 阶段1 空，阶段2 补位取 poem1。
    """
    session.add(Poem(id=1, title="泛主题诗", author="张三", dynasty="宋", content="x"))
    session.add(PoemTerm(poem_id=1, term="人生感悟", position=""))
    await session.commit()

    # 全扩展词（无精确词）→ 走补位通道
    ids = await hotspot_service._rule_candidates(session, {"人生感悟": 0.3}, limit=200)
    assert 1 in ids, "精确词缺失时扩展词补位应仍能召回候选（保住泛主题兜底）"


@pytest.mark.asyncio
async def test_group_id_dedup_keeps_one_per_group(session):
    """2026-09-04 问题4 守护：同 group_id 组诗仅保留池中 1 首，消除同质重复占位。

    构造：《秋兴八首》其一/其二 同 group_id=99 均命中 '秋兴'；独立诗 group_id=None。
    期望 group_id=99 在返回中仅出现 1 次（其一在前、其二被去重），独立诗不受影响。
    """
    session.add(Poem(id=1, title="秋兴八首其一", author="杜甫", dynasty="唐", content="秋兴", group_id=99))
    session.add(Poem(id=2, title="秋兴八首其二", author="杜甫", dynasty="唐", content="秋兴", group_id=99))
    session.add(Poem(id=3, title="独立诗", author="李白", dynasty="唐", content="秋兴", group_id=None))
    session.add_all([
        PoemTerm(poem_id=1, term="秋兴", position=""),
        PoemTerm(poem_id=2, term="秋兴", position=""),
        PoemTerm(poem_id=3, term="秋兴", position=""),
    ])
    await session.commit()
    ids = await hotspot_service._rule_candidates(session, ["秋兴"], limit=200)
    grp99 = [pid for pid in ids if pid in (1, 2)]
    assert len(grp99) == 1, f"同 group_id(99) 组诗应仅保留 1 首，实际 {grp99}"
    assert 3 in ids, "独立诗(group_id=None) 不受影响必须入选"
