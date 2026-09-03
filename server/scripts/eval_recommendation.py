"""离线评估：base（hits-only）vs score（多维度）在 12 条 fallback hotspot 上的对比。

运行：
    cd server && .venv/Scripts/python scripts/eval_recommendation.py

输出每个 hotspot 标题下两种 top-3 推荐（朝代/作者/分值），
最后输出整体朝代分布对比，便于人工审核与拍板。
"""
from __future__ import annotations

import asyncio
import sys
import os
import json
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.database import async_session_factory
from app.models.poem import Poem
from app.services.hotspot import hotspot_service, FALLBACK_HOTSPOTS
from app.services.recommend_scoring import (
    PoemLike, score_poem, rank_by_score,
    diverse_top_k, dynasty_bucket,
    build_author_prestige_table,
)


def flat_hotspots() -> list[dict]:
    """将 FALLBACK_HOTSPOTS 展开为 (title, platform) 列表"""
    out = []
    for plat, items in FALLBACK_HOTSPOTS.items():
        for it in items[:5]:
            out.append({"title": it["title"], "hot": it["hot"], "platform": plat})
    return out


def to_poem_like(poem: Poem) -> PoemLike:
    return PoemLike(
        id=poem.id,
        title=poem.title or "",
        author=poem.author or "",
        dynasty=poem.dynasty or "",
        content=poem.content or "",
    )


async def main(limit_candidates: int = 200):
    """跑离线评估"""
    hotspots = flat_hotspots()
    print(f"Eval 模式：{len(hotspots)} 条 fallback hotspot × 倒排表 Top-{limit_candidates} 候选")
    print("=" * 80)
    cur = date(2026, 9, 3)

    base_results = []   # (title, [(poem_dict), ...])
    score_results = []  # (title, [(poem_dict, score), ...])

    async with async_session_factory() as db:
        # 1) 启动期构造作者权威表（一次）
        prest_table = await build_author_prestige_table(db)
        print(f"作者权威表: 共 {len(prest_table)} 位作者，"
              f"Top5 入选数：{sorted(prest_table.items(), key=lambda x: -x[1])[:5]}")
        print("=" * 80)

        for h in hotspots:
            title = h["title"]
            kws = hotspot_service._keywords_to_terms(title)
            cand_ids = await hotspot_service._rule_candidates(db, kws, limit=limit_candidates)
            if not cand_ids:
                continue
            # 拉详情
            stmt = select(Poem).where(Poem.id.in_(cand_ids))
            poems = (await db.execute(stmt)).scalars().all()
            candidates = [to_poem_like(p) for p in poems]

            # ---- base：按倒排表 hits 数降序取 Top-3（与历史行为一致）----
            base_top = []
            # 简化：base 用顺序（_rule_candidates 已经按 hits 降序）
            for p in candidates[:3]:
                base_top.append(p)

            # ---- new：按多维度评分排序 ----
            # relevance 用 (rank 反序归一化)：rank=0 → 1.0，rank=N-1 → 0.5
            n = len(candidates)
            def rel(p: PoemLike) -> float:
                idx = next((i for i, q in enumerate(candidates) if q.id == p.id), n)
                return max(0.5, 1.0 - idx / n * 0.5)
            new_top = diverse_top_k(candidates, title=title, current=cur,
                                   prest_table=prest_table, relevance_fn=rel, top_k=3)

            print(f"\n📌 [{h['platform']}] {title}")
            print(f"   检索词: {kws[:6]} (命中 {len(candidates)} 首)")
            print(f"   ── BASELINE（按 hit 数）──")
            for p in base_top:
                b = dynasty_bucket(p.dynasty, p.author)
                print(f"     [{b[:3]}]《{p.title}》 {p.author} ({p.dynasty})")
            print(f"   ── NEW (多样性 + 多维分) ──")
            for p, sc in new_top:
                b = dynasty_bucket(p.dynasty, p.author)
                print(f"     [{b[:3]}]《{p.title}》 {p.author} ({p.dynasty})  score={sc:.2f}")

            base_results.append((title, base_top))
            score_results.append((title, [(p, s) for p, s in new_top]))

    # ===== 汇总 =====
    print()
    print("=" * 80)
    print("汇总：朝代 / 作者 Top-15 分布")
    print("=" * 80)

    def dist(rs, label):
        dyn = Counter(); aut = Counter()
        for _, items in rs:
            for it in (items if isinstance(items[0], tuple) else [(p, None) for p in items]):
                p = it[0]
                dyn[p.dynasty or "未知"] += 1
                aut[p.author or "未知"] += 1
        print(f"\n[{label}] 朝代分布:")
        for d, n in dyn.most_common(15):
            print(f"   {n:>3}  {d}")
        print(f"\n[{label}] 作者 Top-15:")
        for a, n in aut.most_common(15):
            print(f"   {n:>3}  {a}")

    dist(base_results, "BASELINE")
    dist(score_results, "NEW 多维评分")


if __name__ == "__main__":
    asyncio.run(main())
