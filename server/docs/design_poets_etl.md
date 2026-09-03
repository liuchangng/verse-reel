# poets 诗人本体 ETL 设计（终版 v2）

> 状态: 已确认 · 2026-09-03 · ETL 已落地 (commit bd48e9f, 60321 实体)
> 验收标准于 2026-09-03 修订: 原「S/A/B 合计 >300」废止, 改为候选池口径 + 纯度硬约束 (**§9.1**)
> **注意**: poems.dynasty 实际有 224 种细分取值 (盛唐/北宋/明末清初/清末民国初/十六国/十国/脏值),
> 非本表假设的"唐/宋/清"主朝代, 必须先归一 (ETL 已实现 → 16 主朝代, 54 项边界用例)。
> 目标: 把"作者名望/生平/影响"从 poem 行级启发式升级为 **canonical 诗人实体 + 客观计数型名望分**。
> 用户定稿要求（两轮评审）:
> 1. 必须覆盖: 祖辈家世 / 文人相轻相惜 / 文人圈层流派 / 政治人物派系影响深远 / 对社会贡献 / 当时社会 / 官位
> 2. **中立性铁律**: 所有入分信号必须是"计数型客观事实"(数出来的), 禁止"评价型主观立场"(评出来的)。
>    政治人物只记"做过什么"的客观层级, 不评"好不好"; 忠奸/变法对错/派系功过永不入分。

## 0. 问题定性 (#87 实测 poems.db 203.5万行)

| 现象 | 实测 | 影响 |
|---|---|---|
| 文章拆行 | 《黄帝问玄女兵法》12行 author=玄女(神话) genre=文 | 伪候选 |
| genre='文' | 21.0万行 / 14281作者 | 非诗词需剔除/重罚 |
| 同名异人 | 王珪跨隋末唐初/北宋/南宋; 方岳跨南宋/宋/明 | (author,dynasty) 消歧 |
| 繁简分裂 | 查愼行 5302 vs 查慎行 3(同一人) | author 归一 |
| 高产怪人 | 弘历 43290 / 赵冕镐7663(朝鲜) / 李穑6037(朝鲜) | 高产≠名望 |
| 占位作者 | 无名氏4838+佚名3256+待考1946+阙名881 | 无法归因 |
| 伪作者模式 | 僧人/道人/域外/帝王/神话等未分类 | 需 author_category |

## 1. 公开名望信号 (#86 调研)

- 唐诗三百首(蘅塘退士 311首/77家): 杜甫38 王维29 李白27 李商隐22
- 宋词三百首(朱孝臧 ~300首/88家): 周邦彦15 辛弃疾12 苏轼11 李清照10
- 王兆鹏大数据影响指数: 唐诗杜甫>李白>王维; 宋词辛弃疾>苏轼>周邦彦; 宋诗苏轼>陆游
- 部编教材: 苏轼14篇(现统编) / 李白杜甫百年26/24篇

## 2. 全因素清单（用户点名 → 客观代理信号）

| # | 用户因素 | 客观代理(纯计数/事实) | 入分 |
|---|---|---|---|
| 1 | 祖辈/家世 | 文学世家标记 family_lineage(三苏/二谢/三曹, 种子~50家族) | 5 |
| 2 | 文人相轻/相惜 | 同代唱和/赠答诗数 social_poems(title 含寄/赠/和/送+他人名) | 15 |
| 3 | 文人是一家/流派 | 流派标签 school_tags(种子), 只做标签不做分 | 0(标签) |
| 4 | 影响深远(后世) | **被后世追和/次韵/拟作数 tribute_hits**(已验证: 全库45148行致敬诗) | 30 |
| 5 | 对社会贡献 | 官职客观层级 official_rank(宰相4/部级3/州郡2/布衣1/无考0) + 重大工程 fact_tags(只记做过不评对错) | 10 |
| 6 | 名声(历史共识) | anthology_hits 三百首收录 + textbook_hits 教材 | 25 |
| 7 | 当时社会 | dynasty 朝代(客观归类, 先秦/唐/宋 正宗加权) | 10 |
| 8 | 官位 | official_rank(同#5) | 并入#5 |
| 9 | 诗词特点 | style_tags(边塞/田园/豪放... 标签不评分) | 0(标签) |

**中立性铁律**: 全库无 "XX功过/忠奸/利民" 评分类字段。政治人物只记层级与做过的事实。
王安石 = official_rank 4(宰相,客观) + fact_tags[变法/修史], 但没有任何 "变法利国+20" 之类评分。

## 3. 存储形态

- poets 表落 **poems.db 同库**(不另建 db; 历史教训 hotspot.db 0-byte)
- SQLAlchemy Poet 模型注册进 Base → create_all 自愈
- ETL 独立脚本 `server/scripts/build_poets_etl.py`, 同步 sqlite3 直连, 幂等 DELETE+INSERT, 不进服务启动

## 4. poets 表结构（终版）

```sql
CREATE TABLE IF NOT EXISTS poets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  -- 规范名+消歧键
  author TEXT NOT NULL,              -- 简体规范名(繁简归一后)
  author_variants TEXT,              -- JSON 别名 [查慎行,查愼行]
  dynasty TEXT NOT NULL,             -- 归一主朝代(行数最多朝代)
  dynasty_raw TEXT,                  -- JSON {朝代:行数}
  -- 统计特征(自 poems 聚合)
  total_rows INTEGER DEFAULT 0,
  prose_rows INTEGER DEFAULT 0,      -- genre='文' 行数
  genre_stats TEXT,                  -- JSON 体裁分布
  split_rows INTEGER DEFAULT 0,      -- 疑似长文拆分行数
  -- 用户点名因素 → 客观代理分项
  tribute_hits INTEGER DEFAULT 0,    -- 被后世追和/次韵/拟作数 (影响深远)
  social_poems INTEGER DEFAULT 0,    -- 同代唱和/赠答诗数 (文人相惜/圈子)
  official_rank INTEGER DEFAULT 0,   -- 官职层级 0-4 (宰相4/部级3/州郡2/布衣1/无考0)
  family_lineage INTEGER DEFAULT 0,  -- 文学世家标记 0/1 (父祖为诗人)
  anthology_hits INTEGER DEFAULT 0,  -- 唐诗/宋词三百首收录次数(种子)
  textbook_hits INTEGER DEFAULT 0,   -- 教材收录篇数(种子)
  -- 综合名望
  fame_score REAL DEFAULT 0,         -- 0~100 合成(见 §6)
  fame_level TEXT DEFAULT 'E',       -- S/A/B/C/D/E
  -- 标签(不评分, 供过滤/解释)
  author_category TEXT DEFAULT 'normal', -- normal/placeholder/myth/religious/foreign/emperor/prose/unknown
  school_tags TEXT,                  -- JSON 流派 [江西诗派,婉约派]
  style_tags TEXT,                   -- JSON 风格 [豪放,田园,边塞]
  life_tags TEXT,                    -- JSON 生平标签 [贬谪,隐逸,神童](中性文学母题)
  fact_tags TEXT,                    -- JSON 做过的大事(只记事实) [变法,修书,治水]
  src TEXT,                          -- 来源 seed/stat/mix
  updated_at TEXT
);
CREATE INDEX idx_poets_author ON poets(author);
CREATE INDEX idx_poets_dynasty ON poets(dynasty);
CREATE INDEX idx_poets_fame ON poets(fame_score DESC);
```

**实体键 = (author, dynasty)**: 王珪 3 朝 → 3 行; 苏轼(北宋) 1 行。
author 归一: 查愼行→查慎行(并入 variants); 用 author_traditional 反向参考。

## 5. author_category 伪作者分类（替代零散 is_placeholder）

| category | 判定 | 处理 |
|---|---|---|
| placeholder | 无名氏/佚名/待考/阙名/失名/未详 | fame ≤ 5, E档 |
| myth | 玄女/西王母/神话人物(种子+prose+远古规则) | fame ≤ 5, E档 |
| religious | 释XX/僧/道人/法师(法号作者) | 重罚(寒山等诗僧种子可豁免) |
| foreign | 朝鲜/日本/高丽/安南/域外 | 降权(对中文大众传播无意义) |
| emperor | 帝王(弘历/李煜等) | 正常评分(不特判; 李煜凭词作自然高分) |
| prose | genre='文'占比>80% 纯散文作者 | fame ≤ 5, E档 |
| normal | 以上皆非 | 正常评分 |

注: 弘历不靠 category 压, 靠 tribute_hits=0 + anthology=0 自然掉分(机制压制非价值观压制)。
李煜 tribute 高/选本高 → 自然高分, 不因"帝王"身份加分减分。

## 6. fame_score 合成公式（全计数型, 可解释确定性）

```
if category in (placeholder, myth, prose):  fame = 5 (或 0)
else:
    fame = 10                                   # 长尾底分
         + tribute_hits 分   0~30  = min(30, ln(1+tribute_hits)*4.2)  # 苏轼5317→30满
         + anthology_hits 分 0~25  = min(25, anthology_hits*6)         # 杜甫38→25满(实际25)
         + textbook_hits 分  0~15  = min(15, textbook_hits*2.5)        # 苏轼14篇→15满
         + social_poems 分   0~10  = min(10, ln(1+social)*1.8)         # 圈子活跃度
         + official_rank 分  0~10  = {4:10, 3:7, 2:4, 1:1, 0:0}
         + dynasty_bonus    0~10  = 先秦/唐/宋→10, 汉/魏→8, 元→6,
                                    明→5, 清→4, 近现代→0(白名单除外)
         + family_lineage         +5
    cap = 100
```

等级: S>=85, A>=70, B>=55, C>=40, D>=25, E<25

**红线**: tribute/anthology/textbook/social 全部是 COUNT; official/family 是客观事实编码;
**无任何一条是主观评价**。分数随"多少人认可"走, 不随"评价者立场"走。

## 7. ETL 流程（幂等, 分钟级）

1. 直连 server/data/poems.db (sqlite3)
2. 聚合: SELECT author, dynasty, genre, COUNT(*) GROUP BY author, dynasty, genre
3. 繁简归一 author: 简体化 → 与 canonical 冲突则并入 variants
4. 组装 (author, dynasty) 实体: genre_stats / prose_rows / split_rows / dynasty_raw
5. tribute_hits: 标题含 [次韵|追和|和|拟|敬和] + 目标诗人别名 且 作者非本人 → 按别名表统计
   (目标表: ALIASES 内置 **238 位名人**; 标题匹配即计数。为何不扩到全库作者名 → 见 §9.1)
6. social_poems: 该作者标题含 [寄|赠|和|送|答] 他人诗题数量(近似)
7. 查种子表: anthology/textbook/official_rank/family_lineage/category/tags → 合并
8. 算 fame_score → DELETE FROM poets → 批量 INSERT
9. 输出统计: 实体总数/等级分布/高名望 top50/查慎行合并验证/王珪拆分验证
   /**候选池纯度检查**: D 档以上实体中 author_category 必须 100% 为 normal（见 §9 验收①）

## 8. 推荐算法接入 (#88)

- **author_prestige 改读 fame**: `recommend_scoring.author_prestige(poem_count, author,
  fame, category)` 优先取 `poets.fame_score` → 权重映射（`FAME_WEIGHT_BANDS`：
  S 1.35 / A 1.25 / B 1.15 / C 1.00 / D 0.85 / E 0.65）；fame 缺失（作者不在 poets 表）
  才退回原「入诗数启发」。
  - 中立性：非 normal 的 `category`（myth/foreign/prose/…）无论 fame 多少一律 0.45。
  - 高产≠名望：弘历 43290 行（旧权重 1.25）→ fame 14（新 0.65），靠机制自然掉分。
- **候选池不加 fame 档位硬过滤**（关键修正）：实测倒排表 Top200 候选中，C 档以上
  (fame>=40) 通常仅 0~4 首；"秋日登高""离别""AI改变世界"等热点甚至为 0 首。若硬按
  C 档过滤会让这些热点无候选。故名望只通过 `author_prestige` **软加权** 影响排序
  （0.45~1.35），不删除任何候选。
- **唯一硬过滤 = 伪作者**（设计文档 §9 验收① 纯度约束）：非 normal 的 `author_category`
  永不进候选池——同时作用于 LLM 精选路径与规则兜底路径
  （`hotspot._rule_candidates` 之后 `is_pseudo_author` 二次过滤）。实测 Top200 中仅
  0~4 首受影响，且 D 档以上 100% 为 normal，过滤不损召回。
- **进程级缓存**：`hotspot._get_fame_table` 首次调用加载 poets 表 →
  `{author: (fame_score, category)}` 一次（含 `author_variants` 异体索引 + 同名异人取
  最高分），后续直返；poets 表缺失/空 → 返回 `{}` 自动退回入诗数启发。
- 画像标签（style_tags/life_tags）命中加分 → 后续迭代（本期未做）

**接入效果**（8 个真实热点 top3 平均名望）：接入前 16.5 → 接入后 23.5（+42%）；
弘历在"人工智能崛起""大雪节气"等热点中被 fame 权重挤下（非硬过滤，符合中立性铁律）。

## 9. 验收标准（2026-09-03 修订：原验收①已废止，见 §9.1）

1. poets 实体 **≥6.0万** (实测 60321, 原估 6.8万偏高)
   候选池口径（替代原"S/A/B 合计 >300"）:
   - **D 档以上 (fame_score>=25) ≥ 240 个** (实测 246)
   - **C 档以上 (fame_score>=40) ≥ 110 个** (实测 120)
   - **S/A/B 合计 ≥ 45 个** (实测 51)
   - **纯度硬约束**: D 档以上实体中 author_category 必须 **100% 为 normal**
     (实测 246/246 全部 normal —— 伪作者/神话/域外/高产低质全部被压在 E 档,
     这条比"数量达标"更能证明评分体系有效)
2. 查愼行/查慎行 合并同实体(查慎行 variants 含查愼行, 合计 5305 行);
   **王珪 2 实体** (宋 2105 + 隋 2; 原定 3 实体系按唐/北宋/南宋三朝估算,
   归一后北宋+南宋已合并为"宋")
3. 玄女 author_category=myth, fame≤5, 不出现在推荐
4. 苏轼/杜甫/辛弃疾 fame 分 top (苏轼/杜甫/白居易 实测 100/S);
   弘历因 tribute=0 自然低分 (实测 14.0/E <30) —— 机制压制, 非按 category 硬压
5. eval_recommendation.py 重跑: 当代占比<10%, top3 多为 S/A/B 档
6. 测试 test_poets_etl.py (mock 数据) 全绿, 含 §9.1 的回归保护用例

### 9.1 原验收①「S/A/B 合计 >300」废止记录

**实测偏差**: S/A/B 合计仅 **51**, 与预期的 >300 差 6 倍。

**根因**: 归因目标限定为 ALIASES 的 **238 位人工核对名人**。
60321 个实体中 **60075 个 (99.6%)** 四项信号 (tribute/social/anthology/textbook) 全零,
只拿到底分 10 与朝代加权, 天然落在 E 档。这是**归因覆盖问题, 不是公式问题**。

**已否决的方案 —— 扩展归因到全库作者名**（采样实验 1/10, 有数据支撑）:

| 指标 | 现状 (238 人别名) | 扩展后 (14496 全库作者名) |
|---|---|---|
| 有致敬实体 | 176 | 320 |
| 有社交实体 | 208 | 1563 |
| 误匹配 | 无 (人工核对) | **严重** |

扩展后 top 命中混入了非人名: `乐府` (28 次致敬)、`乐章` (29 次社交)、
`王氏`/`李氏` 泛称、截断名 `李德`。**噪音淹没收益**, 故维持 238 人种子级人工核对。

**决策 (用户 2026-09-03 拍板: 方案 E)**: 维持现状, 改验收标准。
理由: 推荐场景只需 top 100~300 的候选池, 当前 D 档以上 246 个且纯度 100% 已够用;
硬凑 300 个 B 档等于稀释名望标准, 违背"名望"的语义。

**后续若确需抬升数量**, 按优先级:
- A 扩展选本种子 (补《千家诗》《唐诗别裁集》《宋诗钞》《清诗三百首》) —— 最符合设计意图, 风险低
- B 作品存量弱信号 (total_rows 对数, 上限 6~8 分) —— 与"高产≠名望"有张力, 弘历也只 +7.5 仍 <30
- C 提高社交权重上限 (10 → 18) —— 改动最小, 但社交诗数受标题写法影响大
- D 调档位阈值 (B≥45 / C≥32) —— 零成本, 但稀释 S/A/B 语义, **不推荐**
