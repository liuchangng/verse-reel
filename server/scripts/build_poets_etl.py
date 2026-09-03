"""离线构建 poets 诗人实体表（推荐算法作者层名望分）

设计文档：docs/design_poets_etl.md（v2 终稿，2026-09-03 用户确认）

要解决的 poems 表作者层缺陷：
1. author 是字符串，同名异人（王珪 跨隋末唐初/北宋/南宋）无法区分 → (author, 归一主朝代) 作实体键
2. 繁简/异体分裂（查愼行 5302 行 vs 查慎行 3 行）→ AUTHOR_MERGE 归一 + variants 留存
3. 高产 ≠ 名望（弘历 43290 行清诗）→ 用 tribute/anthology 等客观计数而非行数
4. 伪作者（玄女=神话、genre='文' 长文拆行、占位作者）→ 强制低分 + category 标记

中立性铁律（用户定稿，本脚本严格执行）：
- 入分信号全部为"计数型客观事实"（tribute/anthology/social 为 COUNT，
  official/family 为客观事实编码）
- 禁止评价型主观立场：忠奸/变法对错/派系功过永不入分；
  政治人物只记官职层级与"做过的事"（fact_tags），不评好坏
- 弘历等"高产低质"不靠 category 硬压，靠 tribute=0/anthology=0 自然掉分
  （机制压制而非价值观压制）

用法：
    python scripts/build_poets_etl.py                # 全量重建（幂等）
    python scripts/build_poets_etl.py --limit 20000  # 小批量试跑（采样前 N 个 author）
    python scripts/build_poets_etl.py --dry-run      # 只算不写库
    python scripts/build_poets_etl.py --stats        # 查看已入库统计
    python scripts/build_poets_etl.py --explain 苏轼  # 分项解释某诗人得分
"""
import sys
import time
import json
import math
import sqlite3
import logging
import argparse
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services import poet_seeds as S

# Windows 控制台中文输出保护（Git Bash / GBK 终端下避免 UnicodeEncodeError）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - 非 UTF-8 环境忽略
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).parent.parent / "data" / "poems.db")
BATCH_SIZE = 5000

# ============================================================
# 一、朝代归一：poems.dynasty 实测 224 种取值（含细分/交叉/诸侯国/脏值）
#     例：李白=盛唐、杜甫=唐、苏轼=北宋、辛弃疾=南宋；
#         另有 明末清初/元末明初/清末民国初 等交叉值与 "1109" 等脏值。
#     不归一会导致同一诗人被拆成多个实体，故必须映射为 16 个主朝代。
# ============================================================

# 近现代关键词：命中即归"近现代"，优先级最高（压倒"取最靠前朝代词"规则）。
# 理由：清末民国初/清末至现当代 等交叉期人物的文化身份属近现代转型期，
#       且设计文档意图是压制近现代的当代噪音（当代 3.4 万行 + 现当代 3.0 万行）。
MODERN_KEYWORDS = ("民国", "现代", "当代", "近现代", "现当代", "太平天国", "共和国")

# (主朝代, 关键词表)。匹配规则：在主朝代词表中找 raw 里出现位置最靠前的关键词，
# 该关键词所属主朝代即为归一结果（"隋末唐初"→隋@0 胜 唐@2 → 隋）。
# 关键词顺序按"越具体越靠前"排列，用于消化长尾（十六国/十国/诸侯国）。
DYNASTY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("先秦", ["远古", "上古", "先秦", "商末", "商", "西周末", "西周", "东周",
              "春秋末", "春秋", "战国末", "战国", "秦末", "秦"]),
    ("汉", ["西汉末", "西汉", "东汉末", "东汉", "新朝", "汉末", "汉初", "汉"]),
    ("魏", ["曹魏末", "曹魏", "蜀汉", "孙吴", "三国末", "三国", "魏"]),
    ("晋", ["西晋末", "西晋", "东晋", "晋末", "晋初", "晋"]),
    ("南北朝", ["南北朝", "南朝", "北朝", "南齐", "南梁", "西梁", "陈朝",
                "北魏", "东魏", "西魏", "北齐", "北周",
                # 十六国（与东晋并列，归魏晋南北朝大区间）
                "十六国", "前秦", "后秦", "西秦", "前凉", "后凉", "南凉", "北凉",
                "西凉", "前燕", "后燕", "南燕", "北燕", "西燕", "成汉",
                "前赵", "后赵", "大夏"]),
    ("隋", ["隋"]),
    ("唐", ["盛唐", "中唐", "晚唐", "初唐", "武周", "唐末", "唐初", "唐"]),
    ("五代", ["五代", "十国", "后梁", "后唐", "后晋", "后汉", "后周",
              "前蜀", "后蜀", "南唐", "吴越", "闽国", "南汉", "北汉",
              "南平", "荆南"]),
    ("宋", ["北宋", "南宋", "宋初", "宋末", "宋"]),
    ("辽金", ["辽", "西夏", "金"]),
    ("元", ["元"]),
    ("明", ["南明", "明"]),
    ("清", ["淸", "清"]),
]

# fame 合成用朝代加成（设计文档 §3 定稿值；晋/南北朝/隋/五代/辽金 为定稿未列项的补齐）
# 注：poet_seeds.DYNASTY_WEIGHTS 是更细粒度的参考权重（清 0.85 等），
#     与定稿分档值数量级不同，本表为唯一真值，避免两处冲突。
DYNASTY_BONUS: dict[str, float] = {
    "先秦": 10, "唐": 10, "宋": 10,
    "汉": 8, "魏": 8,
    "晋": 7, "南北朝": 7, "隋": 7,
    "五代": 6, "辽金": 6, "元": 6,
    "明": 5, "清": 4,
    "近现代": 0, "其他": 0,
}

# 官方层级 → 分（客观事实编码，不评政绩功过）
OFFICIAL_SCORE = {4: 10, 3: 7, 2: 4, 1: 1, 0: 0}

# 近现代白名单补偿：该群体 dynasty_bonus=0，用当代传播度做客观补偿
NEAR_MODERN_BONUS_SCALE = 25.0

# ============================================================
# 二、致敬/社交信号词（用于从标题归因"被致敬者/交往对象"）
#     tribute 与 social 互斥：命中 tribute 词即不再计 social，避免重复计数
# ============================================================

# 追和/次韵/拟作 —— "影响深远"的客观代理
TRIBUTE_PATTERNS = ("次韵", "追和", "用韵", "依韵", "和韵", "步韵",
                    "奉和", "敬和", "拟古", "效古", "拟", "效", "仿")

# 寄/赠/送/答/酬 —— 文人圈层交往的客观代理
SOCIAL_PATTERNS = ("见寄", "见贻", "奉呈", "寄", "赠", "送", "答",
                   "酬", "呈", "别", "和", "奉", "同", "陪", "宴", "饯")

# 域外作者补充名单（ETL 层）：高丽/朝鲜/日本汉诗人 author 为中文化姓名，
# 不含 FOREIGN_KEYWORDS 字面，靠种子关键词无法识别，故在此显式登记。
# 典型：赵冕镐 7663 行、徐居正 5789 行、李穑 6037 行（均入高产 top10）。
FOREIGN_EXTRA_NAMES = {
    # 高丽/朝鲜
    "李穑", "徐居正", "赵冕镐", "李齐贤", "郑梦周", "权近", "成伣",
    "申叔舟", "金宗直", "李滉", "李珥", "尹善道", "朴趾源", "丁若镛",
    "金尚宪", "申纬", "南龙翼", "洪世泰", "赵秀三",
    # 日本
    "菅原道真", "空海", "最澄", "赖山阳", "广濑旭庄", "梁川星岩",
    # 越南
    "阮攸", "阮廌",
}

# 散文占比阈值：prose_rows/total_rows 超过此值且有一定体量 → 判为 prose 伪作者
PROSE_RATIO_THRESHOLD = 0.8
PROSE_MIN_ROWS = 20


# ============================================================
# 三、工具函数
# ============================================================

def normalize_dynasty(raw: str | None) -> str:
    """把 224 种原始朝代取值归一为 16 个主朝代。

    规则：
    1. 命中近现代关键词 → "近现代"（优先级最高，不看位置）
    2. 否则取 raw 中出现位置最靠前的朝代关键词所属主朝代
    3. 无命中 → "其他"（脏值如 "1109"）
    """
    s = (raw or "").strip()
    if not s:
        return "其他"
    for kw in MODERN_KEYWORDS:
        if kw in s:
            return "近现代"
    best_pos, best_dy = 10 ** 9, "其他"
    for dy, keywords in DYNASTY_KEYWORDS:
        for kw in keywords:
            pos = s.find(kw)
            if 0 <= pos < best_pos:
                best_pos, best_dy = pos, dy
    return best_dy


def canon_author(author: str | None) -> str:
    """作者名归一：异体字合并（查愼行 → 查慎行），去空白。"""
    name = (author or "").strip()
    if not name:
        return ""
    return S.AUTHOR_MERGE.get(name, name)


def build_alias_index() -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
    """构建 别名 → 规范名 索引，并按首字符分桶以加速标题扫描。

    返回:
        alias_map: {别名: 规范名}（冲突别名会被剔除，避免误归因）
        buckets:   {首字符: [(别名, 规范名), ...]}
    """
    alias_map: dict[str, str] = {}
    conflict: set[str] = set()
    for canon, aliases in S.ALIASES.items():
        target = canon_author(canon)
        for alias in [canon, *aliases]:
            alias = (alias or "").strip()
            if len(alias) < 2:      # 单字别名（如"仙"）误匹配率过高，丢弃
                continue
            if alias in alias_map and alias_map[alias] != target:
                conflict.add(alias)  # 同别名指向多人 → 无法判定，剔除
            alias_map[alias] = target
    for alias in conflict:
        alias_map.pop(alias, None)
    if conflict:
        logger.info("别名冲突剔除 %d 个：%s", len(conflict), sorted(conflict)[:10])

    buckets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for alias, target in alias_map.items():
        buckets[alias[0]].append((alias, target))
    for lst in buckets.values():
        lst.sort(key=lambda x: -len(x[0]))  # 长别名优先，避免"东坡"吃掉"东坡居士"
    logger.info("别名索引：%d 个别名 → %d 个规范名，分桶 %d 个首字符",
                len(alias_map), len(set(alias_map.values())), len(buckets))
    return alias_map, dict(buckets)


def match_names_in_title(title: str, buckets: dict[str, list[tuple[str, str]]],
                         exclude: str = "") -> set[str]:
    """在标题中扫描出现的诗人名/别名，返回命中的规范名集合（排除作者自己）。

    实现为"按首字符分桶 + startswith"，复杂度约为 O(len(title) × 桶大小)，
    比逐个 alias 做 in 判断（1200 次 × 72 万行）快一个数量级。
    """
    hits: set[str] = set()
    n = len(title)
    i = 0
    while i < n:
        cand = buckets.get(title[i])
        if cand:
            for alias, target in cand:
                if title.startswith(alias, i) and target != exclude:
                    hits.add(target)
                    i += len(alias) - 1  # 命中后跳过该别名，避免短别名重复命中
                    break
        i += 1
    return hits


def judge_category(author: str, total_rows: int, prose_rows: int) -> str:
    """判定实体类别（客观分类，用于过滤与解释）。

    - placeholder：占位作者（无名氏/佚名等，非真实个人）
    - myth：神话传说人物（玄女/西王母等，非历史真实诗人）
    - religious：僧道法号（诗僧寒山/皎然等有选本收录者豁免）
    - foreign：域外汉诗人（高丽/朝鲜/日本/越南）
    - prose：以散文拆行为主（如《黄帝问玄女兵法》拆行）
    - normal：正常诗人
    """
    name = author or ""
    if name in S.PLACEHOLDER_AUTHORS or any(k in name for k in ("无名", "佚名", "待考", "阙名")):
        return "placeholder"
    if name in S.MYTH_AUTHORS:
        return "myth"
    if name in FOREIGN_EXTRA_NAMES or any(k in name for k in S.FOREIGN_KEYWORDS):
        return "foreign"
    if name.startswith(S.RELIGIOUS_PREFIXES):
        # 诗僧/道士若被权威选本收录，视为真实诗人（如 僧皎然入选唐诗三百首）
        if S.TANG_300.get(name, 0) > 0 or S.SONG_300.get(name, 0) > 0 or S.TEXTBOOK_HITS.get(name, 0) > 0:
            return "normal"
        return "religious"
    if total_rows >= PROSE_MIN_ROWS and prose_rows / max(total_rows, 1) >= PROSE_RATIO_THRESHOLD:
        return "prose"
    return "normal"


def compute_fame(e: dict) -> float:
    """名望分合成（设计文档 §3，0~100 确定性可解释）。

    伪作者（占位/神话/散文拆行）直接判 5 分 —— 不参与正常合成。
    正常作者：底分 10 + 致敬 30 + 选本 25 + 教材 15 + 社交 10
              + 官阶 10 + 朝代 10 + 世家 5，封顶 100。

    宗教/域外作者不在此硬压：其 tribute/anthology/textbook 天然为 0，
    靠机制自然掉分（符合"不用价值观压制"的设计原则）。
    """
    if e["author_category"] in ("placeholder", "myth", "prose"):
        return 5.0

    score = 10.0
    # 被后世追和/次韵/拟作（对数压缩，苏轼 5317 次封顶 30）
    score += min(30.0, math.log1p(e["tribute_hits"]) * 4.2)
    # 权威选本收录（杜甫 38 首 → 228 封顶 25）
    score += min(25.0, e["anthology_hits"] * 6.0)
    # 语文教材收录（当代国民认知弱信号）
    score += min(15.0, e["textbook_hits"] * 2.5)
    # 同代唱和/赠答（对数压缩）
    score += min(10.0, math.log1p(e["social_poems"]) * 1.8)
    # 官职客观层级（只记层级不评政绩）
    score += OFFICIAL_SCORE.get(e["official_rank"], 0)
    # 朝代加成
    score += DYNASTY_BONUS.get(e["dynasty"], 0)
    # 文学世家（客观血缘事实）
    score += 5.0 if e["family_lineage"] else 0.0
    # 近现代传播度补偿（该群体 dynasty_bonus=0）
    if e["dynasty"] == "近现代":
        score += S.NEAR_MODERN_WHITELIST.get(e["author"], 0.0) * NEAR_MODERN_BONUS_SCALE
    return min(100.0, score)


def fame_level(score: float) -> str:
    """分档：S≥85 A≥70 B≥55 C≥40 D≥25 E<25"""
    if score >= 85:
        return "S"
    if score >= 70:
        return "A"
    if score >= 55:
        return "B"
    if score >= 40:
        return "C"
    if score >= 25:
        return "D"
    return "E"


# ============================================================
# 四、ETL 各阶段
# ============================================================

DDL = """
CREATE TABLE IF NOT EXISTS poets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author VARCHAR(100) NOT NULL,
    author_variants TEXT,
    dynasty VARCHAR(50) NOT NULL,
    dynasty_raw TEXT,
    total_rows INTEGER DEFAULT 0,
    prose_rows INTEGER DEFAULT 0,
    genre_stats TEXT,
    split_rows INTEGER DEFAULT 0,
    tribute_hits INTEGER DEFAULT 0,
    social_poems INTEGER DEFAULT 0,
    official_rank INTEGER DEFAULT 0,
    family_lineage INTEGER DEFAULT 0,
    anthology_hits INTEGER DEFAULT 0,
    textbook_hits INTEGER DEFAULT 0,
    fame_score FLOAT DEFAULT 0.0,
    fame_level VARCHAR(2) DEFAULT 'E',
    author_category VARCHAR(20) DEFAULT 'normal',
    school_tags TEXT,
    style_tags TEXT,
    life_tags TEXT,
    fact_tags TEXT,
    src VARCHAR(200),
    updated_at VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS idx_poets_author ON poets (author);
CREATE INDEX IF NOT EXISTS idx_poets_dynasty ON poets (dynasty);
CREATE INDEX IF NOT EXISTS idx_poets_fame ON poets (fame_score);
"""

GENRES = ("律诗", "绝句", "古风", "文", "词", "曲", "排律", "联")


def stage1_aggregate(con: sqlite3.Connection, limit: int = 0) -> dict[tuple[str, str], dict]:
    """阶段1：按 (author, dynasty_raw) 聚合统计，再归一到 (author, 主朝代) 实体。

    一次 GROUP BY 同时取：总行数、散文行数、体裁分布、去重标题数（用于算拆行数）。
    """
    sum_genre = ", ".join(
        f"SUM(CASE WHEN genre = '{g}' THEN 1 ELSE 0 END)" for g in GENRES
    )
    sql = f"""
        SELECT author, dynasty, COUNT(*) AS c, COUNT(DISTINCT title) AS t, {sum_genre}
        FROM poems
        WHERE author IS NOT NULL AND author != ''
        GROUP BY author, dynasty
    """
    if limit:
        sql += f" LIMIT {limit}"
    logger.info("阶段1 聚合中（GROUP BY author, dynasty）…")
    t0 = time.time()
    rows = con.execute(sql).fetchall()
    logger.info("阶段1 聚合完成：%d 个 (author, 原始朝代) 组合，耗时 %.1fs",
                len(rows), time.time() - t0)

    entities: dict[tuple[str, str], dict] = {}
    for author_raw, dynasty_raw, cnt, titles, *genre_counts in rows:
        author = canon_author(author_raw)
        if not author:
            continue
        dy = normalize_dynasty(dynasty_raw)
        key = (author, dy)
        e = entities.get(key)
        if e is None:
            e = entities[key] = {
                "author": author,
                "dynasty": dy,
                "dynasty_raw": {},
                "variants": set(),
                "total_rows": 0,
                "distinct_titles": 0,
                "prose_rows": 0,
                "genre_counts": {g: 0 for g in GENRES},
            }
        e["total_rows"] += cnt
        e["distinct_titles"] += titles
        for g, c in zip(GENRES, genre_counts):
            e["genre_counts"][g] += c or 0
        # 记录原始朝代分布与异体写法
        e["dynasty_raw"][dynasty_raw or "未知"] = e["dynasty_raw"].get(dynasty_raw or "未知", 0) + cnt
        if author_raw != author:
            e["variants"].add(author_raw)

    # 派生字段：散文行数、拆行数（同标题重复出现视为长文拆行）
    for e in entities.values():
        e["prose_rows"] = e["genre_counts"].get("文", 0)
        e["split_rows"] = max(0, e["total_rows"] - e["distinct_titles"])
        e["author_category"] = judge_category(e["author"], e["total_rows"], e["prose_rows"])
    logger.info("阶段1 归一完成：%d 个诗人实体（(author, 主朝代)）", len(entities))
    return entities


def stage2_scan_relations(con: sqlite3.Connection, entities: dict[tuple[str, str], dict],
                          buckets: dict[str, list[tuple[str, str]]]) -> None:
    """阶段2：扫描标题中的致敬/社交信号，归因给被致敬（交往）对象。

    归因目标：标题里出现的其他诗人（排除作者自己），归因到该规范名下
    **行数最多的实体**（标题只含人名、不含朝代，无法区分同名异人）。
    """
    # 规范名 → 主实体（行数最多），用于无朝代信息的归因
    main_entity: dict[str, tuple[str, str]] = {}
    for key, e in entities.items():
        cur_key = main_entity.get(e["author"])
        if cur_key is None or e["total_rows"] > entities[cur_key]["total_rows"]:
            main_entity[e["author"]] = key

    for e in entities.values():
        e["tribute_hits"] = 0
        e["social_poems"] = 0

    def scan(patterns: tuple[str, ...], field: str, label: str) -> None:
        where = " OR ".join(f"title LIKE '%{p}%'" for p in patterns)
        sql = f"SELECT author, title FROM poems WHERE {where}"
        t0 = time.time()
        n_hit = 0
        for author_raw, title in con.execute(sql):
            author = canon_author(author_raw)
            title = title or ""
            targets = match_names_in_title(title, buckets, exclude=author)
            for tgt in targets:
                key = main_entity.get(tgt)
                if key is None:
                    continue
                entities[key][field] += 1
                n_hit += 1
        logger.info("阶段2 %s 扫描完成：归因 %d 次，耗时 %.1fs", label, n_hit, time.time() - t0)

    scan(TRIBUTE_PATTERNS, "tribute_hits", "致敬(次韵/追和/拟作)")
    scan(SOCIAL_PATTERNS, "social_poems", "社交(寄/赠/送/答/酬)")


def stage3_apply_seeds(entities: dict[tuple[str, str], dict]) -> None:
    """阶段3：合并人工种子（选本收录/教材/官阶/世家/标签）。

    种子按"规范名"命中，与该名下的所有朝代实体共享（如苏轼宋 / 王珪宋+唐）。
    """
    for e in entities.values():
        name = e["author"]
        e["anthology_hits"] = S.TANG_300.get(name, 0) + S.SONG_300.get(name, 0)
        e["textbook_hits"] = S.TEXTBOOK_HITS.get(name, 0)
        e["official_rank"] = S.OFFICIAL_RANK.get(name, 0)
        e["family_lineage"] = 1 if name in S.LITERARY_FAMILIES else 0
        e["school_tags"] = S.SCHOOL_TAGS.get(name, [])
        e["style_tags"] = S.STYLE_TAGS.get(name, [])
        e["life_tags"] = S.LIFE_TAGS.get(name, [])
        e["fact_tags"] = S.FACT_TAGS.get(name, [])
        # src 标注信号构成，便于审计：stat=统计信号, seed=种子命中
        has_seed = bool(e["anthology_hits"] or e["textbook_hits"]
                        or e["official_rank"] or e["family_lineage"])
        e["src"] = "mix" if has_seed else "stat"
    logger.info("阶段3 种子合并完成：%d 个实体", len(entities))


def stage4_compute_fame(entities: dict[tuple[str, str], dict]) -> None:
    """阶段4：合成 fame_score 并分档。"""
    for e in entities.values():
        e["fame_score"] = round(compute_fame(e), 2)
        e["fame_level"] = fame_level(e["fame_score"])
    levels: dict[str, int] = defaultdict(int)
    for e in entities.values():
        levels[e["fame_level"]] += 1
    logger.info("阶段4 名望分合成完成，档位分布：%s",
                {k: levels[k] for k in "SABCDE" if levels[k]})


def stage5_write(con: sqlite3.Connection, entities: dict[tuple[str, str], dict],
                 dry_run: bool = False) -> None:
    """阶段5：幂等写入（DELETE 全表 + 批量 INSERT）。"""
    if dry_run:
        logger.info("阶段5 --dry-run：跳过写库（共 %d 条待写）", len(entities))
        return
    updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    con.execute("DELETE FROM poets")
    con.commit()
    sql = """
        INSERT INTO poets (author, author_variants, dynasty, dynasty_raw, total_rows,
                           prose_rows, genre_stats, split_rows, tribute_hits, social_poems,
                           official_rank, family_lineage, anthology_hits, textbook_hits,
                           fame_score, fame_level, author_category, school_tags, style_tags,
                           life_tags, fact_tags, src, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    batch, total = [], 0
    for e in entities.values():
        genre_stats = {g: c for g, c in e["genre_counts"].items() if c}
        batch.append((
            e["author"],
            json.dumps(sorted(e["variants"]), ensure_ascii=False) if e["variants"] else None,
            e["dynasty"],
            json.dumps(e["dynasty_raw"], ensure_ascii=False),
            e["total_rows"], e["prose_rows"],
            json.dumps(genre_stats, ensure_ascii=False),
            e["split_rows"],
            e["tribute_hits"], e["social_poems"],
            e["official_rank"], e["family_lineage"],
            e["anthology_hits"], e["textbook_hits"],
            e["fame_score"], e["fame_level"], e["author_category"],
            json.dumps(e["school_tags"], ensure_ascii=False) if e["school_tags"] else None,
            json.dumps(e["style_tags"], ensure_ascii=False) if e["style_tags"] else None,
            json.dumps(e["life_tags"], ensure_ascii=False) if e["life_tags"] else None,
            json.dumps(e["fact_tags"], ensure_ascii=False) if e["fact_tags"] else None,
            e["src"], updated_at,
        ))
        if len(batch) >= BATCH_SIZE:
            con.executemany(sql, batch)
            con.commit()
            total += len(batch)
            logger.info("  已写入 %d / %d", total, len(entities))
            batch = []
    if batch:
        con.executemany(sql, batch)
        con.commit()
        total += len(batch)
    logger.info("阶段5 写入完成：%d 个诗人实体（updated_at=%s）", total, updated_at)


# ============================================================
# 五、命令行入口
# ============================================================

def show_stats(con: sqlite3.Connection) -> None:
    """查看已入库统计（验收用）。"""
    total = con.execute("SELECT COUNT(*) FROM poets").fetchone()[0]
    print(f"\n实体总数：{total}")
    print("档位分布：")
    for lv, c in con.execute(
            "SELECT fame_level, COUNT(*) FROM poets GROUP BY fame_level ORDER BY fame_score DESC"):
        print(f"  {lv}: {c}")
    print("\nTop 20 名望：")
    for r in con.execute(
            "SELECT author, dynasty, fame_score, fame_level, tribute_hits, anthology_hits, "
            "textbook_hits, total_rows FROM poets ORDER BY fame_score DESC LIMIT 20"):
        print(f"  {r[0]}({r[1]}) {r[2]:.1f}/{r[3]}  致敬{r[4]} 选本{r[5]} 教材{r[6]} 行数{r[7]}")
    print("\n伪作者类别分布：")
    for cat, c in con.execute(
            "SELECT author_category, COUNT(*) AS c FROM poets WHERE author_category != 'normal' "
            "GROUP BY author_category ORDER BY c DESC"):
        print(f"  {cat}: {c}")


def explain(con: sqlite3.Connection, name: str) -> None:
    """分项解释某诗人的名望分构成（可解释性验收）。"""
    rows = con.execute(
        "SELECT author, dynasty, dynasty_raw, fame_score, fame_level, tribute_hits, social_poems,"
        " anthology_hits, textbook_hits, official_rank, family_lineage, total_rows, prose_rows,"
        " author_category, src FROM poets WHERE author LIKE ? ORDER BY fame_score DESC",
        (f"%{name}%",)).fetchall()
    if not rows:
        print(f"未找到诗人：{name}")
        return
    for r in rows:
        (author, dy, dy_raw, fame, lv, tribute, social, anth, tb,
         rank, fam, total, prose, cat, src) = r
        print(f"\n=== {author}（{dy}） fame={fame:.1f}/{lv} 类别={cat} 来源={src} ===")
        print(f"  底分            10.0")
        print(f"  致敬(次韵/拟作)  +{min(30.0, math.log1p(tribute) * 4.2):.1f}   (tribute_hits={tribute})")
        print(f"  权威选本         +{min(25.0, anth * 6.0):.1f}   (anthology_hits={anth})")
        print(f"  语文教材         +{min(15.0, tb * 2.5):.1f}   (textbook_hits={tb})")
        print(f"  社交唱和         +{min(10.0, math.log1p(social) * 1.8):.1f}   (social_poems={social})")
        print(f"  官职层级         +{OFFICIAL_SCORE.get(rank, 0)}   (official_rank={rank})")
        print(f"  朝代加成         +{DYNASTY_BONUS.get(dy, 0)}   (dynasty={dy})")
        print(f"  文学世家         +{5 if fam else 0}   (family_lineage={fam})")
        if dy == "近现代":
            print(f"  近现代补偿       +{S.NEAR_MODERN_WHITELIST.get(author, 0.0) * NEAR_MODERN_BONUS_SCALE:.1f}")
        print(f"  合计（封顶100）  = {fame:.1f}")
        print(f"  统计特征：总行数={total} 散文行={prose} 原始朝代分布={dy_raw}")


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 poets 诗人实体名望表")
    ap.add_argument("--limit", type=int, default=0, help="小批量试跑：只取前 N 个聚合分组")
    ap.add_argument("--dry-run", action="store_true", help="只计算不写库")
    ap.add_argument("--stats", action="store_true", help="查看已入库统计")
    ap.add_argument("--explain", metavar="NAME", help="分项解释某诗人得分")
    ap.add_argument("--no-relations", action="store_true", help="跳过致敬/社交扫描（快速调试）")
    args = ap.parse_args()

    if not Path(DB_PATH).exists():
        logger.error("数据库不存在：%s", DB_PATH)
        return 1

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(DDL)

    if args.stats:
        show_stats(con)
        con.close()
        return 0
    if args.explain:
        explain(con, args.explain)
        con.close()
        return 0

    t0 = time.time()
    entities = stage1_aggregate(con, limit=args.limit)
    if args.no_relations:
        for e in entities.values():
            e["tribute_hits"] = 0
            e["social_poems"] = 0
        logger.info("已跳过致敬/社交扫描（--no-relations）")
    else:
        _, buckets = build_alias_index()
        stage2_scan_relations(con, entities, buckets)
    stage3_apply_seeds(entities)
    stage4_compute_fame(entities)
    stage5_write(con, entities, dry_run=args.dry_run)
    logger.info("ETL 全部完成，总耗时 %.1fs", time.time() - t0)

    if not args.dry_run:
        show_stats(con)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
