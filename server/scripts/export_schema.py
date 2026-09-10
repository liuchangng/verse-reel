"""从 SQLAlchemy 模型（唯一事实源）导出 SQLite 建表 DDL。

用法：python scripts/export_schema.py [输出文件]
     不带参数打印到 stdout。
     用于开源 README：数据库二进制不入库，别人 clone 后按这份 DDL 即可重建 schema。
     生成方式：import 全部模型（app.models 自动注册所有 Table）→ 对内存 SQLite
     跑 create_all → 从 sqlite_master 取真实 CREATE 语句（与运行时方言一致）。
"""
import sys
import os

from sqlalchemy import create_engine
from app.database import Base  # 声明式基类定义在 database.py

# 必须逐个 import 全部模型模块（与 app.database.init_db 一致），
# 否则 __init__.py 未 re-export 的 hotspot/job/system_setting 不会注册进 Base.metadata。
from app.models import (  # noqa: F401
    poem, task, script, poem_term, poem_tag, hotspot, job, system_setting, poet,
)

TABLES = [t for t in Base.metadata.sorted_tables]


def build_ddl() -> str:
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    lines = []
    # 按 sorted_tables 顺序（外键依赖已排好）取真实 DDL
    with eng.connect() as conn:
        import re
        names = [t.name for t in TABLES]
        for tbl in TABLES:
            # 直接读 sqlite_master 的该表 DDL
            row = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=? AND sql IS NOT NULL",
                (tbl.name,),
            ).fetchone()
            ddl = row[0] if row else None
            if ddl:
                lines.append(f"-- ===== 表 {tbl.name} =====")
                lines.append(ddl + ";")
                lines.append("")
            # 该表上的索引（sqlite_master type='index'，排除自动 pk）
            idx_rows = conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (tbl.name,),
            ).fetchall()
            for name, idx_sql in idx_rows:
                if idx_sql:
                    lines.append(idx_sql + ";")
                    lines.append("")
    eng.dispose()
    return "\n".join(lines)


if __name__ == "__main__":
    ddl = build_ddl()
    header = "-- 古诗词短视频工厂 数据库 schema（SQLite，poems.db）\n"
    header += f"-- 由 scripts/export_schema.py 从 SQLAlchemy 模型自动生成，勿手改。\n"
    header += f"-- 共 {len(TABLES)} 张表：" + ", ".join(t.name for t in TABLES) + "\n\n"
    out = header + ddl
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8") as f:
            f.write(out)
        print(f"已写出 {sys.argv[1]}（{len(TABLES)} 张表）")
    else:
        print(out)
