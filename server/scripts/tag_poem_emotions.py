"""诗词情感标签批量打标（2026-09-09 问题 4b，用户定夺"像监督学习一样打标"）

缺口：poem_tags 现有 172 万条几乎全是季节节令标签，没有诗词真正的情感倾向
（压抑/积极/消极/爱国/不得志/思乡…）。本脚本用 LLM 批量为质量池打 emotion 标签。

设计：
- 只打质量池（quality_score >= 阈值，默认 55，约 5.9 万首）——源头有质量的作品
  才值得花 LLM token；全库 200w 首不打（打不完也没必要）。
- 固定词表分类（防 LLM 自造标签污染标签空间）；每次调用批量 10 首；
  写入 poem_tags(tag_type='emotion', source='ai_infer', confidence=80)。
- 幂等：已有 emotion 标签的诗跳过（--force 覆盖）。
- 断点续跑：按 poem_id 升序处理，中断后重跑自动续。

成本估算：5.9 万首 ÷ 10 首/次 ≈ 5900 次调用，18 RPM ≈ 5.5 小时。建议挂后台跑。

用法（在 server/ 目录）：
    python scripts/tag_poem_emotions.py --limit 10     # 试点 10 首
    python scripts/tag_poem_emotions.py                # 全量质量池（数小时）
    python scripts/tag_poem_emotions.py --dry-run      # 只统计不打标
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, func, delete  # noqa: E402

from app.database import async_session_factory, init_db  # noqa: E402
from app.models.poem import Poem  # noqa: E402
from app.models.poem_tag import PoemTag  # noqa: E402
from app.services.agnes import agnes_client  # noqa: E402

# 固定情感词表（闭集分类，防 LLM 自造标签）
EMOTION_TAGS = (
    "思乡", "爱国", "壮志难酬", "豪迈", "消极", "积极", "压抑", "孤寂",
    "闲适", "离别", "边塞", "咏史怀古", "爱情", "亲情", "哲理", "悼亡",
    "隐逸", "忧民", "喜悦", "悲秋",
)

BATCH_SIZE = 10

PROMPT_TEMPLATE = """你是古典文学标注员。为下列诗词标注情感倾向标签（可多选，从固定词表中选）。

固定词表：{tags}

诗词列表（行号\\t标题\\t作者\\t内容前120字）：
{poems}

只输出 JSON 对象：{{"行号": ["标签1", "标签2"]}}，最多 3 个标签/首，不要输出其他内容。
"""


def _parse_reply(raw: str) -> dict[int, list[str]]:
    """解析 LLM 回复 {"0": ["思乡"], ...}；容忍 markdown 代码块包裹。"""
    m = re.search(r"\{[\s\S]*\}", raw or "")
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    valid = set(EMOTION_TAGS)
    out = {}
    for k, tags in data.items():
        if not isinstance(tags, list):
            continue
        kept = [t for t in tags if isinstance(t, str) and t in valid][:3]
        if kept:
            try:
                out[int(k)] = kept
            except (ValueError, TypeError):
                continue
    return out


async def tag_batch(session, poems: list[Poem]) -> int:
    """一批 10 首：调 LLM → 写 poem_tags。返回写入标签数。"""
    lines = "\n".join(
        f"{i}\t{p.title}\t{p.author}\t{(p.content or '')[:120]}"
        for i, p in enumerate(poems)
    )
    prompt = PROMPT_TEMPLATE.format(tags="、".join(EMOTION_TAGS), poems=lines)
    raw = await agnes_client.generate_text(
        [{"role": "user", "content": prompt}], max_tokens=400, temperature=0.2,
    )
    parsed = _parse_reply(raw)
    written = 0
    for i, tags in parsed.items():
        if i >= len(poems):
            continue
        p = poems[i]
        for tag in tags:
            session.add(PoemTag(
                poem_id=p.id, tag=tag, tag_type="emotion",
                confidence=80, source="ai_infer",
            ))
            written += 1
    await session.commit()
    return written


async def main_async(args) -> int:
    await init_db()
    # 与服务端 lifespan 同源：先加载 system_settings 的 DB overlay，
    # 否则独立脚本进程拿不到用户在设置页保存的 api_key（Bearer 空 → LocalProtocolError）
    from app.config import settings
    from app.services.settings_store import load_config_from_db, apply_overlay
    async with async_session_factory() as session:
        overlay = await load_config_from_db()
        apply_overlay(settings, overlay)
    async with async_session_factory() as session:
        # 统计
        done_ids = set((await session.execute(
            select(PoemTag.poem_id).where(PoemTag.tag_type == "emotion")
        )).scalars().all())
        pool = (await session.execute(
            select(Poem.id)
            .where(Poem.quality_score >= args.threshold)
            .order_by(Poem.id)
        )).scalars().all()
        todo = [pid for pid in pool if pid not in done_ids]
        print(f"质量池(>={args.threshold}) {len(pool)} 首，已打标 {len(done_ids)}，待打标 {len(todo)}")
        if args.dry_run or not todo:
            return 0
        todo = todo[: args.limit] if args.limit else todo

        written_total = 0
        for off in range(0, len(todo), BATCH_SIZE):
            batch_ids = todo[off: off + BATCH_SIZE]
            rows = (await session.execute(
                select(Poem).where(Poem.id.in_(batch_ids))
            )).scalars().all()
            try:
                written = await tag_batch(session, rows)
                written_total += written
                print(f"  批次 {off // BATCH_SIZE + 1}: {len(rows)} 首 → {written} 条标签"
                      f"（累计 {written_total}）")
            except Exception as e:
                print(f"  批次 {off // BATCH_SIZE + 1} 失败（跳过，下批继续）: {e}")
        print(f"完成：共写入 {written_total} 条 emotion 标签")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少首（0=全部质量池）")
    ap.add_argument("--threshold", type=int, default=55, help="质量池门槛")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
