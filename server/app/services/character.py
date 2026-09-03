"""角色一致性服务 - 定妆照系统 + 多镜头一致性（S4）

设计目标（对齐 LocalMiniDrama / ArcReel 的角色一致性方案）：
- 先生成一张「角色定妆照」（character sheet）作为 i2i 参考图，注入每张分镜图，
  实现视觉一致性；
- 同时抽取一份「六层特征锚点」结构化描述，回灌进每张分镜图的提示词，
  实现语义一致性（双保险，杜绝"6 张人物各不相同"）；
- 角色定妆照生成失败时 **fail-loud**：直接抛错让任务标记 failed，
  而不是静默继续生成 6 张互不一致的分镜图。

agnes 图片模型（agnes-image-2.1-flash）**支持**图生图，参考图走
``extra_body.image``（见 agnes-media-generator SKILL.md 图生图章节），
因此 i2i 注入路径是合法且应当生效的。
"""
import json
import logging
from typing import Optional

from app.services.agnes import agnes_client

logger = logging.getLogger(__name__)


# 六层特征锚点：抽取/生成角色描述时强制覆盖这六个维度，
# 回灌提示词保证跨镜头一致。
CHARACTER_LAYERS = [
    "身份",        # 是谁（诗人/历史人物/虚拟角色）
    "年龄气质",    # 大概年龄 + 整体气场
    "发型妆容",    # 发型、胡须、妆容
    "服饰",        # 朝代/款式/颜色
    "五官特征",    # 脸型、眉眼、显著标记
    "配色画风",    # 整体配色与统一画风
]


class CharacterService:
    """角色一致性服务"""

    async def extract_character_from_script(self, script: str) -> dict:
        """从文案中抽取角色六层特征锚点。

        优先用 LLM（agnes 文本）结构化抽取；任何异常都回退到写死的诗人档案，
        保证不阻塞主流程。返回的 description 是六层锚点的拼接文本，
        会回灌到每张分镜图的提示词。
        """
        # 1) LLM 结构化抽取
        try:
            profile = await self._llm_extract(script)
            if profile and profile.get("description"):
                return profile
        except Exception as e:
            logger.warning(f"LLM 抽取角色失败，回退字典: {e}")

        # 2) 回退：写死诗人档案（覆盖常见诗人）
        poet_profiles = {
            "李白": {"name": "李白", "description": "唐代诗人，40岁左右，白衣长衫，长须飘逸，仙风道骨，剑眉星目，清俊超逸", "era": "唐代"},
            "杜甫": {"name": "杜甫", "description": "唐代诗人，50岁左右，清瘦面容，忧国忧民神情，灰袍布衣，短须，眉心微蹙", "era": "唐代"},
            "苏轼": {"name": "苏轼", "description": "宋代文人，45岁左右，圆脸微胖，儒雅随和，官袍幞头，三缕长须，目光温和", "era": "宋代"},
            "李清照": {"name": "李清照", "description": "宋代女词人，30岁左右，清秀婉约，书卷气，素色襦裙，发髻高挽，眉目含愁", "era": "宋代"},
            "辛弃疾": {"name": "辛弃疾", "description": "宋代词人，40岁左右，英武豪迈，文武双全，铠甲常服，浓眉方脸，目光如炬", "era": "宋代"},
            "王维": {"name": "王维", "description": "唐代诗人，35岁左右，清雅脱俗，隐士风范，青衫幅巾，淡眉朗目，神色恬淡", "era": "唐代"},
            "白居易": {"name": "白居易", "description": "唐代诗人，45岁左右，和蔼可亲，平民诗人，素袍便帽，圆脸无须，神情温厚", "era": "唐代"},
            "陆游": {"name": "陆游", "description": "宋代诗人，60岁左右，白发苍苍，爱国情怀，旧袍佝偻，瘦削长脸，眉宇悲怆", "era": "宋代"},
        }
        for name, profile in poet_profiles.items():
            if name in script:
                logger.info(f"从文案中检测到角色(字典): {name}")
                return profile
        return {"name": "古代文人", "description": "古代文人，30岁左右，书生打扮，儒雅气质，青衫方巾，清秀面容，长身玉立", "era": "古代"}

    async def _llm_extract(self, script: str) -> Optional[dict]:
        """用 agnes 文本模型把文案结构化成一角色六层锚点。"""
        layers_hint = "、".join(CHARACTER_LAYERS)
        sys_msg = (
            "你是角色设定师。从古诗词解说文案中提取唯一主角的外貌与气质设定，"
            f"必须覆盖六层：{layers_hint}。只输出 JSON，不要解释。"
        )
        user_msg = (
            f"文案：\n{script[:1500]}\n\n"
            "请输出 JSON：\n"
            '{"name":"角色名","era":"朝代/时期","description":"把六层锚点用逗号拼成一个自然描述，'
            '例如：唐代诗人，40岁左右，白衣长衫，长须飘逸，剑眉星目，清俊超逸配色"}'
        )
        text = await agnes_client.generate_text(
            [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
            max_tokens=400,
            temperature=0.4,
        )
        # 容错解析：截取第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return None
        obj = json.loads(text[start:end + 1])
        desc = obj.get("description")
        if not desc:
            return None
        return {
            "name": obj.get("name") or "主角",
            "era": obj.get("era") or "",
            "description": desc.strip(),
        }

    async def generate_character_portrait(
        self,
        character_description: str,
        style: str = "古风写实",
    ) -> Optional[str]:
        """生成角色定妆照（四视图参考图，i2i 一致性锚点）。

        参考 LocalMiniDrama / ArcReel：先出一张角色设定图（正面为主 + 3/4 侧脸
        为辅），统一发型/服饰/五官/画风，作为后续每张分镜图的参考图。

        Args:
            character_description: 六层锚点描述
            style: 画风
        Returns:
            定妆照 URL；生成失败返回 None（调用方应 fail-loud）
        """
        prompt = (
            f"{style}风格，角色一致性参考图：同一人物的[正面清晰半身肖像]与[3/4侧脸小图]并列展示，"
            f"统一人物特征——{character_description}，"
            f"纯色背景，专业摄影质感，高清，面部细节清晰，"
            f"右下角用细小文字标注角色名。用于多镜头一致性参照。"
        )
        logger.info(f"生成角色定妆照(四视图): {character_description[:50]}...")
        image_url = await agnes_client.generate_image(prompt, size="1K", ratio="1:1")
        if not image_url:
            return None
        logger.info(f"角色定妆照生成完成: {image_url}")
        return image_url

    async def generate_character_reference(
        self,
        script: str,
        style: str = "古风写实",
    ) -> dict:
        """从文案提取角色并生成定妆照，返回 {ref, description, name}。

        Returns:
            {
              "ref": 定妆照 URL 或 None,
              "description": 六层锚点描述（回灌提示词用）,
              "name": 角色名,
            }
        """
        character = await self.extract_character_from_script(script)
        ref = await self.generate_character_portrait(
            character_description=character["description"],
            style=style,
        )
        return {
            "ref": ref,
            "description": character["description"],
            "name": character.get("name", "主角"),
        }


# 全局服务实例
character_service = CharacterService()
