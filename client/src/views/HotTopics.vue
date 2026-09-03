<template>
  <div class="hot-topics">
    <!-- 页面头部 -->
    <div class="page-header">
      <div>
        <div class="decorative-line"></div>
        <h1 class="page-title">🔥 热点选题</h1>
        <div class="header-subtitle">
          实时热搜 · AI智能匹配诗词 · 一键创建任务
        </div>
      </div>
      <div class="header-right">
        <div class="refresh-info" v-if="lastRefresh">
          <span class="refresh-dot" :class="{ active: autoRefreshEnabled }"></span>
          刷新于 {{ formatTime(lastRefresh) }}
        </div>
        <button class="btn btn-secondary" @click="refreshHotspots(true)" :disabled="loading">
          🔄 {{ loading ? '刷新中...' : '手动刷新' }}
        </button>
        <label class="auto-refresh-toggle">
          <input type="checkbox" v-model="autoRefreshEnabled" />
          <span>自动刷新</span>
        </label>
      </div>
    </div>

    <!-- 加载状态（仅首次无数据时显示） -->
    <div v-if="loading && !hasLoadedOnce" class="loading-state">
      <div class="loading-spinner"></div>
      <div>正在获取热点数据...</div>
    </div>

    <!-- 静默刷新提示（有旧数据时：前端轮询 或 后台异步刷新） -->
    <div v-else-if="loading || refreshing" class="silent-refresh">
      <span class="refresh-dot active"></span> {{ loading ? '数据更新中...' : '后台刷新中，稍后自动更新...' }}
    </div>
    
    <!-- 热搜列表（有数据就展示，静默刷新时不隐藏） -->
    <div class="hotspot-list" v-if="hotspots.length > 0">
      <div 
        v-for="(item, index) in hotspots" 
        :key="index"
        class="hotspot-card"
      >
        <!-- 热搜标题 -->
        <div class="hotspot-header">
          <div class="hotspot-rank">
            <span class="rank-number">{{ index + 1 }}</span>
            <span class="hotspot-badge" :class="getHotspotBadgeClass(item.hot)">
              {{ getHotspotBadgeText(item.hot) }}
            </span>
          </div>
          <div class="hotspot-title">{{ item.title }}</div>
          <div class="hotspot-hot">{{ formatHot(item.hot) }}热度</div>
        </div>
        
        <!-- 推荐诗词 -->
        <div class="recommended-poems" v-if="item.recommended_poems && item.recommended_poems.length > 0">
          <div class="recommended-label">📜 AI推荐诗词：</div>
          <div 
            v-for="(poem, pIndex) in item.recommended_poems" 
            :key="pIndex"
            class="poem-recommendation"
          >
            <div class="poem-info">
              <span class="poem-title">《{{ poem.title }}》</span>
              <span class="poem-author">- {{ poem.author }} ({{ poem.dynasty }})</span>
            </div>
            <div class="poem-quote">"{{ poem.content_preview }}"</div>
            <div class="poem-reason">💡 {{ poem.match_reason }}</div>
            <button class="btn btn-primary btn-sm" @click="createTask(poem)">
              创建任务 →
            </button>
          </div>
        </div>
        
        <!-- 无推荐诗词 -->
        <div class="no-recommendation" v-else>
          <span class="no-rec-text">暂无匹配诗词</span>
        </div>
      </div>
      
      <!-- 空状态 -->
      <div class="empty-state" v-if="hotspots.length === 0 && !loading">
        <div class="empty-icon">🔍</div>
        <div class="empty-text">暂无热搜数据</div>
        <button class="btn btn-primary" @click="refreshHotspots(true)">刷新获取</button>
      </div>
    </div>
    
    <!-- 最具市场潜力诗词 -->
    <div class="top-poems" v-if="topPoems.length > 0">
      <div class="top-poems-header">
        <h2 class="top-poems-title">🏆 最具市场潜力诗词 Top {{ topPoems.length }}</h2>
        <span class="top-poems-sub">基于热点选题 AI 推荐命中次数统计 · 命中越高越值得做</span>
      </div>
      <div class="top-poems-list">
        <div
          v-for="(p, i) in topPoems"
          :key="p.id"
          class="top-poem-card"
        >
          <span class="top-rank" :class="{ 'top-rank-1': i === 0, 'top-rank-2': i === 1, 'top-rank-3': i === 2 }">{{ i + 1 }}</span>
          <div class="top-poem-info">
            <span class="top-poem-title">《{{ p.title }}》</span>
            <span class="top-poem-author">{{ p.author }} · {{ p.dynasty }}</span>
          </div>
          <span class="top-poem-count">{{ p.recommend_count }} 次命中</span>
          <button class="btn btn-primary btn-sm" @click="createTask(p)">创建任务 →</button>
        </div>
      </div>
    </div>
    
    <!-- 创建任务弹窗 -->
    <div class="modal-overlay" v-if="showCreateModal" @click.self="showCreateModal = false">
      <div class="modal-content">
        <div class="modal-header">
          <h3>创建任务</h3>
          <button class="modal-close" @click="showCreateModal = false">×</button>
        </div>
        <div class="modal-body">
          <div class="selected-poem">
            <span class="poem-title">《{{ selectedPoem?.title }}》</span>
            <span class="poem-author">- {{ selectedPoem?.author }}</span>
          </div>
          <div class="modal-tip">
            将按「设置」页中配置的默认发布平台（默认全选）发布，可在设置中调整。
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn btn-secondary" @click="showCreateModal = false">取消</button>
          <button class="btn btn-primary" @click="confirmCreateTask">确认创建</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()

// ====== 模块级缓存：跨导航保持，切走再回来不重新加载 ======
const _cache = { hotspots: [], lastRefresh: null, topPoems: [] }

// 状态
const loading = ref(false)
const hotspots = ref(_cache.hotspots)
const lastRefresh = ref(_cache.lastRefresh)
const topPoems = ref(_cache.topPoems)
const autoRefreshEnabled = ref(true)
const hasLoadedOnce = ref(_cache.hotspots.length > 0)

// 弹窗状态
const showCreateModal = ref(false)
const selectedPoem = ref(null)

// 自动刷新定时器
let refreshTimer = null

// 获取热搜数据（force=true 强制重抓覆盖写库；否则读库缓存）
// 后端策略：库有数据 → 立即返回(<100ms)，过期则后台异步刷新写库
const refreshing = ref(false)

const refreshHotspots = async (force = false) => {
  // 库有数据时不显示全页 loading（后端 <100ms 即返）
  const hasOldData = hotspots.value.length > 0 || _cache.hotspots.length > 0
  if (!hasOldData) {
    loading.value = true  // 仅首次/库空时显示加载态
  }
  try {
    const data = await api.getHotspots(undefined, force)
    const items = data.hotspots || []
    // 同步到模块级缓存 + 响应式 ref
    _cache.hotspots = items
    _cache.lastRefresh = new Date()
    hotspots.value = items
    topPoems.value = data.top_poems || []
    _cache.topPoems = data.top_poems || []
    lastRefresh.value = _cache.lastRefresh
    hasLoadedOnce.value = true
    // 后端标志：正在后台刷新（数据稍后会更新）
    refreshing.value = !!data.refreshing
  } catch (error) {
    console.error('获取热搜失败', error)
  } finally {
    loading.value = false
  }
}

// 格式化热度值
const formatHot = (hot) => {
  if (!hot) return ''
  if (hot >= 10000) {
    return (hot / 10000).toFixed(1) + '万'
  }
  return hot.toString()
}

// 获取热搜徽章样式
const getHotspotBadgeClass = (hot) => {
  if (hot >= 1000000) return 'badge-hot'
  if (hot >= 500000) return 'badge-warm'
  return 'badge-new'
}

// 获取热搜徽章文本
const getHotspotBadgeText = (hot) => {
  if (hot >= 1000000) return '🔥 热'
  if (hot >= 500000) return '🟠 新'
  return '🔵 热门'
}

// 格式化时间
const formatTime = (time) => {
  if (!time) return ''
  const d = new Date(time)
  return d.toLocaleTimeString('zh-CN')
}

// 打开创建任务弹窗（直接传诗词对象）
const createTask = (poem) => {
  selectedPoem.value = poem
  showCreateModal.value = true
}

// 确认创建任务
const confirmCreateTask = async () => {
  if (!selectedPoem.value) return

  try {
    // 不传 platforms，由后端回退 settings.output_platforms（默认全选）
    const result = await api.createTask(selectedPoem.value.id, [])
    showCreateModal.value = false

    // 跳转到任务管理页
    router.push('/tasks')
  } catch (error) {
    console.error('创建任务失败', error)
    alert('创建任务失败：' + error.message)
  }
}

// 启动自动刷新
const startAutoRefresh = () => {
  if (refreshTimer) return
  refreshTimer = setInterval(() => {
    if (autoRefreshEnabled.value) {
      refreshHotspots()
    }
  }, 10 * 60 * 1000) // 每10分钟刷新
}

// 停止自动刷新
const stopAutoRefresh = () => {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

// 初始化
onMounted(() => {
  // 有缓存 → 秒显，后台静默刷新
  if (_cache.hotspots.length > 0) {
    hasLoadedOnce.value = true
    hotspots.value = _cache.hotspots
    lastRefresh.value = _cache.lastRefresh
    topPoems.value = _cache.topPoems
    refreshHotspots() // 后台更新
  } else {
    refreshHotspots() // 首次加载
  }
  startAutoRefresh()
})

// 清理
onUnmounted(() => {
  stopAutoRefresh()
})
</script>

<style scoped>
.hot-topics {
  width: 100%;
}

.header-subtitle {
  color: var(--color-text-muted);
  font-size: 14px;
  margin-top: var(--spacing-2);
}

.header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
}

.refresh-info {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
  font-size: 13px;
  color: var(--color-text-muted);
}

.refresh-dot {
  width: 8px;
  height: 8px;
  background: var(--color-border);
  border-radius: 50%;
}

.refresh-dot.active {
  background: var(--color-success);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.auto-refresh-toggle {
  display: flex;
  align-items: center;
  gap: var(--spacing-1);
  font-size: 13px;
  color: var(--color-text-muted);
  cursor: pointer;
}

.auto-refresh-toggle input {
  cursor: pointer;
}

/* 静默刷新提示 */
.silent-refresh {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-2);
  padding: var(--spacing-3);
  font-size: 13px;
  color: var(--color-text-muted);
  background: rgba(201, 166, 107, 0.06);
  border-radius: var(--radius-md);
  margin-bottom: var(--spacing-3);
}

/* 热搜卡片 */
.hotspot-card {
  background: var(--color-bg-card);
  border-radius: var(--radius-lg);
  padding: var(--spacing-5);
  margin-bottom: var(--spacing-4);
  box-shadow: var(--shadow-sm);
  border: 1px solid var(--color-border-light);
}

.hotspot-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
  margin-bottom: var(--spacing-4);
}

.hotspot-rank {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
}

.rank-number {
  font-size: 24px;
  font-weight: 700;
  color: var(--color-primary);
  min-width: 32px;
}

.hotspot-badge {
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: 12px;
}

.badge-hot {
  background: #FFF2F0;
  color: #FF4D4F;
}

.badge-warm {
  background: #FFF7E6;
  color: #FA8C16;
}

.badge-new {
  background: #E6F7FF;
  color: #1890FF;
}

.hotspot-title {
  flex: 1;
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text);
}

.hotspot-hot {
  font-size: 13px;
  color: var(--color-text-muted);
}

/* 推荐诗词 */
.recommended-poems {
  background: var(--color-bg);
  border-radius: var(--radius-md);
  padding: var(--spacing-4);
}

.recommended-label {
  font-size: 13px;
  color: var(--color-text-muted);
  margin-bottom: var(--spacing-3);
}

.poem-recommendation {
  padding: var(--spacing-3);
  background: var(--color-bg-card);
  border-radius: var(--radius-md);
  margin-bottom: var(--spacing-2);
  border: 1px solid var(--color-border-light);
}

.poem-recommendation:last-child {
  margin-bottom: 0;
}

.poem-info {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
  margin-bottom: var(--spacing-2);
}

.poem-title {
  font-weight: 600;
  color: var(--color-text);
}

.poem-author {
  font-size: 13px;
  color: var(--color-text-muted);
}

.poem-quote {
  font-size: 14px;
  color: var(--color-text-secondary);
  margin-bottom: var(--spacing-2);
  font-style: italic;
}

.poem-reason {
  font-size: 12px;
  color: var(--color-primary);
  margin-bottom: var(--spacing-2);
}

.no-recommendation {
  padding: var(--spacing-3);
  text-align: center;
  color: var(--color-text-muted);
  font-size: 13px;
}

/* 最具市场潜力诗词 */
.top-poems {
  margin-top: var(--spacing-6);
  background: linear-gradient(135deg, rgba(201, 166, 107, 0.06), rgba(201, 166, 107, 0.02));
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-lg);
  padding: var(--spacing-5);
}

.top-poems-header {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-3);
  flex-wrap: wrap;
  margin-bottom: var(--spacing-4);
}

.top-poems-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--color-text);
  margin: 0;
}

.top-poems-sub {
  font-size: 12px;
  color: var(--color-text-muted);
}

.top-poems-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-2);
}

.top-poem-card {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
  padding: var(--spacing-3) var(--spacing-4);
  background: var(--color-bg-card);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  transition: border-color 0.2s, transform 0.2s;
}

.top-poem-card:hover {
  border-color: var(--color-primary);
  transform: translateY(-2px);
}

.top-rank {
  font-size: 18px;
  font-weight: 700;
  color: var(--color-text-muted);
  min-width: 28px;
  text-align: center;
}

.top-rank-1 { color: #FF4D4F; }
.top-rank-2 { color: #FA8C16; }
.top-rank-3 { color: #1890FF; }

.top-poem-info {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.top-poem-title {
  font-weight: 600;
  color: var(--color-text);
}

.top-poem-author {
  font-size: 12px;
  color: var(--color-text-muted);
}

.top-poem-count {
  font-size: 12px;
  color: var(--color-primary);
  background: rgba(201, 166, 107, 0.1);
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  white-space: nowrap;
}

/* 空状态 */
.empty-state {
  text-align: center;
  padding: 60px var(--spacing-6);
}

.empty-icon {
  font-size: 48px;
  margin-bottom: var(--spacing-4);
}

.empty-text {
  font-size: 16px;
  color: var(--color-text-muted);
  margin-bottom: var(--spacing-4);
}

/* 加载状态 */
.loading-state {
  text-align: center;
  padding: 60px;
  color: var(--color-text-muted);
}

.loading-spinner {
  width: 40px;
  height: 40px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 1s linear infinite;
  margin: 0 auto var(--spacing-4);
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* 弹窗 */
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal-content {
  background: var(--color-bg-card);
  border-radius: var(--radius-lg);
  width: 400px;
  max-width: 90vw;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--spacing-4) var(--spacing-5);
  border-bottom: 1px solid var(--color-border-light);
}

.modal-header h3 {
  font-size: 18px;
  font-weight: 600;
}

.modal-close {
  background: none;
  border: none;
  font-size: 24px;
  cursor: pointer;
  color: var(--color-text-muted);
}

.modal-body {
  padding: var(--spacing-5);
}

.selected-poem {
  padding: var(--spacing-3);
  background: var(--color-bg);
  border-radius: var(--radius-md);
  margin-bottom: var(--spacing-4);
}

.modal-tip {
  font-size: 13px;
  color: var(--color-text-muted);
  background: rgba(201, 166, 107, 0.08);
  padding: var(--spacing-3);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-primary);
  line-height: 1.6;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--spacing-2);
  padding: var(--spacing-4) var(--spacing-5);
  border-top: 1px solid var(--color-border-light);
}
</style>
