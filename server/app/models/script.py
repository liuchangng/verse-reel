"""文案数据模型"""
from sqlalchemy import Column, Integer, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.database import Base


class Script(Base):
    """文案表"""
    __tablename__ = "scripts"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, comment="关联任务ID")
    hook = Column(Text, comment="痛点钩子")
    rebrand = Column(Text, comment="人设重塑")
    details = Column(Text, comment="电影级细节")
    alignment = Column(Text, comment="灵魂对齐")
    emotion = Column(Text, comment="情绪出口")
    full_script = Column(Text, comment="完整文案")
    score = Column(Integer, comment="评分 0-10")
    score_feedback = Column(Text, comment="评分反馈")
    retry_count = Column(Integer, default=0, comment="重试次数")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    def __repr__(self):
        return f"<Script(id={self.id}, task_id={self.task_id}, score={self.score})>"
