"""提示词优化器 - 基于风格模板 + 打分驱动的提示词优化"""
import json
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)

# 提示词风格模板
STYLE_TEMPLATES = {
    "情感治愈": {
        "name": "情感治愈",
        "description": "温暖叙事、治愈系结尾",
        "creator_prompt": """你是一位擅长情感叙事的短视频文案策划。
风格要求：
- 用温暖、治愈的语调讲述诗人故事
- 重点刻画诗人内心的情感变化
- 结尾要有治愈感，让观众感到温暖
- 适合深夜观看，像朋友在耳边倾诉""",
        "critic_prompt": """你是苛刻的情感类内容编辑。
评分标准：
- 情感共鸣度（是否能触动观众内心）
- 治愈感（结尾是否让人感到温暖）
- 语言优美度（是否用词恰当、节奏流畅）
- 与古诗的关联度（是否自然融入诗句）""",
        "image_prompt_prefix": "温暖治愈风格，柔和光线，暖色调，画面干净无噪点高清电影感，主体居中，四边留白构图，禁止任何文字、字幕、金句出现在图内（中文由后期配音烧录），",
        "video_prompt_prefix": "缓慢流动的镜头，温暖色调，治愈氛围，",
        "subtitle_style": "emotion",
    },
    "职场共鸣": {
        "name": "职场共鸣",
        "description": "犀利吐槽、痛点钩子",
        "creator_prompt": """你是一位擅长职场话题的短视频文案策划。
风格要求：
- 用犀利、直击痛点的语言开场
- 将古诗与现代职场困境巧妙结合
- 共鸣感强，让打工人感同身受
- 结尾要有力量感，引发思考""",
        "critic_prompt": """你是苛刻的职场类内容编辑。
评分标准：
- 痛点抓取（是否精准命中职场人的情绪）
- 共鸣度（是否让观众觉得"说的就是我"）
- 古今结合（是否自然将古诗与现代联系）
- 传播性（是否容易引发讨论和转发）""",
        "image_prompt_prefix": "现代都市风格，职场氛围，对比强烈，画面干净无噪点高清电影感，主体居中，四边留白构图，禁止任何文字、字幕、品牌名出现在图内，",
        "video_prompt_prefix": "快节奏剪辑，现代都市，对比感，",
        "subtitle_style": "modern",
    },
    "历史解读": {
        "name": "历史解读",
        "description": "深度分析、知识密度",
        "creator_prompt": """你是一位擅长历史解读的短视频文案策划。
风格要求：
- 深度分析诗人的历史背景和创作动机
- 知识密度高，但表述通俗易懂
- 像纪录片旁白一样庄重、有质感
- 引用历史事件增强说服力""",
        "critic_prompt": """你是苛刻的历史类内容编辑。
评分标准：
- 历史准确性（引用的史实是否正确）
- 知识密度（是否信息量大但不枯燥）
- 叙事节奏（是否像纪录片一样引人入胜）
- 权威感（是否让人信服）""",
        "image_prompt_prefix": "历史纪实风格，古装人物，宫殿朝堂，画面干净无噪点高清电影感，主体居中，四边留白构图，禁止任何文字、字幕、年号印章出现在图内（中文书法由后期配音烧录），",
        "video_prompt_prefix": "纪录片风格，庄重氛围，缓慢推进，",
        "subtitle_style": "ancient",
    },
    "人生感悟": {
        "name": "人生感悟",
        "description": "哲理思考、灵魂对齐",
        "creator_prompt": """你是一位擅长人生感悟的短视频文案策划。
风格要求：
- 从诗人的经历中提炼人生哲理
- 引导观众思考自己的人生
- 语言平实但有深度
- 结尾要有金句，适合截图传播""",
        "critic_prompt": """你是苛刻的哲理类内容编辑。
评分标准：
- 哲理深度（是否提炼出有价值的人生感悟）
- 金句质量（结尾是否有传播力的金句）
- 思辨性（是否引发思考而非灌输）
- 与古诗的契合度（哲理是否源于诗句本身）""",
        "image_prompt_prefix": "意境深远，留白构图，月下独酌，画面干净无噪点高清电影感，主体居中，四边留白构图，禁止任何文字、字幕、题诗出现在图内（中文诗词由后期配音烧录），",
        "video_prompt_prefix": "静谧氛围，自然光，缓慢镜头，",
        "subtitle_style": "default",
    },
}

# 无热点/未显式指定时的兜底风格（保持历史行为）
DEFAULT_STYLE = "人生感悟"


def list_styles() -> list[dict]:
    """返回可选风格列表（供创建任务下拉使用）。

    Returns:
        [{"name": 风格名, "description": 风格描述}, ...]
    """
    return [
        {"name": tpl["name"], "description": tpl.get("description", "")}
        for tpl in STYLE_TEMPLATES.values()
    ]


def normalize_style(style: str | None) -> str | None:
    """把外部传入的风格名归一为合法值；非法/空返回 None（由调用方走自动推断）。"""
    if not style:
        return None
    style = style.strip()
    return style if style in STYLE_TEMPLATES else None


@dataclass
class PromptVersion:
    """提示词版本"""
    version: int
    content: str
    score: float = 0.0
    feedback: str = ""


@dataclass
class StylePromptManager:
    """风格提示词管理器"""
    style_name: str
    creator_versions: list[PromptVersion] = field(default_factory=list)
    critic_versions: list[PromptVersion] = field(default_factory=list)
    image_prefix_versions: list[PromptVersion] = field(default_factory=list)
    video_prefix_versions: list[PromptVersion] = field(default_factory=list)


class PromptOptimizer:
    """提示词优化器"""
    
    def __init__(self):
        # 加载风格模板
        self.styles = {}
        for name, template in STYLE_TEMPLATES.items():
            self.styles[name] = StylePromptManager(style_name=name)
            # 初始版本
            self.styles[name].creator_versions.append(
                PromptVersion(version=1, content=template["creator_prompt"])
            )
            self.styles[name].critic_versions.append(
                PromptVersion(version=1, content=template["critic_prompt"])
            )
            self.styles[name].image_prefix_versions.append(
                PromptVersion(version=1, content=template["image_prompt_prefix"])
            )
            self.styles[name].video_prefix_versions.append(
                PromptVersion(version=1, content=template["video_prompt_prefix"])
            )
        
        # 加载已优化的版本（如果有）
        self._load_optimized_versions()
    
    def _load_optimized_versions(self):
        """从文件加载已优化的版本"""
        config_dir = Path(settings.output_dir).parent / "config"
        versions_file = config_dir / "prompt_versions.json"
        
        if versions_file.exists():
            try:
                data = json.loads(versions_file.read_text(encoding="utf-8"))
                for style_name, versions in data.items():
                    if style_name in self.styles:
                        manager = self.styles[style_name]
                        if "creator" in versions:
                            manager.creator_versions = [
                                PromptVersion(**v) for v in versions["creator"]
                            ]
                        if "critic" in versions:
                            manager.critic_versions = [
                                PromptVersion(**v) for v in versions["critic"]
                            ]
                logger.info(f"加载已优化的提示词版本: {versions_file}")
            except Exception as e:
                logger.warning(f"加载提示词版本失败: {e}")
    
    def _save_optimized_versions(self):
        """保存优化后的版本到文件"""
        config_dir = Path(settings.output_dir).parent / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        versions_file = config_dir / "prompt_versions.json"
        
        data = {}
        for style_name, manager in self.styles.items():
            data[style_name] = {
                "creator": [
                    {"version": v.version, "content": v.content, "score": v.score, "feedback": v.feedback}
                    for v in manager.creator_versions
                ],
                "critic": [
                    {"version": v.version, "content": v.content, "score": v.score, "feedback": v.feedback}
                    for v in manager.critic_versions
                ],
            }
        
        versions_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"保存提示词版本: {versions_file}")
    
    def get_creator_prompt(self, style: str = "人生感悟") -> str:
        """获取当前版本的创作者提示词"""
        manager = self.styles.get(style, self.styles["人生感悟"])
        return manager.creator_versions[-1].content
    
    def get_critic_prompt(self, style: str = "人生感悟") -> str:
        """获取当前版本的苛刻编辑提示词"""
        manager = self.styles.get(style, self.styles["人生感悟"])
        return manager.critic_versions[-1].content
    
    def get_image_prefix(self, style: str = "人生感悟") -> str:
        """获取图片提示词前缀"""
        manager = self.styles.get(style, self.styles["人生感悟"])
        return manager.image_prefix_versions[-1].content
    
    def get_video_prefix(self, style: str = "人生感悟") -> str:
        """获取视频提示词前缀"""
        manager = self.styles.get(style, self.styles["人生感悟"])
        return manager.video_prefix_versions[-1].content
    
    def get_subtitle_style(self, style: str = "人生感悟") -> str:
        """获取字幕风格"""
        template = STYLE_TEMPLATES.get(style, STYLE_TEMPLATES["人生感悟"])
        return template.get("subtitle_style", "default")
    
    def update_prompt(
        self,
        style: str,
        prompt_type: str,
        new_content: str,
        score: float = 0.0,
        feedback: str = "",
    ):
        """
        更新提示词（优化后调用）
        
        Args:
            style: 风格名称
            prompt_type: 提示词类型（creator/critic/image/video）
            new_content: 新的提示词内容
            score: 当前版本评分
            feedback: 评分反馈
        """
        manager = self.styles.get(style)
        if not manager:
            return
        
        # 获取当前版本
        if prompt_type == "creator":
            versions = manager.creator_versions
        elif prompt_type == "critic":
            versions = manager.critic_versions
        elif prompt_type == "image":
            versions = manager.image_prefix_versions
        elif prompt_type == "video":
            versions = manager.video_prefix_versions
        else:
            return
        
        # 更新当前版本的评分
        if versions:
            versions[-1].score = score
            versions[-1].feedback = feedback
        
        # 添加新版本
        new_version = len(versions) + 1
        versions.append(PromptVersion(version=new_version, content=new_content))
        
        logger.info(f"更新提示词: {style}/{prompt_type} v{new_version}")
        
        # 保存到文件
        self._save_optimized_versions()
    
    def optimize_prompt(
        self,
        style: str,
        prompt_type: str,
        feedback: str,
    ) -> str:
        """
        根据反馈优化提示词
        
        Args:
            style: 风格名称
            prompt_type: 提示词类型
            feedback: 打分反馈
            
        Returns:
            优化后的提示词（需要调用 LLM 生成）
        """
        manager = self.styles.get(style)
        if not manager:
            return ""
        
        # 获取当前版本
        if prompt_type == "creator":
            current = manager.creator_versions[-1].content
        elif prompt_type == "critic":
            current = manager.critic_versions[-1].content
        elif prompt_type == "image":
            current = manager.image_prefix_versions[-1].content
        elif prompt_type == "video":
            current = manager.video_prefix_versions[-1].content
        else:
            return ""
        
        # 构建优化请求
        optimize_request = f"""当前提示词：
{current}

评分反馈：
{feedback}

请根据反馈优化提示词，保持原有风格要求，改进不足之处。直接输出优化后的提示词，不要解释。"""
        
        # 返回优化请求（需要调用 LLM）
        return optimize_request


# 全局实例
prompt_optimizer = PromptOptimizer()
