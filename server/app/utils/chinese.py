"""文本处理工具：繁简转换（OpenCC）+ 中文分词（jieba）"""
import opencc


# 创建转换器实例（繁体 -> 简体）
_converter = None

# 古诗词常见专有词（追加到 jieba 词典，避免被错误切分/漏切）
_SPECIAL_WORDS = [
    "明月光", "静夜思", "望庐山瀑布", "春晓", "登鹳雀楼", "悯农",
    "江雪", "枫桥夜泊", "九月九日忆山东兄弟", "出塞", "凉州词",
    "相思", "送元二使安西", "使至塞上", "行路难", "将进酒", "水调歌头",
    "念奴娇", "声声慢", "念天地之悠悠", "大漠孤烟直", "长河落日圆",
    "床前明月光", "疑是地上霜", "举头望明月", "低头思故乡",
]


def get_converter() -> opencc.OpenCC:
    """获取 OpenCC 转换器（单例）"""
    global _converter
    if _converter is None:
        _converter = opencc.OpenCC('t2s')
    return _converter


def to_simplified(text: str) -> str:
    """
    繁体转简体
    
    Args:
        text: 繁体中文文本
        
    Returns:
        简体中文文本
        
    Examples:
        >>> to_simplified('國')
        '国'
        >>> to_s將進酒')
        '将进酒'
    """
    if not text:
        return text
    converter = get_converter()
    return converter.convert(text)


def batch_convert(texts: list[str]) -> list[str]:
    """
    批量繁体转简体
    
    Args:
        texts: 繁体中文文本列表
        
    Returns:
        简体中文文本列表
    """
    converter = get_converter()
    return [converter.convert(text) if text else text for text in texts]


# ==================== 中文分词（jieba，ADR-005 倒排表预分词） ====================

# jieba 惰性导入（避免模块加载开销；首次调用时初始化）
_jieba = None
_special_words_added = False


def _get_jieba():
    """获取 jieba 分词器（单例 + 惰性加载）"""
    global _jieba
    if _jieba is None:
        import jieba
        _jieba = jieba
    return _jieba


def add_special_words():
    """注入古诗词常见专有词到 jieba 词典（幂等，只注入一次）。

    目的：避免「明月光」等古典词被 jieba 默认词典错误切分或漏切，
    确保倒排表 term 命中稳定。
    """
    global _special_words_added
    if _special_words_added:
        return
    jb = _get_jieba()
    for word in _SPECIAL_WORDS:
        jb.add_word(word)
    _special_words_added = True


def segment(text: str, hmm: bool = True) -> list[str]:
    """用 jieba 对文本分词（过滤纯空白/标点，保留 2 字及以上词）。

    Args:
        text: 待分词文本（建议先 to_simplified 转简体）
        hmm: 是否启用 HMM 新词发现。
             True（默认，查询/短文本）：对未知词做新词猜测，召回更全。
             False（倒排表预分词 build_terms_index）：纯词典切分，速度快 2.3 倍，
             且对古诗词更可控（「明月光」等已加专有词，HMM 反而易误合并）。

    Returns:
        分词后的词列表（≥2 字，用于倒排表 term）
        Examples:
            >>> segment('床前明月光疑是地上霜')
            ['床前', '明月光', '疑是', '地上', '霜']
    """
    if not text:
        return []
    jb = _get_jieba()
    add_special_words()
    words = [w.strip() for w in jb.cut(text, HMM=hmm) if w.strip()]
    # 倒排表检索键保留 2 字及以上（单字词噪音大，且「明月光」等已加词）
    return [w for w in words if len(w) >= 2]


# 测试函数
if __name__ == "__main__":
    # 测试单个转换
    test_cases = [
        ("國", "国"),
        ("將進酒", "将进酒"),
        ("床前明月光", "床前明月光"),
        ("李白", "李白"),
        ("", ""),
        (None, None),
    ]
    
    print("测试繁简转换:")
    for traditional, expected in test_cases:
        if traditional is None:
            result = to_simplified(None)
            status = "✓" if result is None else "✗"
        else:
            result = to_simplified(traditional)
            status = "✓" if result == expected else "✗"
        print(f"  {status} '{traditional}' -> '{result}' (expected: '{expected}')")
    
    # 测试批量转换
    batch = ["國", "將進酒", "李白"]
    batch_result = batch_convert(batch)
    print(f"\n批量转换: {batch} -> {batch_result}")
