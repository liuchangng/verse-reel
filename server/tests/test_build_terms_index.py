"""build_terms_index 脚本单元测试（独立内存库，不碰生产 poems.db）"""
import sys
import os
import sqlite3
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import build_terms_index as b


@pytest.fixture
def mem_conn():
    """内存 SQLite，建 poems + poem_terms 表，塞样本诗词"""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
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
    """)
    b.ensure_term_table(conn)
    conn.executemany(
        "INSERT INTO poems(title, content, author, dynasty, genre) VALUES(?,?,?,?,?)",
        [
            ("静夜思", "床前明月光疑是地上霜举头望明月低头思故乡", "李白", "唐", "五言绝句"),
            ("登鹳雀楼", "白日依山尽黄河入海流欲穷千里目更上一层楼", "王之涣", "唐", "五言绝句"),
            ("春晓", "春眠不觉晓处处闻啼鸟夜来风雨声花落知多少", "孟浩然", "唐", "五言绝句"),
        ]
    )
    conn.commit()
    return conn


class TestBuildTermsIndex:
    """倒排表构建脚本"""

    def test_build_index_writes_rows(self, mem_conn):
        """build_index 为样本诗词写入 term 行"""
        b.build_index(mem_conn, limit=None, resume=False)
        rows = mem_conn.execute("SELECT COUNT(*) FROM poem_terms").fetchone()[0]
        assert rows > 0

    def test_term_contains_keyword(self, mem_conn):
        """『明月』应被切分并写入 poem_terms"""
        b.add_special_words()
        b.build_index(mem_conn, limit=None, resume=False)
        found = mem_conn.execute(
            "SELECT COUNT(*) FROM poem_terms WHERE term LIKE '%明月%'"
        ).fetchone()[0]
        assert found > 0

    def test_position_field(self, mem_conn):
        """position 标记来源（t=标题 / c=正文）"""
        b.build_index(mem_conn, limit=None, resume=False)
        title_terms = mem_conn.execute(
            "SELECT COUNT(*) FROM poem_terms WHERE position='t'"
        ).fetchone()[0]
        content_terms = mem_conn.execute(
            "SELECT COUNT(*) FROM poem_terms WHERE position='c'"
        ).fetchone()[0]
        assert title_terms > 0
        assert content_terms > 0

    def test_terms_deduped_per_poem(self, mem_conn):
        """每首诗词的同一 term 只记一次（set 去重）"""
        b.build_index(mem_conn, limit=None, resume=False)
        # 检查是否存在同一 poem_id + term 的重复行
        dup = mem_conn.execute(
            "SELECT poem_id, term, COUNT(*) c FROM poem_terms GROUP BY poem_id, term HAVING c > 1"
        ).fetchall()
        assert dup == []

    def test_resume_skips_processed(self, mem_conn):
        """resume 模式下，已索引的最大 poem_id 之后的才处理"""
        # 先处理全部
        b.build_index(mem_conn, limit=None, resume=False)
        total_after_first = mem_conn.execute("SELECT COUNT(*) FROM poem_terms").fetchone()[0]
        # 再 resume 跑一次：不应新增（最大 poem_id 已覆盖全部）
        b.build_index(mem_conn, limit=None, resume=True)
        total_after_resume = mem_conn.execute("SELECT COUNT(*) FROM poem_terms").fetchone()[0]
        assert total_after_resume == total_after_first
