"""诗人本体模型 - 推荐算法作者层名望分（ETL 产出）

设计终版（见 docs/design_poets_etl.md v2）：
poems 表 203 万行以 "author 字符串" 为作者层，存在质量问题：
1. 长文拆行（《黄帝问玄女兵法》author=玄女, genre=文）→ 伪作者需剔除
2. 同名异人（王珪 跨隋末唐初/北宋/南宋）→ (author, dynasty) 作实体键
3. 繁简/异体分裂（查愼行 5302 行 vs 查慎行 3 行）→ author 归一 + variants
4. 高产 ≠ 名望（弘历 4.3 万行清诗）

中立性铁律（用户定稿）：
- 入分信号全部是"计数型客观事实"（tribute/anthology/social 是 COUNT，
  official/family 是客观事实编码）
- 禁止"评价型主观立场"：忠奸/变法对错/派系功过永不入分；
  政治人物只记客观层级与"做过的事"，不评好坏

本表由 server/scripts/build_poets_etl.py 幂等重建，服务端只读。
"""
from sqlalchemy import Column, Integer, String, Float, Text, Index

from app.database import Base


class Poet(Base):
    """诗人实体：一个 (author, dynasty) 一行，含客观计数型名望分。"""
    __tablename__ = "poets"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ===== 规范名 + 消歧键 =====
    author = Column(String(100), nullable=False, comment="简体规范名（异体归一后）")
    author_variants = Column(Text, comment="JSON 别名表（含繁体/异体原始写法）")
    dynasty = Column(String(50), nullable=False, comment="归一主朝代（行数最多，实体键第二维）")
    dynasty_raw = Column(Text, comment="JSON 原始朝代分布 {朝代: 行数}")

    # ===== 统计特征（自 poems 聚合，仅特征/清洗用途）=====
    total_rows = Column(Integer, default=0, comment="该实体全部行数")
    prose_rows = Column(Integer, default=0, comment="genre='文' 行数（文章拆行信号）")
    genre_stats = Column(Text, comment="JSON 体裁分布")
    split_rows = Column(Integer, default=0, comment="疑似长文拆分行数")

    # ===== 用户点名因素 → 客观代理分项（全部计数/事实，无主观评价）=====
    tribute_hits = Column(Integer, default=0, comment="被后世追和/次韵/拟作数（影响深远，计数）")
    social_poems = Column(Integer, default=0, comment="同代唱和/赠答诗数（文人相惜/圈子活跃，计数）")
    official_rank = Column(Integer, default=0, comment="官职客观层级 0-4（4宰相/3部级/2州郡/1布衣/0无考，客观事实）")
    family_lineage = Column(Integer, default=0, comment="文学世家标记 0/1（父/祖为诗人，客观事实）")
    anthology_hits = Column(Integer, default=0, comment="唐诗/宋词三百首收录次数（种子计数）")
    textbook_hits = Column(Integer, default=0, comment="语文教材收录篇数（种子计数）")

    # ===== 综合名望 =====
    fame_score = Column(Float, default=0.0, comment="综合名望 0~100（合成公式见设计文档 §6）")
    fame_level = Column(String(2), default="E", comment="S/A/B/C/D/E 档")

    # ===== 标签（不评分，供过滤/解释）=====
    author_category = Column(String(20), default="normal",
                             comment="normal/placeholder/myth/religious/foreign/emperor/prose")
    school_tags = Column(Text, comment="JSON 流派标签 [江西诗派,婉约派]")
    style_tags = Column(Text, comment="JSON 风格标签 [豪放,田园,边塞]")
    life_tags = Column(Text, comment="JSON 生平标签（中性文学母题）[贬谪,隐逸,神童]")
    fact_tags = Column(Text, comment="JSON 做过的大事（只记事实不评对错）[变法,修书,治水]")

    # ===== 元信息 =====
    src = Column(String(200), comment="来源注释(seed/stat/mix)")
    updated_at = Column(String(32), comment="ETL 更新时间")

    __table_args__ = (
        Index("idx_poets_author", "author"),
        Index("idx_poets_dynasty", "dynasty"),
        Index("idx_poets_fame", "fame_score"),
    )

    def __repr__(self):
        return (f"<Poet(author={self.author}, dynasty={self.dynasty}, "
                f"fame={self.fame_score:.0f}/{self.fame_level}, cat={self.author_category})>")
