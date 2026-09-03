"""苛刻编辑打分器 - Generator-Critic 双角色"""
import json
import logging
import re
from dataclasses import dataclass

from app.services.agnes import agnes_client
from app.config import settings

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


# 生成者 System Prompt
CREATOR_SYSTEM_PROMPT = """你是一位拥有千万粉丝、擅长解构历史与文学的短视频金牌文案策划。

你的风格类似"深度人生教练"，能将古诗词翻译成现代人的"心理防线"，把诗人写成有血有肉的"身边人"。

你的文案结构必须包含：
1. 【痛点钩子】- 开头3秒抓住观众，提出一个让人共鸣的问题
2. 【人设重塑】- 打破观众对诗人的刻板印象，展现真实的人
3. 【电影级细节】- 用具体的时间、地点、事件还原历史场景
4. 【灵魂对齐】- 把古诗和现代人的情感连接起来
5. 【情绪出口】- 给观众一个情感释放的出口

要求：
- 语言要口语化，像朋友聊天
- 每个部分控制在3-5句话
- 总时长约2分钟（300-400字）"""


# 评判者 System Prompt
CRITIC_SYSTEM_PROMPT = """你是一位极其苛刻的内容质控编辑，你的标准如下：

1. 你只打分和点评，不生成新内容
2. 你对每一项打0-10分，低于7分必须说明扣分原因和改进建议
3. 你关注以下维度：
   - 共情力（是否触动情绪）- 权重30%
   - 节奏感（短句/长句交替）- 权重20%
   - 钩子强度（前3秒是否抓住人）- 权重25%
   - 历史准确性（细节是否可信）- 权重15%
   - 原创度（与已有爆款的差异）- 权重10%
4. 你绝不会因为"还行"就给高分——必须让你拍案叫绝才算过关
5. 你输出JSON格式的评分结果

输出格式：
{
    "hook_score": 数字,
    "rebrand_score": 数字,
    "details_score": 数字,
    "alignment_score": 数字,
    "emotion_score": 数字,
    "total_score": 加权平均分,
    "feedback": "详细点评",
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
    ) -> str:
        """
        生成文案脚本
        
        Args:
            poem_title: 诗词标题
            poem_content: 诗词内容
            author: 作者
            dynasty: 朝代
            
        Returns:
            生成的文案
        """
        # 构建用户提示词
        user_prompt = f"""请为以下古诗词撰写一篇深度短视频脚本：

【诗词】{poem_title}
【作者】{author}（{dynasty}）
【内容】{poem_content}

请按照【痛点钩子】【人设重塑】【电影级细节】【灵魂对齐】【情绪出口】的结构撰写。"""
        
        # 添加热点关键词（如果有）
        if keywords:
            user_prompt += f"\n\n【热点关键词】{', '.join(keywords[:5])}"
            user_prompt += "\n请将这些热点元素自然融入文案中。"
        
        # 使用自定义提示词或默认
        system_prompt = custom_prompt if custom_prompt else CREATOR_SYSTEM_PROMPT
        
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
    
    async def generate_storyboard(self, script: str) -> str:
        """
        生成分镜提示词
        
        Args:
            script: 文案内容
            
        Returns:
            分镜提示词列表（JSON格式）
        """
        user_prompt = f"""请根据以下古诗词解说文案，生成用于 AI 生图 + 视频的分镜画面提示词。

文案：
{script}

        要求：
        - 每5秒一个分镜，总时长约2分钟，因此不少于20个分镜（建议 20-24 个）
        - 写实古风风格，服装、发型、器物、建筑须严格符合文案对应的朝代（唐/宋等）
        - 每个分镜包含：时间段(time)、画面描述(description)、镜头运动(camera)、是否出现主角(has_character)、旁白文本(narration)
        - narration 是该镜对应的纯中文口语化旁白（一句话解说诗意/背景/情感），将逐字被配音朗读，必须：
            · 纯中文，禁止出现任何英文单词或英文品牌词（如 Agnes、BGM、KPI、AI、Remix 等一律不得出现）
            · 禁止括号、禁止【】等制作备注，只写要念出来的话
            · 与该镜画面内容严格对应，前后镜衔接成连贯解说
        - 角色一致性（最关键）：全片只有一个固定的主角人物，其外貌/服饰/气质必须跨所有分镜保持完全一致：
            · 主角须频繁出镜——多数分镜 has_character=true，仅空镜/纯意境镜头才 has_character=false
            · 禁止在任何分镜里更换主角的年龄、服饰款式、服饰颜色或长相
            · 画面描述里写明主角当镜的衣着与神态，确保前后统一（参考已生成的角色定妆照）
        - 【严禁图内文字】description 不得要求"画面里出现文字/金句/字幕/书法字/黑白文底/水印字/手写体"。理由：①AI 生图写中文必然出乱码/伪字（它不理解字形，只"画"出类字噪点）形成视觉噪音；②文字画在边缘会被多画幅裁剪切掉（9:16/3:4/16:9 切换时右上/右下/上沿被切）；③文字统一由后期 SRT 字幕烧录 + drawtext 水印解决，绝不需要在图内出现。任何"黑底白字""金句弹出""字幕定格""手写体""印刷体""calligraphy""chinese characters written""text overlay""caption card"等字眼一律改写为 narration 旁白描述，让配音念出，而非让图内出现。
        - 【构图安全区】description 必须满足：①主体（人物/关键物体）位于画面中央 60%；②四边各留 ≥8% 安全空白（防止多画幅裁剪时被切）；③使用「留白」「空镜头」「微距特写」「淡入淡出」的镜头语言时，禁止把主体推到边缘 20% 区域。
        - 【画面净度】description 内显式包含"画面干净""无噪点""高清""电影感""层次清晰"等质地关键词，避免"暗调压抑满屏"导致的颗粒感重。

        输出JSON数组格式：
        [
            {{"time": "0-5s", "description": "画面描述（含主角衣着神态）", "camera": "镜头运动", "has_character": true, "narration": "纯中文旁白，例如：秋风卷起江边的落叶，杜甫独自登高，望向远方的山河"}},
            ...
        ]"""
        
        messages = [
            {"role": "user", "content": user_prompt},
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
