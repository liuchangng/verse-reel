"""发布服务 - 自动发布到各平台"""
import hashlib
import json
import logging
import re
import subprocess
from typing import Optional
from dataclasses import dataclass

from app.config import settings
from app.services.agnes import agnes_client

logger = logging.getLogger(__name__)


# 各平台发布文案字段规范（2026-09-10 Q3 调研沉淀，LLM 生成时注入 prompt 的硬约束）。
# 依据：抖音 LobeHub douyin-smart-publisher / skillsmp；小红书 发现报告实操宝典 / 优畅主题；
# 快手 新榜 newrank 发布规则解析 / 虾果。字数与话题数各平台差异明显，故按平台分别约束。
PLATFORM_COPY_SPECS = {
    "douyin": {
        "name": "抖音",
        "title_max": 55,
        "title_min": 20,
        "desc_range": "100-200字",
        "tag_count": "3-5个（1-2个精准大话题 + 2-3个垂直小话题）",
        "style_hint": "前3秒定生死：标题用悬念/痛点+核心关键词+emoji，前15字必埋核心词；正文钩子开头，结尾带互动引导。",
    },
    "xiaohongshu": {
        "name": "小红书",
        "title_max": 20,
        "title_min": 12,
        "desc_range": "50-200字",
        "tag_count": "5-15个（核心标签+长尾标签+话题标签三级）",
        "style_hint": "标题短平快含关键词+行动导向；正文场景化引入，带emoji增节奏，话题覆盖项目/地域/行业关键词。",
    },
    "kuaishou": {
        "name": "快手",
        "title_max": 30,
        "title_min": 15,
        "desc_range": "50字内",
        "tag_count": "1-3个（1个核心大流量话题 + 2-3个精准内容话题）",
        "style_hint": "标题用疑问句/感叹句引互动，禁用'震惊''必看'等诱导词；正文与画面强关联，结尾开放式提问引评论。",
    },
    "bilibili": {
        "name": "B站",
        "title_max": 80,
        "title_min": 10,
        "desc_range": "≤250字",
        "tag_count": "最多10个（单个≤20字，建议3-5个）",
        "style_hint": "标题可玩梗但需点题（20-30字最佳），简介放合集/系列信息+置顶引导三连；标签走「知识/传统文化」垂类，B站受众吃「硬核解读」。",
    },
    "youtube": {
        "name": "YouTube",
        "title_max": 100,
        "title_min": 15,
        "desc_range": "前150字最关键（上限5000字符，可放系列链接/时间轴）",
        "tag_count": "3-5个hashtag（前3个显示在标题上方，用#开头）",
        "style_hint": "标题前70字符最关键（移动端可见），含核心关键词；描述开头150字写清看点，正文可放时间轴与系列链接；结尾引导订阅/点赞，标签用#古诗词#classical等。",
    },
}


@dataclass
class PublishResult:
    """发布结果"""
    success: bool
    platform: str
    message: str
    url: Optional[str] = None


class PublisherService:
    """发布服务"""
    
    # 平台→sau 命令映射
    PLATFORM_MAP = {
        "douyin": "douyin",
        "xiaohongshu": "xiaohongshu",
        "kuaishou": "kuaishou",
        "bilibili": "bilibili",
        "video_account": "tencent",
        "weibo": "weibo",
        "youtube": "youtube",
    }
    
    async def publish_video(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: list[str],
        platform: str,
        account_name: str = "default",
    ) -> PublishResult:
        """
        发布视频到指定平台
        
        Args:
            video_path: 视频文件路径
            title: 视频标题
            description: 视频描述
            tags: 标签列表
            platform: 平台名称
            account_name: 账号名称
            
        Returns:
            发布结果
        """
        sau_platform = self.PLATFORM_MAP.get(platform)
        if not sau_platform:
            return PublishResult(
                success=False,
                platform=platform,
                message=f"不支持的平台: {platform}",
            )
        
        try:
            # 构建 sau 命令
            tags_str = ",".join(tags) if tags else ""
            
            cmd = [
                "sau", sau_platform, "upload-video",
                "--account", account_name,
                "--file", video_path,
                "--title", title,
                "--desc", description,
            ]
            
            if tags_str:
                cmd.extend(["--tags", tags_str])
            
            logger.info(f"发布视频: {' '.join(cmd[:6])}...")
            
            # 执行命令
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            
            if result.returncode == 0:
                logger.info(f"发布成功: {platform}")
                return PublishResult(
                    success=True,
                    platform=platform,
                    message="发布成功",
                )
            else:
                error_msg = result.stderr or result.stdout
                logger.error(f"发布失败: {error_msg}")
                return PublishResult(
                    success=False,
                    platform=platform,
                    message=f"发布失败: {error_msg[:200]}",
                )
                
        except FileNotFoundError:
            return PublishResult(
                success=False,
                platform=platform,
                message="sau 工具未安装，请运行: pip install social-auto-upload",
            )
        except subprocess.TimeoutExpired:
            return PublishResult(
                success=False,
                platform=platform,
                message="发布超时",
            )
        except Exception as e:
            logger.error(f"发布异常: {e}")
            return PublishResult(
                success=False,
                platform=platform,
                message=f"发布异常: {str(e)}",
            )
    
    async def publish_to_multiple(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: list[str],
        platforms: list[str],
        account_name: str = "default",
    ) -> list[PublishResult]:
        """
        发布到多个平台
        
        Args:
            video_path: 视频文件路径
            title: 视频标题
            description: 视频描述
            tags: 标签列表
            platforms: 平台列表
            account_name: 账号名称
            
        Returns:
            各平台发布结果列表
        """
        results = []
        
        for platform in platforms:
            result = await self.publish_video(
                video_path=video_path,
                title=title,
                description=description,
                tags=tags,
                platform=platform,
                account_name=account_name,
            )
            results.append(result)
        
        return results
    
    async def generate_publish_content(
        self,
        script: str,
        poem_title: str,
        platform: str,
    ) -> dict:
        """
        根据文案生成发布内容（标题、描述、标签）
        
        Args:
            script: 文案内容
            poem_title: 诗词标题
            platform: 平台名称
            
        Returns:
            {"title": "...", "description": "...", "tags": [...]}
        """
        # 提取文案前100字作为描述
        description = script[:100].replace("\n", " ").strip()
        
        # 根据平台调整标题长度
        if platform == "douyin":
            title = f"古诗解读｜{poem_title}"
            if len(title) > 30:
                title = title[:30]
        elif platform == "xiaohongshu":
            title = f"📚 古诗词解读｜{poem_title}｜人生感悟"
            if len(title) > 20:
                title = title[:20]
        elif platform == "kuaishou":
            title = f"古诗新解#{poem_title}"
        else:
            title = f"古诗词解读：{poem_title}"
        
        # 生成标签
        tags = ["古诗词", "人生感悟", poem_title]
        
        return {
            "title": title,
            "description": description,
            "tags": tags,
        }

    # LLM 生成的平台文案进程内缓存：key=(task_id, script 哈希, platforms 串)。
    # 前端"查看时按需生成"，同一任务同文案短时间内多次进详情页不重复调 LLM。
    # 脚本一旦变化（regenerate script）哈希即变 → 自然失效，无需手动清。
    _copy_cache: dict = {}
    _COPY_CACHE_MAX = 64  # 防止无界增长（多任务长跑场景）

    def _copy_cache_key(self, task_id, script, platforms) -> str:
        s_hash = hashlib.md5((script or "").encode("utf-8")).hexdigest()[:12]
        return f"{task_id}:{s_hash}:{','.join(sorted(platforms))}"

    def _copy_cache_get(self, key):
        return self._copy_cache.get(key)

    def _copy_cache_set(self, key, value):
        if len(self._copy_cache) >= self._COPY_CACHE_MAX:
            # 简单粗清理（非 LRU，够用）：留一半
            for k in list(self._copy_cache)[: self._COPY_CACHE_MAX // 2]:
                self._copy_cache.pop(k, None)
        self._copy_cache[key] = value

    async def generate_platform_copy(
        self,
        task_id: int,
        script: str,
        poem_title: str,
        author: str = "",
        dynasty: str = "",
        platforms: Optional[list] = None,
    ) -> dict:
        """按需（查看时）用 LLM 为各平台生成吸引眼球的发布文案。

        Args:
            task_id: 任务 ID（仅用于缓存键，便于同任务同文案复用）
            script: 文案脚本正文（LLM 取材源）
            poem_title: 诗词标题
            author/dynasty: 作者/朝代（增强标题信息量与相关性）
            platforms: 要生成的平台列表；None 时取全部已定义规范平台

        Returns:
            {platform: {"title": ..., "description": ..., "tags": [..], "generated": True/False}}
            LLM 失败的平台回退到 generate_publish_content 的规则版（generated=False），绝不空。
        """
        platforms = platforms or list(PLATFORM_COPY_SPECS.keys())
        platforms = [p for p in platforms if p in PLATFORM_COPY_SPECS]
        if not platforms:
            return {}

        cache_key = self._copy_cache_key(task_id, script, platforms)
        cached = self._copy_cache_get(cache_key)
        if cached:
            return cached

        result = {}
        # 1) 规则版兜底（LLM 失败/部分缺失时保证每个平台都有内容）
        for p in platforms:
            rule = await self.generate_publish_content(
                script=script or "", poem_title=poem_title, platform=p
            )
            result[p] = {**rule, "generated": False}

        if not script:
            return result  # 没有文案可取材，直接返回规则版

        # 2) LLM 一次生成所有请求平台的文案（按各平台字数/话题规范注入 prompt）
        try:
            spec_lines = []
            for p in platforms:
                s = PLATFORM_COPY_SPECS[p]
                spec_lines.append(
                    f'- 平台 {s["name"]}({p})：标题 {s["title_min"]}-{s["title_max"]} 字；'
                    f'描述 {s["desc_range"]}；话题 {s["tag_count"]}；风格：{s["style_hint"]}'
                )
            sys_prompt = (
                "你是资深短视频运营文案，专精古诗词解读类内容的平台化发布文案。"
                "要求：标题要吸引眼球、含核心关键词、与诗词强相关；"
                "描述贴合文案脚本、口语化；话题标签精准不堆砌。"
                "严格按下方各平台字数/数量规范产出，且每个平台标题与文案正文不得雷同。"
                "只输出一个 JSON 对象，不要任何解释或 markdown。"
            )
            user_prompt = f"""请为以下古诗词短视频解说，为指定平台各生成一组发布文案。

【诗词】{poem_title}（{author}·{dynasty}）
【文案脚本】
{script[:800]}

【各平台字段规范】
{chr(10).join(spec_lines)}

请输出 JSON，键为平台 id，值含 title/description/tags 三个字段，tags 为字符串数组：
{{
  "douyin": {{"title": "...", "description": "...", "tags": ["...", "..."]}},
  "xiaohongshu": {{"title": "...", "description": "...", "tags": ["..."]}},
  "kuaishou": {{"title": "...", "description": "...", "tags": ["..."]}},
  "bilibili": {{"title": "...", "description": "...", "tags": ["..."]}},
  "youtube": {{"title": "...", "description": "...", "tags": ["..."]}}
}}
只包含上面要求的平台键。"""

            raw = await agnes_client.generate_text(
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1500,
                temperature=0.7,
            )
            parsed = self._parse_copy_json(raw)
            # 用 LLM 结果覆盖对应平台（校验字段齐全 + 标题非空 + 字数软约束）
            for p in platforms:
                entry = parsed.get(p)
                if isinstance(entry, dict) and str(entry.get("title", "")).strip():
                    tags = entry.get("tags") or []
                    if not isinstance(tags, list):
                        tags = [str(tags)]
                    result[p] = {
                        "title": str(entry.get("title", "")).strip(),
                        "description": str(entry.get("description", "")).strip(),
                        "tags": [str(t) for t in tags if str(t).strip()],
                        "generated": True,
                    }
        except Exception as e:
            logger.warning(
                f"[{task_id}] LLM 生成平台文案失败，回退规则版: {str(e)[:160]}"
            )

        self._copy_cache_set(cache_key, result)
        return result

    @staticmethod
    def _parse_copy_json(text: str) -> dict:
        """从 LLM 输出稳健提取平台文案 JSON（容忍 markdown/杂质/裸控制字符）。
        解析失败返回 {}（调用方逐平台回退规则版），绝不抛错中断。"""
        text = text or ""
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}


# 全局服务实例
publisher_service = PublisherService()
