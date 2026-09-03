"""诗词标签模型（P1：主题/季节/金句等语义标签层）。

设计意图（hotspot v2.3 §4.1）：
- 替代纯 jieba term 倒排的语义缺口（古风/豪放/剑仙等 term 覆盖不足）。
- 与 term 倒排并行：_rule_candidates 在 term 命中后追加 tag 加权召回。
- source/confidence 字段支持人工/规则/LLM 多级标注，后续可人工校准。

标签类型：
- theme   主题流派（豪放/婉约/思乡/咏史…）
- season  季节节令（春/夏/秋/冬/春节/中秋/重阳…）
- emotion 情感倾向（悲秋/离愁/豪迈…）
- style   风格（仙气/剑仙/浪漫/沉郁…）
- golden  金句级（标志性诗句，position='g' 已记在 poem_terms）
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime
from app.database import Base


class PoemTag(Base):
    __tablename__ = "poem_tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    poem_id = Column(Integer, nullable=False, comment="关联诗词 ID")
    tag = Column(String(32), nullable=False, comment="标签值")
    tag_type = Column(String(16), nullable=False, default="theme", comment="标签类型: theme/season/emotion/style/golden")
    confidence = Column(Integer, nullable=False, default=100, comment="置信度 0-100（规则 100，LLM 推断 <100）")
    source = Column(String(32), nullable=False, default="rule", comment="来源: manual/rule/ai_infer")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
