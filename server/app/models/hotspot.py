"""热点数据模型"""
from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, DateTime

from app.database import Base


class Hotspot(Base):
    """热点表 —— 多平台热搜落库，定时刷新覆盖"""
    __tablename__ = "hotspots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(32), index=True, comment="平台标识/名称")
    title = Column(String(255), index=True, comment="热搜标题")
    hot = Column(Integer, default=0, comment="热度值")
    url = Column(String(512), default="", comment="链接")
    recommended_poems = Column(Text, default="", comment="推荐诗词ID列表(JSON)")
    fetched_at = Column(DateTime, default=datetime.now, comment="抓取时间")
