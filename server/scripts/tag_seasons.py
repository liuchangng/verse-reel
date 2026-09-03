"""P1 季节标签：按标题/内容关键词批量打标 season 标签。

决策 H3（hotspot v2.3 §10.6）：合并 + 季节同时上。

标签体系（季节类）：
- 节令优先：春节/元宵 → 清明/踏青 → 端午 → 中秋 → 重阳 → 冬至/腊八
- 四季：春/夏/秋/冬（含典型意象词）

优先级：节令 > 季节；多节令命中取最先出现的；最多 2 个标签/首。

用法：
    python scripts/tag_seasons.py                   # 全量重算
    python scripts/tag_seasons.py --dry-run          # 只统计不写入
    python scripts/tag_seasons.py --resume           # 续跑（跳过已有 season 标签的诗）
"""
import sys
import time
import logging
import sqlite3
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).parent.parent / "data" / "poems.db")
BATCH_SIZE = 10000

# 季节关键词规则（节令优先，顺序即权重）
SEASON_RULES: list[tuple[str, list[str]]] = [
    ("春节",    ["春节", "元宵", "元夜", "岁首", "正旦"]),
    ("清明",    ["清明", "踏青"]),
    ("端午",    ["端午", "端阳", "粽子", "龙舟", "蒲月"]),
    ("七夕",    ["七夕", "乞巧", "牛女", "星桥"]),
    ("中秋",    ["中秋", "月饼", "桂子", "团圆"]),
    ("重阳",    ["重阳", "登高", "茱萸", "菊花"]),
    ("冬至",    ["冬至", "数九", "冬尽"]),
    ("腊八",    ["腊八", "腊日", "腊祭"]),
    ("春",      ["春", "芳", "桃", "柳", "燕", "莺", "韶光", "东风", "细雨", "芳草", "杏花", "春雨"]),
    ("夏",      ["夏", "荷", "莲", "蝉", "炎", "暑", "蛙", "榴花", "竹阴", "荷风", "夏木"]),
    ("秋",      ["秋", "菊", "枫", "雁", "霜", "梧", "桐", "桂", "砧声", "寒蝉", "秋月", "秋风"]),
    ("冬",      ["冬", "雪", "梅", "寒", "岁", "腊", "冰", "琼", "朔风", "冬雪", "冬梅"]),
]


def ensure_poem_tags_table(conn: sqlite3.Connection) -> bool:
    """确保 poem_tags 表 + 索引存在。"""
    try:
        conn.execute("SELECT 1 FROM poem_tags LIMIT 0")
        has_table = True
    except Exception:
        has_table = False

    if not has_table:
        conn.execute("""
            CREATE TABLE poem_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poem_id INTEGER NOT NULL,
                tag VARCHAR(32) NOT NULL,
                tag_type VARCHAR(16) NOT NULL DEFAULT 'season',
                confidence INTEGER NOT NULL DEFAULT 100,
                source VARCHAR(32) NOT NULL DEFAULT 'rule',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX idx_poem_tags_tag ON poem_tags(tag)")
        conn.execute("CREATE INDEX idx_poem_tags_poem ON poem_tags(poem_id)")
        conn.execute("CREATE INDEX idx_poem_tags_type ON poem_tags(tag_type)")
        conn.commit()
        logger.info("poem_tags 表 + 索引已新建")
    else:
        # 幂等补索引
        for idx_name in ("idx_poem_tags_tag", "idx_poem_tags_poem", "idx_poem_tags_type"):
            try:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON poem_tags({idx_name.split('_')[-1]})")
            except Exception:
                pass
        conn.commit()
    return has_table


def tag_one_poem(title: str, content: str) -> list[str]:
    """对一首诗，按优先级返回匹配的 season 标签列表（去重，最多 2 个）。"""
    text = (title or "") + " " + (content or "")
    matched: list[str] = []
    seen: set[str] = set()
    for season, keywords in SEASON_RULES:
        if season in seen:
            continue
        for kw in keywords:
            if kw in text:
                matched.append(season)
                seen.add(season)
                break
        if len(matched) >= 2:
            break
    return matched


def main(dry_run: bool, resume: bool):
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    new_table = ensure_poem_tags_table(conn)

    if resume:
        has_tag = set(row[0] for row in conn.execute(
            "SELECT DISTINCT poem_id FROM poem_tags WHERE tag_type = 'season'"
        ).fetchall())
        logger.info(f"已有 {len(has_tag):,} 首诗有 season 标签，续跑跳过")
    else:
        has_tag = set()
        if not dry_run and not new_table:
            # 幂等：清空旧 season 标签，保留其他类型（theme/emotion 等未来扩展）
            deleted = conn.execute(
                "DELETE FROM poem_tags WHERE tag_type = 'season'"
            ).rowcount
            conn.commit()
            logger.info(f"已清空 {deleted:,} 条旧 season 标签（保留其他类型）")

    total_poems = conn.execute("SELECT COUNT(*) FROM poems").fetchone()[0]
    total_tagged = 0
    start = time.time()
    offset = 0
    inserts_buf: list[tuple[int, str]] = []

    while True:
        rows = conn.execute(
            "SELECT id, title, content FROM poems ORDER BY id LIMIT ? OFFSET ?",
            (BATCH_SIZE, offset),
        ).fetchall()
        if not rows:
            break
        for pid, title, content in rows:
            if pid in has_tag and resume:
                continue
            seasons = tag_one_poem(title, content)
            if seasons:
                total_tagged += len(seasons)
                if not dry_run:
                    inserts_buf.extend((pid, s) for s in seasons)
                    if len(inserts_buf) >= BATCH_SIZE:
                        conn.executemany(
                            "INSERT INTO poem_tags (poem_id, tag, tag_type, confidence, source) VALUES (?, ?, 'season', 100, 'rule')",
                            inserts_buf,
                        )
                        conn.commit()
                        inserts_buf.clear()
        offset += BATCH_SIZE
        if offset % 100000 == 0 or offset >= total_poems:
            elapsed = time.time() - start
            rate = offset / elapsed if elapsed > 0 else 0
            eta = (total_poems - offset) / rate if rate > 0 else 0
            logger.info(
                f"  progress: {offset:,}/{total_poems:,} ({offset * 100 / max(total_poems, 1):.1f}%) "
                f"| {elapsed:.0f}s elapsed | {rate:.0f} poems/s | ETA:{eta:.0f}s | tags={total_tagged:,}"
            )
        if not dry_run:
            conn.commit()

    #  flush last buf
    if inserts_buf and not dry_run:
        conn.executemany(
            "INSERT INTO poem_tags (poem_id, tag, tag_type, confidence, source) VALUES (?, ?, 'season', 100, 'rule')",
            inserts_buf,
        )
        conn.commit()

    elapsed = time.time() - t0
    logger.info(f"✓ 完成: {total_tagged:,} 条 season 标签 | 用时 {elapsed:.1f}s")
    if not dry_run:
        summary = conn.execute(
            "SELECT tag, COUNT(*) FROM poem_tags WHERE tag_type='season' GROUP BY tag ORDER BY COUNT(*) DESC"
        ).fetchall()
        logger.info(f"标签分布: {dict(summary)}")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="P1 季节标签：按标题/内容关键词批量打标")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    ap.add_argument("--resume", action="store_true", help="续跑：跳过已有 season 标签的诗")
    args = ap.parse_args()
    main(args.dry_run, args.resume)
