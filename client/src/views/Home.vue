<template>
  <div class="home">
    <!-- 页面头部 -->
    <div class="page-header">
      <div>
        <div class="decorative-line"></div>
        <h1 class="page-title">📚 诗词选题</h1>
      </div>
      <div class="btn-group">
        <select class="filter-input" v-model="selectedPlatform" style="width: 140px; height: 38px;">
          <option value="douyin">📱 抖音 (9:16)</option>
          <option value="xiaohongshu">📕 小红书 (3:4)</option>
          <option value="kuaishou">⚡ 快手 (9:16)</option>
          <option value="bilibili">📺 B站 (16:9)</option>
          <option value="youtube">▶️ YouTube (16:9)</option>
        </select>
        <button class="btn btn-primary" @click="batchCreateTasks">➕ 批量创建任务</button>
      </div>
    </div>
    
    <!-- 统计卡片 -->
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-icon">📖</div>
        <div class="stat-value">{{ stats.total?.toLocaleString() || '-' }}</div>
        <div class="stat-label">诗词总数</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">🎯</div>
        <div class="stat-value">{{ selectedPoems.length }}</div>
        <div class="stat-label">已选题材</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">⚡</div>
        <div class="stat-value">{{ processingCount }}</div>
        <div class="stat-label">进行中任务</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">✅</div>
        <div class="stat-value">{{ completedCount }}</div>
        <div class="stat-label">已完成视频</div>
      </div>
    </div>
    
    <!-- 筛选区 -->
    <div class="filter-bar">
      <div class="filter-item" style="flex: 2;">
        <div class="filter-label">🔍 搜索标题/作者</div>
        <input 
          class="filter-input" 
          v-model="filters.search" 
          placeholder="输入诗词标题或作者名..."
          @keyup.enter="fetchPoems"
        />
      </div>
      <div class="filter-item">
        <div class="filter-label">📅 朝代</div>
        <select class="filter-input" v-model="filters.dynasty">
          <option value="">全部朝代</option>
          <option v-for="d in dynasties" :key="d" :value="d">{{ d }}</option>
        </select>
      </div>
      <div class="filter-item">
        <div class="filter-label">📝 体裁</div>
        <select class="filter-input" v-model="filters.genre">
          <option value="">全部体裁</option>
          <option v-for="g in genres" :key="g" :value="g">{{ g }}</option>
        </select>
      </div>
      <div class="filter-item">
        <div class="filter-label">🏷️ 状态</div>
        <select class="filter-input" v-model="filters.status">
          <option value="">全部状态</option>
          <option value="pending">未使用</option>
          <option value="done">已生成</option>
        </select>
      </div>
      <button class="btn btn-primary" style="height: 38px; align-self: flex-end;" @click="fetchPoems">筛选</button>
    </div>
    
    <!-- 诗词列表 -->
    <div class="table-container">
      <table class="table">
        <thead>
          <tr>
            <th style="width: 40px;"><input type="checkbox" @change="toggleAll" :checked="allSelected" /></th>
            <th>标题</th>
            <th>作者</th>
            <th>朝代</th>
            <th>体裁</th>
            <th>内容预览</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="poem in poems" :key="poem.id">
            <td><input type="checkbox" :value="poem.id" v-model="selectedIds" /></td>
            <td><strong>{{ poem.title }}</strong></td>
            <td>{{ poem.author }}</td>
            <td>{{ poem.dynasty }}</td>
            <td>{{ poem.genre }}</td>
            <td class="content-preview">{{ poem.content_preview }}</td>
            <td>
              <span :class="['status-tag', poem.has_task ? 'status-done' : 'status-pending']">
                {{ poem.has_task ? '✓ 已生成' : '○ 未使用' }}
              </span>
            </td>
            <td class="actions">
              <button class="btn btn-secondary btn-sm" @click="viewPoem(poem)">查看</button>
              <button class="btn btn-primary btn-sm" @click="createTask(poem)">创建任务</button>
            </td>
          </tr>
          <tr v-if="poems.length === 0">
            <td colspan="8" style="text-align: center; padding: 40px; color: var(--color-text-muted);">
              暂无数据
            </td>
          </tr>
        </tbody>
      </table>
      <div class="pagination">
        <span>共 {{ pagination.total.toLocaleString() }} 条</span>
        <button class="page-btn" @click="prevPage" :disabled="pagination.current <= 1">&lt;</button>
        <button class="page-btn active">{{ pagination.current }}</button>
        <button class="page-btn" @click="nextPage" :disabled="pagination.current >= totalPages">&gt;</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()

// 数据
const poems = ref([])
const stats = ref({})
const selectedIds = ref([])

// 目标平台
const selectedPlatform = ref('douyin')

// 筛选条件
const filters = reactive({
  search: '',
  dynasty: '',
  genre: '',
  status: '',
})

// 分页
const pagination = reactive({
  current: 1,
  pageSize: 20,
  total: 0,
})

// 选项
const dynasties = ['盛唐', '中唐', '晚唐', '北宋', '南宋', '元', '明', '清']
const genres = ['律诗', '绝句', '古风', '词', '曲', '文']

// 计算属性
const processingCount = computed(() => stats.value.processing_tasks || 0)
const completedCount = computed(() => stats.value.completed_tasks || 0)
const totalPages = computed(() => Math.ceil(pagination.total / pagination.pageSize))
const allSelected = computed(() => poems.value.length > 0 && selectedIds.value.length === poems.value.length)
const selectedPoems = computed(() => poems.value.filter(p => selectedIds.value.includes(p.id)))

// 获取诗词列表
const fetchPoems = async () => {
  try {
    const params = {
      page: pagination.current,
      page_size: pagination.pageSize,
    }
    if (filters.search) params.search = filters.search
    if (filters.dynasty) params.dynasty = filters.dynasty
    if (filters.genre) params.genre = filters.genre
    
    const data = await api.getPoems(params)
    poems.value = data.items || []
    pagination.total = data.total || 0
  } catch (error) {
    console.error('获取诗词列表失败', error)
  }
}

// 获取统计
const fetchStats = async () => {
  try {
    stats.value = await api.getPoemStats() || {}
  } catch (error) {
    console.error('获取统计失败', error)
  }
}

// 全选/取消全选
const toggleAll = (e) => {
  if (e.target.checked) {
    selectedIds.value = poems.value.map(p => p.id)
  } else {
    selectedIds.value = []
  }
}

// 创建任务
const createTask = async (poem) => {
  try {
    await api.createTask(poem.id, selectedPlatform.value)
    alert(`已创建任务: ${poem.title} (${selectedPlatform.value})`)
    router.push('/tasks')
  } catch (error) {
    alert('创建任务失败')
  }
}

// 批量创建
const batchCreateTasks = async () => {
  if (selectedIds.value.length === 0) {
    alert('请先选择诗词')
    return
  }
  try {
    for (const id of selectedIds.value) {
      await api.createTask(id, selectedPlatform.value)
    }
    alert(`已创建 ${selectedIds.value.length} 个任务 (${selectedPlatform.value})`)
    selectedIds.value = []
    router.push('/tasks')
  } catch (error) {
    alert('批量创建失败')
  }
}

// 查看诗词
const viewPoem = (poem) => {
  alert(`${poem.title} - ${poem.author}\n\n${poem.content}`)
}

// 分页
const prevPage = () => {
  if (pagination.current > 1) {
    pagination.current--
    fetchPoems()
  }
}

const nextPage = () => {
  if (pagination.current < totalPages.value) {
    pagination.current++
    fetchPoems()
  }
}

// 初始化
onMounted(() => {
  fetchPoems()
  fetchStats()
})
</script>

<style scoped>
.content-preview {
  max-width: 300px;
  color: var(--color-text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.actions {
  display: flex;
  gap: var(--spacing-2);
}
</style>
