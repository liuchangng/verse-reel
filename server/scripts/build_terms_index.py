"""离线构建 poem_terms 倒排表脚本（ADR-005）

用途：把 200 万首诗词的 title+content 用 jieba 分词，写入 poem_terms 倒排表，
供「热点 → 主题词 → 诗词」毫秒级精确检索（替代 content LIKE 全表扫描 18.8s）。

特性：
- 同步 sqlite3（与 import_xml_fast.py 一致，避免 async 锁）
- 断点续跑：--resume 记录已处理的最大 poem_id 游标，跳过已建索引的诗词
- 批量 5000 条/事务
- 每首诗词每个 term 去重（set），term 过长（>64 字符）自动截断

用法：
    python scripts/build_terms_index.py            # 全量
    python scripts/build_terms_index.py --limit 1000   # 小批量试跑
    python scripts/build_terms_index.py --resume        # 续跑
    python scripts/build_terms_index.py --stats         # 查询统计
"""
import sys
import time
import logging
import sqlite3
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.chinese import segment, add_special_words

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 数据库路径（与 config.py / import_xml_fast.py 保持一致，指向 server/data）
DB_PATH = str(Path(__file__).parent.parent / "data" / "poems.db")

BATCH_SIZE = 5000
TERM_MAX_LEN = 64


def print_progress(current, total, start_time):
    """进度条（沿用 import_xml_fast.py 风格）"""
    elapsed = time.time() - start_time
    rate = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / rate if rate > 0 else 0
    pct = current / total * 100 if total > 0 else 0
    bar_len = 30
    filled = int(bar_len * pct / 100)
    bar = '=' * filled + '>' + '-' * (bar_len - filled)
    eta_str = f"{eta:.0f}s" if eta < 300 else f"{eta/60:.0f}min"
    print(f"\r  [{bar}] {pct:.1f}% | {current:,}/{total:,} | {rate:.0f}/s | ETA:{eta_str}", end='', flush=True)


def ensure_term_table(conn):
    """创建 poem_terms 表（索引延后到插完再建，避免逐行维护 B 树拖慢批量写入）"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poem_terms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poem_id INTEGER NOT NULL,
            term VARCHAR(64) NOT NULL,
            position VARCHAR(1) NOT NULL DEFAULT 'c'
        )
    """)
    conn.commit()
    logger.info("poem_terms 表结构就绪")


def get_max_processed_poem_id(conn):
    """断点续跑：返回已建索引的最大 poem_id（0 表示从零开始）"""
    cur = conn.execute("SELECT COALESCE(MAX(poem_id), 0) FROM poem_terms")
    return cur.fetchone()[0]


def get_processed_poem_ids(conn, limit=200000):
    """返回已索引的 poem_id 集合（用于跳过，防止重复；仅取最近一批避免内存过载）"""
    cur = conn.execute("SELECT DISTINCT poem_id FROM poem_terms ORDER BY poem_id DESC LIMIT ?", (limit,))
    return set(row[0] for row in cur.fetchall())


def build_index(conn, limit=None, resume=False):
    """主流程：遍历 poems → 分词 → 写 poem_terms"""
    add_special_words()  # 注入古诗词专有词

    # 批量写入期间删除索引，写完一次性重建（避免逐行维护 B 树，E: 盘随机 I/O 是主要瓶颈）
    conn.execute("DROP INDEX IF EXISTS idx_poem_terms_term")
    conn.execute("DROP INDEX IF EXISTS idx_poem_terms_poem_id")
    conn.commit()

    # 断点续跑：仅处理 max_processed 之后的 poem_id
    start_id = get_max_processed_poem_id(conn) if resume else 0

    # 统计总诗词数（用于进度条）
    total_poems = conn.execute("SELECT COUNT(*) FROM poems WHERE id > ?", (start_id,)).fetchone()[0]
    if limit:
        total_poems = min(total_poems, limit)
    logger.info(f"待处理诗词: {total_poems:,} 首（从 id > {start_id} 开始）")

    # 分批拉取 poems（按 id 升序，避免全量载入内存）
    batch = []
    inserted = 0
    processed = 0
    t0 = time.time()

    last_id = start_id
    while True:
        cur = conn.execute(
            "SELECT id, title, content, author FROM poems WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, BATCH_SIZE)
        )
        rows = cur.fetchall()
        if not rows:
            break

        for poem_id, title, content, author in rows:
            # 拼接标题 + 正文 + 作者分词。
            # 注意：数据已抽样验证为简体（5000 首仅 0.06% 边缘字形差异，非真繁体），
            # 故跳过 OpenCC to_simplified，省去每首 2 次无谓转换（约 8 分钟）。
            # 若将来导入繁体重度数据，重建索引时再按需开启。
            text_parts = []
            if title:
                text_parts.append(("t", title))
            if content:
                text_parts.append(("c", content))
            # #20260904-E2E: 补充作者名索引。
            # 原 ETL 仅索引 title+content，导致"苏轼《定风波》走红"类热点无法召回
            # 苏轼作品（因 poem_terms 中"苏轼"命中 939 首是"提及苏轼"的诗，
            # 而非"苏轼所作"的诗）。补充 author 字段（position='a'）后，
            # 作者名查询可直接召回该作者全部作品。
            if author and len(author) >= 2:
                text_parts.append(("a", author))

            # 逐部分分词并去重（每首每个 term 只记一次，position 标记首现）
            # hmm=False：纯词典切分，速度 2.3x（2020 vs 873 poems/s），
            # 且对古诗词更准确（不依赖 HMM 新词猜测，专有词已注入词典）。
            seen_terms = set()
            for position, text in text_parts:
                for word in segment(text, hmm=False):
                    # 倒排表检索键要求 ≥2 字、不超阈值
                    if len(word) < 2 or len(word) > TERM_MAX_LEN:
                        continue
                    if word in seen_terms:
                        continue
                    seen_terms.add(word)
                    batch.append((poem_id, word, position))

            processed += 1
            last_id = poem_id

            # 批量提交
            if len(batch) >= BATCH_SIZE * 4:  # 参考一次约 20000 行再提交
                conn.executemany(
                    "INSERT INTO poem_terms(poem_id, term, position) VALUES(?,?,?)",
                    batch
                )
                conn.commit()
                inserted += len(batch)
                batch = []
                print_progress(processed, total_poems, t0)

            if limit and processed >= limit:
                break

        if limit and processed >= limit:
            break

    # 最后一批
    if batch:
        conn.executemany(
            "INSERT INTO poem_terms(poem_id, term, position) VALUES(?,?,?)",
            batch
        )
        conn.commit()
        inserted += len(batch)

    elapsed = time.time() - t0
    logger.info(f"✅ 建索引完成: 处理 {processed:,} 首, 写入 {inserted:,} 行 term, 耗时 {elapsed:.0f}s")

    # 批量写入完成后一次性重建索引（供倒排初筛 + 按 poem_id 查询）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_poem_terms_term ON poem_terms(term)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_poem_terms_poem_id ON poem_terms(poem_id)")
    conn.commit()
    logger.info("poem_terms 索引已重建")

    # 将 WAL 合并回主库并截断 WAL 文件，保证外部读者拿到一致数据、不残留巨型 WAL
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    logger.info("WAL 已 checkpoint（TRUNCATE）")

    # 输出统计
    stats(conn)


def stats(conn):
    """输出 poem_terms 统计"""
    total_rows = conn.execute("SELECT COUNT(*) FROM poem_terms").fetchone()[0]
    distinct_terms = conn.execute("SELECT COUNT(DISTINCT term) FROM poem_terms").fetchone()[0]
    title_terms = conn.execute("SELECT COUNT(*) FROM poem_terms WHERE position='t'").fetchone()[0]
    logger.info(f"poem_terms: 共 {total_rows:,} 行, {distinct_terms:,} 个不同 term, 来源标题 {title_terms:,} 行")


def main():
    parser = argparse.ArgumentParser(description="构建 poem_terms 倒排表")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 首（调试用）")
    parser.add_argument("--resume", action="store_true", help="断点续跑（跳过已索引 poem_id）")
    parser.add_argument("--stats", action="store_true", help="仅输出统计信息")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    # 长构建中避免瞬时锁（如外部读者/写者）直接报 "database is locked"
    conn.execute("PRAGMA busy_timeout=30000")
    # 提速：WAL + synchronous=NORMAL 让批量 INSERT 的提交不再每次 fsync 整个库
    # （poem_terms 是可再生索引，NORMAL 在应用崩溃下仍 durable，仅断电有极小风险）
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    if not args.resume:
        # 全量重建：先清空旧 poem_terms（可能来自不同分词配置，如 HMM=True），
        # 保证整表分词策略一致；旧 4.5M 行来自早期慢速 HMM=True 构建，直接丢弃。
        conn.execute("DROP TABLE IF EXISTS poem_terms")
        conn.commit()
        # 关键：立即把 DROP（以及 WAL 中可能残留的历史 --resume 构建 INSERT）合并进主库
        # 并截断 WAL，确保后续批量写入从真正空表开始。否则末尾的 wal_checkpoint(TRUNCATE)
        # 会把"DROP 之前"的历史脏数据按 WAL 顺序一并合入主库，造成旧分词数据回灌（已踩坑）。
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.info("poem_terms 旧表已清空，准备全量重建（HMM=False）")
    ensure_term_table(conn)

    if args.stats:
        stats(conn)
        return

    build_index(conn, limit=args.limit, resume=args.resume)
    conn.close()


if __name__ == "__main__":
    main()
