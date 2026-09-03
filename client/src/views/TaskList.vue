<template>
  <div class="task-list">
    <!-- 页面头部 -->
    <div class="page-header">
      <div>
        <div class="decorative-line"></div>
        <h1 class="page-title">📋 任务管理</h1>
      </div>
      <div class="header-actions">
        <button class="btn btn-secondary" @click="fetchTasks">
          <span class="btn-icon">🔄</span> 刷新
        </button>
        <button class="btn btn-primary" @click="$router.push('/poetry')">
          <span class="btn-icon">➕</span> 新建任务
        </button>
      </div>
    </div>

    <!-- 统计卡片 -->
    <div class="stats-cards">
      <div class="stat-card">
        <div class="stat-card-icon">📋</div>
        <div class="stat-card-body">
          <span class="stat-card-label">全部任务</span>
          <span class="stat-card-value">{{ stats.total.toLocaleString() }}</span>
        </div>
      </div>
      <div class="stat-card stat-card--success">
        <div class="stat-card-icon">✅</div>
        <div class="stat-card-body">
          <span class="stat-card-label">已完成</span>
          <span class="stat-card-value">{{ stats.done }}</span>
        </div>
      </div>
      <div class="stat-card stat-card--warning">
        <div class="stat-card-icon">🔄</div>
        <div class="stat-card-body">
          <span class="stat-card-label">进行中</span>
          <span class="stat-card-value">{{ stats.processing }}</span>
        </div>
      </div>
      <div class="stat-card stat-card--danger">
        <div class="stat-card-icon">❌</div>
        <div class="stat-card-body">
          <span class="stat-card-label">已失败</span>
          <span class="stat-card-value">{{ stats.failed }}</span>
        </div>
      </div>
      <div class="stat-card stat-card--info">
        <div class="stat-card-icon">🔍</div>
        <div class="stat-card-body">
          <span class="stat-card-label">待审核</span>
          <span class="stat-card-value">{{ stats.pendingReview }}</span>
        </div>
      </div>
    </div>

    <!-- 任务列表 -->
    <div class="task-table-wrap" v-if="tasks.length > 0">
      <table class="task-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>诗词</th>
            <th>阶段</th>
            <th>进度</th>
            <th>创建时间</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="task in tasks" :key="task.id" :class="{ 'row-failed': task.status === 'failed', 'row-done': task.status === 'done' }">
            <td class="col-id">#{{ String(task.id).padStart(3, '0') }}</td>
            <td class="col-poem">
              <div class="poem-name">{{ task.poem_title }}</div>
              <div class="poem-author">{{ task.poem_author || task.author }} · {{ getDynastyBadge(task) }}</div>
            </td>
            <td><span class="stage-badge">{{ getStageLabel(task.current_stage) }}</span></td>
            <td>
              <div class="progress-dots">
                <span :class="dotClass(task.script_status)" title="文案">文</span>
                <span :class="dotClass(task.image_status)" title="图片">图</span>
                <span :class="dotClass(task.video_status)" title="视频">视</span>
              </div>
            </td>
            <td class="col-time">{{ formatTime(task.created_at) }}</td>
            <td><span :class="['status-badge', statusBadgeClass(task.status)]">{{ getStatusLabel(task.status) }}</span></td>
            <td class="col-action">
              <button class="btn btn-sm btn-ghost" @click="viewDetail(task)">详情 →</button>
              <button v-if="canPublish(task)" class="btn btn-sm btn-primary" @click="publishTask(task)">发布</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 加载中 -->
    <div v-else-if="loading" class="empty-state">
      <div class="empty-icon">⏳</div>
      <div class="empty-text">加载中...</div>
    </div>
    <!-- 空状态 -->
    <div v-else class="empty-state">
      <div class="empty-icon">📭</div>
      <div class="empty-text">暂无任务</div>
      <p class="empty-hint">去 <a href="/poetry" class="link">诗词库</a> 选一首诗创建第一个任务吧</p>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()
const tasks = ref([])
const loading = ref(true)

// 统计数据
const stats = computed(() => {
  const s = { total: tasks.value.length, done: 0, processing: 0, failed: 0, pendingReview: 0 }
  for (const t of tasks.value) {
    if (t.status === 'done') s.done++
    else if (t.status === 'processing') s.processing++
    else if (t.status === 'failed') s.failed++
    else if (t.status === 'pending_review') s.pendingReview++
  }
  return s
})

// 获取任务列表
const fetchTasks = async () => {
  loading.value = true
  try {
    const data = await api.getTasks()
    tasks.value = data.items || []
  } catch (error) {
    console.error('获取任务列表失败', error)
  } finally {
    loading.value = false
  }
}

// 阶段标签
const getStageLabel = (stage) => {
  const labels = { script: '文案', image: '图片', video: '视频', done: '完成' }
  return labels[stage] || stage || '-'
}

// 进度圆点样式
const dotClass = (status) => {
  if (status === 'done') return 'dot dot-done'
  if (status === 'processing') return 'dot dot-active'
  return 'dot dot-pending'
}

// 状态 badge 样式
const statusBadgeClass = (status) => {
  const map = { processing: 'badge-running', pending_review: 'badge-review', done: 'badge-done', failed: 'badge-error' }
  return map[status] || ''
}

// 状态标签
const getStatusLabel = (status) => {
  const labels = { processing: '进行中', pending_review: '待审核', done: '已完成', failed: '已失败' }
  return labels[status] || status || '-'
}

// 朝代标签
const getDynastyBadge = (task) => {
  // 尝试从 poem 获取朝代信息
  return task.dynasty || ''
}

// 是否可发布
const canPublish = (task) => task.status === 'done' && task.video_url

// 格式化时间
const formatTime = (time) => {
  if (!time) return '-'
  const d = new Date(time)
  const pad = n => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// 查看详情
const viewDetail = (task) => router.push(`/tasks/${task.id}`)

// 发布
const publishTask = async (task) => {
  try {
    const result = await api.publishTask(task.id, [task.platform || 'douyin'])
    const failed = (result.results || []).filter(r => !r.success)
    alert(failed.length ? `发布失败：${failed.map(f => f.message).join('；')}` : `任务 #${task.id} 发布成功`)
  } catch (error) {
    alert('发布失败：' + (error.message || error))
  }
}

onMounted(() => fetchTasks())
</script>

<style scoped>
.task-list { width: 100%; }

/* 页面头部 */
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: var(--spacing-5); }
.header-actions { display: flex; gap: var(--spacing-2); }

/* ====== 统计卡片 ====== */
.stats-cards { display: grid; grid-template-columns: repeat(5, 1fr); gap: var(--spacing-3); margin-bottom: var(--spacing-5); }
.stat-card { background: linear-gradient(135deg, #fff 0%, #faf8f5 100%); border: 1px solid rgba(180, 140, 90, 0.15); border-radius: var(--radius-lg); padding: var(--spacing-3) var(--spacing-4); display: flex; align-items: center; gap: var(--spacing-3); box-shadow: 0 2px 12px rgba(139, 90, 43, 0.06); transition: all 0.25s ease; position: relative; overflow: hidden; }
.stat-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: linear-gradient(90deg, #c9a66b, #e8d5a8, #c9a66b); opacity: 0.7; }
.stat-card:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(139, 90, 43, 0.1); }
.stat-card-icon { font-size: 26px; flex-shrink: 0; width: 48px; height: 48px; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, rgba(201, 166, 107, 0.12), rgba(232, 213, 168, 0.08)); border-radius: var(--radius-md); }
.stat-card-body { display: flex; flex-direction: column; min-width: 0; }
.stat-card-label { font-size: 12px; color: #8b7355; font-weight: 500; margin-bottom: 2px; }
.stat-card-value { font-size: 22px; font-weight: 700; color: #5a4a2f; line-height: 1.2; font-variant-numeric: tabular-nums; }

/* 卡片变体色 */
.stat-card--success::before { background: linear-gradient(90deg, #52c41a, #95de64, #52c41a); }
.stat-card--success .stat-card-value { color: #389e0d; }
.stat-card--warning::before { background: linear-gradient(90deg, #faad14, #ffd666, #faad14); }
.stat-card--warning .stat-card-value { color: #d48806; }
.stat-card--danger::before { background: linear-gradient(90deg, #ff4d4f, #ff7875, #ff4d4f); }
.stat-card--danger .stat-card-value { color: #cf1322; }
.stat-card--info::before { background: linear-gradient(90deg, #1890ff, #69c0ff, #1890ff); }
.stat-card--info .stat-card-value { color: #096dd9; }

/* ====== 表格 ====== */
.task-table-wrap { background: var(--color-bg-card); border: 1px solid var(--color-border-light); border-radius: var(--radius-lg); overflow: hidden; box-shadow: var(--shadow-sm); }
.task-table { width: 100%; border-collapse: collapse; }
.task-table thead { background: linear-gradient(180deg, #faf8f5, #f5efe6); }
.task-table th { padding: 12px 16px; text-align: left; font-size: 13px; font-weight: 600; color: #8b7355; border-bottom: 2px solid rgba(180, 140, 90, 0.2); white-space: nowrap; letter-spacing: 0.3px; }
.task-table td { padding: 14px 16px; border-bottom: 1px solid var(--color-border-light); font-size: 14px; vertical-align: middle; }
.task-table tbody tr:last-child td { border-bottom: none; }
.task-table tbody tr:hover { background: rgba(201, 166, 107, 0.04); transition: background 0.15s; }
.task-table tbody tr.row-failed { background: rgba(255, 77, 79, 0.03); }
.task-table tbody tr.row-done { background: rgba(82, 196, 26, 0.03); }

/* 列宽 */
.col-id { font-family: monospace; color: var(--color-text-muted); font-size: 13px; white-space: nowrap; }
.col-poem { min-width: 200px; }
.poem-name { font-weight: 600; color: var(--color-text); margin-bottom: 2px; }
.poem-author { font-size: 12px; color: var(--color-text-muted); }
.col-time { color: var(--color-text-muted); font-size: 13px; white-space: nowrap; }
.col-action { white-space: nowrap; }

/* 阶段 badge */
.stage-badge { display: inline-block; padding: 2px 10px; border-radius: var(--radius-sm); font-size: 12px; font-weight: 500; background: rgba(201, 166, 107, 0.1); color: #8b6914; }

/* 进度圆点 */
.progress-dots { display: flex; gap: 6px; }
.dot { width: 24px; height: 24px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; border: 1.5px solid var(--color-border); color: var(--color-text-muted); }
.dot-done { background: #f6ffed; border-color: #b7eb8f; color: #52c41a; }
.dot-active { background: #e6f7ff; border-color: #91d5ff; color: #1890ff; animation: pulse-dot 1.5s ease-in-out infinite; }
.dot-pending { background: var(--color-bg); color: var(--color-text-muted); }

@keyframes pulse-dot {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

/* 状态 badge */
.status-badge { display: inline-block; padding: 3px 12px; border-radius: var(--radius-full); font-size: 12px; font-weight: 600; white-space: nowrap; }
.badge-running { background: #e6f7ff; color: #096dd9; border: 1px solid #91d5ff; }
.badge-review { background: #fffbe6; color: #d48806; border: 1px solid #ffe58f; }
.badge-done { background: #f6ffed; color: #52c41a; border: 1px solid #b7eb8f; }
.badge-error { background: #fff2f0; color: #cf1322; border: 1px solid #ffccc7; }

/* 按钮 */
.btn-ghost { background: transparent; border: 1px solid var(--color-border); color: var(--color-text-secondary); }
.btn-ghost:hover { border-color: var(--color-primary); color: var(--color-primary); background: var(--color-primary-bg); }

/* 空状态 */
.empty-state { text-align: center; padding: 80px 20px; background: var(--color-bg-card); border-radius: var(--radius-lg); border: 1px dashed var(--color-border); }
.empty-icon { font-size: 48px; margin-bottom: var(--spacing-3); }
.empty-text { font-size: 15px; color: var(--color-text-muted); margin-bottom: var(--spacing-2); }
.empty-hint { font-size: 13px; color: var(--color-text-secondary); }
.link { color: var(--color-primary); text-decoration: none; font-weight: 500; }
.link:hover { text-decoration: underline; }
</style>
