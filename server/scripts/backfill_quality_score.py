"""诗词源头打标：quality_score 规则回填（2026-09-09 问题 4b）

用户定夺："像监督学习给数据打标签一样，从源头处理"——诗词本来就分好坏，
推荐算法不该在 200w 全量噪音里打捞。本脚本把 poets 表已有的诗人级打标
（fame_score 名望 / textbook_hits 教材入选 / anthology_hits 选本入选）
合成为作品级 quality_score（0-100）：

    base = fame_score * 0.85
    + 教材加成 min(15, textbook_hits * 3)     # 进过教材 = 公认名篇
    + 选本加成 min(10, anthology_hits * 1)    # 入选本 = 流传证据
    封顶 100
    当代/现代朝代封顶 45（质量池 excludes 现代标题党诗，除非极有名望）
    无 poets 匹配 → 0（大多数无名氏作品）

阈值消费：推荐召回只查 quality_score >= settings.poem_quality_threshold（默认 55，
对应约 7.5 万首质量池 = 全库 3.7%）。全库保留不删数据，可随时重跑回滚。

用法（在 server/ 目录）：
    python scripts/backfill_quality_score.py            # 全量回填
    python scripts/backfill_quality_score.py --dry-run  # 只统计不写库
"""
import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "poems.db"

# 当代/现代朝代关键词（dynasty 字段模糊匹配，封顶 45）
MODERN_DYNASTY_SQL = (
    "dynasty LIKE '%当代%' OR dynasty LIKE '%现代%' OR dynasty LIKE '%近现代%' "
    "OR dynasty LIKE '%民国%' OR dynasty LIKE '%当代%'"
)


def build_score_sql() -> str:
    """主回填：全部朝代统一按 fame/教材/选本合成（当代诗人如毛泽东 fame 56 也能得分），
    随后由 build_modern_cap_sql 把当代/现代朝代封顶 45。"""
    return """
    UPDATE poems SET quality_score = MIN(100, CAST(
        COALESCE(t.fame_score, 0) * 0.85
        + MIN(15, COALESCE(t.textbook_hits, 0) * 3.0)
        + MIN(10, COALESCE(t.anthology_hits, 0) * 1.0)
    AS INTEGER))
    FROM poets t
    WHERE poems.author = t.author
    """


def build_modern_cap_sql() -> str:
    return f"""
    UPDATE poems SET quality_score = MIN(COALESCE(quality_score, 0), 45)
    WHERE ({MODERN_DYNASTY_SQL})
      AND quality_score IS NOT NULL
    """


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计分布，不写库")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: 数据库不存在: {DB_PATH}")
        return 1

    db = sqlite3.connect(str(DB_PATH))
    cur = db.cursor()
    ver = sqlite3.sqlite_version
    major, minor = int(ver.split(".")[0]), int(ver.split(".")[1])
    if (major, minor) < (3, 33):
        print(f"ERROR: SQLite {ver} 不支持 UPDATE...FROM（需 >= 3.33）")
        return 1

    # 前置：确保列存在（init_db 的 migrate_schema 只在应用启动时跑）
    cols = {r[1] for r in cur.execute("PRAGMA table_info(poems)")}
    if "quality_score" not in cols:
        print("补列 poems.quality_score ...")
        cur.execute("ALTER TABLE poems ADD COLUMN quality_score INTEGER")
    if "translation" not in cols:
        print("补列 poems.translation ...")
        cur.execute("ALTER TABLE poems ADD COLUMN translation TEXT")

    if args.dry_run:
        for lo in (80, 70, 60, 55, 50):
            n = cur.execute(
                "SELECT COUNT(*) FROM poems p JOIN poets t ON p.author=t.author "
                "WHERE COALESCE(t.fame_score,0) >= ?", (lo,)).fetchone()[0]
            print(f"  预览 fame_score>={lo}: {n} 首")
        return 0

    print("回填 quality_score（古典朝代）...")
    cur.execute(build_score_sql())
    print(f"  更新 {cur.rowcount} 行")

    print("当代/现代朝代封顶 45 ...")
    cur.execute(build_modern_cap_sql())
    print(f"  更新 {cur.rowcount} 行")

    print("无 poets 匹配 → 0 ...")
    cur.execute("UPDATE poems SET quality_score = 0 WHERE quality_score IS NULL")
    print(f"  更新 {cur.rowcount} 行")

    db.commit()

    print("\n=== 回填后分布 ===")
    cur.execute("""SELECT
        SUM(CASE WHEN quality_score >= 80 THEN 1 ELSE 0 END),
        SUM(CASE WHEN quality_score >= 55 THEN 1 ELSE 0 END),
        SUM(CASE WHEN quality_score >= 30 THEN 1 ELSE 0 END),
        COUNT(*)
        FROM poems""")
    s80, s55, s30, total = cur.fetchone()
    print(f"  >=80: {s80} 首 | >=55（质量池）: {s55} 首 | >=30: {s30} 首 | 全库: {total} 首")

    print("\n=== 抽查名人 ===")
    for name in ("苏轼", "李白", "杜甫", "纳兰性德"):
        row = cur.execute(
            "SELECT MAX(quality_score) FROM poems WHERE author = ?", (name,)).fetchone()
        print(f"  {name}: 最高分 {row[0]}")
    row = cur.execute(
        "SELECT MAX(quality_score) FROM poems WHERE dynasty LIKE '%当代%'").fetchone()
    print(f"  当代朝代最高分（应 <= 45）: {row[0]}")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
