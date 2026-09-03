# 古诗词短视频工厂 — 设计方案

> 日期: 2026-09-02 | 状态: 待评审  
> 问题 1: 固化古诗词朗读参考音色（按风格/男女）  
> 问题 2: 字幕过大根因分析与自适应字号修复

---

## 一、问题 1：固化古诗词朗读参考音色

### 1.1 背景与目标

当前 CosyVoice2 零样本克隆的参考音频 `data/ref_voice.wav` 是 edge-tts 合成的占位音频，克隆出的声音 = edge-tts 音色，**无法体现 CosyVoice2 的真实优势**。需要：

1. **调研**一批"朗读古诗词、声音好听"的朗读者/播音艺术家。
2. **按风格和性别分类**，形成可锁定的 voice_preset 目录。
3. **设计配置方案**，使系统可通过 `.env` 或 API 切换预设音色。

### 1.2 朗读者分类表

#### 男声

| 预设 ID | 风格标签 | 代表朗读者 | 签名作品 | 声音特征 | 推荐参考来源 |
|---|---|---|---|---|---|
| `male_majestic` | 浑厚大气 | **方明** | 《荡气回肠唐宋篇》《岳阳楼记》 | 中气十足，气场强大，央广播音指导 | 学习强国「美声雅韵·方明篇」(16首) |
| `male_elegant` | 儒雅洒脱 | **濮存昕** | 《将进酒》《钗头凤》 | 书卷气，收放自如，半醉半醒间自然流露 | 唐宋名篇音乐朗诵会(1999-2014巡演) |
| `male_deep` | 深沉浑厚 | **乔榛** | 《蜀道难》《长恨歌》(合作) | 深沉厚重，配音艺术家，抚慰人心 | 中外名篇经典诵读 CD |
| `male_heroic` | 雄浑激昂 | **薛飞** / **李龙吟** | 《满江红》《破阵子》 | 怒发冲冠，气吞山河，字字有情 | 唐宋名篇音乐朗诵会 |
| `male_solemn` | 苍劲有力 | **焦晃** / **徐涛** | 《短歌行》《出师表》《再别康桥》 | 苍劲深沉，话剧级演诵，气贯长虹 | 视听《名家名篇朗诵》合集 |
| `male_warm` | 温润浑厚 | **康庄** / **瞿弦和** | 《念奴娇·过洞庭》《观沧海》 | 大气沉稳，播音指导级 | 中国文学标准朗读·古诗词篇 |
| `male_news` | 新闻播报风 | **康辉** / **撒贝宁** / **王劲松** | 《念奴娇·赤壁怀古》《青玉案·元夕》 | 央视主播腔，清晰端正 | 央视《经典咏流传》 |

#### 女声

| 预设 ID | 风格标签 | 代表朗读者 | 签名作品 | 声音特征 | 推荐参考来源 |
|---|---|---|---|---|---|
| `female_gentle` | 温婉深情 | **丁建华** | 《茅屋为秋风所破歌》《长恨歌》 | 温婉深情，千余部译制片主配 | 唐宋名篇音乐朗诵会 / 中外名篇经典诵读 |
| `female_melancholy` | 哀婉凄楚 | **肖雄** | 《钗头凤》《陋室铭》 | 哀婉凄楚，情浓意蜜，空政一级演员 | 唐宋名篇音乐朗诵会 |
| `female_sweet` | 甜美清亮 | **姚锡娟** / **杜宁林** | 《唐多令》《木兰辞》 | 甜美（《血疑》幸子原声）/ 一级演员 | 唐宋名篇 / 【悦读悦动听】 |
| `female_educational` | 音韵和美 | **虹云** | 《诗经》80+首、《观沧海》《春江花月夜》 | 有筋骨有温度，少儿国学启蒙范读近百首 | 学习强国 / 小学生必背古诗词标准范读 |
| `female_fresh` | 清新雅致 | **雅坤** / **张筠英** | 《春江花月夜》《爱莲说》《观沧海》 | 清新雅致，中国文学标准朗读 | 中外名篇经典诵读 CD |
| `female_grand` | 大气磅礴 | **斯琴高娃** / **董卿** | 《祖国啊亲爱的祖国》《可爱的中国》 | 大气磅礴，舞台级演绎 | 央视朗诵会 |
| `female_child` | 童声/教学 | **婷婷姐姐** | 65+首中小学必背古诗词 | 儿童诗教，当下孩子喜爱 | 学习强国「婷婷姐姐唱诗歌」(50首) |

### 1.3 参考音频获取方案

| 来源 | 版权状态 | 可用性 | 说明 |
|---|---|---|---|
| **学习强国 APP** 「美声雅韵」「中华经典系列」「婷婷姐姐唱诗歌」 | 官方平台，免费收听 | 需 APP 内播放或下载 | 最权威；方明/虹云/婷婷姐姐等均有完整专辑。可作为个人学习参考音频（6-10s 片段用于 CosyVoice2 zero-shot 参考）。 |
| **喜马拉雅** 「诗歌中国(65首)」「听唱古诗词」 | 平台版权，需会员/付费下载 | 可在线试听 | 哈若蕙等中青年朗诵者，风格多样。 |
| **当当/实体 CD** 「中外名篇经典诵读(5CD)」 | 购买后个人使用 | 需购买 | 夏青/方明/雅坤/虹云等老一辈艺术家，音质最佳。 |
| **自录**（推荐主路径） | 完全自有 | 即时可用 | 用手机录制 6-10s 清晰人声朗读一首短诗（如《春晓》），作为 CosyVoice2 参考音频。**零版权风险，克隆效果最忠实于本人声音。** |
| **edge-tts 占位**（当前） | 无版权问题 | 已集成 | 音色机械，仅作开发/测试用，不建议作为最终产品音色。 |

### 1.4 配置方案设计

#### 1.4.1 Voice Preset JSON Schema

```json
// server/data/voice_presets.json
{
  "presets": [
    {
      "id": "male_majestic",
      "label": "方明·浑厚大气",
      "gender": "male",
      "style_tags": ["majestic", "formal", "classic"],
      "reciter": "方明",
      "engine": "cosyvoice",       // "cosyvoice" | "edge-tts"
      "ref_wav": "data/ref_voices/fangming.wav",
      "prompt_text": "春眠不觉晓，处处闻啼鸟。",
      "instruct_text": "",
      "description": "央广播音指导，中气十足，适合豪放派诗词"
    },
    {
      "id": "female_gentle",
      "label": "丁建华·温婉深情",
      "gender": "female",
      "style_tags": ["gentle", "emotional"],
      "reciter": "丁建华",
      "engine": "cosyvoice",
      "ref_wav": "data/ref_voices/dingjianhua.wav",
      "prompt_text": "床前明月光，疑是地上霜。",
      "instruct_text": "",
      "description": "上译厂配音演员，温婉深情，适合婉约派诗词"
    }
    // ... 更多预设
  ],
  "default_preset": "male_majestic"
}
```

#### 1.4.2 .env 扩展

```env
# 当前单参考音频（向后兼容）
COSYVOICE_PROMPT_WAV=data/ref_voice.wav
COSYVOICE_PROMPT_TEXT=春眠不觉晓，处处闻啼鸟。夜来风雨声，花落知多少。
COSYVOICE_INSTRUCT_TEXT=

# 新增：多预设模式（优先于单参考音频）
VOICE_PRESET_FILE=data/voice_presets.json
VOICE_PRESET_DEFAULT=male_majestic
```

#### 1.4.3 代码改造点

| 改动位置 | 内容 |
|---|---|
| `config.py` | 新增 `voice_preset_file` / `voice_preset_default` Field |
| `tts_core.py` | `generate_tts()` 支持 `preset_id` 参数 → 从 JSON 加载 ref_wav/prompt_text/instruct_text |
| `app/api/tts.py` (或 tasks API) | 新增 `POST /api/tts/preset` 列表接口 + `GET /api/tts/preset/{id}` 切换接口 |
| `server/data/ref_voices/` | 存放各预设的参考 wav 文件 |

---

## 二、问题 2：字幕过大 — 根因分析与修复设计

### 2.1 问题现象

最终生成的视频（抖音 1080×1920）字幕**占据画面 67% 高度、83–99% 宽度**，几乎填满整个屏幕，严重遮挡画面内容。

### 2.2 根因分析（已通过 ~10s 复现验证）

**根本原因：`force_style` 缺少 `PlayResX` / `PlayResY`，导致 ASS 字号坐标映射错误。**

| 项目 | 值 | 说明 |
|---|---|---|
| ASS 默认 `PlayResY` | **288** | libass/SRT 默认脚本坐标系高度 |
| 视频实际高度 H | **1920** | 抖音 9:16 |
| `force_style` 中 `FontSize` | **57** | 意图：57px（H × 0.03） |
| **实际渲染字号** | **57 × (1920/288) ≈ 380px** | 每个"FontSize 单位"被放大 6.67 倍！ |
| 47 字 cue 实际行数 | **~16 行**（每行 ~3 字） | 16 × 380px ≈ 6080px >> 1920px → 填满屏幕 |
| 底部边距 MarginV=96 | **96 × 6.67 ≈ 640px** | 同样被放大，但相对比例不变 |

**证据链**（`server/data/output/task_repro_sub/`）：
1. `frame_current_sub.png` — 当前配置渲染结果：巨形字幕填满屏幕 ✅ **复现成功**
2. `frame_playres_fix.png` — 添加 `PlayResX=1080,PlayResY=1920` 后：正常大小 ✅ **修复验证**
3. 量化对比：

| 指标 | 当前（bug） | PlayRes 修复后 | 目标 |
|---|---|---|---|
| 字幕块宽度 | 83–99% | 68–86% | ≤ 90% |
| 字幕块高度 | **67% (1288px)** | **8.5% (164px)** | ≤ 12% |
| 底部边距 | 33% | 5.2% | 3–8% |
| 单字渲染大小 | ~380px | ~55px | 40–60px |

### 2.3 修复设计方案

#### 2.3.1 必修项（P0 — 修复坐标映射）

**改动文件**: `server/app/services/pipeline.py` → `_burn_subtitles()` 方法（约第 1325 行）

**改动内容**: 在 `force_style` 构建中追加 `PlayResX={W},PlayResY={H}`：

```python
# 修复前（第 1325-1329 行）
force_style = (f"FontName={style['font']},FontSize={fs},"
               f"PrimaryColour={style['primary_colour']},"
               f"OutlineColour={style['outline_colour']},"
               f"Outline={outline},Shadow={shadow},"
               f"Alignment={style['alignment']},MarginV={mv}")

# 修复后
force_style = (f"FontName={style['font']},FontSize={fs},"
               f"PrimaryColour={style['primary_colour']},"
               f"OutlineColour={style['outline_colour']},"
               f"Outline={outline},Shadow={shadow},"
               f"Alignment={style['alignment']},MarginV={mv},"
               f"PlayResX={W},PlayResY={H}")   # ← 新增：对齐 ASS 坐标系到像素
```

**效果**: `FontSize=57` 现在精确对应 57px（而非 380px）。一行代码修复。

#### 2.3.2 增强项（P1 — 安全边距 + 自适应字号）

在 P0 修复基础上，进一步优化字幕美学：

**(a) 添加水平安全边距**

```python
ml = mr = max(30, int(W * 0.07))  # 左右边距各 7%
force_style += f",MarginL={ml},MarginR={mr}"
```

效果：长句不会贴边，视觉居中更舒适。

**(b) 自适应字号（可选，按需开启）**

当某条 cue 的文字特别长（如 >40 字），固定 `fontsize_ratio=0.03`（57px）仍可能换行较多。可引入自适应逻辑：

```python
def _adaptive_fontsize(W, H, longest_chars: int,
                       max_block_h_ratio=0.10,
                       max_width_ratio=0.86) -> int:
    """反推最大可读字号，使最长 cue 不超过画面 10% 高度 / 86% 宽度。"""
    max_block_h = max_block_h_ratio * H      # e.g., 192px
    max_w = max_width_ratio * W              # e.g., 929px
    best = 32
    for fs in range(32, 73):
        chars_per_line = max(1, int(max_w / fs))  # 中文全角≈fs px
        lines = (longest_chars + chars_per_line - 1) // chars_per_line
        if lines > 4:  # 超过 4 行则跳过
            continue
        block_h = lines * fs * 1.2             # 行高 ≈ 1.2×字号
        if block_h <= max_block_h:
            best = fs
        else:
            break
    return min(best, int(H * settings.subtitle_fontsize_ratio))  # 不超过配置值
```

调用方式（在 `_burn_subtitles` 中）：
```python
if settings.subtitle_adaptive:  # 新增 config 项，默认 False
    longest = max(len(t) for _, _, t in timeline)
    fs = _adaptive_fontsize(W, H, longest)
else:
    fs = max(56, min(120, int(H * settings.subtitle_fontsize_ratio)))
```

**(c) WrapStyle 显式设置**

```python
force_style += ",WrapStyle=2"  # 2=智能换行（默认），显式声明防意外
```

#### 2.3.3 Config 扩展

```python
# server/app/config.py 新增
subtitle_adaptive: bool = Field(default=False)     # 是否启用自适应字号
subtitle_max_block_h_ratio: float = Field(default=0.10)  # 字幕块最大高度占比
subtitle_margin_h_ratio: float = Field(default=0.07)     # 水平安全边距占比
```

### 2.4 改动清单汇总

| 优先级 | 文件 | 改动 | 行为影响 |
|---|---|---|---|
| **P0** | `pipeline.py:_burn_subtitles` | force_style 追加 `PlayResX/Y` | **修复字幕巨大 bug**（67%→8.5%） |
| P1 | `pipeline.py:_burn_subtitles` | force_style 追加 `MarginL/R`, `WrapStyle` | 安全边距，防止长句贴边 |
| P1 | `pipeline.py` | 新增 `_adaptive_fontsize()` 函数 | 长文案自动缩小字号（可选开关） |
| P1 | `config.py` | 新增 3 个 subtitle_* 配置项 | 用户可调参 |
| P2 | `subtitle.py` | `SUBTITLE_STYLES` 各 preset 的 fontsize 改为 ratio | 统一为比例制（与 pipeline 对齐） |

### 2.5 回归验证

复现脚本 `server/scripts/repro_subtitle_size.py` 可随时重跑：
- `final_current.mp4` — 当前行为（bug 复现）
- `final_playres_fix.mp4` — P0 修复后效果
- `final_proposed.mp4` — P0+P1 完整方案效果

运行命令：
```bash
cd server && .venv/Scripts/python.exe scripts/repro_subtitle_size.py
```

产物位于 `server/data/output/task_repro_sub/`。

---

## 三、实施建议

### 优先级排序

1. **立即执行 P0**（`PlayResX/Y` 一行修复）— 这是真正的 bug，一行代码解决"字幕沾满屏幕"。
2. **接着执行 P1a/b/c**（边距 + 自适应 + WrapStyle）— 提升字幕美学质量。
3. **并行推进问题 1**（voice_presets 配置）— 与字幕修复正交，可独立开发。
4. **用户操作**：录制 6-10s 自己的声音朗读一首短诗 → 替换 `ref_voice.wav` → 体验真实零样本克隆。

### 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| PlayResX/Y 导致其他平台分辨率不匹配 | 低 | 中 | `PlayResX/Y` 已使用动态 W/H（按平台推导），自动适配 |
| 自适应字号在某些极端长文案下过小 (<30px) | 低 | 低 | 下限 clamp 到 32px，且默认关闭（需手动开启） |
| 参考音频版权问题 | 中 | 高 | 主推自录路径；公开平台音频仅作个人学习参考，不嵌入发布产品 |
