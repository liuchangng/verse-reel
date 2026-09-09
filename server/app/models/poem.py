"""诗词数据模型"""
from sqlalchemy import Column, Integer, String, Text
from app.database import Base


class Poem(Base):
    """诗词表"""
    __tablename__ = "poems"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    cnk_id = Column(Integer, unique=True, index=True, comment="CNKGraph 原始ID")
    title = Column(String(200), nullable=False, comment="标题（简体）")
    title_traditional = Column(String(200), comment="标题（繁体）")
    author = Column(String(100), comment="作者（简体）")
    author_traditional = Column(String(100), comment="作者（繁体）")
    dynasty = Column(String(50), comment="朝代")
    genre = Column(String(50), comment="体裁")
    content = Column(Text, comment="内容（简体）")
    content_traditional = Column(Text, comment="内容（繁体）")
    rhyme = Column(String(20), comment="韵部")

    # hotspot v2.1 P1: 同题组诗逻辑归组
    group_id = Column(Integer, nullable=True, index=True, comment="组诗归组 ID；None 表示未归组或独立作品。同 (author, title) 的多条记录共享同一 group_id。")

    # 源头打标（2026-09-09 问题 4b，用户定夺"像监督学习一样给数据打标"）：
    # quality_score 0-100，由 scripts/backfill_quality_score.py 规则合成：
    # poets.fame_score*0.85 + 教材入选加成(min 15) + 选本入选加成(min 10)，
    # 当代/现代朝代封顶 45（质量池门槛见 settings.poem_quality_threshold）。
    quality_score = Column(Integer, nullable=True, index=True, comment="作品质量分 0-100（规则合成，None=未回填）")
    # 译文（部分覆盖即可：能流传下来的名篇优先补；数据来自开源语料 merge，暂无则 NULL）
    translation = Column(Text, nullable=True, comment="白话译文（部分覆盖，名篇优先）")

    def __repr__(self):
        return f"<Poem(id={self.id}, title='{self.title}', author='{self.author}')>"
