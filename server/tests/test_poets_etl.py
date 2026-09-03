"""build_poets_etl 单元测试（独立内存库，不碰生产 poems.db）

覆盖设计文档 §3~§6 的关键约束：
- 朝代归一：224 种原始取值 → 16 主朝代（含交叉朝代/十六国/十国/脏值）
- 作者归一：异体字合并（查愼行 + 查慎行 → 查慎行）
- 伪作者剔除：myth/placeholder/prose → fame=5
- 同名异人：(author, 主朝代) 作实体键
- 致敬/社交归因：次韵/寄赠 归因给被致敬者，排除自引
- 名望合成：机制压制弘历（高产低致敬 → 低分），而非按 category 硬压
"""
import sys
import math
import os
import sqlite3
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import build_poets_etl as etl

POEMS_DDL = """
CREATE TABLE poems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cnk_id INTEGER UNIQUE,
    title TEXT,
    title_traditional TEXT,
    author TEXT,
    author_traditional TEXT,
    dynasty TEXT,
    genre TEXT,
    content TEXT,
    content_traditional TEXT,
    rhyme TEXT
);
"""

# (title, author, dynasty, genre, 重复行数)
SAMPLE = [
    # —— 头部诗人（验证朝代归一与致敬/社交归因）——
    ("静夜思", "李白", "盛唐", "绝句", 1),
    ("赠汪伦", "李白", "盛唐", "绝句", 1),
    ("黄鹤楼送孟浩然之广陵", "李白", "盛唐", "绝句", 1),
    ("春望", "杜甫", "唐", "律诗", 1),
    ("奉和贾至舍人早朝大明宫", "杜甫", "唐", "律诗", 1),
    ("题西林壁", "苏轼", "北宋", "绝句", 1),
    ("水调歌头", "苏轼", "北宋", "词", 1),
    # —— 后世对苏轼的致敬与交往（应归因给苏轼）——
    ("次韵东坡梅花诗", "张三", "南宋", "律诗", 1),
    ("追和东坡赤壁词", "张三", "南宋", "词", 1),
    ("寄苏东坡先生", "李四", "北宋", "律诗", 1),
    ("送东坡归朝", "王五", "北宋", "律诗", 1),
    # 自引排除：苏轼自己写的唱和不应计入苏轼的 tribute/social
    ("次韵子由渑池怀旧", "苏轼", "北宋", "律诗", 1),
    # —— 异体字分裂（应合并）——
    ("敬业堂诗集一", "查愼行", "清初", "律诗", 3),
    ("敬业堂诗集二", "查慎行", "清初", "律诗", 2),
    # —— 伪作者 ——
    ("黄帝问玄女兵法", "玄女", "远古", "文", 2),
    ("古诗十九首其一", "无名氏", "汉", "古风", 4),
    ("某氏家训全文", "某甲", "清", "文", 30),
    # —— 同名异人（应拆为 2 个实体）——
    ("宫词", "王珪", "北宋", "绝句", 10),
    ("咏史", "王珪", "隋末唐初", "律诗", 2),
    # —— 高产低质（机制压制对象）——
    ("御制诗一", "弘历", "清", "律诗", 40),
    ("御制诗二", "弘历", "清", "律诗", 30),
    # —— 域外汉诗人 ——
    ("高丽杂咏", "赵冕镐", "清", "律诗", 20),
]


@pytest.fixture
def mem_conn():
    """内存 SQLite，建 poems 表并塞入样本"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(POEMS_DDL)
    rows = []
    for title, author, dynasty, genre, n in SAMPLE:
        for i in range(n):
            rows.append((f"{title}#{i}" if n > 1 else title, "内容", author, dynasty, genre))
    conn.executemany(
        "INSERT INTO poems(title, content, author, dynasty, genre) VALUES(?,?,?,?,?)", rows)
    conn.commit()
    return conn


@pytest.fixture
def entities(mem_conn):
    """跑完阶段 1~4 的实体字典（跳过写库）"""
    ents = etl.stage1_aggregate(mem_conn)
    _, buckets = etl.build_alias_index()
    etl.stage2_scan_relations(mem_conn, ents, buckets)
    etl.stage3_apply_seeds(ents)
    etl.stage4_compute_fame(ents)
    return ents


def find(entities, author, dynasty=None):
    """按 (author, 朝代) 取实体；未指定朝代时取分数最高的"""
    cands = [e for e in entities.values() if e["author"] == author]
    if dynasty:
        cands = [e for e in cands if e["dynasty"] == dynasty]
    return max(cands, key=lambda e: e["fame_score"]) if cands else None


# ============================================================
# 1. 朝代归一
# ============================================================

@pytest.mark.parametrize("raw,expect", [
    # 细分朝代
    ("盛唐", "唐"), ("中唐", "唐"), ("北宋", "宋"), ("南宋", "宋"),
    # 交叉朝代：取位置最靠前的朝代词
    ("明末清初", "明"), ("元末明初", "元"), ("隋末唐初", "隋"),
    ("唐末至五代", "唐"), ("五代至宋初", "五代"), ("金末元初", "辽金"),
    # 近现代：关键词命中即近现代，压倒位置规则
    ("清末民国初", "近现代"), ("清末至现当代", "近现代"), ("当代", "近现代"),
    ("民国初", "近现代"), ("太平天国", "近现代"),
    # 仍属清（不含近现代关键词）
    ("晚清", "清"), ("清末", "清"), ("清初", "清"),
    # 十六国 / 十国（易与正统朝代混淆）
    ("前秦", "南北朝"), ("后赵", "南北朝"), ("成汉", "南北朝"),
    ("南唐", "五代"), ("后唐", "五代"), ("后汉", "五代"), ("后晋", "五代"),
    ("吴越", "五代"), ("五代十国", "五代"),
    # 三国 / 两晋 / 北朝
    ("曹魏", "魏"), ("蜀汉", "魏"), ("西晋", "晋"), ("东晋", "晋"),
    ("北魏", "南北朝"), ("南朝宋", "南北朝"),
    # 先秦诸侯国
    ("春秋齐国", "先秦"), ("战国楚国", "先秦"), ("远古", "先秦"),
    ("西周", "先秦"), ("商", "先秦"), ("秦", "先秦"),
    # 脏值兜底
    ("1109", "其他"), ("", "其他"), (None, "其他"),
])
def test_normalize_dynasty(raw, expect):
    assert etl.normalize_dynasty(raw) == expect


# ============================================================
# 2. 作者归一与实体拆分
# ============================================================

def test_author_merge_variant(entities):
    """查愼行(3 行) + 查慎行(2 行) 应合并为一个实体 5 行，且留存 variants"""
    e = find(entities, "查慎行")
    assert e is not None, "查慎行实体缺失"
    assert e["total_rows"] == 5, f"合并后应为 5 行，实际 {e['total_rows']}"
    assert "查愼行" in e["variants"], "异体写法应记入 variants"
    assert find(entities, "查愼行") is None, "查愼行不应作为独立实体存在"


def test_homonyn_split_by_dynasty(entities):
    """同名异人：王珪(北宋 10 行) 与 王珪(隋末唐初 2 行) 应为 2 个实体"""
    song = find(entities, "王珪", "宋")
    sui = find(entities, "王珪", "隋")
    assert song is not None and sui is not None, "王珪应拆为宋/隋两个实体"
    assert song["total_rows"] == 10
    assert sui["total_rows"] == 2
    assert song["dynasty_raw"]["北宋"] == 10


def test_dynasty_normalized_in_entity(entities):
    """李白(盛唐) 的实体朝代应归一为 唐，原始值保留在 dynasty_raw"""
    e = find(entities, "李白")
    assert e["dynasty"] == "唐"
    assert e["dynasty_raw"] == {"盛唐": 3}


# ============================================================
# 3. 伪作者识别
# ============================================================

@pytest.mark.parametrize("author,expect_cat", [
    ("玄女", "myth"),
    ("无名氏", "placeholder"),
    ("某甲", "prose"),        # 30 行全为 genre=文
    ("赵冕镐", "foreign"),
    ("李白", "normal"),
])
def test_judge_category(entities, author, expect_cat):
    e = find(entities, author)
    assert e is not None, f"{author} 实体缺失"
    assert e["author_category"] == expect_cat, (
        f"{author} 应为 {expect_cat}，实际 {e['author_category']}")


def test_pseudo_author_forced_low_score(entities):
    """myth/placeholder/prose 强制 fame=5，不参与正常合成"""
    for name in ("玄女", "无名氏", "某甲"):
        assert find(entities, name)["fame_score"] == 5.0
        assert find(entities, name)["fame_level"] == "E"


# ============================================================
# 4. 致敬 / 社交归因
# ============================================================

def test_tribute_attribution(entities):
    """他人所写《次韵东坡…》《追和东坡…》应归因给苏轼（+2）"""
    e = find(entities, "苏轼")
    assert e["tribute_hits"] >= 2, f"苏轼 tribute 应 ≥2，实际 {e['tribute_hits']}"


def test_social_attribution(entities):
    """《寄苏东坡先生》《送东坡归朝》应归因给苏轼（+2）"""
    e = find(entities, "苏轼")
    assert e["social_poems"] >= 2, f"苏轼 social 应 ≥2，实际 {e['social_poems']}"


def test_self_reference_excluded(entities):
    """苏轼自写《次韵子由渑池怀旧》不应计入苏轼自己的 tribute（排除自引）"""
    e = find(entities, "苏轼")
    # 样本中苏轼自写 1 首次韵诗；若自引未排除，tribute 会变成 3
    assert e["tribute_hits"] == 2, (
        f"自引未排除：苏轼 tribute 应为 2，实际 {e['tribute_hits']}")


# ============================================================
# 5. 名望合成
# ============================================================

def test_high_volume_low_fame(entities):
    """机制压制：弘历 70 行但致敬/选本为 0 → fame < 30（非按 category 硬压）"""
    e = find(entities, "弘历")
    assert e["total_rows"] == 70
    assert e["author_category"] == "normal", "弘历是真实诗人，不应被标为伪作者"
    assert e["fame_score"] < 30, f"弘历应靠机制掉分（<30），实际 {e['fame_score']}"


def test_fame_components_formula():
    """分项合成与封顶：按设计文档 §3 公式校验"""
    base = {
        "author": "测试", "dynasty": "唐", "author_category": "normal",
        "tribute_hits": 10, "social_poems": 10, "anthology_hits": 3,
        "textbook_hits": 2, "official_rank": 3, "family_lineage": 1,
    }
    e = dict(base)
    expect = (10.0
              + min(30.0, math.log1p(10) * 4.2)
              + min(25.0, 3 * 6.0)
              + min(15.0, 2 * 2.5)
              + min(10.0, math.log1p(10) * 1.8)
              + 7      # official_rank=3
              + 10     # 唐
              + 5)     # 世家
    assert abs(etl.compute_fame(e) - expect) < 1e-6

    # 封顶 100：极端信号不得溢出
    e2 = dict(base, tribute_hits=100000, anthology_hits=100,
              textbook_hits=100, social_poems=100000)
    assert etl.compute_fame(e2) == 100.0


def test_fame_cap_at_100(entities):
    """头部诗人叠加多重信号后不得溢出 100"""
    for e in entities.values():
        assert 0.0 <= e["fame_score"] <= 100.0


@pytest.mark.parametrize("score,level", [
    (100, "S"), (85, "S"), (84.9, "A"),
    (70, "A"), (69.9, "B"), (55, "B"), (54.9, "C"),
    (40, "C"), (39.9, "D"), (25, "D"), (24.9, "E"), (0, "E"),
])
def test_fame_level(score, level):
    assert etl.fame_level(score) == level


def test_near_modern_bonus():
    """近现代 dynasty_bonus=0，靠传播度白名单补偿（毛泽东应显著高于同条件路人）"""
    base = {"author": "毛泽东", "dynasty": "近现代", "author_category": "normal",
            "tribute_hits": 0, "social_poems": 0, "anthology_hits": 0,
            "textbook_hits": 0, "official_rank": 0, "family_lineage": 0}
    with_bonus = etl.compute_fame(dict(base))
    unknown = etl.compute_fame(dict(base, author="某路人"))
    assert with_bonus > unknown, "白名单作者应获得传播度补偿"


# ============================================================
# 6. 端到端：写库与幂等
# ============================================================

def test_write_and_idempotent(mem_conn, entities):
    """重复写入应幂等：实体数不变，不产生重复行"""
    mem_conn.executescript(etl.DDL)
    etl.stage5_write(mem_conn, entities)
    n1 = mem_conn.execute("SELECT COUNT(*) FROM poets").fetchone()[0]
    etl.stage5_write(mem_conn, entities)
    n2 = mem_conn.execute("SELECT COUNT(*) FROM poets").fetchone()[0]
    assert n1 == len(entities) == n2, f"幂等失败：{n1} vs {n2} vs {len(entities)}"

    row = mem_conn.execute(
        "SELECT author, dynasty, fame_score, fame_level FROM poets "
        "WHERE author='苏轼'").fetchone()
    assert row is not None and row[0] == "苏轼"


def test_alias_index_conflict_and_len():
    """别名索引：单字别名丢弃，冲突别名剔除"""
    alias_map, buckets = etl.build_alias_index()
    assert all(len(k) >= 2 for k in alias_map), "不应存在单字别名"
    # 每个别名都应能按首字符检索到
    for alias in list(alias_map)[:200]:
        assert (alias, alias_map[alias]) in buckets.get(alias[0], [])


# ============================================================
# 7. 候选池口径与纯度（设计文档 §9 验收① + §9.1 回归保护）
# ============================================================

def test_candidate_pool_purity(entities):
    """§9 验收① 纯度硬约束（比数量达标更能证明体系有效）：
    D 档以上 (fame_score>=25) 的实体中，author_category 必须 100% 为 normal。
    伪作者 / 神话 / 域外 / 高产低质一旦混入候选池，即视为评分机制失效。
    """
    pool = [e for e in entities.values() if e["fame_score"] >= 25]
    assert pool, "mock 数据里候选池为空，该断言已失去意义"
    dirty = [(e["author"], e["dynasty"], e["author_category"], round(e["fame_score"], 1))
             for e in pool if e["author_category"] != "normal"]
    assert not dirty, f"候选池混入非 normal 实体：{dirty}"


@pytest.mark.parametrize("author,expect_cat", [
    ("玄女", "myth"),
    ("无名氏", "placeholder"),
    ("某甲", "prose"),
    ("赵冕镐", "foreign"),
])
def test_pseudo_authors_barred_from_pool(entities, author, expect_cat):
    """§8 硬约束：非 normal 实体永不进候选池（fame < 25 落 E 档）"""
    e = find(entities, author)
    assert e is not None, f"{author} 未入库"
    assert e["author_category"] == expect_cat, (
        f"{author} 分类判定错误：{e['author_category']} != {expect_cat}")
    assert e["fame_score"] < 25, (
        f"{author} (category={e['author_category']}) fame={e['fame_score']:.1f} "
        f"进入候选池，违反 §9 验收① 纯度约束")


def test_fame_level_thresholds_documented():
    """§9 验收① 档位阈值若被修改，这里必须同步更新（防止悄悄稀释名望标准）"""
    assert etl.fame_level(85) == "S"
    assert etl.fame_level(70) == "A"
    assert etl.fame_level(55) == "B"
    assert etl.fame_level(40) == "C"
    assert etl.fame_level(25) == "D"
    assert etl.fame_level(24.9) == "E"
