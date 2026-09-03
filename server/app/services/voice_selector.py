"""根据诗词内容自动选择配音音色（voice preset）。

策略（与产品决策一致）：**LLM 推荐为主 + 标题精确匹配兜底**。
- LLM：调用 agnes 文本模型，从 voice_presets.json 的 14 个 preset 中选最契合诗词气质的一个。
- 兜底：poem.title 归一化后命中某 preset.recommended_for → 直接采信该 preset。
- 都没命中 → 返回 ''（pipeline 再回退到 config.default_voice_preset，最后回退全局参考音频）。

重要约束：本模块只负责「选哪个 preset id」；该 preset 是否真能换声取决于其
ref_wav（当前 14 个 preset 的 ref_wav 均为 null，故选择逻辑生效、但听感仍回退到
全局 ref_voice.wav，直到为每个 preset 补齐真实参考音频）。
"""
import logging

from app.services.agnes import agnes_client
from app.services.tts_core import list_presets

logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    """归一化：去空白、转小写，便于标题匹配。"""
    return (s or "").strip().lower()


def _title_match(poem, presets: list[dict]) -> str | None:
    """poem.title 归一化后命中某 preset.recommended_for → 返回该 preset id。"""
    title = _norm(getattr(poem, "title", "") or "")
    if not title:
        return None
    for p in presets:
        for rec in (p.get("recommended_for") or []):
            r = _norm(rec)
            if not r:
                continue
            if r == title or r in title or title.endswith(r):
                return p["id"]
    return None


async def _llm_suggest(poem, presets: list[dict]) -> str:
    """调用 agnes 从 preset 列表里挑最契合的 id；失败/无效返回 ''。"""
    try:
        catalog = "\n".join(
            f"- {p['id']}: {p['name']}（{p['gender']}·{p['style']}）适配：{', '.join(p.get('recommended_for') or [])}"
            for p in presets
        )
        prompt = (
            "你是古诗词短视频的配音导演，要根据诗词内容为旁白挑选最契合的朗读者音色。\n"
            f"可选音色（id → 风格）：\n{catalog}\n\n"
            f"诗词：《{getattr(poem, 'title', '')}》（{getattr(poem, 'author', '')}·{getattr(poem, 'dynasty', '')}）\n"
            f"内容：{(getattr(poem, 'content', '') or '')[:200]}\n\n"
            "只回复一个音色 id（必须是上面列表中的 id，小写拼音），不要任何解释；"
            "若确实无合适项回复 none。"
        )
        text = await agnes_client.generate_text(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=24,
            temperature=0.3,
        )
        rid = _norm(text)
        valid = {p["id"] for p in presets}
        if rid in valid:
            return rid
        # 容错：从回复中提取已知 id 子串（LLM 偶尔多嘴）
        for p in presets:
            if p["id"] in rid:
                return p["id"]
        return ""
    except Exception as e:
        logger.warning("voice LLM 推荐失败，回退标题匹配: %s", e)
        return ""


async def recommend(poem) -> str:
    """返回选中的 preset id（LLM 推荐为主 + 标题兜底），无命中返回 ''。"""
    presets = list_presets()
    if not presets:
        return ""
    # 1) LLM 推荐（为主）
    sug = await _llm_suggest(poem, presets)
    if sug:
        return sug
    # 2) 标题兜底
    tm = _title_match(poem, presets)
    if tm:
        return tm
    return ""
