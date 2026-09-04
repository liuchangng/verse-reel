"""热点服务 - 抓取多平台热搜 + AI 匹配诗词"""
import logging
import json
from typing import Optional
from enum import Enum

import httpx
# case: P2 金句加权（position='g' 条件聚合）依赖，勿删——缺失会让主路召回静默失效
from sqlalchemy import select, func, text, case
from datetime import datetime, timedelta

from app.models.poem_term import PoemTerm
from app.models.poem import Poem
from app.models.hotspot import Hotspot
from app.services.agnes import agnes_client
from app.services.recommend_scoring import (
    PoemLike, score_poem, diverse_top_k,
    build_author_prestige_table, build_author_fame_table, is_pseudo_author,
)

logger = logging.getLogger(__name__)

# ===== 作者权威表：启动期一次性加载 + 进程内缓存 =====
# 203 万首诗的 GROUP BY 一次 ~5s，避免每次推荐都重算。
_prest_table_cache: dict[str, int] | None = None
_prest_table_lock = None  # lazy init (needs asyncio lock per event loop)

# ===== 诗人名望表（poets.fame_score）：设计文档 §8 接入 =====
# 取代上表的行数启发：入诗数只反映"高产"，fame 才反映"名望"。
# 表小（60321 行，索引后 ~5.8 万键），加载 <1s；ETL 未跑时返回 {} 自动降级。
_fame_table_cache: dict[str, tuple] | None = None
_fame_table_lock = None


async def _get_prestige_table(db) -> dict[str, int]:
    """进程级缓存：第一次调用 GROUP BY 一次，后续直返 dict。"""
    global _prest_table_cache, _prest_table_lock
    if _prest_table_cache is not None:
        return _prest_table_cache
    import asyncio
    if _prest_table_lock is None:
        _prest_table_lock = asyncio.Lock()
    async with _prest_table_lock:
        if _prest_table_cache is None:
            _prest_table_cache = await build_author_prestige_table(db)
            logger.info(f"作者权威表已加载: {len(_prest_table_cache)} 位作者")
    return _prest_table_cache


async def _get_fame_table(db) -> dict[str, tuple]:
    """进程级缓存：poets 表 → {author: (fame_score, category)}。

    poets 表缺失/为空（新环境未跑 build_poets_etl.py）时缓存空 dict 并降级，
    score_poem 会自动退回 _get_prestige_table 的行数启发，不影响可用性。
    """
    global _fame_table_cache, _fame_table_lock
    if _fame_table_cache is not None:
        return _fame_table_cache
    import asyncio
    if _fame_table_lock is None:
        _fame_table_lock = asyncio.Lock()
    async with _fame_table_lock:
        if _fame_table_cache is None:
            table = await build_author_fame_table(db)
            _fame_table_cache = table
            if table:
                s = sum(1 for f, c in table.values() if f >= 85)
                logger.info(f"诗人名望表已加载: {len(table)} 个键，S 档 {s} 位")
            else:
                logger.warning("诗人名望表为空（poets 未建/无数据），作者权威退回入诗数启发")
    return _fame_table_cache


def reset_fame_table_cache() -> None:
    """清空名望表缓存（ETL 重跑后调用，或测试隔离用）。"""
    global _fame_table_cache
    _fame_table_cache = None

# 热搜平台配置
HOTSPOT_SOURCES = {
    "weibo": {"name": "微博", "api": "https://weibo.com/ajax/side/hotSearch"},
    "douyin": {"name": "抖音", "api": "https://www.douyin.com/aweme/v1/web/hot/search/list/"},
    "baidu": {"name": "百度", "api": "https://top.baidu.com/board?tab=realtime"},
    "zhihu": {"name": "知乎", "api": "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total"},
    "bilibili": {"name": "B站", "api": "https://api.bilibili.com/x/web-interface/ranking/v2"},
}

# 备用热搜数据（API 不可用时使用，主题贴近古诗词库：月令/山水/思乡/节气/古风）
FALLBACK_HOTSPOTS = {
    "weibo": [
        {"title": "中秋月圆夜", "hot": 1532100},
        {"title": "秋日登高望远", "hot": 1287400},
        {"title": "古诗词里的乡愁", "hot": 1123500},
        {"title": "二十四节气·寒露", "hot": 986500},
        {"title": "苏轼《定风波》走红", "hot": 854300},
        {"title": "汉服出圈国风潮", "hot": 765100},
        {"title": "李白的月亮", "hot": 642800},
    ],
    "douyin": [
        {"title": "古风音乐火了", "hot": 2345600},
        {"title": "山水画卷般的秋景", "hot": 1876500},
        {"title": "故宫的秋天", "hot": 1543200},
        {"title": "诗词朗诵挑战", "hot": 1287600},
        {"title": "汉服游园", "hot": 1054300},
    ],
    "kuaishou": [
        {"title": "田园秋收", "hot": 1123400},
        {"title": "老家院子里的桂花", "hot": 987600},
        {"title": "乡村晨雾", "hot": 765400},
    ],
}

# 热点→诗词主题映射规则（#20260903-A 扩展：覆盖古风/秋景/月圆夜等新热点）
# 关键词匹配 title 字符串；themes 中的项直接作为倒排表检索键（term IN (...)）。
TOPIC_THEME_MAP = {
    # 职场/焦虑类
    "职场": ["失意", "怀才不遇", "壮志难酬"],
    "加班": ["失意", "思乡", "归隐"],
    "内卷": ["失意", "人生感悟", "归隐"],
    "焦虑": ["失意", "人生感悟", "哲理"],
    "35岁": ["失意", "怀才不遇", "人生感悟"],

    # 情感类
    "爱情": ["爱情", "相思", "离别"],
    "分手": ["离别", "失意", "思念"],
    "孤独": ["孤独", "思乡", "人生感悟"],
    "治愈": ["哲理", "人生感悟", "田园"],

    # 历史/文化类
    "历史": ["历史", "豪放", "边塞"],
    "李白": ["豪放", "浪漫", "饮酒"],
    "杜甫": ["忧国忧民", "失意", "现实"],
    "苏轼": ["豪放", "哲理", "人生感悟"],
    "古诗": ["古典", "意境", "山水"],
    # #20260903-A: 古风热 → 仙气/剑仙/饮酒/豪放（梦游天姥、将进酒等）
    "古风": ["豪放", "浪漫", "仙气", "剑仙", "饮酒", "山水"],
    "国风": ["豪放", "古典", "意境", "山水"],

    # 节气/时令
    "中秋": ["思乡", "团圆", "月亮"],
    "春节": ["思乡", "团圆", "归隐"],
    "清明": ["思念", "离别", "怀古"],
    "端午": ["爱国", "历史", "豪放"],

    # 自然/景色
    "春天": ["山水", "田园", "写景"],
    "秋天": ["悲秋", "思乡", "离别"],
    "雪": ["边塞", "冬景", "豪放"],
    "月亮": ["思乡", "相思", "月亮"],
    # #20260903-A: 秋景/山水画卷 → 扩展主题标签
    "秋景": ["悲秋", "秋兴", "山水", "田园", "登高"],
    "山水": ["山水", "田园", "秋景", "隐居"],
    "画": ["山水", "田园", "意境", "秋景"],
    # #20260903-A: 月圆/中秋相关标题
    "月圆": ["月亮", "思乡", "团圆", "中秋"],
}

# #20260903-A: jieba 分词产生的常见噪音词（含短虚词+高频虚词+量词）
# 规则：当主题词为空时，对 jieba 输出做过滤；同时防止"般的/次韵/二首"等进倒排。
_STOPWORDS_JIEBA = frozenset({
    # 虚词/介词/连词
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
    "一", "个", "上", "也", "很", "到", "说", "要", "去", "你", "会",
    "着", "没有", "看", "好", "自己", "这", "那", "么", "但", "而",
    "与", "及", "其", "以", "于", "为", "之", "所", "可", "被", "把",
    # 量词/数词噪音
    "首", "个", "只", "篇", "幅", "张", "曲", "阕", "卷", "章",
    "二", "三", "四", "五", "六", "七", "八", "九", "十", "百", "千", "万",
    # 高频虚词
    "此", "彼", "何", "某", "凡", "皆", "俱", "既", "又", "更", "尚",
    "仍", "或", "若", "如", "似", "同", "共", "相", "互", "各", "每",
    "所", "处", "间", "里", "中", "内", "外", "前", "后", "旁", "侧",
    # 结构/语气词
    "然", "乎", "哉", "焉", "耶", "欤", "兮", "也", "耳", "矣", "夫",
    # 高频泛词（>5万首诗命中，稀释相关性）
    "不知", "不可", "二首", "四首", "万里", "天下", "今日", "平生",
    "千里", "人间", "春风", "天地", "白云", "不见", "风雨", "故人",
    "百年", "十年", "如何", "君子", "先生", "如此", "以为", "不得",
    "可以", "不能",
    # 热点标题里的结构词（"古风音乐火了" 里的 "火了"/"音乐" 部分）
    "音乐", "火了", "般", "般的",
})


class HotspotService:
    """热点服务"""
    
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10.0)
    
    async def close(self):
        await self.client.aclose()
    
    async def fetch_hotspots(
        self,
        platforms: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, list[dict]]:
        """
        抓取多平台热搜
        
        Args:
            platforms: 平台列表，默认全部
            limit: 每个平台返回数量
            
        Returns:
            {"weibo": [{"title": "...", "hot": 12345}, ...], ...}
        """
        if platforms is None:
            platforms = list(HOTSPOT_SOURCES.keys())
        
        results = {}
        for platform in platforms:
            if platform not in HOTSPOT_SOURCES:
                results[platform] = []
                continue
            
            try:
                source = HOTSPOT_SOURCES[platform]
                resp = await self.client.get(
                    source["api"],
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                
                data = resp.json()
                
                # 解析不同平台的返回格式
                items = self._parse_platform_data(platform, data)
                items = items[:limit]
                
                results[platform] = [
                    {
                        "title": item.get("title", ""),
                        "hot": item.get("hot", 0),
                        "url": item.get("url", ""),
                        "platform": source["name"],
                    }
                    for item in items
                ]
                logger.info(f"抓取 {source['name']} 热搜: {len(items)} 条")
                
            except Exception as e:
                logger.warning(f"抓取 {platform} 热搜失败: {e}, 使用备用数据")
                # 使用备用数据
                fallback = FALLBACK_HOTSPOTS.get(platform, [])[:limit]
                results[platform] = [
                    {
                        "title": item.get("title", ""),
                        "hot": item.get("hot", 0),
                        "url": "",
                        "platform": HOTSPOT_SOURCES.get(platform, {}).get("name", platform),
                    }
                    for item in fallback
                ]
        
        return results

    async def save_hotspots(self, db, hotspots: dict[str, list[dict]]) -> None:
        """覆盖写库：清空旧数据后批量插入（用户要求『下次更新直接覆盖』）

        Args:
            db: AsyncSession
            hotspots: {platform: [{title, hot, url, recommended_poems:[id...]}, ...]}
        """
        await db.execute(text("DELETE FROM hotspots"))
        for platform, items in hotspots.items():
            for item in items:
                recs = item.get("recommended_poems") or []
                db.add(Hotspot(
                    platform=platform,
                    title=item.get("title", ""),
                    hot=item.get("hot", 0),
                    url=item.get("url", ""),
                    recommended_poems=json.dumps(recs, ensure_ascii=False),
                ))
        await db.flush()

    async def load_hotspots(self, db) -> tuple[dict, datetime | None]:
        """从库读取热点，按平台分组；含各条目的推荐诗词 id 列表。

        Returns:
            (platform -> [{title,hot,url,recommended_poems:[id...]}, ...], 最近抓取时间)
            库空时 ([], None)
        """
        result = await db.execute(select(Hotspot).order_by(Hotspot.hot.desc()))
        rows = result.scalars().all()
        if not rows:
            return {}, None
        by_platform: dict[str, list[dict]] = {}
        max_fetched = max(r.fetched_at for r in rows)
        for r in rows:
            recs: list = []
            if r.recommended_poems:
                try:
                    recs = json.loads(r.recommended_poems)
                except (json.JSONDecodeError, TypeError):
                    recs = []
            by_platform.setdefault(r.platform, []).append({
                "title": r.title,
                "hot": r.hot,
                "url": r.url,
                "recommended_poems": recs,
            })
        return by_platform, max_fetched

    async def hydrate_recommendations(
        self, db, by_platform: dict[str, list[dict]]
    ) -> dict[str, list[dict]]:
        """读库命中时把推荐诗词 id 还原为完整诗词对象（不再调 LLM，直接查 poems 表）

        Args:
            by_platform: load_hotspots 的返回（recommended_poems 为 id 列表）
        Returns:
            {platform: [{title, hot, url, recommended_poems:[完整对象...]}, ...]}
        """
        ids: set[int] = set()
        for items in by_platform.values():
            for it in items:
                ids.update(it.get("recommended_poems") or [])
        poems: dict[int, dict] = {}
        if ids:
            res = await db.execute(select(Poem).where(Poem.id.in_(list(ids))))
            for p in res.scalars().all():
                poems[p.id] = {
                    "id": p.id, "title": p.title, "author": p.author,
                    "dynasty": p.dynasty, "content_preview": (p.content or "")[:60],
                    "match_reason": "热门选题高频匹配",
                }
        out: dict[str, list[dict]] = {}
        for platform, items in by_platform.items():
            out[platform] = []
            for it in items:
                rec_ids = it.get("recommended_poems") or []
                out[platform].append({
                    "title": it.get("title", ""),
                    "hot": it.get("hot", 0),
                    "url": it.get("url", ""),
                    "recommended_poems": [poems[i] for i in rec_ids if i in poems],
                })
        return out

    async def get_top_poems(self, db, limit: int = 10) -> list[dict]:
        """最具市场潜力诗词：聚合所有热点条目推荐的诗词，按命中次数降序取 Top-N。

        Returns:
            [{id, title, author, dynasty, recommend_count}, ...]
        """
        res = await db.execute(select(Hotspot.recommended_poems))
        counter: dict[int, int] = {}
        for (rp,) in res.fetchall():
            if not rp:
                continue
            try:
                ids = json.loads(rp)
            except (json.JSONDecodeError, TypeError):
                continue
            for pid in ids:
                counter[pid] = counter.get(pid, 0) + 1
        if not counter:
            return []
        top_ids = [pid for pid, _ in sorted(counter.items(), key=lambda x: -x[1])[:limit]]
        poems_res = await db.execute(select(Poem).where(Poem.id.in_(top_ids)))
        poems = {p.id: p for p in poems_res.scalars().all()}
        return [
            {
                "id": pid,
                "title": poems[pid].title if pid in poems else "",
                "author": poems[pid].author if pid in poems else "",
                "dynasty": poems[pid].dynasty if pid in poems else "",
                "recommend_count": counter[pid],
            }
            for pid in top_ids
        ]

    def _parse_platform_data(self, platform: str, data: dict) -> list[dict]:
        """解析不同平台的返回数据"""
        items = []
        
        try:
            if platform == "weibo":
                # 微博: {"data": {"realtime": [{"word": "...", "num": ...}, ...]}}
                realtime = data.get("data", {}).get("realtime", [])
                items = [{"title": r.get("word", ""), "hot": r.get("num", 0)} for r in realtime]
            elif platform == "douyin":
                # 抖音: {"data": {"word_list": [{"word": "...", "hot_value": ...}, ...]}}
                word_list = data.get("data", {}).get("word_list", [])
                items = [{"title": w.get("word", ""), "hot": w.get("hot_value", 0)} for w in word_list]
            elif platform == "bilibili":
                # B站: {"data": {"list": [{"title": "...", "hot": ...}, ...]}}
                blist = data.get("data", {}).get("list", [])
                items = [{"title": b.get("title", ""), "hot": b.get("hot", 0)} for b in blist]
            elif platform == "zhihu":
                # 知乎: {"data": [{"target": {"title": "..."}, "detail_text": "..."}]}
                zlist = data.get("data", [])
                items = [{"title": z.get("target", {}).get("title", ""), "hot": 0} for z in zlist]
            else:
                # 通用解析
                if isinstance(data.get("data"), list):
                    items = data["data"][:20]
        except Exception as e:
            logger.warning(f"解析 {platform} 数据失败: {e}")
        
        return items

    def match_themes(self, hotspots: dict[str, list[dict]]) -> list[str]:
        """
        从热搜中提取匹配的诗词主题
        
        Args:
            hotspots: 热搜数据
            
        Returns:
            匹配的主题列表（去重）
        """
        matched_themes = set()
        
        for platform, items in hotspots.items():
            for item in items:
                title = item.get("title", "")
                # 匹配关键词
                for keyword, themes in TOPIC_THEME_MAP.items():
                    if keyword in title:
                        matched_themes.update(themes)
        
        # 如果没有匹配到，返回默认主题
        if not matched_themes:
            return ["人生感悟", "哲理", "古典"]
        
        return list(matched_themes)[:5]  # 最多返回5个主题
    
    def get_trending_keywords(self, hotspots: dict[str, list[dict]]) -> list[str]:
        """
        提取热搜关键词（用于注入提示词）
        
        Args:
            hotspots: 热搜数据
            
        Returns:
            关键词列表
        """
        keywords = []
        seen = set()
        
        for platform, items in hotspots.items():
            for item in items[:3]:  # 每个平台取前3
                title = item.get("title", "")
                # 简单分词（取核心词）
                words = self._extract_keywords(title)
                for word in words:
                    if word not in seen and len(word) >= 2:
                        seen.add(word)
                        keywords.append(word)
        
        return keywords[:10]  # 最多返回10个关键词
    
    def _extract_keywords(self, text: str) -> list[str]:
        """简单关键词提取"""
        # 去除标点
        import re
        text = re.sub(r'[^\w\s]', ' ', text)
        # 按空格分词
        words = text.split()
        # 过滤短词
        return [w for w in words if len(w) >= 2]

    def _keywords_to_terms(self, title: str) -> dict[str, float]:
        """热度标题 → {检索词: 权重}（倒排表加权查询键）。

        #20260903-A: 停用词过滤 + TOPIC_THEME_MAP 扩展。
        #20260904-问题7: 精确标题词(jieba)权重 1.0；TOPIC_THEME_MAP 扩展主题词
        权重 0.3（宽泛，仅补位用）——避免"人生感悟/豪放/思乡/月亮"等宽标签命中的
        海量无关诗污染候选池序，挤掉精确标题词（定风波/中秋/秋日）匹配的真正相关诗。
        流程：
        1. TOPIC_THEME_MAP 命中主题，收集 themes（扩展，权重 0.3）
        2. jieba 对标题分词，过滤停用词（精确，权重 1.0）
        3. 合并：同一词若同时是精确词则保留 1.0（精确优先于扩展）
        4. 按权重降序截断前 10，精确词恒在前
        """
        from app.utils.chinese import segment, add_special_words
        add_special_words()

        # 1. 用 TOPIC_THEME_MAP 主题 → 扩展检索词（宽泛，低权重）
        theme_terms = set()
        for keyword, themes in TOPIC_THEME_MAP.items():
            if keyword in title:
                for theme in themes:
                    theme_terms.add(theme)

        # 2. 标题 jieba 分词补充，过滤停用词/单字/短虚词（精确，高权重）
        title_terms_raw = set(segment(title, hmm=False))
        title_terms = {w for w in title_terms_raw if w not in _STOPWORDS_JIEBA}

        # 3. 合并：精确标题词权重 1.0，扩展主题词权重 0.3；同词取高
        result: dict[str, float] = {}
        for w in title_terms:
            result[w] = 1.0
        for t in theme_terms:
            result[t] = min(result.get(t, 1.0), 0.3)  # 若精确词已占，保留 1.0
        if not result:
            result = {"人生感悟": 0.3, "哲理": 0.3, "古典": 0.3}
        # 4. 按权重降序截断前 10（精确词恒在前）
        items = sorted(result.items(), key=lambda x: -x[1])[:10]
        return dict(items)

    async def _rule_candidates(
        self,
        db,
        keywords: list[str] | dict[str, float],
        limit: int = 200,
    ) -> list[int]:
        """规则初筛：倒排表 term IN (...) 加权匹配，聚合 poem_id 得分取 Top-N。

        两阶段召回（#20260904-问题7 修复）：
        - 阶段1（主路）：精确标题词(jieba) + 金句(position='g')，主路评分
          `score = golden_hits*3.0 + precise_hits*1.0`，取 Top-N。
        - 阶段2（扩展词仅补位）：TOPIC_THEME_MAP 扩展主题词（人生感悟/思乡/月亮 等
          宽泛标签）不混入主路评分，仅当主路不足 limit 时按命中数补位填充剩余配额。
        → 宽标签不再累积权重压过精确词，作者名(苏轼)被内容误命中也不污染池序。
        - keywords 兼容 list（全精确 1.0，旧单测/调试用）与 dict（term→weight）。
        #20260903: limit 默认 200（原 20），让多样性策略有机会覆盖古代正宗 /
        明清优秀等 bucket（旧 20 候选全集中在同一朝代/作者桶里）。
        #20260903-P2: 接入 golden 加权，使金句库注入生效。
        #20260903-C: 兜底混合名望 Top50 代表作，防止"检索词没命中"导致名诗漏出。
        """
        if not db or not keywords:
            return []

        # 归一化：dict → 精确词(>=1.0) / 扩展词(<1.0)；list → 全精确(权重1.0)
        if isinstance(keywords, dict):
            precise_terms = [k for k, w in keywords.items() if w >= 1.0]
            expanded_terms = [k for k, w in keywords.items() if w < 1.0]
        else:
            precise_terms = list(keywords)
            expanded_terms = []

        # --- 主路：精确词召回（高权重）+ 扩展词仅补位（2026-09-04 问题7） ---
        # 精确标题词(jieba) 与金句(position='g') 走主路并主路评分排序取 Top-N；
        # 扩展主题词(人生感悟/思乡/月亮 等宽泛标签) 不混入主路评分，仅当主路不足
        # limit 时才补位——避免宽标签累积权重压过精确词，或作者名(苏轼)被内容误
        # 命中污染池序（实测《燕魏杂记》吕颐浩因内容含"苏轼"被顶到定风波热点 Top1）。
        primary_ids: list[int] = []

        # 1) 精确词主路（金句加权 3.0，普通精确词 1.0）
        if precise_terms:
            precise_expr = case((PoemTerm.position == "g", 3.0), else_=1.0)
            stmt = (
                select(
                    PoemTerm.poem_id,
                    func.sum(precise_expr).label("score"),
                    func.count().label("hits"),
                    func.sum(case((PoemTerm.position == "g", 1), else_=0)).label("golden_hits"),
                )
                .where(PoemTerm.term.in_(precise_terms))
                .group_by(PoemTerm.poem_id)
                .order_by(func.sum(precise_expr).desc())
                .limit(limit)
            )
            try:
                result = await db.execute(stmt)
                rows = result.fetchall()
                primary_ids = [row[0] for row in rows]
                scored = [(row[0], row[1], row[2], row[3]) for row in rows]
                logger.info(
                    f"_rule_candidates 精确词召回 {len(primary_ids)} 首，"
                    f"top5 评分明细: {[(pid, round(sc, 1), h, gh) for pid, sc, h, gh in scored[:5]]}"
                )
            except Exception as e:
                # 主路失效 = 推荐退化为纯名望兜底（主题相关性全丢），属严重降级而非可忽略告警，
                # 故用 error + 堆栈保证可观测。此处不重抛，是为了让 fame 兜底仍能产出候选，
                # 保住接口可用性（2026-09-04: 曾因漏 import `case` 使主路静默失效未被发现）。
                logger.error(f"倒排表精确词初筛失败（主路召回失效，将退化为名望兜底）: {e}", exc_info=True)

        # 2) 扩展主题词仅补位：主路不足 limit 时才用宽标签填充剩余配额
        if expanded_terms and len(primary_ids) < limit:
            gap = limit - len(primary_ids)
            stmt2 = (
                select(PoemTerm.poem_id, func.count().label("hits"))
                .where(PoemTerm.term.in_(expanded_terms))
                .group_by(PoemTerm.poem_id)
                .order_by(func.count().desc())
                .limit(gap)
            )
            try:
                result2 = await db.execute(stmt2)
                added = 0
                for (pid, _hits) in result2.fetchall():
                    if pid not in primary_ids:
                        primary_ids.append(pid)
                        added += 1
                        if len(primary_ids) >= limit:
                            break
                logger.info(f"_rule_candidates 扩展词补位 {added} 首（主路缺口 {gap}）")
            except Exception as e:
                logger.error(f"倒排表扩展词补位失败: {e}", exc_info=True)

        # --- C 兜底：名望 S/A 档作者的代表作（fame>=70 且 normal）---
        # 即使倒排召回满 200 首，也确保名家的标志性作品有机会进入候选池，
        # 避免"检索词跑偏 → 当代打油诗挤掉李白"这类情况。
        fame_ids: list[int] = []
        try:
            fame_table = await _get_fame_table(db)
            if fame_table:
                # S/A 档作者按 fame_score 降序取前 50
                sorted_authors = sorted(
                    [(author, fame, cat) for author, (fame, cat) in fame_table.items()
                     if fame >= 70 and cat == "normal"],
                    key=lambda x: x[1],
                    reverse=True,
                )[:50]
                # 每位作者取代表作 2 首（按 id 序即入库序；S/A 档 26 人 → ~52 首）。
                # 注意：不用"全局 author IN (...) + LIMIT 100"，那会让先入库的高产
                # 作者（李白 1111 首等）占满上限，其他名家反而进不了兜底池。
                for author, _, _ in sorted_authors:
                    stmt2 = (
                        select(Poem.id)
                        .where(Poem.author == author)
                        .order_by(Poem.id)
                        .limit(2)
                    )
                    result2 = await db.execute(stmt2)
                    fame_ids.extend(row[0] for row in result2.fetchall())
        except Exception as e:
            logger.warning(f"名望兜底查询失败: {e}")

        logger.info(
            f"_rule_candidates: 倒排召回 {len(primary_ids)} 首 + 名望兜底 {len(fame_ids)} 首 → 合并保底 ≤{limit}"
        )
        # C 兜底语义（2026-09-04 修正）：名家作品只保证"入选"，不再保证"排前"。
        # 旧逻辑把 fame_ids 放在 merged 头部（占位优先），导致候选池前 52 位恒为兜底，
        # 而 _rule_top_by_score 的 rel() 按候选索引给相关性分 → 兜底诗恒得高分 →
        # 无论什么热点 Top1 都是同一首兜底诗（实测《李监宅》杜甫在两个无关热点均 Top1）。
        # 现改为：主路（主题相关）优先填至 main_cap，兜底用剩余容量补位，
        # 既保住"检索词跑偏时名家兜底"的原意，又让热点相关性决定前排。
        # 兜底不留空：给固定配额 FALLBACK_QUOTA，避免倒排满 limit 时兜底被整体截掉。
        # 也不宜无上限扩池——_llm_select 会把全部候选拼进 prompt，超 200 条易致 LLM 截断。
        FALLBACK_QUOTA = 50
        main_cap = limit - FALLBACK_QUOTA if limit > FALLBACK_QUOTA else limit
        merged: list[int] = []
        reserved: set[int] = set()
        for pid in primary_ids:
            if pid in reserved:
                continue
            if len(merged) >= main_cap:
                break
            merged.append(pid)
            reserved.add(pid)
        for pid in fame_ids:
            if pid in reserved:
                continue
            if len(merged) >= limit:
                break
            merged.append(pid)
            reserved.add(pid)

        # 2026-09-04 问题4：组诗归组去重（P1 ETL 已建 group_id）。
        # 同 group_id 的组诗（如《秋兴八首》8 条）仅保留池中评分最高(最前)的 1 首，
        # 消除"组诗占满 8 个候选槽、同质重复"问题——这是 P1 归组的原定验收目标。
        # group_id 为 NULL（独立作品/未归组）不受影响；查不到 group_id 时原样返回。
        if merged:
            try:
                grp_rows = await db.execute(
                    select(Poem.id, Poem.group_id).where(Poem.id.in_(merged))
                )
                gid_by_pid = {r[0]: r[1] for r in grp_rows.fetchall()}
                seen_groups: set[int] = set()
                deduped: list[int] = []
                for pid in merged:
                    g = gid_by_pid.get(pid)
                    if g is not None:
                        if g in seen_groups:
                            continue
                        seen_groups.add(g)
                    deduped.append(pid)
                merged = deduped
            except Exception as e:
                logger.warning(f"组诗去重查询失败（跳过去重）: {e}")

        return merged

    async def _llm_select(
        self,
        candidates: list[dict],
        hotspot: dict,
    ) -> list[dict]:
        """LLM 从候选池精选 ≤3 首，返回 [{id,title,author,dynasty,content_preview,match_reason}]。

        失败或无结果时返回空列表（由 recommend_poems 兜底规则 Top-3）。
        """
        if not candidates:
            return []

        prompt_candidates = "\n".join(
            f"- id={c['id']} 《{c['title']}》{c['author']}({c['dynasty']}): {c['content_preview'][:120]}"
            for c in candidates
        )
        messages = [
            {"role": "system", "content": (
                "你是古诗词匹配专家，负责把热点选题匹配到最打动人的诗词。"
                "核心标准（按优先级，#20260903 修正）：\n"
                "1) 经典名篇优先：优先选择「含有能独立成句、广为流传或极具画面感的"
                "千古名句/金句」的诗词。\n"
                "2) 作者权威【显著加分】：唐诗/宋词黄金时代的诗人（李白、杜甫、王维、"
                "孟浩然、白居易、李商隐、杜牧、王昌龄、岑参、刘禹锡、温庭筠、柳宗元、"
                "高适、苏轼、辛弃疾、李清照、陆游、范仲淹、王安石、欧阳修、秦观、"
                "周邦彦、姜夔、刘克庄、文天祥 等）以及明清名家（纳兰性德、唐寅、"
                "龚自珍、王士禛、袁枚、查慎行 等）的作品优先推荐；近现代虽被大众熟知，"
                "仅毛泽东、徐志摩、戴望舒、闻一多、艾青、海子、舒婷、北岛 等已被公认"
                "的名篇可入选，其余非权威作者的「古风」打油诗避免推荐。\n"
                "3) 契合热点：诗句意境或情感与热点标题能形成关联或反差共鸣。\n"
                "从候选中精选最契合的≤3首，仅输出JSON数组，每项含 poem_id(整数) 和 "
                "match_reason(一句话中文理由，必须点出是哪一句/哪种情感打动了人)。若无契合返回空数组[]。"
            )},
            {"role": "user", "content": f"热点标题：{hotspot.get('title','')}\n候选诗词：\n{prompt_candidates}\n请输出JSON。"},
        ]

        try:
            raw = await agnes_client.generate_text(messages, max_tokens=800)
            data = self._parse_llm_json(raw)
            return self._hydrate_candidates(data, candidates)
        except Exception as e:
            logger.warning(f"LLM 精选失败: {e}")
            return []

    def _parse_llm_json(self, raw: str) -> list[dict]:
        """解析 LLM 返回的 JSON（容忍 全文/``` 包裹）"""
        if not raw:
            return []
        # 去掉 ```json ``` 包围
        text = raw.strip()
        if "```" in text:
            import re
            text = re.sub(r"```[\w]*\n?", "", text).strip().rstrip("`").strip()
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "poems" in data:
                return data["poems"]
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"LLM JSON 解析失败: {raw[:120]}")
        return []

    def _hydrate_candidates(self, selected: list[dict], candidates: list[dict]) -> list[dict]:
        """把 LLM 选中的 poem_id 映射回候选详情（含 match_reason）"""
        by_id = {c["id"]: c for c in candidates}
        result = []
        for item in selected:
            pid = item.get("poem_id") or item.get("id")
            if pid not in by_id:
                continue
            base = dict(by_id[pid])
            base["match_reason"] = item.get("match_reason", "主题匹配")
            result.append(base)
        return result[:3]

    async def _rule_top_by_score(
        self,
        db,
        candidates: list[dict],
        title: str,
    ) -> list[dict]:
        """候选池用规则多维评分排序：朝代×作者×金句度 + 节令加成 + 关键词相关性，
        最后做朝代 bucket 多样性重排，返回 top-3 候选 + 占位 match_reason。

        用于以下场景：
        1. LLM 失败/无结果兜底
        2. 单条热点调试快速查看
        """
        from app.services.recommend_scoring import diverse_top_k, dynasty_bucket
        if not candidates:
            return []
        # 转 PoemLike
        poem_likes = [
            PoemLike(
                id=c["id"], title=c["title"] or "", author=c["author"] or "",
                dynasty=c["dynasty"] or "", content=c.get("content_preview", "") or "",
            )
            for c in candidates
        ]
        # 取作者权威表（缓存）；名望表优先，缺失时退回入诗数启发
        prest_table = await _get_prestige_table(db)
        fame_table = await _get_fame_table(db)
        # relevance 反馈：candidates 自身按原顺序（已按 hits 降序），所以 idx 越小相关性越高
        n = len(poem_likes)
        def rel(p: PoemLike) -> float:
            idx = next((i for i, q in enumerate(poem_likes) if q.id == p.id), n)
            return max(0.5, 1.0 - idx / max(n, 1) * 0.5)
        picked = diverse_top_k(poem_likes, title=title,
                               prest_table=prest_table, relevance_fn=rel, top_k=3,
                               fame_table=fame_table)
        # 转回 candidates 字段格式
        by_id = {c["id"]: c for c in candidates}
        result = []
        for p, sc in picked:
            base = by_id.get(p.id, {})
            item = dict(base)
            item["match_reason"] = (
                f"主题词命中：{', '.join(list(self._keywords_to_terms(title).keys())[:2])}（评分 {sc:.2f}）"
            )
            result.append(item)
        return result

    async def recommend_poems(
        self,
        db,
        hotspot: dict,
    ) -> list[dict]:
        """热点 → AI 推荐诗词（#20260903 重写：倒排 Top200 + 多样性 + LLM 精选）

        流程：
        1. 倒排表 Top-200 拉候选（池子从 20 提到 200，让多样性 bucket 有覆盖）
        2. LLM 从 200 中选 3（prompt 已恢复名家权威优先）
        3. LLM 失败/无结果 → 规则 diverse_top_k 兜底

        Args:
            db: SQLAlchemy AsyncSession
            hotspot: 单条热点 dict

        Returns:
            [{id, title, author, dynasty, content_preview, match_reason}]
        """
        title = hotspot.get("title", "")
        keywords = self._keywords_to_terms(title)
        logger.info(f"热点推荐: '{title}' → 检索词 {keywords}")

        try:
            # 1. 规则初筛 → Top-200 候选 poem_id
            candidate_ids = await self._rule_candidates(db, keywords)
            if candidate_ids:
                # 拉取候选详情
                stmt = select(Poem).where(Poem.id.in_(candidate_ids))
                result = await db.execute(stmt)
                poems = result.scalars().all()
                candidates = [
                    {
                        "id": p.id, "title": p.title, "author": p.author,
                        "dynasty": p.dynasty,
                        "content_preview": (p.content or "")[:60],
                    }
                    for p in poems
                ]
            else:
                candidates = []

            # 1.5 候选池二次过滤：非 normal 作者永不进池（设计文档 §8 硬约束）
            #     同时作用于 LLM 路径与规则兜底路径，避免伪作者被 LLM 选中
            if candidates:
                _fame = await _get_fame_table(db)
                kept = [c for c in candidates
                        if not is_pseudo_author(c.get("author"), _fame)]
                if kept:
                    candidates = kept

            # 2. LLM 精选（prompt 已恢复"作者权威"加权）
            selected = await self._llm_select(candidates, hotspot)
            if selected:
                return selected

            # 3. 兜底：LLM 失败/无结果 → diverse_top_k 按多维评分排序
            fallback = await self._rule_top_by_score(db, candidates, title)
            if fallback:
                return fallback

            # 4. 终极兜底：取 candidates 前 3（保 UI 不空）
            top3 = []
            for c in candidates[:3]:
                item = dict(c)
                item["match_reason"] = f"主题词命中：{', '.join(keywords[:2])}"
                top3.append(item)
            return top3

        except Exception as e:
            logger.error(f"推荐诗词失败: {e}")
            return []


# 全局服务实例
hotspot_service = HotspotService()
