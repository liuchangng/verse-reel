"""苛刻编辑打分器 - Generator-Critic 双角色"""
import json
import logging
import re
from dataclasses import dataclass

from app.services.agnes import agnes_client
from app.config import settings, TIER_PROFILES, tier_script_guidelines

logger = logging.getLogger(__name__)


def _extract_json(text: str) -> str:
    """从 LLM 输出中稳健提取 JSON 字符串。

    处理三类常见问题：
    1. markdown 代码块包裹（```json ... ```）
    2. JSON 前后夹杂乱文本
    3. 字符串值内混入裸控制字符（换行/制表等）—— 非法 JSON，需清洗
    """
    # 1. 去 markdown 代码块
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    # 2. 截出最外层 {} 包裹的内容（容忍前后杂质）
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    # 3. 清洗非法控制字符（\x00-\x1f 及 \x7f），JSON 字符串内不允许裸控制字符
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text.strip()


def parse_score_json(text: str, threshold: float) -> "ScoreResult":
    """解析评分 JSON，失败时安全降级（绝不因解析错误误杀合格文案）。"""
    raw = text
    try:
        result = json.loads(_extract_json(raw))
        total_score = float(result.get("total_score", 0))
        feedback = result.get("feedback", "")
        return ScoreResult(
            score=total_score,
            feedback=feedback,
            passed=total_score >= threshold,
        )
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as e:
        logger.warning(f"解析评分结果失败: {e}，尝试正则兜底提取")
        # 兜底：用正则从原始文本中提取 total_score（容忍 LLM 输出格式偏差）
        score_match = re.search(r'"total_score"\s*:\s*([\d.]+)', raw)
        fb_match = re.search(r'"feedback"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if score_match:
            total_score = float(score_match.group(1))
            feedback = fb_match.group(1) if fb_match else ""
            logger.info(f"正则兜底提取评分成功: {total_score}/10")
            return ScoreResult(
                score=total_score,
                feedback=feedback,
                passed=total_score >= threshold,
            )
        # 彻底无法提取：返回零分但不阻断流水线（由调用方决定是否重试）
        return ScoreResult(
            score=0,
            feedback=f"评分解析失败: {raw[:200]}",
            passed=False,
        )


@dataclass
class ScoreResult:
    """评分结果"""
    score: float
    feedback: str
    passed: bool
    
    def __repr__(self):
        status = "✓ 通过" if self.passed else "✗ 未通过"
        return f"<ScoreResult(score={self.score}, {status})>"


# 生成者 System Prompt —— 默认 = config.tier_script_guidelines("S")（S 快档三段式 + 80–130 字）。
# 文案改造轮 W3（video-comm 决策 1 定稿默认产出 S 档）把旧的"五段式 + 300–400 字/2 分钟"
# 固定文案 prompt 档位化为 config.TIER_SCRIPT_STRUCTURE（数字由 TIER_PROFILES 注入，
# 单一事实源）：S 三段式 = 设计文档 §四 快档三段式；L 五段式预案留档在 config，本轮不产出。
# 仅当调用方不传 custom_prompt 时此默认生效；pipeline 生产路径总是传组装后的
# "档位段 + 风格段" 完整 prompt（见 pipeline._generate_script）。
CREATOR_SYSTEM_PROMPT: str = tier_script_guidelines("S")


# 评判者 System Prompt —— S 快档单套评审卡（video-comm 决策 2 定稿，2026-09-03）
# 维度权重（传播向，合计 100）：钩子 30 / 共情 25 / 史实 15 / 节奏 10 / 原创 10 / 收藏动机 10
#   - 钩子 30：2026 权重排序收藏率第一，且 S 档"0-3s 决定去留"，无铺垫容错；
#   - 节奏降至 10：S 档三段式结构模板（§四）已从生成侧保证节奏，评审不必重复苛求；
#   - 收藏动机 10：新增维度（旧卡缺失，2026 权重第一维度无评审）。
# JSON 键与维度对齐（修复 §一 卫生问题：旧键 hook/rebrand/details/alignment/emotion
#   取自生成 prompt 段落名，与评审维度错位，LLM 只能自行猜键义 → 输出不可解释）。
# 字数适切：S 档全文须 80–130 字（tier 档案 S.chars），区间外 total 扣至多 0.5（软约束，
#   不硬失败不烧重试，video-comm 决策 2「软约束」方案）。
CRITIC_SYSTEM_PROMPT = """你是一位极其苛刻的内容质控编辑，专审 25–40 秒抖音/快手/小红书竖屏短视频的 S 快档文案（80–130 字）。你的标准如下：

1. 你只打分和点评，不生成新内容
2. 你对每一项打0-10分，低于7分必须说明扣分原因和改进建议
3. 你关注以下维度（按权重排序）：
   - 钩子强度（0–3秒是否原诗金句/情绪问题/反差事实直给开局；出现"大家好/今天讲"式铺垫直接给低分）- 权重30%
   - 共情力（是否触动情绪、把诗意接到现代观众） - 权重25%
   - 历史准确性（细节是否可信、无张冠李戴） - 权重15%
   - 收藏动机（有没有给人收藏/回看/转发的理由） - 权重10%
   - 节奏感（三段式：金句钩子→白话直给→现代对齐收尾；句短直给无注水） - 权重10%
   - 原创度（与已有爆款的差异） - 权重10%
4. 字数适切性：全文（不含标题）须在 80–130 字之间；超出区间在 total_score 上扣至多 0.5 分，并在 feedback 里注明实际字数
5. 你绝不会因为"还行"就给高分——必须让你拍案叫绝才算过关
6. 你输出JSON格式的评分结果

输出格式：
{
    "hook_score": 数字,
    "empathy_score": 数字,
    "accuracy_score": 数字,
    "save_motive_score": 数字,
    "rhythm_score": 数字,
    "originality_score": 数字,
    "total_score": 加权平均分（按上述权重；字数出区间另扣至多0.5）,
    "feedback": "详细点评（含实际字数）",
    "improvements": ["改进建议1", "改进建议2"]
}"""


# 图片评分 System Prompt
IMAGE_CRITIC_SYSTEM_PROMPT = """你是一位专业的视觉内容审核编辑，专注于短视频画面质量。

你对每张图片打分（0-10分），关注：
1. 构图（是否符合电影感）- 权重25%
2. 历史准确性（服饰、场景是否符合朝代）- 权重25%
3. 氛围感（是否符合诗词意境）- 权重30%
4. 清晰度（画面是否清晰）- 权重20%

输出JSON格式：
{
    "composition_score": 数字,
    "accuracy_score": 数字,
    "atmosphere_score": 数字,
    "clarity_score": 数字,
    "total_score": 加权平均分,
    "feedback": "详细点评"
}"""


# ====== 分镜提示词档位化（video-comm 设计文档 §五「分镜层规则」，文案改造轮 W4）======
# storyboard_user_prompt 输出的分镜 prompt 承载档位预算（镜头数/镜长/旁白字数/五拍弧线/
# 主角出镜率），数字由 config.TIER_PROFILES 注入（单一事实源）；质量铁律（图内无字/构图
# 安全区/画面净度/角色一致性）跨档共用。未知/空档位回退 S。
def storyboard_budget_block(tier: str | None = None) -> str:
    """档位预算段：镜头预算 + 镜长窗 + 旁白字数双口径 + 五拍弧线落点 + 主角出镜率。"""
    t = (tier or "").upper()
    p = TIER_PROFILES.get(t, TIER_PROFILES["S"])
    if t == "L":
        return f"""【本轮档位预算】L 深档 · 整片 {p['dur_min']}–{p['dur_max']} 秒，分镜 {p['shots_min']}–{p['shots_max']} 镜，镜长按信息点 {p['shot_sec_min']}–{p['shot_sec_max']} 秒（禁止按固定 5s 均分）；全片旁白合计 {p['chars_min']}–{p['chars_max']} 字（与文案字数同口径，逐字配音）。
【五拍弧线 · L 落点】钩子镜 = 第1镜（0–3s）→ 人设/背景镜 = 第2–4镜 → 意境蓄力镜 = 中段铺 2–3 镜（空镜留白，BGM 情绪带）→ 金句镜 = 全片约 2/3 处（构图最强一镜）→ 收尾定格镜 = 末镜（旁白结束补 1.5–2s 定格）。
【主角出镜率】L 档 ≥50% 镜有主角（人设叙事向）。"""
    return f"""【本轮档位预算】S 快档 · 整片 {p['dur_min']}–{p['dur_max']} 秒（硬上限 {p['dur_hard_max']} 秒），分镜 {p['shots_min']}–{p['shots_max']} 镜，镜长按信息点 {p['shot_sec_min']}–{p['shot_sec_max']} 秒（可 3/4/5/6/7s，禁止再按"每5秒一镜"均分）；全片旁白合计 {p['chars_min']}–{p['chars_max']} 字（与文案字数同口径，逐字配音）→ 每镜 narration ≤{p['nar_max']} 字。
【五拍弧线 · S 落点】（按此布局 {p['shots_min']}–{p['shots_max']} 镜；镜数不足时合并第②③拍，钩子/金句/收尾三拍不许吞）：
 ① 钩子镜 = 第 1 镜（0–3s：近景人物动势或意象特写，情绪入场）
 ② 人设/背景镜 = 第 2–3 镜（交代诗人处境，中景转全景）
 ③ 意境蓄力镜 = 中段 1–2 镜（空镜留白：山水大远景/微距自然物，慢速运镜）
 ④ 金句镜 = 次末镜（全片构图最强一镜：大留白 + 人物剪影或山河全景）
 ⑤ 收尾定格镜 = 末镜（拉远/定格，旁白结束后补 1.5–2 秒定格，画面静默）
【主角出镜率】S 档允许意境空镜：has_character=true 约占 30–50%（不必多数出镜）。"""


def storyboard_user_prompt(script: str, tier: str | None = None) -> str:
    """构建分镜 user prompt（tier 档位预算段注入 + 跨档质量铁律）。"""
    t = (tier or "").upper()
    p = TIER_PROFILES.get(t, TIER_PROFILES["S"])
    budget = storyboard_budget_block(t)
    return f"""请根据以下古诗词解说文案，生成用于 AI 生图 + 视频的分镜画面提示词。

文案：
{script}

要求：
{budget}
- 写实古风风格，服装、发型、器物、建筑须严格符合文案对应的朝代（唐/宋等）
- 每个分镜包含：时间段(time)、画面描述(description)、镜头运动(camera)、是否出现主角(has_character)、旁白文本(narration)
- 运镜情绪词典：开阔/释怀 → 缓推或拉远；悲怆/沉重 → 下摇或缓慢横移；金句 → 推近特写后停顿；禁止连续两镜同机位且无景别级差（相邻镜至少升/降 1 级：远景-中景-近景不重复）
- narration 是该镜对应的纯中文口语化旁白（一句话解说诗意/背景/情感），将逐字被配音朗读，必须：
    · 纯中文，禁止出现任何英文单词或英文品牌词（如 Agnes、BGM、KPI、AI、Remix 等一律不得出现）
    · 禁止括号、禁止【】等制作备注，只写要念出来的话
    · 与该镜画面内容严格对应，前后镜衔接成连贯解说
    · 每镜 narration ≤ {p['nar_max']} 字（全片旁白合计 = 文案字数，见档位预算）
- 角色一致性（最关键）：全片只有一个固定的主角人物，其外貌/服饰/气质必须跨所有分镜保持完全一致：
    · 主角出镜率按档位预算（见上）；凡出镜镜，禁止更换主角年龄、服饰款式、服饰颜色或长相
    · 画面描述里写明主角当镜的衣着与神态，确保前后统一（参考已生成的角色定妆照）
- 【严禁图内文字】description 不得要求"画面里出现文字/金句/字幕/书法字/黑白文底/水印字/手写体"。理由：①AI 生图写中文必然出乱码/伪字（它不理解字形，只"画"出类字噪点）形成视觉噪音；②文字画在边缘会被多画幅裁剪切掉（9:16/3:4/16:9 切换时右上/右下/上沿被切）；③文字统一由后期 SRT 字幕烧录 + drawtext 水印解决，绝不需要在图内出现。任何"黑底白字""金句弹出""字幕定格""手写体""印刷体""calligraphy""chinese characters written""text overlay""caption card"等字眼一律改写为 narration 旁白描述，让配音念出，而非让图内出现。
- 【构图安全区】description 必须满足：①主体（人物/关键物体）位于画面中央 60%；②四边各留 ≥8% 安全空白（防止多画幅裁剪时被切）；③使用「留白」「空镜头」「微距特写」「淡入淡出」的镜头语言时，禁止把主体推到边缘 20% 区域。
- 【画面净度】description 内显式包含"画面干净""无噪点""高清""电影感""层次清晰"等质地关键词，避免"暗调压抑满屏"导致的颗粒感重。

输出JSON数组格式：
[
    {{"time": "0-4s", "description": "画面描述（含主角衣着神态）", "camera": "镜头运动", "has_character": true, "narration": "纯中文旁白，例如：秋风卷起江边的落叶，杜甫独自登高，望向远方的山河"}},
    ...
]"""


class CriticService:
    """苛刻编辑打分器"""
    
    async def generate_script(
        self,
        poem_title: str,
        poem_content: str,
        author: str,
        dynasty: str,
        custom_prompt: str | None = None,
        keywords: list[str] | None = None,
        tier: str = "S",
    ) -> str:
        """
        生成文案脚本

        Args:
            poem_title: 诗词标题
            poem_content: 诗词内容
            author: 作者
            dynasty: 朝代
            custom_prompt: 完整 system prompt（覆盖默认；pipeline 传"档位段+风格段"组装结果）；
                           为空则取 tier_script_guidelines(tier) 档位默认（默认 S 快档）。
            keywords: 热点关键词
            tier: 产出档位（S/L），仅 custom_prompt 为空时生效

        Returns:
            生成的文案
        """
        # 构建用户提示词（结构指引由 system prompt 的档位段全权负责，此处不写死五段名，
        # 否则 S 三段式会与 L 五段式 user 提示冲突）
        user_prompt = f"""请为以下古诗词撰写一篇短视频解说文案：

【诗词】{poem_title}
【作者】{author}（{dynasty}）
【内容】{poem_content}

严格按照 system 提示词中的档位与结构模板撰写，直接输出文案正文，不要解释、不要 Markdown 标题。"""
        
        # 添加热点关键词（如果有）
        if keywords:
            user_prompt += f"\n\n【热点关键词】{', '.join(keywords[:5])}"
            user_prompt += "\n请将这些热点元素自然融入文案中。"
        
        # 使用自定义提示词或按档位取默认（S 三段式/L 五段式，文本源 config.TIER_SCRIPT_STRUCTURE）
        system_prompt = custom_prompt if custom_prompt else tier_script_guidelines(tier)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        
        return await agnes_client.generate_text(messages, max_tokens=2000)
    
    async def score_script(self, script: str) -> ScoreResult:
        """
        评分文案脚本
        
        Args:
            script: 文案内容
            
        Returns:
            评分结果
        """
        user_prompt = f"""请对以下短视频文案进行评分：

{script}

请严格按照评分标准打分，并给出JSON格式的评分结果。"""
        
        messages = [
            {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        
        response = await agnes_client.generate_text(messages, max_tokens=1000)
        return parse_score_json(response, settings.script_score_threshold)
    
    async def generate_storyboard(self, script: str, tier: str = "S") -> str:
        """
        生成分镜提示词（video-comm 文案改造轮 W4：按档位注入镜头/时长/旁白预算）

        Args:
            script: 文案内容
            tier: 产出档位（S/L；S 快档 6–9 镜·镜长 3–7s·narration≤20 字，
                  L 深档 14–22 镜·5–10s，见 config.TIER_PROFILES）

        Returns:
            分镜提示词列表（JSON格式）
        """
        messages = [
            {"role": "user", "content": storyboard_user_prompt(script, tier)},
        ]
        
        return await agnes_client.generate_text(messages, max_tokens=3000)
    
    async def score_image(self, image_url: str, prompt: str) -> ScoreResult:
        """
        评分图片
        
        Args:
            image_url: 图片 URL
            prompt: 生成提示词
            
        Returns:
            评分结果
        """
        user_prompt = f"""请对以下生成的图片进行评分。

参考提示词：{prompt}

请严格按照评分标准打分，并给出JSON格式的评分结果。"""
        
        messages = [
            {"role": "system", "content": IMAGE_CRITIC_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        
        response = await agnes_client.generate_text(messages, max_tokens=1000)
        return parse_score_json(response, settings.image_score_threshold)


# 全局服务实例
critic_service = CriticService()
