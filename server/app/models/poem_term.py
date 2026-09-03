"""诗词倒排索引模型（jieba 预分词）
用于热点→主题词→诗词 的毫秒级精确检索，替代 `content LIKE` 全表扫描。
"""
from sqlalchemy import Column, Integer, String, Index, ForeignKey
from app.database import Base


class PoemTerm(Base):
    """诗词倒排表：记录每个 jieba 分词后的词（term）对应的诗词。

    设计（ADR-005）：
    - term 为热点主题词/关键词（如「明月」「故乡」）；
    - position 标记来源（'t'=标题 / 'c'=正文），便于按来源加权；
    - 查询用 `term IN (...)` 精确匹配（走 idx_poem_terms_term 索引），毫秒级。
    """
    __tablename__ = "poem_terms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    poem_id = Column(Integer, ForeignKey("poems.id"), nullable=False, comment="关联诗词ID")
    term = Column(String(64), nullable=False, comment="jieba 分词后的词（检索键）")
    position = Column(String(1), nullable=False, default="c", comment="来源: t=标题 / c=正文")

    __table_args__ = (
        Index("idx_poem_terms_term", "term"),
    )

    def __repr__(self):
        return f"<PoemTerm(poem_id={self.poem_id}, term='{self.term}', position='{self.position}')>"
