"""课标篇目 → poems.id 匹配脚本。

流程：读 docs/curriculum_primary.json（篇名/作者/朝代），对每条在 poems 表
按 (title LIKE 篇名 AND author LIKE 作者) 查候选，按规则挑最优 poem_id 写回。
不匹配的输出未命中清单，用作者+正文首句兜底。

用法：
  cd server
  ./.venv/Scripts/python.exe scripts/match_curriculum.py
"""
import asyncio
import json
import logging
from pathlib import Path

from sqlalchemy import select

from app.models.poem import Poem

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CURRICULUM = PROJECT_ROOT / "docs" / "curriculum_primary.json"


async def main():
    data = json.loads(CURRICULUM.read_text(encoding="utf-8"))
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config import settings
    engine = create_async_engine(settings.database_url)
    matched = 0
    unmatched = []
    async with engine.connect() as conn:
        for item in data["items"]:
            title = item["title"]
            author = item.get("author", "")
            first_line = item.get("first_line", "")
            pick = None
            # 层1：精确 标题+作者
            stmt = select(Poem.id, Poem.title, Poem.author, Poem.dynasty).where(
                Poem.title == title,
            )
            if author:
                stmt = stmt.where(Poem.author == author)
            rows = (await conn.execute(stmt)).fetchall()
            if rows:
                pick = rows[0]
            else:
                # 层2：标题 LIKE 篇名（同题组集合名如"凉州词二首"/"渭城曲 送元二使安西"）
                base = title.rstrip("二首")
                stmt = select(Poem.id, Poem.title, Poem.author, Poem.dynasty).where(
                    Poem.title.like(f"%{base}%"),
                )
                if author:
                    stmt = stmt.where(Poem.author == author)
                rows = (await conn.execute(stmt)).fetchall()
                if rows:
                    pick = rows[0]
                else:
                    # 层3：作者 + 正文首句兜底
                    if first_line:
                        stmt = (
                            select(Poem.id, Poem.title, Poem.author, Poem.dynasty)
                            .where(Poem.content.like(f"{first_line}%"))
                        )
                        if author:
                            stmt = stmt.where(Poem.author == author)
                        rows = (await conn.execute(stmt)).fetchall()
                        if rows:
                            pick = rows[0]
            if pick:
                item["poem_id"] = pick[0]
                item["matched"] = True
                item["match_note"] = f"{pick[1]} / {pick[2]}"
                matched += 1
            else:
                item["matched"] = False
                unmatched.append(item)
    data["matched_count"] = matched
    data["unmatched_count"] = len(unmatched)
    data["unmatched"] = [
        {"no": u["no"], "title": u["title"], "author": u.get("author", ""), "dynasty": u.get("dynasty", "")}
        for u in unmatched
    ]
    CURRICULUM.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== 匹配完成：{matched}/{len(data['items'])} 命中 ===")
    if unmatched:
        print(f"未命中 {len(unmatched)} 首：")
        for u in unmatched:
            print(f"  - #{u['no']} {u['title']} ({u.get('author','?')}) [{u.get('dynasty','')}]")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
