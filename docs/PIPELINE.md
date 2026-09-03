# 短视频生成流水线（固定架构）

> 状态：**已锁定为默认流程**。任何改动不得回退到下列已验证的架构决策。
> 核心思路：**逐镜以图定音**——每一镜单独生成旁白、单独合成片段，时长天然精准，无截断/错位。

## 1. 阶段流水线

```
热点抓取匹配 → 文案(script) → 分镜(storyboard) → 角色定妆照 → 分镜图(image)
   → 逐镜 TTS(narration_i.mp3) → 逐镜 seg_i.mp4(图+旁白) → base.mp4(镜间转场)
   → final.mp4(烧字幕 + 混 BGM + 水印)
```

入口：`server/app/services/pipeline.py`（`PipelineEngine`），支持按阶段幂等重跑（`run_stage`）。
独立重烧脚本：`scripts/burn_segments.py`（不依赖后端进程，复用已生成旁白）。

## 2. 已锁定的关键决策（不得回退）

| 决策 | 做法 | 反模式（废弃） |
|---|---|---|
| 时长精准 | 每镜独立 TTS → 独立 `seg_i.mp4`（`-shortest` 贴合旁白）→ `concat`；总时长 = Σ旁白 | 整段 TTS 倒推幻灯片 → 5 秒截断/错位 |
| Ken Burns | 放大 1.12× + `crop` 时间平移（sin/cos 漂移），内存安全 | `zoompan`：d=1 仅 1 帧致 EOF；长时缓冲全部帧 OOM |
| 镜间过渡 | `xfade` 交叉淡入淡出 0.4s（可关），视频 `xfade` + 音频 `acrossfade` | `concat -c copy` 硬切，生硬 |
| BGM | 真实古风 mp3 低音量 0.22 铺底，按文案 style 选 mood | 难听/无版权的默认曲；agnes 自带人声污染 |
| 字幕 | 古风楷体(KaiTi) 白字+**黑描边3+阴影2**，字号 **H×3%**(抖音≈58px/B站≈56px/小红书≈43px, clamp[56,120])，MarginV **H×5%**(≈96px 贴近底部) | 硬编码 26px、贴底被 UI 遮挡；或 fs=80/outline=2/mv=192 导致用户肉眼完全不可见；或 fs=H×5.5%(106px)导致字幕遮满画面 |
| 多平台 | `output_platforms` 默认 `["douyin","bili","xiaohongshu"]`，循环各分辨率分别合成 `final_{plat}.mp4` | 单平台硬编码，需手动改 platform 重跑 |
| 平台画幅 | `task.platform` 注入**最终合成**阶段（抖音 1080×1920 / B站 1920×1080 / 小红书 1080×1440） | 画幅写死 1080×1350，平台比例不生效 |
| 水印 | `config` 可开关，文字默认"古诗词解说说"，**α=0.8 / 字号 H×6% / fontfile 绝对路径(simkai.ttf)**，视频+分镜图右下角 | 无；或 α≤0.65/字号比≤0.03 导致暗背景上不可见 |
| 热点推荐 | LLM 精选以"经典名句/金句"为首要标准，作者知名度不加分 | 偏向李白/杜甫等大名家名篇 |
| 英文防污染 | TTS 文本正则去拉丁字母（KPI/corner/Agnes 等），DB 直修兜底 | 英文被读出 |

## 3. 配置项（`server/app/config.py`）

```ini
subtitle_style = "kai"        # kai=古风楷体 | yahei=安全黑体 | default
subtitle_fontsize_ratio = 0.03  # 字号占视频高度比（默认3%→抖音58px/B站56px/小红书43px，clamp[56,120]）
subtitle_margin_v_ratio = 0.05   # 字幕底部边距占高度比（默认5%→96px贴近底部）
transition_enabled = true     # 镜间交叉淡入淡出
transition_duration = 0.4     # 单段过渡秒数
transition_type = "fade"
watermark_enabled = true
watermark_text = "古诗词解说说"
watermark_fontfile = "C:/Windows/Fonts/simkai.ttf"  # drawtext fontfile 绝对路径（本机 Fontconfig 缺失）
watermark_fontsize_ratio = 0.06  # 水印字号占视频高度比（默认6%→1024图上61px）
watermark_alpha = 0.8           # 水印透明度（≥0.8 确保暗背景可见，原0.4/0.65均不可见）
watermark_margin_ratio = 0.03
watermark_position = "right_bottom"
output_platforms = ["douyin", "bili", "xiaohongshu"]  # 多平台输出列表（默认全平台）
```

## 4. 已知坑（实现时注意）

- **concat -c copy 元数据失真**：container 时长会虚报（如 197s 实为 159s）。统一以 `_build_segments` 累加的 `segs["duration"]` 为权威时长，烧录用 `-t` 截断。
- **xfade 两处必坑**：① **`-map` 标签 off-by-one**——循环结束后 `out_v` 已自增到 `v{n}`，但最后一次 xfade 实际产出是 `v{n-1}`，`-map` 必须写 `[v{n-1}]`（音频同理），否则报 `Output with label 'vN' does not exist`。② **`durs` 帧量化**——须先量化到 25fps 网格（0.04s），否则 21 段长链按精确浮点累加漂移 ~0.8s，末尾 `offset` 越界。统一在 `_build_segments`/`build_segments` 内 `durs=[round(d*25)/25]`。
- **音频转场用 concat 滤镜，不用 acrossfade**：`acrossfade` 链在本机合并滤镜图中会丢失末尾输出标签（`aN does not exist`），已实测确认弃用；改用 `[0:a][1:a]...[N:a]concat=n=N:v=0:a=1[aout]` gapless 拼接，配合视频 xfade 即可获得自然过渡。单段或无过渡时回退硬切。
- **edge-tts 偶发 "No audio received"**：并发 ≤2 + 重试 3 次 + 静音兜底（保序号不偏移）。
- **字幕/水印字体（本机实测）**：libass 走 **directwrite** 提供器（非 Fontconfig），`subtitles` 滤镜用 `FontName=KaiTi` 可正常渲染楷体（无需 fontfile）；`drawtext` 水印**必须用 `fontfile` 绝对路径**（本机 Fontconfig 缺失，字体名方式报 `Cannot load default config file`），冒号须转义 `\:`。**路径坑**：`subtitles`/`drawtext` 的 SRT/图片路径必须是真实 Windows 绝对路径（如 `E:/...`），`/tmp` 等 Git 虚拟路径 libass 无法解析 → "Error initializing filters"。
- **字幕/水印可见性诊断（像素级验证方法论）**：
  - **h264 二次重编码导致同源帧 ~72% 像素漂移（crf23）**——不能用 base-vs-final delta 法检测字幕/水印是否渲染；必须用**绝对亮度阈值法**（如底部区域 white>180 像素计数）。
  - **字幕不可见根因链**：fs 太小(手机端<30px) + MarginV 太高(推到图像内容区) + outline 太细(白字与背景融为一体) → 用户肉眼完全看不到。修复：fs=H×3%(≈58px), mv=H×5%(≈96px), outline=3, shadow=2。0.055(106px)矫枉过正致遮满画面，已回调至3%。
  - **水印不可见根因**：alpha≤0.65 在暗背景上仅产生 RGB~110 的深灰 + 字号比≤0.03(1024图上31px) → 肉眼不可见。修复：α≥0.8, 字号比≥0.06(61px)。
  - **验证标准（2026-09-01 实测通过）**：1080x1920 底部15%区亮像素(>180) >50000 = 字幕清晰可见；右下角25%区亮像素(>180) >1000 = 水印清晰可见。

## 5. 重烧 / 回滚

- 仅重烧视频（复用旁白，默认全平台）：`python scripts/burn_segments.py 1 3`
- 指定平台子集：`python scripts/burn_segments.py 1 --platforms douyin,bili`
- 加 `--reuse` 跳过已编码 seg（仅字幕/水印变更时用）。
- 任一镜想单独重生成：清空对应 `seg_i.mp4` 后重跑该阶段。
- 回滚到旧成片：备份文件 `final.bak.mp4` 可随时还原。

## 6. 多平台输出

默认 `output_platforms = ["douyin", "bili", "xiaohongshu"]`，一次运行产出三份：

| 平台 | 分辨率 | 输出文件 | 适用场景 |
|---|---|---|---|
| douyin/kuaishou | 1080×1920 (9:16) | `final.mp4` | 抖音/快手竖屏 |
| bili | 1920×1080 (16:9) | `final_bili.mp4` | B站横屏 |
| xiaohongshu | 1080×1440 (3:4) | `final_xiaohongshu.mp4` | 小红书 |

TTS/图片/SRT 全平台复用，仅 seg 编码按分辨率分别执行（存储 ×3，耗时 ×~2.5）。
