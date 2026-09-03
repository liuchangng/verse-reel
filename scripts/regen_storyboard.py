"""为已存在任务用新 critic（带 narration 纯中文旁白）重建 storyboard。

用法：python scripts/regen_storyboard.py [task_id ...]
默认处理 task 1 和 3（杜甫《登高》、李清照《声声慢》）。
重建后配合 run_stage(subtitle, force=True) 即可逐镜以图定音重烧成片。
"""
import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath("server"))
from app.services.critic import critic_service

DB = "server/data/poems.db"


async def main():
    ids = [int(x) for x in sys.argv[1:]] or [1, 3]
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    for tid in ids:
        row = con.execute("SELECT script FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row or not row["script"]:
            print(f"task {tid}: 无 script，跳过")
            continue
        script = row["script"]
        print(f"task {tid}: 重新生成 storyboard（带 narration）...")
        raw = await critic_service.generate_storyboard(script)
        try:
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0]
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0]
            data = json.loads(raw.strip())
            for it in data:
                if not it.get("narration"):
                    it["narration"] = it.get("description", "")
            con.execute("UPDATE tasks SET storyboard=? WHERE id=?",
                        (json.dumps(data, ensure_ascii=False), tid))
            con.commit()
            print(f"  task {tid}: storyboard 更新，{len(data)} 镜")
        except Exception as e:
            print(f"  task {tid} 解析失败: {e}")
    con.close()


if __name__ == "__main__":
    asyncio.run(main())
