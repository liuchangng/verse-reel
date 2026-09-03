"""XML 流式解析器 - 支持 1.8GB 大文件增量读取"""
import logging
from typing import Generator, Optional
from lxml import etree

from app.utils.chinese import to_simplified

logger = logging.getLogger(__name__)


class PoemData:
    """诗词数据结构"""
    
    def __init__(
        self,
        cnk_id: int,
        title: str,
        title_traditional: str,
        author: str,
        author_traditional: str,
        dynasty: str,
        genre: str,
        content: str,
        content_traditional: str,
        rhyme: Optional[str] = None,
    ):
        self.cnk_id = cnk_id
        self.title = title
        self.title_traditional = title_traditional
        self.author = author
        self.author_traditional = author_traditional
        self.dynasty = dynasty
        self.genre = genre
        self.content = content
        self.content_traditional = content_traditional
        self.rhyme = rhyme
    
    def __repr__(self):
        return f"<PoemData(id={self.cnk_id}, title='{self.title}', author='{self.author}')>"


def parse_poem_element(poem_elem) -> Optional[PoemData]:
    """
    解析单个 Poem XML 元素
    
    Args:
        poem_elem: lxml etree 元素
        
    Returns:
        PoemData 对象，解析失败返回 None
    """
    try:
        # 提取基本属性
        cnk_id = int(poem_elem.get("Id", 0))
        if cnk_id == 0:
            return None
        
        dynasty = poem_elem.get("D", "")
        author_traditional = poem_elem.get("AU", "")
        genre = poem_elem.get("T", "")
        rhyme = poem_elem.get("R", "")
        
        # 提取标题
        title_elem = poem_elem.find("Title")
        title_traditional = title_elem.get("C", "") if title_elem is not None else ""
        
        # 提取内容（所有 Ju 元素）
        content_parts = []
        ju_elems = poem_elem.findall(".//Ju")
        for ju in ju_elems:
            c = ju.get("C", "")
            if c:
                content_parts.append(c)
        content_traditional = "".join(content_parts)
        
        # 繁简转换
        title = to_simplified(title_traditional)
        author = to_simplified(author_traditional)
        content = to_simplified(content_traditional)
        
        return PoemData(
            cnk_id=cnk_id,
            title=title,
            title_traditional=title_traditional,
            author=author,
            author_traditional=author_traditional,
            dynasty=dynasty,
            genre=genre,
            content=content,
            content_traditional=content_traditional,
            rhyme=rhyme,
        )
    except Exception as e:
        logger.warning(f"解析诗词元素失败: {e}")
        return None


def parse_xml_file(
    file_path: str,
    batch_size: int = 1000,
) -> Generator[list[PoemData], None, None]:
    """
    流式解析 XML 文件，批量 yield 诗词数据
    
    Args:
        file_path: XML 文件路径
        batch_size: 每批返回的诗词数量
        
    Yields:
        每批诗词数据列表
        
    Example:
        for batch in parse_xml_file("CNKGraph.Writings.xml"):
            save_to_database(batch)
    """
    logger.info(f"开始解析 XML 文件: {file_path}")
    
    batch = []
    total_count = 0
    
    # 使用 iterparse 流式解析，避免一次性加载整个文件
    context = etree.iterparse(file_path, events=("end",), tag="Poem")
    
    for event, elem in context:
        poem = parse_poem_element(elem)
        if poem:
            batch.append(poem)
            total_count += 1
            
            if len(batch) >= batch_size:
                logger.info(f"已解析 {total_count} 首诗词")
                yield batch
                batch = []
        
        # 清理已处理的元素，释放内存
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]
    
    # 处理最后一批
    if batch:
        logger.info(f"已解析 {total_count} 首诗词（完成）")
        yield batch
    
    logger.info(f"XML 解析完成，共 {total_count} 首诗词")


def get_file_stats(file_path: str) -> dict:
    """
    获取 XML 文件统计信息
    
    Args:
        file_path: XML 文件路径
        
    Returns:
        统计信息字典
    """
    import os
    
    stats = {
        "file_size": os.path.getsize(file_path),
        "file_size_mb": os.path.getsize(file_path) / (1024 * 1024),
    }
    
    # 快速统计诗词数量
    count = 0
    context = etree.iterparse(file_path, events=("end",), tag="Poem")
    for event, elem in context:
        count += 1
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]
        if count % 100000 == 0:
            logger.info(f"统计中... 已计数 {count}")
    
    stats["total_poems"] = count
    return stats


if __name__ == "__main__":
    # 测试解析
    import sys
    
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
    else:
        file_path = "G:/cnkgraph/CNKGraph.Writings.xml"
    
    print(f"解析文件: {file_path}")
    total = 0
    for batch in parse_xml_file(file_path, batch_size=100):
        total += len(batch)
        if total <= 3:
            for poem in batch:
                print(f"  {poem}")
    
    print(f"\n总计: {total} 首诗词")
