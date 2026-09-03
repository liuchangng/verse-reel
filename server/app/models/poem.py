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

    def __repr__(self):
        return f"<Poem(id={self.id}, title='{self.title}', author='{self.author}')>"
