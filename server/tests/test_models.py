"""模型与检索基础单元测试"""
import pytest
from sqlalchemy import inspect

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Base
from app.models.poem_term import PoemTerm  # noqa: F401  (注册模型)


class TestPoemTermModel:
    """poem_terms 倒排表模型"""

    def test_table_exists_in_models(self):
        """PoemTerm 已在 Base.metadata 注册"""
        assert "poem_terms" in Base.metadata.tables

    def test_columns(self):
        """字段定义正确（id/poem_id/term/position）"""
        cols = {c: Base.metadata.tables["poem_terms"].columns[c].type
                for c in ["id", "poem_id", "term", "position"]}
        assert "id" in cols
        assert "poem_id" in cols
        assert "term" in cols
        assert "position" in cols

    def test_term_index(self):
        """term 上有索引（查询加速，毫秒级检索的关键）"""
        indexes = Base.metadata.tables["poem_terms"].indexes
        assert any("idx_poem_terms_term" in i.name or "term" in str(i.columns)
                   for i in indexes)

    def test_poem_id_foreign_key(self):
        """poem_id 是外键，指向 poems.id（一致性）"""
        fk = list(Base.metadata.tables["poem_terms"].columns["poem_id"].foreign_keys)
        assert len(fk) == 1
        assert fk[0].column.table.name == "poems"

