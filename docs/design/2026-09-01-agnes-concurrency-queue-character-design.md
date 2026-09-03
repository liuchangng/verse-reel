# 古诗词视频流水线：并发治理 + 任务队列 + 角色一致性 设计方案

> 版本 v1.0 ｜ 2026-09-01 ｜ 分支 `feature-poem-pipeline-closure`
> 结论先行：**只控制并发数无法解决 agnes 限流**（agnes 限的是 RPM 不是并发），必须三层治理；队列采用你提的**方式二**（文图批量并发 + 视频串行）；角色一致性采用 **LMD 工业参考表定妆照 + ArcReel 职责分离** 的混合方案。

---

## 0. TL;DR（结论先行）

| 你的问题 | 结论 | 关键依据 |
|---|---|---|
| ① agnes 全部参数 | 现行 `agnes.py` 参数与官方规范**完全一致，无需改动**；但 `text_model` 用的 `agnes-2.5-flash` 虽实测可用（200），官方 FAQ 只列 `2.0-flash`/`1.5-flash` | 实测 3 个文本模型 + 2 种图片 size 写法 |
| ② 按类型设并发 | 现有「系统设置」是**假的**：只写 localStorage，后端 `POST /api/settings` 只做 `setattr` **重启即丢**；且「🧪 测试」按钮 **100% 报错**（引用了不存在的 `settings.agnes_base_url`） | 实锤：测试返回 `success:0, failed:2` |
| ③ 限流怎么处理 | 采用**方式二**，但必须补 **RPM 令牌桶**——只设并发闸门 `image_concurrency=5`，若单图 8s 完成则实际 37.5 req/min > 官方 20 RPM，**照样 429** | 官方 RPM 表 + 并发≠RPM 推导 |
| ④ 角色一致性 | 定妆照改造为**白底四视图工业参考表**（ArcReel 布局 + LMD 润色链路），分镜 prompt **不写外貌**（职责分离），失败必须 **fail-loud** 不允许静默降级 | LMD `characterLibraryService.js` + ArcReel `prompt_builders.py` 实证 |

**一句话方案**：把「并发数」升级为「并发闸门 + RPM 令牌桶 + 自适应退避」三层限流；把「一次性 pipeline」拆成 DB 持久化的双 Lane 队列（快 Lane 批量跑文图、慢 Lane 串行跑视频）；把「随机人脸」换成「定妆照→参考图注入」的一致性链路。

---

## 1. 事实基线（全部经过实测/官方文档核实，非推测）

### 1.1 agnes 官方限流表（免费/默认 key）

来源：`AgnesAI-Labs/AgnesAI-Models` 仓库 `docs/TOKEN_PLAN_FAQ.md`（最后更新 2026-06-28），与 `wiki.agnes-ai.com/en/docs/tokenplan` 一致。

| 类型 | 用户档位 | 分辨率 | 公开请求 RPM | **实际可执行 RPM** |
|---|---|---|---:|---:|
| 文本 | 免费/默认 | — | 30 | **20** |
| 文本 | 企业认证 | — | 60 | 40 |
| 文本 | Token Plan | — | 1,000 | 1,000 |
| 图片 | 免费/默认 | 1K | 30 | **20** |
| 图片 | 免费/默认 | 2K | 20 | **10** |
| 图片 | 免费/默认 | 3K | 2 | 1 |
| 图片 | 免费/默认 | 4K | 1 | 1 |
| 图片 | Token Plan | 1K | 120 | 100 |
| **视频** | **免费/默认** | — | **2** | **1** |
| 视频 | 企业认证 | — | 2 | 2 |
| 视频 | Token Plan | — | 6 | 5 |

配套规则（原文要点）：

- **RPM 与订阅配额同时生效**。Token Plan：图片 4,000 张/天，视频 **500 秒/天**（按生成时长计），文本按请求数计。
- 限额**按 key 类型共享池**：同类型开多个 key **不提升**额度；免费 key 与 Token Plan key 是不同池，可并存。
- 超限返回 `rate_limit_exceeded`（实测表现为 **429 或 400**）。

> 我们的 key 属免费/默认档 → **文本 20 RPM、图片 1K 20 RPM、视频 1 RPM**。

### 1.2 本项目实测记录（2026-09-01，key 尾部 `...OrAN`）

| 测试项 | 请求 | 结果 |
|---|---|---|
| 文本 `agnes-2.5-flash` | `POST /chat/completions` | ✅ **200**，`content:"OK"` |
| 文本 `agnes-2.0-flash` | 同上 | ✅ **200** |
| 文本 `agnes-1.5-flash` | 同上 | ❌ **503** `model_not_found: No available channel ... under group default` |
| 图片 `size="1K" + ratio="16:9"` | `POST /images/generations` | ✅ **200**，返回 `data[0].url` |
| 图片 `size="1792x1024"` | 同上（无 `ratio`） | ✅ **200** |
| 设置页并发测试 | `GET /api/settings/test-concurrency?type=text&concurrency=2` | ❌ `success:0, failed:2`，错误 `'Settings' object has no attribute 'agnes_base_url'` |

**结论**：
1. `agnes-2.5-flash` **确实可用**（当前默认配置无误），但不在官方 FAQ 模型表中，属于"未文档化但开放"的模型，保留但记入风险。
2. 图片两种 size 写法都通：`size:"1K"+ratio`（SKILL 规范，我们现行）与 `size:"1792x1024"`（LMD 白名单写法）。**现行写法不必改**。
3. **设置页并发测试是坏的**——这是用户点得最多、却永远是 ❌ 的功能。

### 1.3 参考项目实证（两个本地项目源码）

**LocalMiniDrama**（Vue3 + Node，对 agnes 有定制分支）

| 要点 | 位置 | 内容 |
|---|---|---|
| 参考图注入 | `backend-node/src/services/imageClient.js:1524-1540` | 走 **OpenAI 兼容分支**：`extra_body:{image: resolvedRefs, response_format:'url'}`（**不是顶层 `image`**） |
| 视频入参 | `backend-node/src/services/videoClient.js:2479` | `width/height/num_frames/frame_rate`；`num_frames` 只允许 **{81,121,161,241,441}**；`frame_rate=24`；**单图走顶层 `image`（string）**，多图走 `extra_body.image`（≤10 张） |
| 视频轮询 | `videoClient.js:3855` | 10s × 300 次；completed 后额外 12 轮宽限等直链 |
| 定妆照 | `characterLibraryService.js:532` | 两步：LLM 把 `appearance` 润色成工业参考表描述（`max_tokens:4000`，存 `characters.polished_prompt`，UI 可手改）→ 生图，**size 固定 `1792x1024`** |
| 定妆照布局 | `promptI18n.js:1243` | 左 1/3 **FACE HERO CLOSE-UP** + 右 2/3 **正面/背面/侧脸/服装/材质**，**纯白底 RGB(255,255,255)** |
| 特征锚点 | `characterGenerationService.js:13` | `enrichIdentityAnchors`，`temperature:0.1`，六层：骨相/五官/辨识标记/色值/皮肤/发型 |
| 图→文 | `characterLibraryService.js:610` | vision 模型反提 `appearance`（150-250 字） |
| 参考图标签 | `imageClient.js:1430-1441` | 注入时附 `[... — FOR REFERENCE ONLY, DO NOT copy its layout or framing]` |
| 并发 | `routes/settings.js:29-30` + `FilmCreate.vue:2845` | 默认图片 3/视频 3，存 SQLite `global_settings` 表；**并发只在前端 worker 池实现** |
| ⚠️ 短板 | 全仓检索 | **无 429 退避**，失败即返回 error |

**ArcReel**（Python，React18 前端）

| 要点 | 位置 | 内容 |
|---|---|---|
| 参考图注入 | `lib/image_backends/agnes.py:107-116` | **顶层 `image` = base64 data-URI 数组**（无 `ratio`、无 `extra_body`） |
| 定妆照 prompt | `lib/prompt_builders.py:22-34,63-73` | 白底 **16:9 四格**：左 40% 胸像特写 + 右三栏 **正面/四分之三侧面/背面 A-Pose 全身**；守卫句「四个面板中角色面部、发型、服装、配饰完全一致」 |
| **职责分离** ★ | `lib/prompt_builders_reference.py:119-121` | 分镜文本**禁止写外貌服装**，只用 `@[名称]` 引用——视觉一致性**全部交给参考图** |
| fail-loud | `agnes.py:165-188` | 参考图缺失抛 `ImageCapabilityError`，**绝不静默丢图还照常计费** |
| 并发 | `lib/generation_worker.py:224-274` | 租约式槽位台账 `SlotTable`（非 Semaphore，避免 `Semaphore(0)` 死锁），`IMAGE 5 / VIDEO 3 / AUDIO 10`，可按 provider 覆盖 |
| ⚠️ 短板 | `lib/retry.py:43-48` | 退避仅 **2-4-8s**，远小于视频 60s 周期 → 视频必撞限 |

> 两个项目**都没有真正解决 RPM 限流**：LMD 无退避，ArcReel 退避时长不够。这正是我们要补的差异化能力。

---

## 2. 现状诊断（7 个已确认缺陷）

代码基线：`93ca704` 之后的 `1200c34`，工作区含未提交的 `queue.py` / `job.py` 原型。

| # | 级别 | 缺陷 | 定位 | 影响 |
|---|---|---|---|---|
| D1 | **P0** | 设置不持久化：`POST /api/settings` 仅 `setattr(settings, key, ...)` | `server/app/main.py:100-113` | 重启后并发配置全部丢失 |
| D2 | **P0** | 并发测试 100% 失败：引用不存在的 `settings.agnes_base_url` / `agnes_api_key` | `server/app/main.py:133,143,153` | 实锤 `success:0 failed:2` |
| D3 | **P0** | 测试用的视频参数是**旧协议** `mode/seconds/size/aspect_ratio`，与现行 `width/height/num_frames/frame_rate` 不符 | `main.py:155-163` | 即使修好 D2，视频测试也是错的 |
| D4 | **P0** | 只有并发闸门，**没有 RPM 整形** | `queue.py:53-75` | 并发 5 + 快速返回 → 实际 30+ req/min 撞 429 |
| D5 | **P1** | 依赖死锁：`image` 的 prereq 含 `script`，若只入队 `image/character`（`stage=image` 场景）则 `script` job 不存在 → 永远不满足 | `queue.py:34-41, 224-230` | 单阶段重生成永久卡住 |
| D6 | **P1** | 信号量在 `__init__` 快照，设置热改不生效；`_claim_and_dispatch` 每次只派发 1 个且 claimed>0 时不 sleep → **忙轮询打 DB** | `queue.py:69-75, 194-222` | CPU/DB 压力 |
| D7 | **P1** | 视频无最小间隔：sem=1 只保证"同时 1 个"，不保证"每分钟 1 个" → 必然先撞 429 再退避 65s，浪费一次配额 | `queue.py` + `agnes.py` | 视频吞吐下降 + 配额浪费 |

另：`TaskDetail.vue:128-139` 的「🎨 分镜画面」是**纯文字占位框**（`<div class="sb-img">帧 N</div>`），没有渲染真实分镜图——前端美化 P0 项。

---

## 3. 总体架构

```
                        ┌─────────────── 生产者 ───────────────┐
  创建任务 / 重新生成 ──▶ │ enqueue_task() → generation_jobs 表 │
                        └───────────────┬────────────────────┘
                                        │ DB 持久化（重启可恢复）
                        ┌───────────────▼────────────────────┐
                        │      QueueService 双 Lane 调度器      │
                        └───────┬────────────────────┬───────┘
                                │                    │
              ┌─────────────────▼──────┐  ┌──────────▼─────────────┐
              │  FAST Lane（批量并发）   │  │  SLOW Lane（串行+节流）  │
              │ script / character     │  │  video                  │
              │ image / tts / subtitle │  │  最小间隔 62s            │
              └─────────────┬──────────┘  └──────────┬─────────────┘
                            │                        │
              ┌─────────────▼────────────────────────▼─────────────┐
              │            三层限流治理（每个 Lane 独立）              │
              │  L1 并发闸门 Semaphore(N)                            │
              │  L2 RPM 令牌桶（滑动窗口，容量=官方实际执行 RPM）      │
              │  L3 自适应退避 + 熔断半开（429/400 rate_limit）       │
              └─────────────────────┬───────────────────────────────┘
                                    │
                        ┌───────────▼────────────┐
                        │  PipelineEngine.run_stage │ 幂等、可重入
                        └───────────┬────────────┘
                                    │
                        ┌───────────▼────────────┐
                        │  角色一致性链路（§6）      │
                        │  定妆照 → i2i 注入 → 分镜 │
                        └────────────────────────┘
```

---

## 4. 并发治理：为什么"只设并发数"不够（核心论点）

### 4.1 推导

agnes 限的是 **RPM（每分钟请求数）**，不是并发。设 `image_concurrency = 5`：

| 单图耗时 | 实际吞吐 | vs 官方 1K 档 20 RPM | 结果 |
|---|---|---|---|
| 30 s | 5 × 2 = **10 req/min** | 低于 20 | ✅ 安全 |
| 12 s | 5 × 5 = **25 req/min** | 高于 20 | ❌ 撞 429 |
| 8 s | 5 × 7.5 = **37.5 req/min** | 远高于 20 | ❌ 撞 429 |

单图耗时**不由我们控制**（服务端排队波动），因此**并发闸门无法给出稳定保证**。必须有 RPM 层。

### 4.2 三层限流设计

| 层 | 机制 | 作用 | 参数（免费档建议默认） |
|---|---|---|---|
| **L1 并发闸门** | `asyncio.Semaphore(N)` | 限制同时在飞请求数，防止瞬时打爆 | text 4 / image 3 / video 1 / tts 3 / subtitle 2 |
| **L2 RPM 令牌桶** | 滑动窗口计数（60s），取令牌才能发请求；窗口内计数落 DB，重启不丢 | **把吞吐硬性压到官方 RPM 以下** | text 18/min、image 18/min、video 1/min |
| **L3 自适应退避** | 撞 `rate_limit_exceeded` → 指数退避 + 抖动；连续 N 次 → 熔断，半开探测；并**自动下调该 Lane 的 RPM 上限** | 兜底 + 自愈 | 基础 2/4/8s（快 Lane）；视频 Lane 固定 62s 间隔 |

> 安全边际：RPM 上限设官方值的 **90%**（20 → 18），给时钟漂移和重试留出余量。

### 4.3 方案 trade-off

| 方案 | 实现成本 | 是否真正防 429 | 吞吐 | 参考项目做法 |
|---|---|---|---|---|
| A. 只限并发 | 低 | ❌ 否（耗时波动即失效） | 中 | LMD（3/3 前端池）、ArcReel（SlotTable） |
| B. 只靠 429 退避 | 低 | ⚠️ 部分（先撞再退，浪费配额） | 低 | ArcReel（2-4-8s，视频不适用） |
| C. 纯串行 | 最低 | ✅ 是 | **最低**（你的方式一） | — |
| **D. 并发闸门 + RPM 桶 + 自适应退避** ★ | 中 | ✅ **是（提前节流，不撞）** | **高** | 本方案 |

**结论：选 D。** 关键差异是"主动节流"而非"撞了再退"——不浪费失败配额，也不污染限流池。

### 4.4 系统设置：让并发配置真正生效（对应你的问题 2）

新增 `system_settings` 表持久化，取代内存 `setattr`：

| key | 默认值 | 说明 |
|---|---|---|
| `text_concurrency` / `text_rpm` | 4 / 18 | 文本（文案+润色+critic 打分共用池） |
| `image_concurrency` / `image_rpm` / `image_size` | 3 / 18 / `1K` | 图片（含定妆照） |
| `video_concurrency` / `video_rpm` / `video_min_interval` | 1 / 1 / 62 | 视频（三参数冗余保险） |
| `tts_concurrency` / `subtitle_concurrency` | 3 / 2 | 本地资源，无 agnes 限流 |
| `rate_limit_auto_learn` | true | 撞限后自动下调 RPM 上限 |

**热更新**：信号量与令牌桶容量从 DB 读，设置保存后**下一个调度周期即生效**，无需重启。

设置页同步修复 D2/D3：字段改名 `text_base_url` 等真实字段；视频测试改用 `width/height/num_frames/frame_rate`；图片测试用 `size:"1K"+ratio`。

---

## 5. 队列设计：方式二（文图批量 + 视频串行）

### 5.1 为什么是方式二

| 方式 | 10 个任务耗时（估算） | 说明 |
|---|---|---|
| 方式一：整任务串行 | ≈ **42 min** | 每任务 ≈250s（script 15 + character 30 + 6图 2批×40 + tts 20 + video 90 + subtitle 15） |
| 方式二：文图批量 + 视频串行 | ≈ **12~15 min** | 文图受 RPM 桶约束约 600s；视频 10 × 60s = 600s；两者**并行推进**，取 max 而非 sum |

> 估算基于单图 ~25s、单视频 ~90s（含轮询）。实际以压测为准，但**方式二的量级优势确定**：它把串行瓶颈（视频）从关键路径上挪走了。

### 5.2 阶段拆分与依赖

```
script ──┬──▶ character ──▶ image ──┬──▶ video ──▶ subtitle
         └──────────────────────────┴──▶ tts ──────▶┘
```

| 阶段 | Lane | 依赖 | 幂等判据（进入先查，有则跳过） |
|---|---|---|---|
| `script` | FAST | — | `task.script` 非空且 `script_score` 已评 |
| `character` | FAST | script | `task.character_ref` 非空 |
| `image` | FAST | script, character | `len(image_urls) == len(storyboard)` |
| `tts` | FAST | script | `task.audio_url` 非空 |
| `video` | SLOW | image | `task.video_url` 非空 |
| `subtitle` | FAST | video, tts | `task.subtitle_url` 非空 |

**解决 D5 死锁**：依赖判定改为**查产物而非查 Job 状态**——`stage=image` 单独重生成时，`script` 无 Job 但 `task.script` 已存在 → 判定满足。

### 5.3 Job 表结构（`generation_jobs`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | PK | — |
| `task_id` | FK → tasks | 关联业务任务 |
| `stage` | str | script/character/image/tts/video/subtitle |
| `status` | str | `pending` / `running` / `done` / `failed` |
| `priority` | int | 快 Lane 高、video 低 |
| `attempts` | int | 已重试次数 |
| `depends_on` | str(JSON) | 前置阶段列表 |
| `lease_until` | datetime | 租约到期（替代 heartbeat，超时可被重新认领） |
| `last_error` | text | 最近错误 |
| `started_at` / `finished_at` | datetime | 计时与统计 |

### 5.4 调度算法（伪码）

```python
async def schedule_loop():
    while not stop:
        dispatched = 0
        # 1) 快 Lane：批量派发，允许多任务交错（方式二核心）
        for job in claim_fast_jobs(limit=BATCH):      # 按 priority DESC, created_at ASC
            if lane_full("fast"): break               # L1 并发闸门
            if not await rate_limiter.acquire("image"): break   # L2 RPM 桶
            if not deps_satisfied_by_artifacts(job):  continue  # 查产物，非 Job 状态
            dispatch(job); dispatched += 1

        # 2) 慢 Lane：串行 + 最小间隔（不等撞限，主动节流）
        if not lane_busy("video") and video_interval_elapsed(62):
            job = next_video_job()
            if job and deps_satisfied_by_artifacts(job):
                dispatch(job); dispatched += 1

        await sleep(POLL if dispatched == 0 else 0.2)   # 解决 D6 忙轮询
```

**关键点**：
- 快 Lane 一次派发一批，**跨任务交错**（任务 A 的 6 张图还没跑完，任务 B 的 script 就可以开始）→ 这正是你描述的"文本图片放一批生成，完了生成其他任务的"。
- 慢 Lane 独立，**由 62s 间隔主动节流**（而非 sem=1 后再撞 429 退避）→ 解决 D7。
- 两 Lane 并行推进，视频不再阻塞文图。

### 5.5 重启恢复

1. 启动时 `recover()`：`status='running'` 且 `lease_until < now()` → 复位 `pending`，`attempts` 不惩罚（进程崩溃不算失败）。
2. 每个阶段**幂等**：进入先查产物，已完成的直接标记 `done` 跳过 → 重跑不重复烧配额。
3. **租约续期**：长任务（视频轮询）定期续 `lease_until`，防止被误判为孤儿。
4. 进度通过 WS 推送（现有 `ws.py`），前端无需轮询。

---

## 6. 角色一致性设计（对应你的问题 4）

### 6.1 三种做法对比

| 做法 | LMD | ArcReel | 本项目现状 | 本方案 |
|---|---|---|---|---|
| 定妆照形态 | 工业参考表（左脸特写+右多栏），`1792x1024` | 白底四视图 16:9（胸像+正/3-4侧/背） | 单人半身 t2i | **白底四视图 16:9**（融合两者） |
| 外貌来源 | LLM 润色 `appearance` → `polished_prompt`，UI 可改 | `description` 三段（穿着/相貌/气质） | 正则匹配诗人 | **LLM 润色 + 落库 `character_description`，UI 可编辑** |
| 注入方式 | `extra_body.image`（URL 数组） | 顶层 `image`（base64 数组） | `extra_body.image`（已实现） | **沿用 `extra_body.image`**（实测 200 + 人物一致） |
| 分镜 prompt | 带 `[FOR REFERENCE ONLY]` 标签 | **禁止写外貌**（职责分离） | 只写场景，**已近似** | **明确禁止写外貌，只写场景/动作/构图/光影** |
| 特征锚点 | `identity_anchors` 六层，temp 0.1 | 无 | 无 | **增加六层锚点**（骨相/五官/辨识标记/色值/皮肤/发型） |
| 失败处理 | 降级取历史面板 | **fail-loud 抛错** | 静默降级 → 随机人脸 | **fail-loud**（杜绝"6 张各不相同"复发） |

### 6.2 定妆照 Prompt 模板（融合版）

```
【画风·最高优先级】四格统一：{style_zh}
MANDATORY ART STYLE (all 4 panels): {style_en}

Industrial character reference sheet — image only, no text reply.
横版 16:9 四格布局，纯白 (#FFFFFF) 背景：
- 左侧约 40% 宽：FACE HERO CLOSE-UP（清晰展示面部、发型、配饰、上装）
- 右侧三个等宽面板：正面 / 四分之三侧面 / 背面 A-Pose 全身视图

---
{character_description}

一致性守卫：四个面板中角色面部、发型、服装、配饰完全一致；
五官对称、手指完整为五指、肢体比例协调。
GENDER: {gender} only — do not feminize/masculinize.

画面避免：水印、多余文字、低分辨率、手指畸形。
```

`character_description` 由 **LLM 以 `temperature=0.1` 从诗词 + 风格润色**生成，六层结构：

```
骨相：…  五官：…  辨识标记：…  主色值：#XXXXXX  皮肤：…  发型/服饰：…
```

### 6.3 分镜图注入（职责分离）

```python
# 分镜 prompt 只写场景/动作/构图/光影，外貌完全交给参考图
prompt = f"{image_prefix}{scene_desc}，{action}，{composition}，{lighting}，电影级构图，高质量"
# 参考图注入（agnes 图生图链路，实测 200）
url = await agnes_client.generate_image(
    prompt,
    size="1K", ratio=aspect_ratio,
    reference_image=task.character_ref,     # extra_body.image=[定妆照]
)
```

并附 LMD 的防污染标签（写入 prompt 尾部）：

```
[参考图仅用于保持人物一致 — FOR REFERENCE ONLY, DO NOT copy its layout or framing]
```

**fail-loud 规则**：`character_ref` 为空或注入失败 → **该任务直接 failed** 并提示"定妆照生成失败"，绝不退回无参考图的随机生成。

### 6.4 已验证效果

上一轮 task 9 重跑：定妆照（t2i）+ 5 张分镜（全 i2i）→ **逐张视觉比对，5 张含人物分镜面部完全一致**。本方案在保持该链路的基础上，把定妆照升级为四视图参考表 + 六层锚点，一致性上限更高。

---

## 7. 前端美化设计

### 7.1 改造清单（按投入产出比排序）

| 优先级 | 改造 | 现状痛点 | 参考 |
|---|---|---|---|
| **P0** | 分镜画面渲染真实图 | `TaskDetail.vue:128-139` 是纯文字占位框 | LMD 分镜行 |
| **P0** | 队列可视化面板 | 无队列状态可见 | ArcReel `TaskHud` 三通道分区 |
| **P0** | 系统设置真正生效 | D1/D2/D3 | LMD `global_settings` |
| **P1** | 分镜改竖向时间线 | 纯网格不符合视频叙事 | LMD 竖向分镜行 |
| **P1** | 低分兜底图标记 | 兜底通过的图无标识 | ArcReel `StatusBadge` |
| **P1** | 定妆照卡片增强 | 只有一张图+说明 | LMD 状态标签 + ArcReel `RefChip` |
| P2 | 版本时光机 | 多次 regen 只能看最新 | ArcReel `VersionTimeMachine` |
| P2 | 提示词字幕覆盖层 | 看不到每张图的 prompt | LMD `.sb-main-img-prompt` |

### 7.2 P0-1：分镜竖向时间线（替换纯文字占位）

```
┌─ 帧 1 ────────────────────────────────────────────┐
│ ┌──────────┐  🎬 00:00-00:05                       │
│ │          │  水墨文人剪影，远山淡影，留白构图        │
│ │  分镜图   │  ⚠️ 评分 5.05（兜底保留）  [重新生成]   │
│ │  (i2i)   │  🎭 已注入定妆照                       │
│ └──────────┘                                       │
└────────────────────────────────────────────────────┘
```

每行 = 一个 shot = 一段时间段；左图右信息；低分图黄色角标；hover 显示完整 prompt（LMD `.sb-main-img-prompt` 覆盖层）。

### 7.3 P0-2：队列可视化面板（新增）

```
生成队列                                    [运行中]
┌─────────────┬────────┬──────────┬─────────────┐
│ FAST Lane   │  进行中 │ RPM 12/18│ ▓▓▓▓▓▓░░░░ │
│ SLOW Lane   │  排队 3 │ RPM  1/1 │ ▓▓▓▓▓▓▓▓▓▓ │
└─────────────┴────────┴──────────┴─────────────┘
#12 《静夜思》  video    排队中（预计 3 分 20 秒后开始）
#13 《春晓》    image    4/6 张
#9  《水调歌头》 subtitle 进行中
```

数据来源新增 `GET /api/queue/status`（返回 lane 占用、RPM 用量、等待队列）。

### 7.4 P0-3：系统设置页改造

每个模型卡片增加 **RPM 上限** 输入（并发数旁边的第二参数），并区分"并发（同时在飞）"与"RPM（每分钟上限）"两个概念：

```
并发数   [− 3 +]   同时在飞请求上限
RPM 上限 [− 18 +]  每分钟请求上限（官方 1K 档 20，留 10% 余量）
[🧪 测试]  ← 修好后真实探测并给出建议值
```

### 7.5 设计语言

沿用现有古风主题（`--color-primary` 褐色系、宣纸底纹），新增组件复用 `section-card` / `status-pill` 现有 class，不引入新 UI 库。

---

## 8. 实施计划（每阶段独立可验证）

| 阶段 | 内容 | 验收标准 | 依赖 |
|---|---|---|---|
| **S1** 配置持久化 | `system_settings` 表 + 设置 API 落库 + 修 D2/D3 测试接口 | 改并发 → 重启 → 值仍在；🧪 测试返回真实 `success:N` | — |
| **S2** 限流内核 | 三层限流（`rate_limiter.py`）：Semaphore + 滑动窗口 RPM 桶 + 自适应退避 | 压测 20 张图 **0 次 429** | S1 |
| **S3** 队列重构 | 双 Lane 调度器 + 依赖改查产物（修 D5）+ 租约（修 D6/D7）+ `run_stage` 幂等 | 10 个任务入队，文图交叉、视频串行 62s，kill 进程后重启自动续跑 | S2 |
| **S4** 角色一致性 | 四视图定妆照 prompt + 六层锚点 + fail-loud + `character_description` 落库 | 6 张图人物一致；定妆照失败时任务 failed 而非静默降级 | — |
| **S5** 前端美化 | 分镜真实图 + 队列面板 + 设置页 RPM 项 | 页面可见队列状态与真实分镜图 | S1,S3 |
| **S6** 压测调优 | 20 任务批量压测，标定各 Lane 最优并发/RPM | 无 429，吞吐达标 | S1-S5 |

> S2 与 S4 无依赖，可并行。建议顺序：S1 → (S2 ∥ S4) → S3 → S5 → S6。

---

## 9. 风险与兜底

| 风险 | 影响 | 兜底 |
|---|---|---|
| `agnes-2.5-flash` 未文档化，随时可能下线 | 文案/打分全挂 | 已实测 `agnes-2.0-flash` 可用（200），配置里保留备选，一键切换 |
| 视频 500 秒/天配额（Token Plan 档） | 批量生成中断 | 队列记录当日已用秒数，接近阈值告警并暂停 video lane |
| 免费档 RPM 突然下调 | 429 激增 | L3 自适应退避自动下调 RPM 上限并告警 |
| 定妆照生成失败 | 整任务阻塞（fail-loud 的代价） | 支持**手动上传定妆照**兜底（参考 ArcReel `CharacterCard` 上传入口） |
| 单图耗时波动导致估算偏差 | 排队时间预估不准 | 前端展示"预计等待"改为区间值，并按实测耗时滚动修正 |

---

## 附录 A：实测命令记录

```bash
# 文本模型探测
curl -X POST https://api.agnes-ai.cn/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"agnes-2.5-flash","messages":[{"role":"user","content":"只回复OK"}],"max_tokens":5}'
# → 200 {"content":"OK"}                                    ✅
# → agnes-2.0-flash 200 ✅ ／ agnes-1.5-flash 503 model_not_found ❌

# 图片两种 size 写法
curl -X POST https://api.agnes-ai.cn/v1/images/generations ... \
  -d '{"model":"agnes-image-2.1-flash","prompt":"...","size":"1K","ratio":"16:9","n":1,"extra_body":{"response_format":"url"}}'
# → 200 ✅（现行写法）
curl ... -d '{"model":"agnes-image-2.1-flash","prompt":"...","size":"1792x1024","n":1,"extra_body":{"response_format":"url"}}'
# → 200 ✅（LMD 白名单写法）

# 设置页并发测试（缺陷实锤）
curl "http://127.0.0.1:8000/api/settings/test-concurrency?type=text&concurrency=2"
# → {"success":0,"failed":2,"details":[{"error":"'Settings' object has no attribute 'agnes_base_url'"}]}
```

## 附录 B：参考资料

| 来源 | 链接/路径 | 用途 |
|---|---|---|
| agnes 官方限流表 | `github.com/AgnesAI-Labs/AgnesAI-Models` → `docs/TOKEN_PLAN_FAQ.md` | RPM/配额权威数据（2026-06-28 更新） |
| agnes Wiki 同表 | `wiki.agnes-ai.com/en/docs/tokenplan` | 交叉验证 |
| agnes 接口规范 | `E:\workspace\skill\spec-superflow-enhanced\skills\agnes-media-generator\SKILL.md` | 图片/视频参数规范 |
| LocalMiniDrama | `backend-node/src/services/imageClient.js`、`characterLibraryService.js`、`videoClient.js` | agnes 定制实现、定妆照流程 |
| ArcReel | `lib/prompt_builders.py`、`lib/image_backends/agnes.py`、`lib/generation_worker.py` | 四视图 prompt、职责分离、槽位台账 |
