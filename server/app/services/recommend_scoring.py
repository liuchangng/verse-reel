"""热点推荐诗词——多维度评分模块。

设计目标（基于 2026-09-03 用户反馈）：
原算法只按「倒排表 term 命中数」降序取 Top-20 候选，再交 LLM 筛 → 候选池被
近现代/当代高产诗人（卢青山/邵祖平/张伯驹等）霸占；且 prompt 写反"作者知名度
【不作为加分项】"，导致李杜苏辛这类真正的名篇被压低。

本模块把评分从「倒排表命中数单一维度」改为「朝代 × 作者权威 × 金句度 × 节令加成
× 关键词相关性」多维度公式，用户已选择「乘积+加成」方案：

    score = dynasty_weight × author_prestige × classic_quote
          + seasonal_bonus × relevance

参考王兆鹏五维加权法（选本 50% + 评点 20% + 研究论文 15% + 网页 10% + 唱和 5%）
思想：将"历代权威"这一长时段信号分离成 dynasty_weight + author_prestige；
经典度启发式按字数/字数偏好给分；节令/关键词按当前热搜计算加成。

## 使用范式

    sc = score_poem(poem, title='中秋月圆夜', current_date=date.today(),
                    relevance=0.8, prest_table=prestige_dict)
    ranked = sorted(candidates, key=lambda p: score_poem(p, ...), reverse=True)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

logger = logging.getLogger(__name__)

# ===== 1. 朝代权重 =====
# 数据来源：peoms.db 实测分布（总计 203 万首）
#   唐 36k / 宋 92k / 北宋 145k / 南宋 178k / 明 397k / 清 575k / 元 44k
#   当代 34k / 现当代 29k / 近现代 8k / 明末清初 168k / 元末明初 58k 等
# 设计原则：
#   - 「古代正宗」（先秦 / 唐诗 / 宋词 / 魏晋）权重大，名句多；
#   - 「混合时代」（明末清初、宋末元初）跨代权威，按中间值处理；
#   - 「近现代 / 当代」绝大多数是押韵打油/练笔，但保留毛泽东/徐志摩/海子等
#     已公认名篇加权；其余一律低权。
DYNASTY_WEIGHT: dict[str, float] = {
    # ===== 顶级（古代正宗，文学源头/巅峰）=====
    "先秦": 1.30, "诗经": 1.30, "楚辞": 1.30, "汉": 1.20, "魏晋": 1.20,
    "三国": 1.20, "南北朝": 1.20, "南朝": 1.20, "北朝": 1.20,
    "五帝": 1.20, "上古": 1.20,
    "唐": 1.30, "中唐": 1.30, "初唐": 1.30, "晚唐": 1.30,
    "宋": 1.30, "北宋": 1.30, "南宋": 1.30, "辽": 1.15,
    # 词选所在朝代（北宋/南宋已上；金元/明清词人入此）
    "金": 1.15, "金末元初": 0.95, "元": 1.05, "元末明初": 0.80,
    # ===== 优秀（明清两代，文人诗社成熟）=====
    "明": 0.85, "明初": 0.85, "明末": 0.80, "清": 0.80,
    "清初": 0.80, "清末": 0.70, "清末民国初": 0.55, "清末至民国": 0.55,
    "晚清": 0.75, "清末至现当代": 0.45, "明末清初": 0.65,
    # ===== 近现代（白名单外一律低权）=====
    "近现代": 0.20, "现当代": 0.15, "当代": 0.15,
    # ===== 其他 =====
    "无名氏": 0.55,  # 词选/民歌集合，金句可能多但作者不可考
}

# 近现代名篇白名单：作者 → 权重（覆盖 DYNASTY_WEIGHT 同名朝代的低权）
# 这些诗人虽处近现代/当代，但作品已被学术/大众公认入选权威选本。
NEAR_MODERN_WHITELIST: dict[str, float] = {
    "毛泽东": 0.95, "徐志摩": 0.75, "戴望舒": 0.70, "闻一多": 0.70,
    "艾青": 0.70, "舒婷": 0.65, "北岛": 0.65, "顾城": 0.65,
    "海子": 0.70, "食指": 0.55, "卞之琳": 0.65, "穆旦": 0.65,
    "余光中": 0.65, "林徽因": 0.55,
}

# ===== 2. 节令加成（按当前日期） =====
# 设计与日期联动：
#   - 节气前后一周内 +0.30 / 命中节日名 +0.20 / 命中节气词 +0.10
SEASONAL_FESTIVALS: dict[str, tuple[int, ...]] = {
    # (月, 日) — 农历对应用公历近似节气日期（按 2024-2026 三年的中位近似）
    "春节": (2, 10), "元宵": (2, 24), "端午": (6, 1), "七夕": (8, 22),
    "中秋": (9, 17), "重阳": (10, 11), "寒食": (4, 4), "清明": (4, 5),
    "腊八": (1, 10), "小年": (1, 25),
}
SOLAR_TERMS: dict[str, tuple[int, int]] = {
    "立春": (2, 4), "雨水": (2, 19), "惊蛰": (3, 6), "春分": (3, 21),
    "清明": (4, 5), "谷雨": (4, 20), "立夏": (5, 6), "小满": (5, 21),
    "芒种": (6, 6), "夏至": (6, 21), "小暑": (7, 7), "大暑": (7, 23),
    "立秋": (8, 7), "处暑": (8, 23), "白露": (9, 8), "秋分": (9, 23),
    "寒露": (10, 8), "霜降": (10, 24), "立冬": (11, 7), "小雪": (11, 22),
    "大雪": (12, 7), "冬至": (12, 22), "小寒": (1, 6), "大寒": (1, 20),
}

# 标题命中即获加成的意象词（按权重）
SEASONAL_IMAGERY: dict[str, float] = {
    "月": 0.08, "雪": 0.10, "寒": 0.05, "春": 0.06, "秋": 0.06, "夏": 0.04, "冬": 0.04,
    "花": 0.05, "酒": 0.05, "思乡": 0.12, "乡愁": 0.12, "思归": 0.10, "归": 0.05,
    "江": 0.04, "夜": 0.04, "寺": 0.05, "寺山": 0.05, "古寺": 0.08,
    "佳节": 0.08, "团圆": 0.10, "寒食": 0.12, "怀古": 0.08,
    "山": 0.03, "水": 0.03, "田园": 0.06, "山水": 0.05, "隐": 0.06, "渔": 0.05,
    "风": 0.03, "云": 0.03, "舟": 0.04, "酒": 0.05, "老": 0.04,
    "登高": 0.10, "中秋": 0.20, "端午": 0.18, "清明": 0.18, "重阳": 0.18,
    "岁暮": 0.08, "年关": 0.08,
}


def dynasty_weight(dynasty: str | None, author: str | None = "") -> float:
    """朝代权威权重：先看近现代白名单（作者级），再查朝代 dict，未知默认 0.40。"""
    if author and author in NEAR_MODERN_WHITELIST:
        return NEAR_MODERN_WHITELIST[author]
    if not dynasty or dynasty.strip() == "":
        return 0.40
    weight = DYNASTY_WEIGHT.get(dynasty)
    if weight is not None:
        return weight
    # 子串匹配兜底（如 "宋中期" / "南宋初" 等未具名朝代）
    for k, v in DYNASTY_WEIGHT.items():
        if k in dynasty:
            return v
    return 0.40


# ===== 3. 作者权威（基于全库入诗数） =====
# 设计：与朝代权重正交 → 即使朝代 0.15（当代），作者权威 1.30（高产当代）仍 0.195
#   数据观察：陆游 10176 / 刘克庄 7902 / 苏轼 7866 / 赵冕镐 7663 ...
#   这是"高产"信号而非"权威"信号，需谨慎对待，故入诗数仅作为参考加权。
#   #20260903: 入诗数已被 poets.fame_score 取代（见 §3b），此处仅作 fallback。
def author_prestige(
    poem_count: int | None,
    author: str | None = "",
    fame: float | None = None,
    category: str = "normal",
) -> float:
    """作者权威权重：优先取 poets.fame_score，无数据才退回入诗数启发。

    Args:
        poem_count: 该作者在 poems 表的入选数量（fallback 用）
        author: 作者名
        fame: poets.fame_score（0~100）；None 表示无诗人实体记录
        category: poets.author_category；非 normal 一律压到最低档

    Returns:
        权重 0.45~1.35
    """
    if not author or author.strip() == "" or author == "无名氏":
        # 无作者/匿名 → 压低（防卢青山/邵祖平类高产作者一统天下）
        return 0.55
    # 名望分优先（设计文档 §8）
    w = fame_prestige(fame, category)
    if w is not None:
        return w
    # fallback：无诗人实体 → 原入诗数启发
    if poem_count is None or poem_count < 0:
        return 0.80  # 未入表，给中性值
    if poem_count >= 5000:
        return 1.25  # 高产名家
    if poem_count >= 1000:
        return 1.10
    if poem_count >= 200:
        return 0.95
    if poem_count >= 50:
        return 0.80
    return 0.70  # 长尾作者


# ===== 3b. 名望权重（poets.fame_score → 权重映射） =====
# 设计文档 §8：author_prestige(行数启发) → 查 poets.fame_score。
# 取代入诗数的核心收益：弘历 43290 行（旧 1.25）→ fame 14.0（新 0.65）；
# 苏轼 7866 行（旧 1.25）→ fame 100（新 1.35）。高产≠名望由此体现。
FAME_WEIGHT_BANDS: list[tuple[float, float]] = [
    (85.0, 1.35),  # S 档
    (70.0, 1.25),  # A 档
    (55.0, 1.15),  # B 档
    (40.0, 1.00),  # C 档
    (25.0, 0.85),  # D 档
]
FAME_WEIGHT_FLOOR = 0.65      # E 档（含 6 万长尾作者）
FAME_WEIGHT_PSEUDO = 0.45     # 非 normal：伪作者/神话/域外/散文（正常会被硬过滤，此为兜底）


def fame_prestige(fame: float | None, category: str = "normal") -> float | None:
    """poets.fame_score(0~100) → 作者权威权重。

    Returns:
        float: fame 可用（含非 normal 的强制低权重）
        None: 无诗人实体记录，调用方应退回 author_prestige 的行数启发
    """
    # 非 normal 无论 fame 多少，一律压到最低（设计文档 §8 硬约束）
    if category and category != "normal":
        return FAME_WEIGHT_PSEUDO
    if fame is None:
        return None
    for lo, w in FAME_WEIGHT_BANDS:
        if fame >= lo:
            return w
    return FAME_WEIGHT_FLOOR


def is_pseudo_author(author: str | None, fame_table: Mapping | None) -> bool:
    """候选池硬约束：非 normal 作者永不进池（placeholder/myth/prose/foreign/religious）。

    设计文档 §9 验收① 纯度约束：D 档以上 100% normal，故非 normal 必在 E 档，
    排除它们不会影响召回（实测 top200 候选中非 normal 仅 0~4 首）。
    """
    if not author or not fame_table:
        return False
    info = fame_table.get(author)
    if not info:
        return False
    _fame, category = info
    return bool(category) and category != "normal"


# ===== 4. 经典度（按内容启发式） =====
def classic_quote_score(content: str | None, title: str | None = "") -> float:
    """经典度启发式：短诗偏金句 / 长诗略低；标题含经典词牌名加权。

    数据观察：2026-09 实测 "推荐多打油" 候选内容>500字的长篇偏多，故短诗加权。
    """
    text = content or ""
    n = len(text)
    if n == 0:
        base = 0.80  # 无内容，中性
    elif n < 30:
        base = 1.15  # 极短，千古绝句概率最高
    elif n < 80:
        base = 1.08
    elif n < 200:
        base = 1.00
    elif n < 500:
        base = 0.95
    else:
        base = 0.85

    # 标题命中经典词牌/体裁 → bonus
    title_bonus = 0.0
    title_str = title or ""
    classic_tokens = [
        "静夜思", "春望", "登高", "登鹳雀楼", "黄鹤楼", "望庐山瀑布",
        "水调歌头", "念奴娇", "如梦令", "声声慢", "一剪梅", "满江红",
        "西江月", "沁园春", "菩萨蛮", "蝶恋花", "醉翁亭记", "岳阳楼记",
        "九月九日", "春江花月夜", "长恨歌", "琵琶行",
    ]
    for tk in classic_tokens:
        if tk in title_str:
            title_bonus = max(title_bonus, 0.15)
    return base + title_bonus


# ===== 5. 节令加成 =====
def seasonal_bonus(title: str, current: date | None = None) -> float:
    """节令加成：日期临近某节日/节气 +0.20-+0.40；标题含节日/意象 +0.05-+0.20。"""
    if not title:
        return 0.0
    score = 0.0
    cur = current or date.today()

    # 1) 节日时效加成（±3 天）
    for festival, (m, d) in SEASONAL_FESTIVALS.items():
        if festival in title:
            try:
                festival_date = date(cur.year, m, d)
            except ValueError:
                continue
            days = abs((cur - festival_date).days)
            if days <= 3:
                score = max(score, 0.50)
            elif days <= 10:
                score = max(score, 0.30)
            else:
                score = max(score, 0.15)
            break

    # 2) 节气时效加成（±2 天）
    for term, (m, d) in SOLAR_TERMS.items():
        if term in title:
            try:
                term_date = date(cur.year, m, d)
            except ValueError:
                continue
            days = abs((cur - term_date).days)
            if days <= 2:
                score = max(score, 0.40)
            elif days <= 8:
                score = max(score, 0.25)
            else:
                score = max(score, 0.12)
            break

    # 3) 标题意象叠加
    for token, bonus in SEASONAL_IMAGERY.items():
        if token in title:
            score += bonus * 0.3  # 多意象叠加按 0.3 衰减，防止秋/月/夜/酒/江叠加超 1
    return min(score, 0.55)


# ===== 6. 总评分 =====
@dataclass
class PoemLike:
    """最小可评分单元：仅要求 id/title/author/dynasty/content 字段。"""
    id: int
    title: str = ""
    author: str = ""
    dynasty: str = ""
    content: str = ""


def score_poem(
    p: PoemLike,
    title: str,
    current: date | None = None,
    relevance: float = 1.0,
    prest_table: Mapping[str, int] | None = None,
    fame_table: Mapping[str, tuple[float, str]] | None = None,
) -> float:
    """多维度评分（用户选定的「乘积+加成」方案）：

        score = dynasty × prestige × classic + seasonal × relevance × 0.5

    Args:
        p: 候选诗词（PoemLike / Poem 均可，duck typing）
        title: 热点标题
        current: 当前日期（用于节令加成；不传则取 system today）
        relevance: 关键词相关性（0.5~1.0），由倒排表 hits 数归一化得到
        prest_table: {author: poem_count}，可选；缺则按 None 退化
        fame_table: {author: (fame_score, author_category)}，可选；
            有则用它算作者权威（设计文档 §8），缺则退回 prest_table 行数启发

    Returns:
        score: float，常规 0.5~2.0；当代打油 ~0.10；唐宋名家+名篇 ~1.30~2.0
    """
    count = prest_table.get(p.author) if prest_table else None
    info = fame_table.get(p.author) if fame_table else None
    fame, category = (info if info else (None, "normal"))
    d_w = dynasty_weight(p.dynasty, p.author)
    a_w = author_prestige(count, p.author, fame=fame, category=category)
    c_s = classic_quote_score(p.content, p.title)
    sb = seasonal_bonus(title, current=current)
    base = d_w * a_w * c_s
    add = sb * max(relevance, 0.5) * 0.5
    return base + add


# ===== 7. 朝代 bucket（多样性配额用） =====
# 用户反馈：单一排序被高产当代刷屏；此处引入"朝代 bucket 配额"，
# 保证 Top-3 多样性：A 古代正宗 ≥1 + B 明清优秀 ≥1 + 余下兜底。
DYNASTY_BUCKET: dict[str, set[str]] = {
    "A_top_classical": {
        "先秦", "诗经", "楚辞", "汉", "魏晋", "三国", "南北朝", "南朝", "北朝",
        "五帝", "上古",
        "唐", "中唐", "初唐", "晚唐",
        "宋", "北宋", "南宋", "辽",
    },
    "B_mid_classical": {
        "金", "金末元初",
        "元", "元末明初",
        "明", "明初", "明末",
        "清", "清初", "晚清",
    },
    "C_mixed_late": {
        "明末清初", "清末", "清末民国初", "清末至民国", "清末至现当代",
    },
}

# "无名氏" / 其他落入 X 兜底 bucket
DEFAULT_BUCKET = "X_fallback"


def dynasty_bucket(dynasty: str | None, author: str | None = "") -> str:
    """返回朝代桶 key（A/B/C/D_modern_whitelist/X）"""
    if author and author in NEAR_MODERN_WHITELIST:
        return "D_modern_whitelist"
    if not dynasty:
        return DEFAULT_BUCKET
    for k, v in DYNASTY_BUCKET.items():
        if dynasty in v:
            return k
    # 子串匹配兜底
    for k, v in DYNASTY_BUCKET.items():
        for known in v:
            if known in dynasty:
                return k
    return DEFAULT_BUCKET


# ===== 8. 多样性策略 =====
# 多样性配额：默认 Top-3 覆盖 A+B+X（古代正宗 + 明清优秀 + 兜底）
# bucket 内按 score 排序取 Top-1；不足时顺序 fallback。
DEFAULT_BUCKET_QUOTA = {"A_top_classical": 1, "B_mid_classical": 1, "X_fallback": 1, "C_mixed_late": 0, "D_modern_whitelist": 0}

# 配额顺序：A → B → D → C → X
_BUCKET_ORDER = ["A_top_classical", "B_mid_classical", "D_modern_whitelist", "C_mixed_late", "X_fallback"]


def diverse_top_k(
    candidates: list[PoemLike],
    title: str,
    current: date | None = None,
    prest_table: Mapping[str, int] | None = None,
    relevance_fn=None,
    quota: dict[str, int] | None = None,
    top_k: int = 3,
    fame_table: Mapping[str, tuple[float, str]] | None = None,
    exclude_pseudo: bool = True,
) -> list[tuple[PoemLike, float]]:
    """多样性 Top-K：在每个朝代 bucket 内按 score 排序，填满 quota；不足按 _BUCKET_ORDER 顺序补齐。

    Args:
        candidates: 候选池（PoemLike 列表）
        title: 热点标题
        current: 当前日期
        prest_table: 作者权威表（入诗数，fame 缺失时 fallback）
        relevance_fn: 可选 callable(poem) -> float
        quota: 自定义配额，例如 {A: 1, B: 1, X: 1} 默认 3; 可调 D_modern_whitelist: 1 加白名单
        top_k: 取前 k
        fame_table: {author: (fame_score, category)}；用于名望加权 + 伪作者过滤
        exclude_pseudo: 是否硬排除非 normal 作者（设计文档 §8 硬约束，默认开）

    Note:
        候选池**不按 fame 档位硬过滤**。实测 top200 候选中 C 档以上仅 0~4 首
        （"离别"/"AI改变世界"等为 0 首），硬过滤会让这些热点无候选可用。
        名望通过 author_prestige 权重软性影响排序（0.45~1.35）。
    """
    quota = dict(quota or DEFAULT_BUCKET_QUOTA)
    # 0) 候选池二次过滤：非 normal 作者永不进池（设计文档 §8 硬约束）
    #    实测影响面极小（top200 中 0~4 首），且全被过滤后退回原候选，保证不空
    if exclude_pseudo and fame_table:
        kept = [p for p in candidates if not is_pseudo_author(p.author, fame_table)]
        if kept:
            candidates = kept
    # 1) 按 bucket 分组 + 算 score
    buckets: dict[str, list[tuple[PoemLike, float]]] = {b: [] for b in _BUCKET_ORDER}
    for p in candidates:
        rel = relevance_fn(p) if relevance_fn else 1.0
        sc = score_poem(p, title=title, current=current, relevance=rel,
                       prest_table=prest_table, fame_table=fame_table)
        b = dynasty_bucket(p.dynasty, p.author)
        buckets.setdefault(b, []).append((p, sc))
    # 每个 bucket 内按 score 降序
    for b in buckets.values():
        b.sort(key=lambda x: -x[1])

    # 2) 按配额取：先按预定 bucket 顺序分配，缺额按 fallback 顺序补齐
    #    同 bucket 内同作者只取一次（防止高产作者刷屏）
    picked: list[tuple[PoemLike, float]] = []
    picked_ids: set[int] = set()
    seen_authors: set[str] = set()
    # 配额阶段
    for b in _BUCKET_ORDER:
        want = quota.get(b, 0)
        added = 0
        for p, sc in buckets.get(b, []):
            if added >= want:
                break
            if p.id in picked_ids:
                continue
            if p.author and p.author in seen_authors:
                continue  # 同作者已选过，让位同 bucket 内的下个不同作者
            picked.append((p, sc))
            picked_ids.add(p.id)
            if p.author:
                seen_authors.add(p.author)
            added += 1
    # Fallback 阶段（补到 top_k，仍按 bucket 顺序）
    for b in _BUCKET_ORDER:
        if len(picked) >= top_k:
            break
        for p, sc in buckets.get(b, []):
            if p.id in picked_ids:
                continue
            if p.author and p.author in seen_authors:
                # Fallback 阶段允许放宽：若允许同作者 fallback 可以 break；
                # 当前策略：仍跳过同作者，避免刷屏
                continue
            picked.append((p, sc))
            picked_ids.add(p.id)
            if p.author:
                seen_authors.add(p.author)
            if len(picked) >= top_k:
                break
    return picked[:top_k]


# ===== 9. 启动期构造作者权威表 =====
async def build_author_prestige_table(db) -> dict[str, int]:
    """一次性扫描 poems.author，预计算 author → 入诗数 dict。

    Returns:
        {author: poem_count}
    """
    from sqlalchemy import select, func
    from app.models.poem import Poem

    stmt = select(Poem.author, func.count().label("c")).group_by(Poem.author)
    result = await db.execute(stmt)
    return {row[0]: int(row[1]) for row in result.fetchall() if row[0]}


async def build_author_fame_table(db) -> dict[str, tuple[float, str]]:
    """一次性加载 poets 表 → {作者名: (fame_score, author_category)}。

    设计文档 §8 接入点。要点：
    1. 同名异人（(author, dynasty) 多行实体）取 **fame 最高**者，
       因为推荐时只有 author 字符串，无法消歧，偏向名望更符合用户预期。
    2. **author_variants 一并建索引**：poets.author 存的是归一后的简体名
       （查慎行），而 poems.author 可能是异体写法（查愼行）。实测不建
       variants 索引会有 5 个作者查不到（如" 无名氏"带前导空格）。
    3. poets 表不存在（新环境未跑 ETL）→ 返回 {}，调用方自动退回行数启发。

    Returns:
        {author: (fame_score, author_category)}；表缺失或为空时返回 {}
    """
    from sqlalchemy import select
    from app.models.poet import Poet

    try:
        stmt = select(Poet.author, Poet.author_variants,
                      Poet.fame_score, Poet.author_category)
        result = await db.execute(stmt)
        rows = result.fetchall()
    except Exception as e:  # poets 表未建 / ETL 未跑 → 静默降级
        logger.warning(f"poets 表不可用，作者权威退回入诗数启发: {e}")
        return {}

    table: dict[str, tuple[float, str]] = {}

    def _put(name: str, fame: float, cat: str) -> None:
        if not name:
            return
        cur = table.get(name)
        # 同名异人取最高分；同分时 normal 优先（避免被低分伪作者覆盖）
        if cur is None or fame > cur[0] or (fame == cur[0] and cur[1] != "normal"):
            table[name] = (fame, cat)

    for author, variants, fame, cat in rows:
        if not author:
            continue
        fame = float(fame or 0.0)
        cat = cat or "normal"
        _put(author, fame, cat)
        # variants：ETL 存的 JSON 数组，含繁体/异体原始写法（部分带空格脏值）
        if variants:
            try:
                for v in json.loads(variants):
                    if isinstance(v, str):
                        _put(v.strip(), fame, cat)
            except (ValueError, TypeError):
                pass
    return table


def rank_by_score(
    candidates: Iterable[PoemLike],
    title: str,
    current: date | None = None,
    prest_table: Mapping[str, int] | None = None,
    relevance_fn=None,
    top_k: int = 3,
    fame_table: Mapping[str, tuple[float, str]] | None = None,
) -> list[tuple[PoemLike, float]]:
    """对候选池排序，返回 top_k 个 (poem, score) 元组，按分数降序。

    Args:
        candidates: 候选池（任意可迭代）
        title: 热点标题
        current: 当前日期
        prest_table: {author: count}；缺则退化为中性
        relevance_fn: 可选 callable(poem) -> float (0.5~1.0)
        top_k: 取前 k 名
        fame_table: {author: (fame_score, category)}；可选，名望加权
    """
    scored = []
    for p in candidates:
        rel = relevance_fn(p) if relevance_fn else 1.0
        sc = score_poem(p, title=title, current=current, relevance=rel,
                       prest_table=prest_table, fame_table=fame_table)
        scored.append((p, sc))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]
