"""quality_score 规则回填守护测试（2026-09-09 问题 4b）

在内存 SQLite 里建迷你 poets/poems 表，执行 scripts/backfill_quality_score.py
导出的同款 SQL，验证合成公式与当代封顶语义。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backfill_quality_score import build_score_sql, build_modern_cap_sql  # noqa: E402


def _mk_db():
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("""CREATE TABLE poets (
        id INTEGER PRIMARY KEY, author TEXT, fame_score REAL,
        textbook_hits INTEGER, anthology_hits INTEGER, dynasty TEXT)""")
    cur.execute("""CREATE TABLE poems (
        id INTEGER PRIMARY KEY, author TEXT, dynasty TEXT,
        quality_score INTEGER, translation TEXT)""")
    # 名人（S 档满分） / 有教材选本的 / 普通清代 / 当代名人 / 无匹配
    cur.executemany("INSERT INTO poets VALUES (?,?,?,?,?,?)", [
        (1, "苏轼", 100.0, 14, 11, "宋"),
        (2, "某名家", 60.0, 5, 0, "清"),
        (3, "某普通", 20.0, 0, 0, "清"),
        (4, "毛泽东", 56.17, 0, 0, "近现代"),
    ])
    cur.executemany("INSERT INTO poems (id, author, dynasty) VALUES (?,?,?)", [
        (1, "苏轼", "宋"),
        (2, "某名家", "清"),
        (3, "某普通", "清"),
        (4, "毛泽东", "当代"),
        (5, "无名氏", "清"),
    ])
    db.commit()
    return db


def test_score_formula_and_modern_cap():
    db = _mk_db()
    cur = db.cursor()
    cur.execute(build_score_sql())
    cur.execute(build_modern_cap_sql())
    cur.execute("UPDATE poems SET quality_score = 0 WHERE quality_score IS NULL")
    scores = dict(cur.execute("SELECT id, quality_score FROM poems").fetchall())

    # 苏轼: 100*0.85 + min(15, 14*3=42→15) + min(10, 11*1=11→10) = 85+15+10 = 110 → 封顶 100
    assert scores[1] == 100
    # 某名家: 60*0.85=51 + min(15,15)=15 + 0 = 66
    assert scores[2] == 66
    # 某普通: 20*0.85 = 17
    assert scores[3] == 17
    # 毛泽东: 56.17*0.85=47.7 → CAST=47 → 当代封顶 45
    assert scores[4] == 45
    # 无 poets 匹配 → 0
    assert scores[5] == 0


def test_quality_pool_size_on_real_db():
    """真实库只读校验：回填已执行过，全库不应有 NULL，质量池规模合理。"""
    db_path = Path(__file__).resolve().parent.parent / "data" / "poems.db"
    if not db_path.exists():
        pytest.skip("真实库不存在（CI 环境）")
    db = sqlite3.connect(str(db_path))
    cur = db.cursor()
    nulls = cur.execute("SELECT COUNT(*) FROM poems WHERE quality_score IS NULL").fetchone()[0]
    assert nulls == 0, f"回填后不应有 NULL quality_score，实际 {nulls}"
    pool = cur.execute(
        "SELECT COUNT(*) FROM poems WHERE quality_score >= 55").fetchone()[0]
    total = cur.execute("SELECT COUNT(*) FROM poems").fetchone()[0]
    # 质量池应在全库 1%~10% 之间（当前 ~2.9%），防公式失配
    assert total * 0.01 <= pool <= total * 0.10, f"质量池 {pool}/{total} 超出合理区间"
    # 名家满分、当代封顶
    su = cur.execute("SELECT MAX(quality_score) FROM poems WHERE author='苏轼'").fetchone()[0]
    assert su == 100
    modern_max = cur.execute(
        "SELECT MAX(quality_score) FROM poems WHERE dynasty LIKE '%当代%'").fetchone()[0]
    assert modern_max is not None and modern_max <= 45
