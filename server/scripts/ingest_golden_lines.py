"""P2 金句库注入：把手工 curate 的经典金句写入 poem_terms（position='g'）。

设计意图（hotspot v2.1 §4.2 + v2.3 §8.3）：
- jieba 分词把"长风破浪会有时"切成 5 个词，无法作为整体召回 key。
- 外部金句库整体注入 position='g'，使金句成为独立检索 key，加权 3x。

用法：
    python scripts/ingest_golden_lines.py          # 全量注入
    python scripts/ingest_golden_lines.py --dry-run  # 只统计不写入
    python scripts/ingest_golden_lines.py --force    # 强制重灌（先删旧 golden）
"""
import sys
import json
import time
import logging
import sqlite3
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).parent.parent / "data" / "poems.db")
GOLDEN_DB = str(Path(__file__).parent.parent / "data" / "golden_lines.json")
BATCH_SIZE = 5000


def ensure_golden_index(conn: sqlite3.Connection):
    """确保 poem_terms.position 列上的索引存在（加速 position='g' 过滤）。"""
    idxs = {row[1] for row in conn.execute("PRAGMA index_list(poem_terms)").fetchall()}
    if "idx_poem_terms_position" not in idxs:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_poem_terms_position ON poem_terms(position)")
        conn.commit()
        logger.info("idx_poem_terms_position 已创建")


def main(dry_run: bool, force: bool):
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    ensure_golden_index(conn)

    # 1. 加载金句库
    with open(GOLDEN_DB, "r", encoding="utf-8") as f:
        golden_lines: list[dict] = json.load(f)
    logger.info(f"金句库加载: {len(golden_lines)} 句")

    # 2. 获取已存在的 golden term（避免重复）
    existing = set()
    if not force:
        rows = conn.execute("SELECT DISTINCT term FROM poem_terms WHERE position='g'").fetchall()
        existing = {r[0] for r in rows}
        logger.info(f"已注入 golden term: {len(existing):,} 个")
    else:
        # 强制重灌：清空旧 golden
        deleted = conn.execute("DELETE FROM poem_terms WHERE position='g'").rowcount
        conn.commit()
        logger.info(f"已清除 {deleted:,} 条旧 golden term")

    # 3. 为每句金句找匹配的诗（content 包含金句的整句）
    new_terms: list[tuple[int, str, str]] = []
    new_poems: set[int] = set()
    unmatched: list[str] = []
    skipped: int = 0

    for entry in golden_lines:
        line = entry["line"]
        author = entry.get("author", "")
        if line in existing:
            skipped += 1
            continue
        # 精确匹配：在 poems.content 或 title 中找包含该金句的诗
        rows = conn.execute(
            "SELECT id FROM poems WHERE author=? AND (content LIKE ? OR title LIKE ?)",
            (author, f"%{line}%", f"%{line}%"),
        ).fetchall()
        if rows:
            # 每个匹配 poem 都加一条 golden term
            for (pid,) in rows:
                new_terms.append((pid, line, "g"))
                new_poems.add(pid)
        else:
            unmatched.append(line)

    if dry_run:
        logger.info(f"DRY-RUN: 新增 {len(new_terms):,} 条 golden term（覆盖 {len(new_poems):,} 首诗）")
        if unmatched:
            logger.info(f"未匹配金句（{len(unmatched)} 条）: {unmatched[:10]}...")
        conn.close()
        return

    # 4. 批量写入
    start = time.time()
    inserts_buf: list[tuple[int, str, str]] = []
    written = 0
    for pid, term, pos in new_terms:
        inserts_buf.append((pid, term, pos))
        written += 1
        if len(inserts_buf) >= BATCH_SIZE:
            conn.executemany(
                "INSERT INTO poem_terms (poem_id, term, position) VALUES (?, ?, ?)",
                inserts_buf,
            )
            conn.commit()
            inserts_buf.clear()
            elapsed = time.time() - start
            rate = written / elapsed if elapsed > 0 else 0
            logger.info(f"  progress: {written:,} golden terms | {elapsed:.1f}s | {rate:.0f}/s")
    if inserts_buf:
        conn.executemany(
            "INSERT INTO poem_terms (poem_id, term, position) VALUES (?, ?, ?)",
            inserts_buf,
        )
        conn.commit()

    elapsed = time.time() - t0
    final_g = conn.execute("SELECT COUNT(*) FROM poem_terms WHERE position='g'").fetchone()[0]
    logger.info(f"✓ 完成: 新增 {written:,} 条 golden term | 累计 {final_g:,} 条 | 用时 {elapsed:.1f}s")
    logger.info(f"  覆盖 {len(new_poems):,} 首不同的诗")
    if unmatched:
        logger.warning(f"未匹配金句（{len(unmatched)} 条）: {unmatched[:10]}...")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="P2 金句库注入：写入 position='g' 金句 term")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    ap.add_argument("--force", action="store_true", help="强制重灌（先删旧 golden）")
    args = ap.parse_args()
    main(args.dry_run, args.force)
