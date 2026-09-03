"""P1 逻辑归组：同作者同题组诗归入同一 group_id。

决策 H1（hotspot v2.3 §10.6）：
- 逻辑归组（不改 DB 表结构，只加 group_id 列），子条保留原始 poem_id。
- 巨型同题组（单题 ≥ THRESHOLD_BIG_GROUP 条）soft-exclude：标 group_id=NULL。

用法：
    python scripts/assign_group_ids.py                  # 全量重算
    python scripts/assign_group_ids.py --dry-run         # 只统计不写入
    python scripts/assign_group_ids.py --threshold 100   # 巨型组门槛（默认 50）
    python scripts/assign_group_ids.py --resume          # 续跑（跳过已 assign 记录）
"""
import sys
import time
import logging
import sqlite3
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).parent.parent / "data" / "poems.db")
THRESHOLD_BIG_GROUP = 50
BATCH_SIZE = 5000


def ensure_group_id_column(conn: sqlite3.Connection) -> bool:
    """确保 poems.group_id 列存在（create_all 对旧表不补列）。"""
    cols = {row[0] for row in conn.execute("PRAGMA table_info(poems)").fetchall()}
    if "group_id" in cols:
        return False
    try:
        conn.execute("ALTER TABLE poems ADD COLUMN group_id INTEGER")
        conn.commit()
        logger.info("poems.group_id 列已新建")
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            logger.info("poems.group_id 列已存在，跳过")
            return False
        raise


def ensure_index(conn: sqlite3.Connection):
    """确保 (author, title) 联合索引存在，加速 GROUP BY。"""
    idxs = {row[1] for row in conn.execute("PRAGMA index_list(poems)").fetchall()}
    if "idx_poems_author_title" not in idxs:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_poems_author_title ON poems(author, title)"
        )
        conn.commit()
        logger.info("idx_poems_author_title 已创建")


def main(dry_run: bool, threshold: int, resume: bool):
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    changed = ensure_group_id_column(conn)
    ensure_index(conn)

    if not dry_run and not changed:
        assigned = conn.execute(
            "SELECT COUNT(*) FROM poems WHERE group_id IS NOT NULL"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM poems").fetchone()[0]
        if assigned > total * 0.95 and not resume:
            logger.info(f"poems.group_id 已分配 {assigned:,}/{total:,} 首，跳过重算（--resume 强制续跑）")
            conn.close()
            return

    # 1. 扫描同题组（分批，避免一次拉全 228k 组合）
    logger.info("扫描同题组诗...")
    groups_fetched = 0
    groups_buf: list[tuple[str, str, str, int]] = []
    offset = 0

    while True:
        batch = conn.execute(
            """
            SELECT author, title, GROUP_CONCAT(CAST(id AS TEXT), ',') AS ids, COUNT(*) AS cnt
            FROM poems
            WHERE author IS NOT NULL AND author != ''
              AND title IS NOT NULL AND title != ''
            GROUP BY author, title
            HAVING cnt > 1
            ORDER BY cnt DESC
            LIMIT ? OFFSET ?
            """,
            (5000, offset),
        ).fetchall()
        if not batch:
            break
        groups_buf.extend(batch)
        groups_fetched += len(batch)
        offset += 5000
        if groups_fetched % 50000 == 0:
            logger.info(f"  扫描进度: {groups_fetched:,} 组")

    logger.info(f"共 {len(groups_buf):,} 个同题组（cnt>1）")
    if dry_run:
        big = sum(1 for _, _, _, cnt in groups_buf if cnt >= threshold)
        poems_in_groups = sum(cnt for _, _, _, cnt in groups_buf)
        logger.info(f"DRY-RUN: {poems_in_groups:,} 首诗在组内，{big:,} 组为巨型组（≥{threshold}条）")
        conn.close()
        return

    # 2. 分配 group_id（分批写入）
    next_group_id = 1
    big_groups = 0
    updated = 0
    start = time.time()
    inserts_buf: list[tuple[int | None, int]] = []

    for author, title, id_str, cnt in groups_buf:
        poem_ids = [int(x) for x in id_str.split(",")]
        is_big = cnt >= threshold
        if is_big:
            big_groups += 1
        gid = next_group_id
        next_group_id += 1
        for pid in poem_ids:
            # 巨型组不赋 group_id（软排除，留 None）
            inserts_buf.append((gid if not is_big else None, pid))
            updated += 1
            if len(inserts_buf) >= BATCH_SIZE:
                conn.executemany(
                    "UPDATE poems SET group_id = ? WHERE id = ?",
                    inserts_buf,
                )
                conn.commit()
                inserts_buf.clear()
                elapsed = time.time() - start
                logger.info(
                    f"  progress: {updated:,} 首已写 | {elapsed:.1f}s | big_groups={big_groups}"
                )
    if inserts_buf:
        conn.executemany("UPDATE poems SET group_id = ? WHERE id = ?", inserts_buf)
        conn.commit()

    elapsed = time.time() - t0
    with_group = conn.execute(
        "SELECT COUNT(*) FROM poems WHERE group_id IS NOT NULL"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM poems").fetchone()[0]
    logger.info(f"✓ 完成: {updated:,} 首诗分配 group_id | 用时 {elapsed:.1f}s")
    logger.info(
        f"  group_id 非空: {with_group:,}/{total:,} "
        f"({with_group * 100 / max(total, 1):.1f}%)"
    )
    logger.info(f"  巨型组（≥{threshold}条，软排除，group_id=NULL）: {big_groups:,} 组")
    logger.info(f"  参与候选池的同题组: {next_group_id - 1:,} 组")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="P1 逻辑归组：同题组诗分配 group_id")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    ap.add_argument(
        "--threshold",
        type=int,
        default=THRESHOLD_BIG_GROUP,
        help=f"巨型组门槛（默认 {THRESHOLD_BIG_GROUP}）",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="续跑：跳过已有 group_id 记录",
    )
    args = ap.parse_args()
    main(args.dry_run, args.threshold, args.resume)
