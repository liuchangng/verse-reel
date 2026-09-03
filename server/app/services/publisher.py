"""发布服务 - 自动发布到各平台"""
import logging
import subprocess
from typing import Optional
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)


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


# 全局服务实例
publisher_service = PublisherService()
