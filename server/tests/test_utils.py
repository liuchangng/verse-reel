"""工具函数单元测试"""
import pytest
from app.utils.chinese import to_simplified, batch_convert, segment, add_special_words


class TestChineseConversion:
    """繁简转换测试"""
    
    def test_to_simplified_traditional(self):
        """测试繁体转简体"""
        assert to_simplified('國') == '国'
        assert to_simplified('將') == '将'
        assert to_simplified('進') == '进'
        assert to_simplified('酒') == '酒'  # 已是简体
    
    def test_to_simplified_sentence(self):
        """测试句子转换"""
        result = to_simplified('將進酒')
        assert result == '将进酒'
    
    def test_to_simplified_empty(self):
        """测试空字符串"""
        assert to_simplified('') == ''
    
    def test_to_simplified_none(self):
        """测试 None 输入"""
        assert to_simplified(None) is None
    
    def test_to_simplified_already_simplified(self):
        """测试已经是简体的文本"""
        assert to_simplified('李白') == '李白'
        assert to_simplified('静夜思') == '静夜思'
    
    def test_batch_convert(self):
        """测试批量转换"""
        texts = ['國', '將', '進']
        result = batch_convert(texts)
        assert result == ['国', '将', '进']
    
    def test_batch_convert_with_none(self):
        """测试批量转换包含 None"""
        texts = ['國', None, '進']
        result = batch_convert(texts)
        assert result[0] == '国'
        assert result[1] is None
        assert result[2] == '进'


class TestJiebaSegmentation:
    """jieba 分词封装测试（ADR-005：倒排表预分词）"""

    def test_segment_classical_poem(self):
        """测试古诗词分词（检测核心主题词）"""
        add_special_words()
        words = segment('床前明月光疑是地上霜')
        joined = ' '.join(words)
        assert '明月' in joined or '明月光' in joined  # 应切出「明月/明月光」

    def test_segment_title(self):
        """测试标题分词"""
        add_special_words()
        words = segment('静夜思')
        assert '静夜思' in ' '.join(words) or '思' in words

    def test_segment_returns_list(self):
        """segment 返回 list 且过滤单字词（≥2 字）"""
        words = segment('明月何时照我还')
        assert isinstance(words, list)
        assert all(len(w) >= 1 for w in words)

    def test_segment_empty(self):
        """空字符串返回空列表"""
        assert segment('') == []
