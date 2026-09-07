"""热点推荐多维度评分单元测试（纯函数，零 DB 依赖）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import date
from app.services.recommend_scoring import (
    PoemLike, score_poem, rank_by_score, diverse_top_k,
    dynasty_weight, author_prestige, classic_quote_score,
    seasonal_bonus, NEAR_MODERN_WHITELIST, build_author_prestige_table,
    fame_prestige, is_pseudo_author, build_author_fame_table,
    FAME_WEIGHT_BANDS, FAME_WEIGHT_FLOOR, FAME_WEIGHT_PSEUDO,
)


class TestDynastyWeight:
    """朝代权重"""

    def test_top_dynasty_high(self):
        assert dynasty_weight("唐") == 1.30
        assert dynasty_weight("北宋") == 1.30
        assert dynasty_weight("南宋") == 1.30

    def test_classical_dynasty_high(self):
        assert dynasty_weight("先秦") == 1.30
        assert dynasty_weight("汉") == 1.20
        assert dynasty_weight("魏晋") == 1.20

    def test_mid_dynasty_mid(self):
        assert dynasty_weight("明") == 0.85
        assert dynasty_weight("清") == 0.80

    def test_mixed_dynasty_mid(self):
        assert dynasty_weight("明末清初") == 0.65
        assert dynasty_weight("清末民国初") == 0.55

    def test_near_modern_low(self):
        assert dynasty_weight("当代") == 0.15
        assert dynasty_weight("近现代") == 0.20
        assert dynasty_weight("现当代") == 0.15

    def test_near_modern_whitelist_overrides(self):
        """白名单作者的近现代诗应被加权"""
        for author in ("毛泽东", "徐志摩", "海子"):
            w = dynasty_weight("当代", author)
            assert w >= 0.65, f"{author} 应当 ≥0.65, got {w}"

    def test_unknown_dynasty_fallback(self):
        assert dynasty_weight("外星") == 0.40  # 未匹配兜底
        assert dynasty_weight("") == 0.40
        assert dynasty_weight(None) == 0.40

    def test_substring_match_fallback(self):
        """子串兜底：'宋中期' 这种未具名朝代应能命中宋"""
        assert dynasty_weight("宋中期") >= 1.20  # ≥宋权重


class TestAuthorPrestige:
    """作者权威"""

    def test_no_author_low(self):
        assert author_prestige(100, "") == 0.55
        assert author_prestige(100, None) == 0.55
        assert author_prestige(100, "无名氏") == 0.55

    def test_high_count_top_author(self):
        """苏轼/陆游级别（5000+）"""
        assert author_prestige(7866, "苏轼") == 1.25
        assert author_prestige(10000, "陆游") == 1.25

    def test_mid_count_author(self):
        assert author_prestige(500, "某诗人") == 0.95
        assert author_prestige(2000, "某诗人") == 1.10

    def test_low_count_author(self):
        assert author_prestige(10, "冷门") == 0.70
        assert author_prestige(0, "无名") == 0.70  # 0 走长尾路径

    def test_mid_range(self):
        assert author_prestige(100, "某诗") == 0.80


class TestClassicQuote:
    """金句度启发式"""

    def test_very_short_high(self):
        """<30 字偏经典（千古绝句）"""
        assert classic_quote_score("床前明月光疑是地上霜举头望明月", "静夜思") == 1.15 + 0.15

    def test_no_content_neutral(self):
        assert classic_quote_score("") == 0.80

    def test_long_low(self):
        """长篇古文启发式略低分"""
        assert classic_quote_score("x" * 600, "长诗") == 0.85

    def test_classic_title_bonus(self):
        """标题命中水调歌头等词牌名加分"""
        assert classic_quote_score("明月几时有", "水调歌头·明月几时有") >= 1.15
        assert classic_quote_score("x" * 50, "登鹳雀楼") >= 1.15


class TestSeasonalBonus:
    """节令加成"""

    def test_empty_no_bonus(self):
        assert seasonal_bonus("") == 0.0
        assert seasonal_bonus("随便") == 0.0

    def test_mid_autumn_high_bonus(self):
        """中秋节当周加成最大"""
        cur = date(2026, 9, 17)  # 2026 中秋公历
        assert seasonal_bonus("中秋月圆夜", cur) >= 0.30

    def test_off_season_partial_bonus(self):
        """非节令日期仍给 partial（标题命中词）"""
        cur = date(2026, 6, 1)  # 夏至
        s = seasonal_bonus("中秋月圆夜", cur)
        # 距离 9/17 远，仍有标题基础项 ~0.15，但 seasonal 加成较小
        assert 0.0 < s <= 0.55

    def test_imagery_overlap(self):
        """多意象叠加有上界 0.55"""
        cur = date(2026, 1, 1)
        s = seasonal_bonus("月雪寒冬酒江寺怀古登高", cur)
        assert s <= 0.55


class TestScorePoem:
    """总评分核心"""

    PREST = {
        "苏轼": 7866, "李白": 5000, "陆游": 10176,
        "卢青山": 50, "邵祖平": 30, "毛泽东": 8,
    }

    def test_li_bai_high(self):
        """李白 + 静夜思 应得最高分"""
        p = PoemLike(id=1, title="静夜思", author="李白", dynasty="唐",
                     content="床前明月光疑是地上霜举头望明月低头思故乡")
        sc = score_poem(p, title="中秋月圆夜",
                       current=date(2026, 9, 17), relevance=0.9,
                       prest_table=self.PREST)
        assert sc >= 1.5

    def test_su_shi_high(self):
        """苏轼 + 水调歌头"""
        p = PoemLike(id=2, title="水调歌头", author="苏轼", dynasty="北宋",
                     content="明月几时有把酒问青天不知天上宫阙今夕是何年我欲乘风归去")
        sc = score_poem(p, title="苏轼《定风波》走红", relevance=0.9,
                       prest_table=self.PREST)
        assert sc >= 1.4

    def test_lu_qingshan_low(self):
        """当代卢青山打油诗应远低于李杜"""
        p = PoemLike(id=3, title="无题", author="卢青山", dynasty="当代",
                     content="x" * 100)
        sc = score_poem(p, title="古风音乐火了", relevance=0.9,
                       prest_table=self.PREST)
        assert sc < 0.5, f"当代不应高过此阈值，得{sc}"

    def test_mao_zedong_whitelist(self):
        """毛泽东近现代白名单显著高于普通当代"""
        p_mao = PoemLike(id=10, title="沁园春·雪", author="毛泽东", dynasty="当代",
                         content="北国风光千里冰封万里雪飘")
        p_other = PoemLike(id=11, title="无题", author="张三", dynasty="当代",
                           content="x" * 50)
        sc_mao = score_poem(p_mao, title="秋日登高", prest_table=self.PREST)
        sc_other = score_poem(p_other, title="秋日登高", prest_table=self.PREST)
        assert sc_mao > sc_other * 2

    def test_seasonal_match_promotes(self):
        """节令匹配的诗明显加分"""
        p_match = PoemLike(id=1, title="水调歌头", author="苏轼", dynasty="北宋",
                           content="明月几时有")
        p_nomatch = PoemLike(id=2, title="春望", author="杜甫", dynasty="唐",
                            content="国破山河在")
        cur = date(2026, 9, 17)  # 中秋节
        sc_mid = score_poem(p_match, title="中秋月圆夜", current=cur,
                           prest_table=self.PREST)
        sc_spring = score_poem(p_match, title="春天花开", current=cur,
                              prest_table=self.PREST)
        assert sc_mid > sc_spring


class TestRankByScore:
    """排序接口"""

    PREST = {"苏轼": 7866, "陆游": 10176, "卢青山": 50, "邵祖平": 30}

    def test_top_k_returns_tuple(self):
        cands = [
            PoemLike(id=1, title="静夜思", author="李白", dynasty="唐", content="短"),
            PoemLike(id=2, title="无题", author="卢青山", dynasty="当代", content="x" * 300),
            PoemLike(id=3, title="登鹳雀楼", author="王之涣", dynasty="唐", content="短"),
        ]
        result = rank_by_score(cands, title="中秋月圆夜", current=date(2026, 9, 17),
                              prest_table=self.PREST, top_k=2)
        assert len(result) == 2
        for tup in result:
            assert hasattr(tup[0], "id") and isinstance(tup[1], float)
        # Top-1 应为李白/王之涣（非当代）
        assert result[0][0].author != "卢青山"

    def test_top_k_filtered(self):
        """top_k 大于候选数时返回全部"""
        cands = [PoemLike(id=i, title=f"诗{i}", author="李白", dynasty="唐",
                          content="x" * 50) for i in range(3)]
        r = rank_by_score(cands, title="月夜", current=date.today(),
                         prest_table=self.PREST, top_k=10)
        assert len(r) == 3


class TestBuildAuthorPrestigeTable:
    """作者权威表异步构造（需要内存 DB 集成测试）

    真实 poems.db 太大不便于每次跑，单元测试仅校验异步签名 + 类型契约。
    """

    @pytest.mark.asyncio
    async def test_signature_returns_dict(self):
        """仅验证异步签名（实际查询需要的 poetry DB 不在测试范围内）"""
        from unittest.mock import MagicMock
        fake_db = MagicMock()

        class FakeResult:
            def fetchall(self):
                return [("苏轼", 7866), ("李白", 5000), ("无名氏", 4838)]

        async def fake_execute(stmt):
            return FakeResult()

        fake_db.execute = fake_execute
        result = await build_author_prestige_table(fake_db)
        assert isinstance(result, dict)
        assert result["苏轼"] == 7866
        assert result["无名氏"] == 4838


class TestDynastyBucket:
    """朝代桶分类"""

    def test_top_classical_in_a(self):
        from app.services.recommend_scoring import dynasty_bucket
        assert dynasty_bucket("唐", "") == "A_top_classical"
        assert dynasty_bucket("北宋", "") == "A_top_classical"
        assert dynasty_bucket("先秦", "") == "A_top_classical"
        assert dynasty_bucket("魏晋", "") == "A_top_classical"

    def test_mid_classical_in_b(self):
        from app.services.recommend_scoring import dynasty_bucket
        assert dynasty_bucket("明", "") == "B_mid_classical"
        assert dynasty_bucket("清", "") == "B_mid_classical"
        assert dynasty_bucket("元", "") == "B_mid_classical"

    def test_mixed_in_c(self):
        from app.services.recommend_scoring import dynasty_bucket
        assert dynasty_bucket("明末清初", "") == "C_mixed_late"
        assert dynasty_bucket("清末民国初", "") == "C_mixed_late"

    def test_modern_whitelist_in_d(self):
        from app.services.recommend_scoring import dynasty_bucket
        assert dynasty_bucket("当代", "毛泽东") == "D_modern_whitelist"
        assert dynasty_bucket("近现代", "海子") == "D_modern_whitelist"

    def test_unknown_in_x(self):
        from app.services.recommend_scoring import dynasty_bucket
        assert dynasty_bucket("当代", "") == "X_fallback"
        assert dynasty_bucket("外星", "") == "X_fallback"
        assert dynasty_bucket(None, "") == "X_fallback"


class TestDiverseTopK:
    """多样性配额（核心：解决被高产当代诗人刷屏）"""

    PREST = {"苏轼": 7866, "陆游": 10176, "王之涣": 200, "李白": 5000,
             "杜甫": 500, "卢青山": 50, "邵祖平": 30}

    def test_default_quota_a_b_x(self):
        """默认 Top-3 应各取 A/B/X 各 1"""
        from app.services.recommend_scoring import diverse_top_k, dynasty_bucket
        cands = [
            PoemLike(id=1, title="静夜思", author="李白", dynasty="唐", content="短"),
            PoemLike(id=2, title="登鹳雀楼", author="王之涣", dynasty="唐", content="短"),
            PoemLike(id=3, title="出塞", author="杜甫", dynasty="唐", content="短"),
            PoemLike(id=4, title="江雪", author="柳宗元", dynasty="唐", content="短"),
            PoemLike(id=5, title="春日", author="朱熹", dynasty="南宋", content="短"),
            PoemLike(id=6, title="石灰吟", author="于谦", dynasty="明", content="短"),
            PoemLike(id=7, title="竹石", author="郑燮", dynasty="清", content="短"),
            PoemLike(id=8, title="无题", author="卢青山", dynasty="当代", content="短"),
            PoemLike(id=9, title="无题2", author="卢青山2", dynasty="当代", content="短"),
        ]
        result = diverse_top_k(cands, title="中秋月圆夜", current=date(2026, 9, 17),
                              prest_table=self.PREST, top_k=3)
        assert len(result) == 3
        buckets = [dynasty_bucket(p.dynasty, p.author) for p, _ in result]
        # 必须有 A 和 B（不允许 3 个同 bucket）；A/B/X 各 1
        assert "A_top_classical" in buckets, f"应含 A bucket，实际 {buckets}"
        assert "B_mid_classical" in buckets, f"应含 B bucket，实际 {buckets}"

    def test_modern_only_filtered_out(self):
        """若候选全是当代非白名单，应退化但仍按 score 排序返回 top_k"""
        from app.services.recommend_scoring import diverse_top_k
        cands = [
            PoemLike(id=i, title=f"无题{i}", author=f"卢某{i}", dynasty="当代",
                    content="x" * 50) for i in range(10)
        ]
        result = diverse_top_k(cands, title="古风音乐", prest_table={}, top_k=3)
        # 退化路径：仍返回 3 个
        assert len(result) == 3

    def test_quota_with_whitelist(self):
        """配额包含 D (modern_whitelist) 时，应能拿到白名单作者"""
        from app.services.recommend_scoring import diverse_top_k, dynasty_bucket
        cands = [
            PoemLike(id=1, title="沁园春", author="毛泽东", dynasty="当代", content="短"),
            PoemLike(id=2, title="静夜思", author="李白", dynasty="唐", content="短"),
            PoemLike(id=3, title="江雪", author="柳宗元", dynasty="唐", content="短"),
            PoemLike(id=4, title="竹石", author="郑燮", dynasty="清", content="短"),
        ]
        result = diverse_top_k(cands, title="秋日", current=date.today(),
                              prest_table=self.PREST,
                              quota={"A_top_classical": 1, "B_mid_classical": 1,
                                     "X_fallback": 0, "D_modern_whitelist": 1},
                              top_k=3)
        assert len(result) == 3
        # 应含毛泽东
        authors = [p.author for p, _ in result]
        assert "毛泽东" in authors

    def test_author_dedup(self):
        """同作者多首候选时仅入选一次（防高产当代诗人刷屏）"""
        from app.services.recommend_scoring import diverse_top_k
        cands = [
            PoemLike(id=1, title="诗1", author="李彦迪", dynasty="明", content="短"),
            PoemLike(id=2, title="诗2", author="李彦迪", dynasty="明", content="短"),
            PoemLike(id=3, title="诗3", author="李彦迪", dynasty="明", content="短"),
            PoemLike(id=4, title="诗4", author="李彦迪", dynasty="明", content="短"),
            PoemLike(id=5, title="诗5", author="李白", dynasty="唐", content="短"),
            PoemLike(id=6, title="诗6", author="苏轼", dynasty="北宋", content="短"),
            PoemLike(id=7, title="诗7", author="柳宗元", dynasty="唐", content="短"),
        ]
        result = diverse_top_k(cands, title="秋日", prest_table=self.PREST, top_k=4)
        # 4 名作者应各不相同
        authors = [p.author for p, _ in result]
        assert len(set(authors)) == len(authors), f"作者刷屏: {authors}"

    def test_targeted_hotspot_picks_target_poem(self):
        """AC1 定向热点命中目标：同 A 桶内连续相关性决胜，名望不压过主题。

        复现问题：苏轼《定风波》走红 → 兜底 Top1 是《送张五归山》王维（名望分压过）。
        构造：王维《登高》标题命中经典词牌 bonus → score 更高；但苏轼《定风波》rel 更高。
        修复后：rel=1.0 的苏轼《定风波》应压过 rel=0.95 的王维（即使王维 score 更高）。
        """
        def rel_fn(p):
            return {1: 1.0, 2: 0.95}[p.id]  # id=1 苏轼定风波（池序最前），id=2 王维
        cands = [
            PoemLike(id=1, title="定风波", author="苏轼", dynasty="北宋", content="莫听穿林打叶声何妨吟啸且徐行"),
            PoemLike(id=2, title="登高", author="王维", dynasty="盛唐", content="短诗"),  # 登高→classic+0.15
        ]
        ft = {"苏轼": (100.0, "normal"), "王维": (100.0, "normal")}
        result = diverse_top_k(cands, title="苏轼《定风波》走红", fame_table=ft,
                               relevance_fn=rel_fn, top_k=3)
        assert result and result[0][0].id == 1, \
            f"定向热点 Top1 应为目标诗《定风波》(id=1)，实际 {[(p.id, p.author) for p, _ in result]}"

    def test_tier1_does_not_force_quota_slot(self):
        """AC2 异朝代不挤占：B/X 桶仅 tier-1（无 tier-2）候选时，配额释放给 A 桶。

        复现问题：默认配额 A:1/B:1/X:1 把 B/X 桶的 tier-1 无关诗强塞进 Top-K。
        修复后：配额阶段仅接受 tier-2；B/X 无 tier-2 → 缺额由 A 桶 tier-2 补位。
        """
        def rel_fn(p):
            return {1: 1.0, 2: 0.95, 3: 0.90, 4: 0.75, 5: 0.70}[p.id]
        cands = [
            PoemLike(id=1, author="苏轼", dynasty="北宋", content="短"),
            PoemLike(id=2, author="李白", dynasty="唐", content="短"),
            PoemLike(id=3, author="杜甫", dynasty="唐", content="短"),
            PoemLike(id=4, author="明人", dynasty="明", content="短"),   # B 桶 tier-1
            PoemLike(id=5, author="当代甲", dynasty="当代", content="短"),  # X 桶 tier-1
        ]
        result = diverse_top_k(cands, title="苏轼《定风波》走红", relevance_fn=rel_fn, top_k=3)
        picked_ids = [p.id for p, _ in result]
        assert 4 not in picked_ids and 5 not in picked_ids, \
            f"tier-1 无关诗不应强占配额: {picked_ids}"
        assert picked_ids == [1, 2, 3], f"Top3 应全为 A 桶相关诗: {picked_ids}"

    def test_diversity_kept_when_tier2_present(self):
        """AC3 宽泛热点保多样性：B/X 桶存在 tier-2 候选时，配额仍覆盖跨朝代。"""
        from app.services.recommend_scoring import diverse_top_k, dynasty_bucket

        def rel_fn(p):
            return {1: 1.0, 2: 0.95, 3: 0.90}[p.id]
        cands = [
            PoemLike(id=1, author="苏轼", dynasty="北宋", content="短"),   # A 桶 tier-2
            PoemLike(id=2, author="于谦", dynasty="明", content="短"),     # B 桶 tier-2
            PoemLike(id=3, author="当代甲", dynasty="当代", content="短"),  # X 桶 tier-2
        ]
        result = diverse_top_k(cands, title="中秋月圆夜", relevance_fn=rel_fn, top_k=3)
        buckets = [dynasty_bucket(p.dynasty, p.author) for p, _ in result]
        assert "A_top_classical" in buckets and "B_mid_classical" in buckets, \
            f"跨朝代多样性应保留: {buckets}"

    def test_relevance_continuous_beats_fame_same_tier(self):
        """AC4 名望退居 tie-break：同档位内连续 rel 决胜，rel 相同才名望决胜。

        id=1 rel=1.0（低名望）应胜 id=2 rel=0.95（高名望）——rel 差异优先于名望分。
        """
        def rel_fn(p):
            return {1: 1.0, 2: 0.95}[p.id]
        cands = [
            PoemLike(id=1, author="二流唐人", dynasty="唐", content="短"),
            PoemLike(id=2, author="唐代名家", dynasty="唐", content="短"),
        ]
        ft = {"二流唐人": (10.0, "normal"), "唐代名家": (95.0, "normal")}
        result = diverse_top_k(cands, title="秋日", fame_table=ft,
                               relevance_fn=rel_fn, top_k=1)
        assert result and result[0][0].id == 1, \
            f"同档内高 rel 低名望应胜: {[(p.id, p.author) for p, _ in result]}"


class TestFameIntegration:
    """§8 接入：poets.fame_score → 作者权威权重 + 伪作者硬过滤。

    核心不变量（设计文档 §8 + §9.1）：
    1. 名望走「软加权」（0.45~1.35），不按 fame 档位硬过滤候选池；
       硬过滤只针对非 normal 的伪作者/神话/域外/散文。
    2. 高产≠名望：弘历 4.3 万行但 fame 14 → 权重 0.65，低于苏轼 fame 100 → 1.35。
    3. 中立性：非 normal 无论 fame 多高，一律压到 0.45（不评好坏）。
    """

    def _fame(self):
        # 模拟 build_author_fame_table 产出的索引（含异体 variants）
        return {
            "苏轼": (100.0, "normal"),
            "杜甫": (95.0, "normal"),
            "李白": (100.0, "normal"),
            "弘历": (14.0, "normal"),
            "查慎行": (100.0, "normal"),
            "查愼行": (100.0, "normal"),   # 异体，应命中查慎行同分
            "玄女": (5.0, "myth"),
            "赵冕镐": (14.0, "foreign"),
        }

    def test_fame_prestige_bands_cover_all_levels(self):
        """fame 分档 → 权重：S 1.35 / A 1.25 / B 1.15 / C 1.00 / D 0.85 / E 0.65"""
        assert fame_prestige(100.0, "normal") == 1.35
        assert fame_prestige(85.0, "normal") == 1.35   # S 边界
        assert fame_prestige(70.0, "normal") == 1.25   # A 边界
        assert fame_prestige(55.0, "normal") == 1.15   # B 边界
        assert fame_prestige(40.0, "normal") == 1.00   # C 边界
        assert fame_prestige(25.0, "normal") == 0.85   # D 边界
        assert fame_prestige(14.0, "normal") == FAME_WEIGHT_FLOOR   # E 档 → 0.65
        assert fame_prestige(0.0, "normal") == FAME_WEIGHT_FLOOR
        # 档位上限与下限常量自检
        assert FAME_WEIGHT_FLOOR < FAME_WEIGHT_BANDS[-1][1]  # 0.65 < 0.85
        assert FAME_WEIGHT_PSEUDO < FAME_WEIGHT_FLOOR         # 0.45 < 0.65

    def test_fame_prestige_pseudo_forced_low_regardless_of_fame(self):
        """非 normal：无论 fame 多高一律 0.45（中立性铁律，不评好坏）"""
        assert fame_prestige(100.0, "myth") == FAME_WEIGHT_PSEUDO
        assert fame_prestige(90.0, "foreign") == FAME_WEIGHT_PSEUDO
        assert fame_prestige(50.0, "prose") == FAME_WEIGHT_PSEUDO
        assert fame_prestige(80.0, "emperor") == FAME_WEIGHT_PSEUDO

    def test_fame_prestige_none_means_fallback(self):
        """无诗人实体（fame=None, normal）→ None，调用方退回入诗数启发。

        但非 normal 的 category 即使 fame=None 也强制 0.45：防御性设计——
        若返回 None，author_prestige 会退回「入诗数启发」，可能把伪作者
        错配成高权重。故非 normal 永远压低。
        """
        assert fame_prestige(None, "normal") is None
        assert fame_prestige(None, "myth") == FAME_WEIGHT_PSEUDO
        assert fame_prestige(None, "foreign") == FAME_WEIGHT_PSEUDO

    def test_is_pseudo_author_classification(self):
        """候选池硬约束：非 normal 永不进池；normal/缺失放行"""
        ft = self._fame()
        assert is_pseudo_author("玄女", ft) is True
        assert is_pseudo_author("赵冕镐", ft) is True
        assert is_pseudo_author("苏轼", ft) is False
        assert is_pseudo_author("李白", ft) is False
        # 查不到的实体视为 normal（不放行过滤）
        assert is_pseudo_author("某个生僻作者", ft) is False
        assert is_pseudo_author("", ft) is False
        assert is_pseudo_author(None, ft) is False
        assert is_pseudo_author("苏轼", None) is False  # 无表则不过滤

    def test_author_prestige_prefers_fame_over_rowcount(self):
        """author_prestige：fame 优先，高产≠名望因此体现"""
        # 苏轼 7866 行（旧 1.25）→ fame 100 → 1.35
        assert author_prestige(7866, "苏轼", fame=100.0, category="normal") == 1.35
        # 弘历 43290 行（旧 1.25）→ fame 14 → 0.65（机制压制，非硬压 category）
        assert author_prestige(43290, "弘历", fame=14.0, category="normal") == 0.65
        # 玄女 myth → 0.45（即使有 fame=5）
        assert author_prestige(3, "玄女", fame=5.0, category="myth") == FAME_WEIGHT_PSEUDO
        # 无 fame 记录 → 退回入诗数启发（弘历若无实体会得 1.25，体现 fallback 差异）
        assert author_prestige(43290, "弘历") == 1.25

    def test_score_poem_fame_soft_weighting(self):
        """score_poem：同内容下，名望软加权决定排序，且低 fame 仍高于匿名"""
        content = "床前明月光疑是地上霜举头望明月低头思故乡"
        sushi = PoemLike(id=1, title="X", author="苏轼", dynasty="北宋", content=content)
        hongli = PoemLike(id=2, title="X", author="弘历", dynasty="清", content=content)
        wuming = PoemLike(id=3, title="X", author="无名氏", dynasty="清", content=content)
        ft = self._fame()
        s = score_poem(sushi, "X", fame_table=ft)
        h = score_poem(hongli, "X", fame_table=ft)
        w = score_poem(wuming, "X", fame_table=ft)   # 无名氏不在 fame 表 → 退回 0.55
        assert s > h > w, f"名望软加权应 苏轼>{hongli}>无名氏: {s:.3f}/{h:.3f}/{w:.3f}"

    def test_diverse_top_k_excludes_pseudo_authors(self):
        """diverse_top_k：非 normal 作者硬过滤出候选池，且不致池空"""
        cands = [
            PoemLike(id=1, author="玄女", dynasty="", content="x"),
            PoemLike(id=2, author="李白", dynasty="唐", content="x"),
            PoemLike(id=3, author="杜甫", dynasty="唐", content="x"),
        ]
        result = diverse_top_k(cands, title="test", fame_table=self._fame(),
                               exclude_pseudo=True, top_k=3)
        authors = [p.author for p, _ in result]
        assert "玄女" not in authors, f"伪作者漏进池: {authors}"
        assert len(result) >= 1, "过滤后不应空池"

    def test_diverse_top_k_fame_soft_rank_within_bucket(self):
        """同朝代 bucket 内：名望高者优先（软加权生效，非档位硬过滤）"""
        cands = [
            PoemLike(id=1, author="二流明人", dynasty="明", content="短"),
            PoemLike(id=2, author="明代名家", dynasty="明", content="短"),
        ]
        ft = {"二流明人": (10.0, "normal"), "明代名家": (90.0, "normal")}
        result = diverse_top_k(cands, title="秋日", fame_table=ft, top_k=1)
        assert result and result[0][0].author == "明代名家"

    def test_diverse_top_k_relevance_beats_fame_within_bucket(self):
        """2026-09-04 方案 B 回归守护：同 bucket 内相关性更高者优先于名望更高者。

        旧逻辑 score = dynasty×prestige×classic（三项相乘），名望乘积淹没相关性，
        导致《李监宅》杜甫（与热点无关）在多个无关热点 Top1。重构后 bucket 内按
        (-relevance_tier, -score) 排序：高相关低名望诗应胜过低相关高名望诗，
        名望降级为同档内的 tie-breaker。
        """
        def rel_fn(p):
            return {1: 0.5, 2: 1.0}[p.id]  # id=1 低相关，id=2 高相关
        cands = [
            PoemLike(id=1, author="李白", dynasty="唐", content="短"),       # 高名望、低相关
            PoemLike(id=2, author="二流唐人", dynasty="唐", content="短"),     # 低名望、高相关
        ]
        ft = {"李白": (95.0, "normal"), "二流唐人": (10.0, "normal")}
        result = diverse_top_k(cands, title="某热点", fame_table=ft,
                               relevance_fn=rel_fn, top_k=1)
        assert result and result[0][0].id == 2, \
            "高相关低名望诗应压过低相关高名望诗（方案 B：相关性决定'该不该来'）"

    def test_soft_quota_releases_tier0_bucket_slot(self):
        """2026-09-04 方案 B 延伸守护：某朝代桶无相关(tier-0)候选时，其保底配额释放，
        不把无关诗保送进 Top-K（避免《李监宅》杜甫式硬配额抢位）。

        构造：A 桶(唐)仅 1 首不相关(rel=0.5,tier0)；X 桶(当代)2 首高相关(rel≥0.9,tier2)。
        默认配额 A:1,B:1,X:1,top_k=3。旧逻辑会强制取 A 桶的无关杜甫进 Top；
        新逻辑跳过 tier-0 → A 配额释放 → Top-K 全来自 X 桶相关诗。
        """
        def rel_fn(p):
            return {1: 1.0, 2: 0.95, 3: 0.5}[p.id]
        cands = [
            PoemLike(id=1, author="当代甲", dynasty="当代", content="秋日登高望远"),
            PoemLike(id=2, author="当代乙", dynasty="当代", content="秋日登高望远"),
            PoemLike(id=3, author="杜甫", dynasty="唐", content="无关内容"),  # tier0
        ]
        result = diverse_top_k(cands, title="秋日登高望远", relevance_fn=rel_fn, top_k=3)
        picked_ids = [p.id for p, _ in result]
        assert 3 not in picked_ids, "tier-0 不相关杜甫不应进 Top-K（配额已释放）"
        assert set(picked_ids) <= {1, 2}, f"Top-K 应全为相关当代诗, got {picked_ids}"

    def test_diverse_top_k_no_fame_table_is_noop(self):
        """无 fame_table 时 diverse_top_k 行为与旧逻辑一致（不破坏召回）"""
        cands = [
            PoemLike(id=1, author="玄女", dynasty="", content="x"),
            PoemLike(id=2, author="李白", dynasty="唐", content="x"),
        ]
        # exclude_pseudo 但 fame_table=None → 不过滤
        result = diverse_top_k(cands, title="test", fame_table=None,
                               exclude_pseudo=True, top_k=3)
        authors = [p.author for p, _ in result]
        assert "玄女" in authors, "无 fame 表时伪作者不应被误删（保持旧召回）"

    @pytest.mark.asyncio
    async def test_build_author_fame_table_variants_and_samename_max(self):
        """build_author_fame_table：variants 索引 + 同名异人取最高分"""
        import tempfile, os
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.database import Base
        from app.models.poet import Poet

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            db.add(Poet(author="查慎行", author_variants='["查愼行"]',
                        fame_score=100.0, author_category="normal", dynasty="宋"))
            db.add(Poet(author="王珪", author_variants="[]",
                        fame_score=100.0, author_category="normal", dynasty="宋"))
            db.add(Poet(author="王珪", author_variants="[]",
                        fame_score=2.0, author_category="normal", dynasty="隋"))
            db.add(Poet(author="玄女", author_variants="[]",
                        fame_score=5.0, author_category="myth", dynasty=""))
            db.add(Poet(author=" 无名氏", author_variants="[]",
                        fame_score=5.0, author_category="placeholder", dynasty=""))
            await db.commit()
            table = await build_author_fame_table(db)
        # variants 索引：异体写法命中规范名
        assert table["查愼行"] == (100.0, "normal"), table.get("查愼行")
        assert table["查慎行"] == (100.0, "normal")
        # 同名异人（王珪跨宋/隋）→ 取最高分 100.0
        assert table["王珪"] == (100.0, "normal"), table.get("王珪")
        # 伪作者分类保留
        assert table["玄女"] == (5.0, "myth")
        assert table[" 无名氏"] == (5.0, "placeholder")
        # 带前导空格的实体键原样保留（poems.author 也会带空格，可命中）
        assert " 无名氏" in table
        os.unlink(tmp.name)

    @pytest.mark.asyncio
    async def test_build_author_fame_table_empty_returns_empty(self):
        """poets 表为空时返回 {}（调用方自动退回入诗数启发）"""
        import tempfile, os
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.database import Base
        from app.models.poet import Poet

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            table = await build_author_fame_table(db)
        assert table == {}
        os.unlink(tmp.name)
