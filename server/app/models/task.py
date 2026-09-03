"""任务数据模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.database import Base


class Task(Base):
    """任务表"""
    __tablename__ = "tasks"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    poem_id = Column(Integer, ForeignKey("poems.id"), nullable=False, comment="关联诗词ID")
    # status: pending/processing/pending_review/done/failed
    status = Column(String(20), default="pending", comment="状态: pending/processing/pending_review/done/failed")
    current_stage = Column(String(20), comment="当前阶段: script/review/image/video/done")
    progress = Column(Integer, default=0, comment="进度百分比 0-100")
    platform = Column(String(20), default="douyin", comment="目标平台(主平台): douyin/xiaohongshu/kuaishou/bilibili")
    # 本任务选中的发布平台列表(JSON 字符串)，为空时回退全局 settings.output_platforms
    platforms = Column(Text, nullable=True, comment="本任务选中的发布平台列表(JSON): douyin/xiaohongshu/kuaishou/bilibili")
    # 多平台合成产物 URL 映射(JSON 字符串)：{平台: 视频URL}，供发布阶段选择
    platform_outputs = Column(Text, nullable=True, comment="多平台视频URL映射(JSON)")
    script = Column(Text, comment="生成的文案脚本")
    script_score = Column(Integer, comment="文案评分")
    storyboard = Column(Text, comment="分镜JSON数据")
    style = Column(String(30), comment="文案风格(用于图片/视频提示词前缀, 跨阶段复用)")
    voice_preset = Column(String(32), default="", comment="自动选择的配音 preset id（LLM 推荐 + 标题兜底；空=用默认/全局参考音频）")
    character_description = Column(Text, comment="角色外貌描述(用于定妆照+分镜图一致性)")
    image_score = Column(Integer, comment="图片评分")
    video_url = Column(String(500), comment="视频URL")
    video_duration = Column(Integer, comment="视频时长(秒)")
    image_urls = Column(Text, comment="生成的图片URL列表(JSON)")
    character_ref = Column(String(500), comment="角色定妆照URL(用于分镜图一致性)")
    audio_url = Column(String(500), comment="配音音频URL")
    subtitle_url = Column(String(500), comment="字幕文件路径")
    error_message = Column(Text, comment="错误信息")
    # 人工审核（Q2A 发布前审核）
    review_status = Column(String(20), default="pending", comment="审核状态: pending/approved/rejected")
    review_comment = Column(Text, nullable=True, comment="审核意见")
    reviewed_at = Column(DateTime(timezone=True), nullable=True, comment="审核时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    completed_at = Column(DateTime(timezone=True), comment="完成时间")
    
    def __repr__(self):
        return f"<Task(id={self.id}, poem_id={self.poem_id}, status='{self.status}')>"
