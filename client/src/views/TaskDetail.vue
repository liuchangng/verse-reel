<template>
  <div class="task-detail">
    <!-- 页面头部 -->
    <div class="page-header">
      <div>
        <div class="decorative-line"></div>
        <h1 class="page-title">🎥 《{{ task.poem_title || '加载中...' }}》- {{ task.poem_author || '' }}</h1>
        <div class="header-meta">
          任务ID: #{{ String(task.id || 0).padStart(3, '0') }} · 创建于 {{ formatTime(task.created_at) }}
          <span v-if="task.dynasty" class="dynasty-tag">{{ task.dynasty }}</span>
        </div>
      </div>
      <div class="btn-group">
        <button class="btn btn-secondary" @click="$router.push('/tasks')">← 返回列表</button>
        <button v-if="task.status === 'done'" class="btn btn-secondary" @click="openPublish">📤 发布</button>
        <button v-if="task.status === 'failed'" class="btn btn-primary" @click="regenerateTask">🔄 重新生成</button>
        <button class="btn btn-ghost" :disabled="!task.video_url" @click="exportVideo">📥 导出视频</button>
      </div>
    </div>

    <!-- 失败告警（最醒目） -->
    <div class="error-banner" v-if="task.status === 'failed' && task.error_message">
      <div class="error-icon">⚠️</div>
      <div class="error-body">
        <div class="error-title">任务执行失败</div>
        <div class="error-msg">{{ task.error_message }}</div>
      </div>
      <button class="btn btn-sm btn-primary" @click="regenerateTask">重新生成</button>
    </div>

    <!-- 审核操作区（仅 pending_review） -->
    <div class="section-card review-card" v-if="reviewable">
      <div class="section-header">
        <div class="section-title">🧐 人工审核</div>
        <span class="review-hint">流水线已完成，请确认产物后再发布</span>
      </div>
      <div class="review-actions">
        <button class="btn btn-success" @click="approveTask">✅ 通过并发布</button>
        <button class="btn btn-danger" @click="showRejectInput = !showRejectInput">✗ 驳回</button>
        <button class="btn btn-secondary" @click="regenerateTask">🔄 重新生成</button>
      </div>
      <div class="reject-input" v-if="showRejectInput">
        <textarea v-model="rejectComment" placeholder="请输入驳回意见..." rows="3"></textarea>
        <div class="reject-actions">
          <button class="btn btn-secondary" @click="showRejectInput = false">取消</button>
          <button class="btn btn-danger" @click="rejectTask">确认驳回</button>
        </div>
      </div>
    </div>

    <!-- 进度步骤条 -->
    <div class="steps-bar">
      <template v-for="(s, i) in steps" :key="s.key">
        <div :class="['step-item', s.state]">
          <div class="step-node">
            <span v-if="s.state === 'done'">✓</span>
            <span v-else-if="s.state === 'active' || s.state === 'error'">{{ s.num }}</span>
            <span v-else>{{ s.num }}</span>
          </div>
          <div class="step-name">{{ s.label }}</div>
        </div>
        <!-- 连接线：flex 自适应宽度，颜色跟随当前步状态 -->
        <div v-if="i < steps.length - 1" :class="['step-connector', s.state]"></div>
      </template>
    </div>

    <!-- 状态总览卡片 -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">📊 状态总览</div>
        <span :class="['status-pill', statusPillClass(task.status)]">
          {{ statusPillLabel(task.status) }}
        </span>
      </div>
      <div class="status-grid">
        <div class="status-cell">
          <span class="cell-label">文案</span>
          <span :class="['cell-value', cellClass(derivedScriptStatus)]">{{ subStatusLabel(derivedScriptStatus) }}</span>
        </div>
        <div class="status-cell">
          <span class="cell-label">图片</span>
          <span :class="['cell-value', cellClass(derivedImageStatus)]">{{ subStatusLabel(derivedImageStatus) }}</span>
        </div>
        <div class="status-cell">
          <span class="cell-label">视频</span>
          <span :class="['cell-value', cellClass(derivedVideoStatus)]">{{ subStatusLabel(derivedVideoStatus) }}</span>
        </div>
        <div class="status-cell">
          <span class="cell-label">当前阶段</span>
          <span class="cell-value cell-stage">{{ getStageLabel(task.current_stage) }}</span>
        </div>
      </div>
    </div>

    <!-- 原诗 -->
    <div class="section-card poem-origin" v-if="task.content">
      <div class="section-header">
        <div class="section-title">📜 原诗</div>
        <span class="origin-meta">{{ task.poem_author }} · {{ task.dynasty }}</span>
      </div>
      <pre class="poem-content">{{ task.content }}</pre>
    </div>

    <!-- 文案脚本 -->
    <div class="section-card" v-if="task.script">
      <div class="section-header">
        <div class="section-title">📝 文案脚本</div>
        <span class="score-tag" v-if="task.script_score">评分 {{ task.script_score }}/10</span>
      </div>
      <pre class="script-block">{{ task.script }}</pre>
    </div>

    <!-- 文案生成中骨架屏 -->
    <div class="section-card" v-else-if="isProcessing && !task.error_message">
      <div class="section-header">
        <div class="section-title">📝 文案脚本</div>
        <span class="gen-badge"><i></i> 生成中...</span>
      </div>
      <div class="skeleton">
        <div class="sk-line" style="width:80%"></div>
        <div class="sk-line" style="width:60%"></div>
        <div class="sk-line" style="width:90%"></div>
        <div class="sk-line" style="width:45%"></div>
      </div>
    </div>

    <!-- 分镜画面（每个分镜对应一张 AI 图片；响应式网格：桌面 3/行、平板 2/行、手机 1/行） -->
    <div
      class="section-card"
      v-if="task.storyboard && task.storyboard.length > 0 && task.image_urls && task.image_urls.length > 0"
    >
      <div class="section-header">
        <div class="section-title">🎨 分镜画面</div>
        <div class="sb-meta">
          <span class="score-tag" v-if="task.image_score">评分 {{ task.image_score }}/10</span>
          <span class="img-count">{{ Math.min(task.storyboard.length, task.image_urls.length) }} 张</span>
          <button
            class="btn-text"
            @click="regenerateOneShot(0)"
            :disabled="regenBusy >= 0"
            :title="`保留文案与定妆照，重跑全部 ${task.image_urls.length} 张分镜图`"
          >{{ regenBusy >= 0 ? '⏳ 提交中…' : '🔁 重生成整组' }}</button>
        </div>
      </div>
      <div class="storyboard-grid">
        <div
          class="sb-card"
          v-for="idx in Math.min(task.storyboard.length, task.image_urls.length)"
          :key="idx - 1"
          @click="previewImage(withOutputToken(task.image_urls[idx - 1]))"
        >
          <div class="sb-card-imgwrap">
            <img class="sb-card-img" :src="withOutputToken(task.image_urls[idx-1])" :alt="`分镜 ${idx}`" loading="lazy" />
            <span class="sb-card-num">{{ idx }}</span>
            <span class="sb-card-cam" v-if="task.storyboard[idx-1] && task.storyboard[idx-1].camera">🎥 {{ task.storyboard[idx-1].camera }}</span>
            <div class="sb-card-prompt">{{ (task.storyboard[idx-1] && task.storyboard[idx-1].description) || '...' }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 角色定妆照（一致性参考图） -->
    <div class="section-card" v-if="task.character_ref">
      <div class="section-header">
        <div class="section-title">🎭 角色定妆照</div>
        <span class="anchor-badge"><i></i> 角色锚点已锁定</span>
      </div>
      <div class="character-ref-wrap">
        <img class="character-ref-img" :src="withOutputToken(task.character_ref)" alt="角色定妆照" loading="lazy" @click="previewImage(withOutputToken(task.character_ref))" />
        <div class="character-ref-side">
          <div class="character-ref-note">6 张分镜图均以此图为 i2i 参考、并以下方特征锚点注入提示词，保证同一人物跨帧一致（参考 LocalMiniDrama / ArcReel 定妆照 + 参考图注入做法）。</div>
          <div class="anchor-anchors" v-if="task.character_description">
            <div class="anchor-label">🔒 一致性特征锚点</div>
            <div class="anchor-text">{{ task.character_description }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- AI 生成图片画廊 -->
    <div class="section-card" v-if="task.image_urls && task.image_urls.length > 0">
      <div class="section-header">
        <div class="section-title">🖼️ AI 生成图片</div>
        <span class="img-count">{{ task.image_urls.length }} 张</span>
      </div>
      <div class="image-gallery">
        <div
          class="gallery-item"
          v-for="(url, idx) in task.image_urls"
          :key="idx"
          @click="previewImage(withOutputToken(url))"
        >
          <img :src="withOutputToken(url)" :alt="`生成图片 ${idx+1}`" loading="lazy" />
          <div class="gallery-caption">
            <span>#{{ idx + 1 }}</span>
            <span v-if="task.storyboard && task.storyboard[idx]">{{ task.storyboard[idx].description }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 图片生成中骨架屏 -->
    <div class="section-card" v-else-if="isProcessing && derivedImageStatus === 'processing'">
      <div class="section-header">
        <div class="section-title">🖼️ AI 生成图片</div>
        <span class="gen-badge"><i></i> 生成中...</span>
      </div>
      <div class="image-skeleton-grid">
        <div class="img-sk" v-for="n in 4" :key="n"></div>
      </div>
    </div>

    <!-- 视频预览（多平台：按比例分组，相同画幅共用一个卡片，标题用平台名组合如"抖音/快手"） -->
    <div class="section-card" v-if="hasVideos">
      <div class="section-header">
        <div class="section-title">🎬 视频预览</div>
        <span class="video-meta">{{ videoGroupsByRatio.length }} 个不同比例成片 · {{ task.video_duration || '?' }}秒</span>
      </div>
      <div class="video-grid">
        <div class="video-card" v-for="g in videoGroupsByRatio" :key="g.ratio">
          <div class="video-card-head">
            <span class="plat-badge">{{ g.label }}</span>
            <span class="plat-ratio">{{ g.ratio }}</span>
          </div>
          <video :src="g.url" controls style="width:100%;max-height:340px;background:#000;"></video>
        </div>
      </div>
    </div>

    <!-- 视频生成中骨架屏 -->
    <div class="section-card" v-else-if="isProcessing && derivedVideoStatus === 'processing'">
      <div class="section-header">
        <div class="section-title">🎬 视频预览</div>
        <span class="gen-badge"><i></i> 生成中...</span>
      </div>
      <div class="video-skeleton">
        <div class="vid-sk-placeholder"></div>
      </div>
    </div>

    <!-- 处理中占位 -->
    <div class="section-card processing-card" v-if="isProcessing && !task.script && !task.video_url && !task.error_message">
      <div class="proc-center">
        <div class="proc-spin"></div>
        <div>
          <div class="proc-title">任务处理中</div>
          <div class="proc-sub">{{ getStageLabel(task.current_stage) }}...</div>
        </div>
      </div>
    </div>

    <!-- 发布弹窗 -->
    <div class="modal-overlay" v-if="showPublishModal" @click.self="showPublishModal = false">
      <div class="modal-content">
        <div class="modal-header"><h3>发布到平台</h3><button class="modal-close" @click="showPublishModal = false">×</button></div>
        <div class="modal-body">
          <label>选择平台：</label>
          <div class="platform-options">
            <label v-for="p in publishPlatforms" :key="p.id" class="platform-option">
              <input type="radio" v-model="publishPlatform" :value="p.id" />{{ p.icon }} {{ p.name }}
            </label>
          </div>
          <p class="publish-warning">⚠️ 发布后视频将公开展示，此操作不可撤销，请确认产物与内容无误。</p>
        </div>
        <div class="modal-footer">
          <button class="btn btn-secondary" @click="showPublishModal = false">取消</button>
          <button class="btn btn-primary" @click="confirmPublish">确认发布（不可撤销）</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api, { withOutputToken } from '../api'

const route = useRoute()
const router = useRouter()
const taskId = ref(route.params.id)
const task = ref({})
let pollTimer = null
let wsConn = null  // WebSocket 连接实例

// 步骤定义
const stepDefs = [
  { key: 'script', label: '文案生成', num: 1 },
  { key: 'review', label: '文案审核', num: 2 },
  { key: 'image', label: '图片生成', num: 3 },
  { key: 'video', label: '视频生成', num: 4 },
  { key: 'done', label: '完成', num: 5 },
]

// 计算每步状态：基于实际数据存在性判断（不再依赖 current_stage 截断）
const steps = computed(() => {
  const t = task.value
  const isFailed = t.status === 'failed'
  const isDone = t.status === 'done'
  const isProcessing = t.status === 'processing'
  const isReview = t.status === 'pending_review'

  // 数据驱动的完成判断
  const scriptDone = !!(t.script && t.script_score)
  const imageDone = !!(t.image_urls && t.image_urls.length > 0) || !!(t.image_score != null && t.image_score > 0)
  const videoDone = !!t.video_url

  return stepDefs.map((s, i) => {
    let state = ''
    if (isDone) {
      // 已完成：全绿
      state = 'done'
    } else if (isFailed) {
      // 失败：按数据判断已完成/当前失败/后续空
      if (s.key === 'script' && scriptDone) state = 'done'
      else if (s.key === 'review' && scriptDone) state = 'done'
      else if (s.key === 'image' && imageDone) state = 'done'
      else if (s.key === 'video' && videoDone) state = 'done'
      else if (!state) { /* 第一个未完成的标记 error */ }
      // 找到第一个未完成的位置
      const prevDone = stepDefs.slice(0, i).every((prev, pi) => {
        if (prev.key === 'script') return scriptDone
        if (prev.key === 'review') return scriptDone
        if (prev.key === 'image') return imageDone
        if (prev.key === 'video') return videoDone
        return true
      })
      if (!prevDone && !state) state = 'error'
    } else if (isProcessing || isReview) {
      // 处理中 / 待审核：按流水线顺序 + 数据判断
      if (s.key === 'script') state = scriptDone ? 'done' : 'active'
      else if (s.key === 'review') state = scriptDone ? 'done' : (scriptDone ? '' : 'active')
      else if (s.key === 'image') state = imageDone ? 'done' : (scriptDone ? 'active' : '')
      else if (s.key === 'video') state = videoDone ? 'done' : (imageDone ? 'active' : '')
      else if (s.key === 'done') state = videoDone ? 'done' : (isReview ? 'active' : '')
    }
    return { ...s, state }
  })
})

// 辅助状态判断
const isProcessing = computed(() => task.value.status === 'processing')
const reviewable = computed(() => task.value.status === 'pending_review' && task.value.reviewable)

// ====== 显示标签 ======
const stageLabels = {
  hotspot: '热点抓取', script: '文案生成', review: '文案审核',
  storyboard: '分镜生成', image: '图片生成', video: '视频生成',
  tts: '配音生成', subtitle: '字幕烧录', done: '已完成',
}
const getStageLabel = (s) => stageLabels[s] || s || '-'

const statusPillLabel = (s) => ({ processing: '进行中', pending_review: '待审核', done: '已完成', failed: '已失败' }[s] || s)
const statusPillClass = (s) => ({ processing: 'pill-running', pending_review: 'pill-review', done: 'pill-done', failed: 'pill-error' }[s] || '')
const subStatusLabel = (s) => ({ done: '已完成', processing: '进行中', pending: '未开始' }[s] || '-')
const cellClass = (s) => ({ done: 'cell-done', processing: 'cell-running', pending: 'cell-idle' }[s] || '')

// 数据驱动的子状态判断（覆盖后端可能不准确的推算）
const derivedScriptStatus = computed(() => {
  const t = task.value
  if (t.script && t.script_score) return 'done'
  if (t.status === 'processing' && t.current_stage === 'script') return 'processing'
  return 'pending'
})
const derivedImageStatus = computed(() => {
  const t = task.value
  if ((t.image_urls && t.image_urls.length > 0) || (t.image_score != null && t.image_score > 0)) return 'done'
  if (t.status === 'processing' && t.current_stage === 'image') return 'processing'
  return 'pending'
})
const derivedVideoStatus = computed(() => {
  const t = task.value
  if (t.video_url) return 'done'
  if (t.status === 'processing' && t.current_stage === 'video') return 'processing'
  return 'pending'
})

// ====== 时间格式化 ======
const formatTime = (t) => t ? new Date(t).toLocaleString('zh-CN') : '-'

// ====== 多平台成片 ======
// task.platform_outputs 为 JSON map: {platform: url}；video_url 作主平台兜底
const platformVideos = computed(() => {
  const t = task.value
  const map = {}
  if (t.platform_outputs) {
    try { Object.assign(map, JSON.parse(t.platform_outputs)) } catch (e) {}
  }
  if (t.video_url && !(t.platform in map)) map[t.platform || 'douyin'] = t.video_url
  return map
})
const hasVideos = computed(() => Object.keys(platformVideos.value).length > 0)
// 平台展示信息（名称 + 比例），与 PLATFORM_CONFIG 保持一致
const PLAT_INFO = {
  douyin: { name: '抖音', ratio: '9:16' },
  xiaohongshu: { name: '小红书', ratio: '3:4' },
  kuaishou: { name: '快手', ratio: '9:16' },
  bilibili: { name: 'B站', ratio: '16:9' },
  youtube: { name: 'YouTube', ratio: '16:9' },
}
const platformName = (p) => (PLAT_INFO[p] && PLAT_INFO[p].name) || p
const platformRatio = (p) => (PLAT_INFO[p] && PLAT_INFO[p].ratio) || '-'
// 按比例分组：相同画幅的平台共用一个预览卡片，标题用平台名"/"拼接（如"抖音/快手"）
// 避免同源视频重复展示；不同比例（如 9:16 与 16:9）独立卡片以呈现裁剪差异
const videoGroupsByRatio = computed(() => {
  const groups = new Map()  // ratio -> { ratio, platforms:[], url }
  for (const [plat, url] of Object.entries(platformVideos.value)) {
    const info = PLAT_INFO[plat] || { name: plat, ratio: '?' }
    const ratio = info.ratio
    if (!groups.has(ratio)) {
      groups.set(ratio, { ratio, platforms: [info.name], url })
    } else {
      const g = groups.get(ratio)
      if (!g.platforms.includes(info.name)) g.platforms.push(info.name)
    }
  }
  return Array.from(groups.values()).map(g => ({
    ratio: g.ratio,
    label: g.platforms.join('/'),
    url: withOutputToken(g.url),
  }))
})

// ====== 操作 ======
const exportVideo = () => task.value.video_url ? window.open(withOutputToken(task.value.video_url)) : alert('视频尚未生成')
const previewImage = (url) => window.open(url, '_blank')

// 审核相关
const showRejectInput = ref(false)
const rejectComment = ref('')
const approveTask = async () => { try { await api.reviewTask(taskId.value, 'approve'); await loadTask() } catch(e) {} }
const rejectTask = async () => {
  try { await api.reviewTask(taskId.value, 'reject', rejectComment.value); showRejectInput.value=false; rejectComment.value=''; await loadTask() }
  catch(e) {}
}
// 分镜图重生成
// 后端语义：POST /regenerate?stage=image → 只清空 image_urls/image_score
// （STAGE_OUTPUTS 限定），保留 script / storyboard / character_ref，
// 再由 run_stage('image') 只跑图片阶段。故这里是「重生成整组分镜图」，
// 不是单张 —— 文案与定妆照都不会被抹掉。
const regenBusy = ref(-1)
const regenerateOneShot = async (idx) => {
  const total = (task.value?.image_urls || []).length
  if (!confirm(`重生成该任务的全部 ${total} 张分镜图？\n\n将保留文案、分镜脚本与角色定妆照，仅重跑图片阶段。`)) return
  regenBusy.value = idx
  try {
    await api.regenerateTask(taskId.value, 'image')
    await loadTask()
  } catch (e) {
    alert('重生成失败：' + e.message)
  } finally {
    regenBusy.value = -1
  }
}
const regenerateTask = async () => {
  // current_stage 可能是流水线子阶段（如 hotspot=抓取热点、done=完成），这些不在
  // 后端重生成白名单（script/character/image/tts/video/subtitle/all + 别名 storyboard/spot）内，
  // 直接透传会 400「未知阶段」。白名单外的值一律回退到 all（重跑整条流水线）。
  const LEGAL_STAGES = ['script', 'character', 'image', 'tts', 'video', 'subtitle', 'all', 'storyboard', 'spot']
  const stage = LEGAL_STAGES.includes(task.value.current_stage) ? task.value.current_stage : 'all'
  try { await api.regenerateTask(taskId.value, stage); await loadTask() }
  catch(e) {}
}

// 发布弹窗
const showPublishModal = ref(false)
const publishPlatform = ref('douyin')
const publishPlatforms = [
  { id: 'douyin', name: '抖音', icon: '🎵' },
  { id: 'xiaohongshu', name: '小红书', icon: '📕' },
  { id: 'kuaishou', name: '快手', icon: '⚡' },
]
const openPublish = () => { publishPlatform.value = task.value.platform || 'douyin'; showPublishModal.value = true }
const confirmPublish = async () => {
  try {
    const r = await api.publishTask(taskId.value, [publishPlatform.value])
    showPublishModal.value = false
    const fail = (r.results||[]).filter(x=>!x.success)
    alert(fail.length ? `失败：${fail.map(f=>f.message).join('；')}` : '发布成功')
  } catch(e) { alert('发布失败：'+(e.message||e)) }
}

// ====== 数据加载 ======
const loadTask = async () => {
  try { Object.assign(task.value, await api.getTask(taskId.value)) } catch(e) {}
}

// WebSocket 实时推送（主导）
const connectWS = () => {
  if (!taskId.value) return
  wsConn = api.createProgressWebSocket(taskId.value, (data) => {
    if (data.type !== 'progress') return
    // 合并 WS 推送数据到 task（增量更新，保留已有字段）
    Object.assign(task.value, {
      status: data.status,
      current_stage: data.current_stage,
      progress: data.progress ?? task.value.progress,
      script_score: data.script_score ?? task.value.script_score,
      image_score: data.image_score ?? task.value.image_score,
    })
    // 推送携带产物 URL 时直接写入
    if (data.image_urls) task.value.image_urls = data.image_urls
    if (data.video_url) task.value.video_url = data.video_url
    if (data.video_duration != null) task.value.video_duration = data.video_duration

    // 终态时关闭 WS
    if (data.status === 'done' || data.status === 'failed' || data.status === 'pending_review') {
      setTimeout(() => { if (wsConn) { wsConn.close(); wsConn = null } }, 2000)
    }
  })
}

// 轮询保底（WS 断开时降级用）
const startPolling = () => {
  pollTimer = setInterval(() => {
    if (isProcessing.value && !wsConn) loadTask()
    else if (!isProcessing.value) stopPolling()
  }, 5000)
}
const stopPolling = () => { clearInterval(pollTimer); pollTimer = null }
const cleanup = () => {
  stopPolling()
  if (wsConn) { wsConn.close(); wsConn = null }
}

onMounted(async () => {
  if (!taskId.value) {
    try { const d = await api.getTasks({page:1, page_size:1}); if(d.items?.length) { taskId.value=d.items[0].id; history.replaceState(null,'',`/tasks/${taskId.value}`)} } catch(e){}
  }
  await loadTask()
  // WebSocket 主导实时推送
  connectWS()
  // 轮询保底
  startPolling()
})
onUnmounted(() => cleanup())
</script>

<style scoped>
/* ====== 页头 ====== */
.header-meta { color: var(--color-text-muted); margin-top: var(--spacing-2); font-size: 13px; display: flex; align-items: center; gap: 8px; }
.dynasty-tag { background: rgba(201,166,107,.12); color: #8b6914; padding: 1px 8px; border-radius: var(--radius-sm); font-size: 12px; }

/* ====== 失败告警 ====== */
.error-banner { display: flex; align-items: center; gap: 16px; background: linear-gradient(135deg,#fff2f0,#fff1f0); border: 1px solid #ffa39e; border-radius: var(--radius-lg); padding: 16px 20px; margin-bottom: var(--spacing-4); box-shadow: 0 2px 8px rgba(255,77,79,.08); }
.error-icon { font-size: 28px; flex-shrink: 0; }
.error-body { flex: 1; min-width: 0; }
.error-title { font-weight: 600; color: #cf1322; margin-bottom: 4px; }
.error-msg { font-size: 14px; color: #a8071a; line-height: 1.6; word-break: break-all; }

/* ====== 审核卡 ====== */
.review-card { background: linear-gradient(135deg, #fffbe6, #fff7e6); border-color: #ffe58f; }
.review-hint { font-size: 13px; color: #d48806; }
.review-actions { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.btn-success { background: #52c41a; color: #fff; border: none; }
.btn-success:hover { opacity: .85; }
.reject-input { margin-top: 12px; padding: 12px; background: var(--color-bg); border-radius: var(--radius-md); border: 1px solid var(--color-border-light); }
.reject-input textarea { width: 100%; border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: 8px 12px; font-family: inherit; resize: vertical; box-sizing: border-box; }
.reject-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }

/* ====== 步骤条（flex 连接器，无 absolute 定位）====== */
.steps-bar { display: flex; align-items: flex-start; padding: 24px 0 36px; margin-bottom: var(--spacing-4); }
.step-item { display: flex; flex-direction: column; align-items: center; gap: 8px; min-width: 0; }
.step-node { width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: 700; border: 2px solid var(--color-border); background: var(--color-bg-card); color: var(--color-text-muted); transition: all .3s; flex-shrink: 0; }
.step-name { font-size: 12px; color: var(--color-text-muted); white-space: nowrap; margin-top: 4px; }
/* done */
.step-item.done .step-node { background: #f6ffed; border-color: #52c41a; color: #52c41a; }
.step-item.done .step-name { color: #52c41a; }
/* active */
.step-item.active .step-node { background: #e6f7ff; border-color: #1890ff; color: #1890ff; animation: node-pulse 1.8s ease-in-out infinite; }
.step-item.active .step-name { color: #1890ff; font-weight: 600; }
/* error */
.step-item.error .step-node { background: #fff2f0; border-color: #ff4d4f; color: #ff4d4f; }
.step-item.error .step-name { color: #cf1322; font-weight: 600; }
@keyframes node-pulse { 0%,100%{box-shadow:0 0 0 0 rgba(24,144,255,.25)}50%{box-shadow:0 0 0 6px rgba(24,144,255,0)} }
/* 连接线：flex 自适应撑满两节点间距，颜色跟随当前步状态 */
.step-connector { flex: 1; height: 2px; margin-top: 17px; background: #e8e8e8; min-width: 16px; transition: background .3s; }
.step-connector.done { background: #52c41a; }
.step-connector.error { background: #ff4d4f; }

/* ====== 状态总览 ====== */
.section-card { background: var(--color-bg-card); border: 1px solid var(--color-border-light); border-radius: var(--radius-lg); padding: var(--spacing-4) var(--spacing-5); margin-bottom: var(--spacing-4); box-shadow: var(--shadow-sm); }
.section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--spacing-3); }
.section-title { font-size: 15px; font-weight: 600; }

/* pill 标签 */
.status-pill { display: inline-flex; align-items: center; padding: 4px 14px; border-radius: var(--radius-full); font-size: 13px; font-weight: 600; }
.pill-running { background:#e6f7ff;color:#096dd9;border:1px solid #91d5ff; }
.pill-review { background:#fffbe6;color:#d48806;border:1px solid #ffe58f; }
.pill-done { background:#f6ffed;color:#389e0d;border:1px solid #b7eb8f; }
.pill-error { background:#fff2f0;color:#cf1322;border:1px solid #ffccc7; }

/* 状态网格 */
.status-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.status-cell { display: flex; flex-direction: column; gap: 6px; padding: 12px; background: var(--color-bg); border-radius: var(--radius-md); border: 1px solid var(--color-border-light); }
.cell-label { font-size: 12px; color: var(--color-text-muted); }
.cell-value { font-size: 14px; font-weight: 600; }
.cell-done { color: #52c41a; }
.cell-running { color: #1890ff; }
.cell-idle { color: var(--color-text-muted); }
.cell-stage { color: var(--color-primary); }

/* 文案 */
.score-tag { font-size: 12px; background: #f6ffed; color: #389e0d; padding: 2px 10px; border-radius: var(--radius-full); font-weight: 600; }
/* 原诗卡片 */
.poem-origin .poem-content { line-height: 2; font-size: 15px; white-space: pre-wrap; padding: 16px; background: var(--color-bg); border-radius: var(--radius-md); margin: 0; font-family: inherit; }
.origin-meta { font-size: 13px; color: var(--color-text-muted); }
.script-block { line-height: 1.9; color: var(--color-text-secondary); white-space: pre-wrap; font-size: 14px; padding: 16px; background: var(--color-bg); border-radius: var(--radius-md); margin: 0; font-family: inherit; }

/* 骨架屏 */
.gen-badge { display:inline-flex;align-items:center;gap:5px;font-size:12px;color:#d48806;font-weight:500;}
.gen-badge i { width:8px;height:8px;border-radius:50%;background:#faad14;display:inline-block;animation:pulse 1s infinite; }
.skeleton { padding: 20px; background: var(--color-bg); border-radius: var(--radius-md); }
.sk-line { height: 14px; background: linear-gradient(90deg,var(--color-border-light) 25%,var(--color-bg) 50%,var(--color-border-light) 75%); background-size:200% 100%; animation: shimmer 1.5s infinite; border-radius: 4px; margin-bottom: 10px; }
@keyframes shimmer { 0%{background-position:200% 0}100%{background-position:-200% 0} }

/* 分镜网格（响应式：桌面 3/行、平板 2/行、手机 1/行） */
.storyboard-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
@media (max-width: 1100px) { .storyboard-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 640px) { .storyboard-grid { grid-template-columns: 1fr; } }
.sb-card {
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  overflow: hidden;
  cursor: zoom-in;
  background: var(--color-bg);
  transition: border-color .2s, box-shadow .2s;
}
.sb-card:hover { border-color: var(--color-primary); box-shadow: 0 4px 14px rgba(0,0,0,.1); }
.sb-card-imgwrap { position: relative; line-height: 0; background: var(--color-bg-secondary); }
.sb-card-img { width: 100%; aspect-ratio: 3 / 4; object-fit: cover; display: block; transition: transform .3s ease; }
.sb-card:hover .sb-card-img { transform: scale(1.03); }
.sb-card-num {
  position: absolute; top: 8px; left: 8px;
  min-width: 22px; height: 22px; padding: 0 6px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 50%;
  background: var(--color-primary); color: #fff;
  font-size: 12px; font-weight: 700;
}
.sb-card-cam {
  position: absolute; top: 8px; right: 8px;
  max-width: calc(100% - 40px);
  padding: 3px 8px; border-radius: var(--radius-full);
  font-size: 11px; line-height: 1.4; color: #fff;
  background: rgba(0,0,0,.55); backdrop-filter: blur(4px);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sb-card-prompt {
  position: absolute; left: 0; right: 0; bottom: 0;
  padding: 18px 10px 8px;
  font-size: 12px; line-height: 1.5; color: #fff;
  background: linear-gradient(transparent, rgba(0,0,0,.78));
  max-height: 50%; overflow: hidden;
  opacity: 0; transition: opacity .25s ease;
}
.sb-card:hover .sb-card-prompt { opacity: 1; }

/* 多平台视频网格 */
.video-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }
.video-card { border: 1px solid var(--color-border-light); border-radius: var(--radius-md); overflow: hidden; background: var(--color-bg); }
.video-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--color-border-light); }
.plat-badge { font-size: 13px; font-weight: 600; }
.plat-ratio { font-size: 11px; background: #e6f7ff; color: #096dd9; padding: 1px 8px; border-radius: var(--radius-full); font-weight: 600; }

.btn-text {
  border: 1px solid var(--color-border-light);
  background: transparent; color: var(--color-text-secondary);
  font-size: 12px; padding: 4px 10px; border-radius: var(--radius-full);
  cursor: pointer; transition: all .2s;
}
.btn-text:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-text:disabled { opacity: .55; cursor: not-allowed; }

.sb-meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

/* 视频 */
.video-meta { font-size: 13px; color: var(--color-text-muted); }
.img-count { font-size: 12px; background: #e6f7ff; color: #096dd9; padding: 2px 10px; border-radius: var(--radius-full); font-weight: 600; }
.video-wrap { background: #000; border-radius: var(--radius-md); overflow: hidden; }

/* 图片画廊 */
.image-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.gallery-item { border: 1px solid var(--color-border-light); border-radius: var(--radius-md); overflow: hidden; cursor: pointer; transition: all .2s; }
.gallery-item:hover { border-color: var(--color-primary); box-shadow: 0 4px 12px rgba(0,0,0,.1); transform: translateY(-2px); }
.gallery-item img { width: 100%; aspect-ratio: 1; object-fit: cover; display: block; background: var(--color-bg); }
.character-ref-wrap { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
.character-ref-img { width: 160px; height: 160px; object-fit: cover; border-radius: var(--radius-md); border: 1px solid var(--color-border-light); cursor: pointer; transition: all .2s; }
.character-ref-img:hover { border-color: var(--color-primary); box-shadow: 0 4px 12px rgba(0,0,0,.1); transform: translateY(-2px); }
.character-ref-note { flex: 1; min-width: 200px; color: var(--color-text-muted); font-size: 13px; line-height: 1.6; }
/* 角色锚点已锁定 玻璃徽标 */
.anchor-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600;
  padding: 3px 12px; border-radius: var(--radius-full);
  background: rgba(201,166,107,.14); color: #8b6914; border: 1px solid rgba(201,166,107,.4);
  backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
.anchor-badge i { width: 7px; height: 7px; border-radius: 50%; background: #8b6914; box-shadow: 0 0 6px #8b6914; animation: pulse 1.6s infinite; }
.character-ref-side { flex: 1; min-width: 220px; display: flex; flex-direction: column; gap: 10px; }
.anchor-anchors { background: rgba(255,255,255,.6); border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md); padding: 10px 12px; backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); }
.anchor-label { font-size: 12px; font-weight: 600; color: var(--color-primary); margin-bottom: 4px; }
.anchor-text { font-size: 13px; line-height: 1.6; color: var(--color-text); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .4; } }
.gallery-caption { padding: 8px 10px; font-size: 12px; color: var(--color-text-secondary); display: flex; justify-content: space-between; background: var(--color-bg); }
.gallery-caption span:last-child { max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 图片骨架屏 */
.image-skeleton-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; padding: 16px; background: var(--color-bg); border-radius: var(--radius-md); }
.img-sk { aspect-ratio: 1; background: linear-gradient(90deg,var(--color-border-light) 25%,var(--color-bg) 50%,var(--color-border-light) 75%); background-size:200% 100%; animation: shimmer 1.5s infinite; border-radius: var(--radius-md); }

/* 视频骨架屏 */
.video-skeleton { padding: 16px; background: var(--color-bg); border-radius: var(--radius-md); }
.vid-sk-placeholder { aspect-ratio: 16/9; max-height: 300px; background: linear-gradient(90deg,var(--color-border-light) 25%,var(--color-bg) 50%,var(--color-border-light) 75%); background-size:200% 100%; animation: shimmer 1.5s infinite; border-radius: var(--radius-md); }

/* 处理中 */
.processing-card { text-align: center; }
.proc-center { display: flex; align-items: center; justify-content: center; gap: 16px; padding: 40px; }
.proc-spin { width: 32px; height: 32px; border: 3px solid var(--color-border); border-top-color: var(--color-primary); border-radius: 50%; animation: spin 1s linear infinite; }
.proc-title { font-size: 16px; font-weight: 600; }
.proc-sub { font-size: 13px; color: var(--color-text-muted); margin-top: 4px; }

/* 弹窗 */
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.5); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal-content { background: var(--color-bg-card); border-radius: var(--radius-lg); width: 400px; max-width: 90vw; box-shadow: var(--shadow-lg); }
.modal-header { display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; border-bottom: 1px solid var(--color-border-light); }
.modal-header h3 { font-size: 17px; font-weight: 600; }
.modal-close { background:none;border:none;font-size:22px;cursor:pointer;color:var(--color-text-muted);line-height:1; }
.modal-body { padding: 20px; }
.modal-body label { display: block; font-size: 14px; font-weight: 500; margin-bottom: 10px; }
.platform-options { display: flex; gap: 12px; flex-wrap: wrap; }
.platform-option { display: flex; align-items: center; gap: 6px; cursor: pointer; padding: 8px 12px; border: 1px solid var(--color-border); border-radius: var(--radius-md); transition: all .15s; }
.platform-option:hover { border-color: var(--color-primary); background: var(--color-primary-bg); }
.publish-warning { margin: 14px 0 0; padding: 10px 12px; font-size: 13px; color: var(--color-danger, #d93026); background: rgba(217, 48, 38, .08); border: 1px solid rgba(217, 48, 38, .25); border-radius: var(--radius-md); line-height: 1.5; }
.modal-footer { display: flex; justify-content: flex-end; gap: 8px; padding: 16px 20px; border-top: 1px solid var(--color-border-light); }

/* 按钮 */
.btn-ghost { background: transparent; border: 1px solid var(--color-border); color: var(--color-text-secondary); }
.btn-ghost:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.btn-ghost:disabled { opacity: .4; cursor: not-allowed; }

@keyframes spin { to { transform: rotate(360deg); } }
@keyframes pulse { 0%,100%{opacity:1}50%{opacity:.4} }
</style>
