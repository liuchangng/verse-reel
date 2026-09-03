"""快速 XML 导入脚本 - 同步模式，支持 200 万级数据"""
import sys
import time
import logging
from pathlib import Path
from lxml import etree
from opencc import OpenCC

sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).parent.parent / "data" / "poems.db")
XML_PATH = "G:/cnkgraph/CNKGraph.Writings.xml"

cc = OpenCC('t2s')  # 繁体 → 简体


def simplify(text):
    """繁简转换"""
    if not text:
        return text
    return cc.convert(text)


def print_progress(current, total, start_time):
    """进度条"""
    elapsed = time.time() - start_time
    rate = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / rate if rate > 0 else 0
    pct = current / total * 100 if total > 0 else 0
    bar_len = 30
    filled = int(bar_len * pct / 100)
    bar = '=' * filled + '>' + '-' * (bar_len - filled)
    eta_str = f"{eta:.0f}s" if eta < 300 else f"{eta/60:.0f}min"
    print(f"\r  [{bar}] {pct:.1f}% | {current:,}/{total:,} | {rate:.0f}/s | ETA:{eta_str}", end='', flush=True)


def create_tables(conn):
    """创建表"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poems (
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
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cnk_id ON poems(cnk_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_title ON poems(title)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_author ON poems(author)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dynasty ON poems(dynasty)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_genre ON poems(genre)")
    conn.commit()
    logger.info("表结构就绪")


def get_existing_ids(conn):
    """获取已导入的 cnk_id 集合"""
    cur = conn.execute("SELECT cnk_id FROM poems")
    return set(row[0] for row in cur.fetchall())


def parse_and_insert(xml_path, conn, batch_size=5000):
    """解析 XML 并批量插入"""
    t0 = time.time()
    imported = 0
    skipped = 0
    failed = 0
    existing_ids = get_existing_ids(conn)

    logger.info(f"开始解析: {xml_path}")
    context = etree.iterparse(xml_path, events=("end",), tag="Poem")

    batch = []
    for event, elem in context:
        try:
            cnk_id = int(elem.get("Id", 0))
            if cnk_id == 0:
                elem.clear()
                continue

            dynasty = elem.get("D", "")
            author_trad = elem.get("AU", "")
            genre = elem.get("T", "")
            rhyme = elem.get("R", "")

            # 标题
            title_elem = elem.find("Title")
            title_trad = title_elem.get("C", "") if title_elem is not None else ""

            # 内容
            content_parts = []
            for ju in elem.findall(".//Ju"):
                c = ju.get("C", "")
                if c:
                    content_parts.append(c)
            content_trad = "".join(content_parts)

            # 跳过重复
            if cnk_id in existing_ids:
                skipped += 1
                elem.clear()
                continue

            batch.append((
                cnk_id,
                simplify(title_trad), title_trad,
                simplify(author_trad), author_trad,
                simplify(dynasty), simplify(genre),
                simplify(content_trad), content_trad,
                simplify(rhyme) if rhyme else None,
            ))
            imported += 1
            existing_ids.add(cnk_id)

        except Exception as e:
            failed += 1

        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]

        # 批量提交
        if len(batch) >= batch_size:
            conn.executemany(
                "INSERT INTO poems(cnk_id,title,title_traditional,author,author_traditional,dynasty,genre,content,content_traditional,rhyme) VALUES(?,?,?,?,?,?,?,?,?,?)",
                batch
            )
            conn.commit()
            processed = imported + skipped
            print_progress(processed, imported + skipped + failed, t0)
            batch = []

    # 最后一批
    if batch:
        conn.executemany(
            "INSERT INTO poems(cnk_id,title,title_traditional,author,author_traditional,dynasty,genre,content,content_traditional,rhyme) VALUES(?,?,?,?,?,?,?,?,?,?)",
            batch
        )
        conn.commit()

    return imported, skipped, failed


def show_stats(conn):
    """统计信息"""
    total = conn.execute("SELECT count(*) FROM poems").fetchone()[0]
    print(f"\n\n{'='*50}")
    print(f"  诗词总数: {total:,}")
    print(f"{'='*50}")

    dynasties = conn.execute("SELECT dynasty, count(*) FROM poems GROUP BY dynasty ORDER BY count(*) DESC LIMIT 10").fetchall()
    print("\n朝代分布 (Top 10):")
    for d, c in dynasties:
        print(f"  {d or '未知':10} {c:>10,}")

    genres = conn.execute("SELECT genre, count(*) FROM poems GROUP BY genre ORDER BY count(*) DESC LIMIT 10").fetchall()
    print("\n体裁分布 (Top 10):")
    for g, c in genres:
        print(f"  {g or '未知':10} {c:>10,}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="导入 CNKGraph XML")
    parser.add_argument("--file", default=XML_PATH)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    if args.stats:
        conn = sqlite3.connect(DB_PATH)
        show_stats(conn)
        conn.close()
        sys.exit(0)

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    imported, skipped, failed = parse_and_insert(args.file, conn, args.batch_size)
    elapsed = time.time() - time.monotonic()
    print()
    print(f"\n导入结果:")
    print(f"  新增: {imported:,} 首")
    print(f"  跳过: {skipped:,} 首")
    print(f"  失败: {failed:,} 首")
    print(f"  耗时: {elapsed:.1f} 秒 ({imported/elapsed:.0f} 首/秒)")
    show_stats(conn)
    conn.close()
