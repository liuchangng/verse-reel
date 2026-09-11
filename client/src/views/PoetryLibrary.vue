<template>
  <div class="poetry-library">
    <!-- 页面头部 -->
    <div class="page-header">
      <div>
        <div class="decorative-line"></div>
        <h1 class="page-title">📚 诗词素材库</h1>
        <div class="header-subtitle">共 {{ total.toLocaleString() }} 首诗词 · 搜索筛选 · 一键创建任务</div>
      </div>
    </div>

    <!-- 统计卡片 -->
    <div class="stats-cards">
      <div class="stat-card">
        <div class="stat-card-icon">📜</div>
        <div class="stat-card-body">
          <span class="stat-card-label">诗词总数</span>
          <span class="stat-card-value">{{ total.toLocaleString() }}</span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-card-icon">📋</div>
        <div class="stat-card-body">
          <span class="stat-card-label">显示范围</span>
          <span class="stat-card-value">{{ (page - 1) * pageSize + 1 }} - {{ Math.min(page * pageSize, total) }}</span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-card-icon">📖</div>
        <div class="stat-card-body">
          <span class="stat-card-label">总页数</span>
          <span class="stat-card-value">{{ totalPages.toLocaleString() }} <small>页</small></span>
        </div>
      </div>
    </div>

    <!-- 搜索筛选栏 -->
    <div class="filter-bar">
      <div class="search-box">
        <span class="search-icon">🔍</span>
        <input
          type="text"
          v-model="searchKeyword"
          placeholder="搜索诗词标题或作者..."
          @input="debounceSearch"
        />
        <button v-if="searchKeyword" class="search-clear" @click="clearSearch" title="清除搜索">✕</button>
      </div>
      <div class="filter-options">
        <select v-model="filterDynasty" @change="loadPoems">
          <option value="">全部朝代</option>
          <option v-for="d in dynastyOptions" :key="d" :value="d">{{ d }}</option>
        </select>
        <select v-model="filterGenre" @change="loadPoems">
          <option value="">全部体裁</option>
          <option v-for="g in genreOptions" :key="g" :value="g">{{ g }}</option>
        </select>
      </div>
    </div>

    <!-- 加载状态 -->
    <div v-if="loading" class="loading-state">
      <div class="loading-spinner"></div>
      <div>加载中...</div>
    </div>

    <!-- 诗词卡片列表 -->
    <div class="poem-list" v-else>
      <div
        v-for="poem in poems"
        :key="poem.id"
        class="poem-card"
        :class="{ 'is-expanded': expandedId === poem.id }"
      >
        <div class="poem-card-main">
          <div class="poem-info">
            <div class="poem-title">{{ poem.title }}</div>
            <div class="poem-meta">
              <span class="meta-item author">{{ poem.author || '佚名' }}</span>
              <span class="meta-divider">·</span>
              <span class="meta-item dynasty">{{ poem.dynasty || '未知' }}</span>
              <span class="meta-divider">·</span>
              <span class="meta-item genre">{{ poem.genre || '其他' }}</span>
            </div>
          </div>
          <div class="poem-actions">
            <button
              class="btn btn-sm btn-outline"
              @click="toggleExpand(poem.id)"
            >
              {{ expandedId === poem.id ? '收起' : '查看详情' }}
            </button>
          </div>
        </div>

        <!-- 详情展开 -->
        <div v-if="expandedId === poem.id" class="poem-detail">
          <div class="detail-content">{{ poem.content }}</div>
          <div class="detail-actions">
            <button class="btn btn-primary" @click="createTask(poem)">创建视频任务</button>
          </div>
        </div>
      </div>

      <!-- 空状态 -->
      <div v-if="poems.length === 0 && !loading" class="empty-state">
        <div class="empty-icon">📜</div>
        <div class="empty-text">未找到匹配的诗词</div>
      </div>
    </div>

    <!-- 分页 -->
    <div class="pagination" v-if="totalPages > 1">
      <button
        class="btn btn-sm btn-secondary"
        @click="changePage(page - 1)"
        :disabled="page <= 1"
      >上一页</button>
      <span class="page-info">第 {{ page }} / {{ totalPages }} 页</span>
      <button
        class="btn btn-sm btn-secondary"
        @click="changePage(page + 1)"
        :disabled="page >= totalPages"
      >下一页</button>
      <select v-model="pageSize" @change="loadPoems" class="page-size-select">
        <option :value="20">20条/页</option>
        <option :value="50">50条/页</option>
        <option :value="100">100条/页</option>
      </select>
    </div>

    <!-- 创建任务弹窗 -->
    <div class="modal-overlay" v-if="showCreateModal" @click.self="showCreateModal = false">
      <div class="modal-content">
        <div class="modal-header">
          <h3>创建视频任务</h3>
          <button class="modal-close" @click="showCreateModal = false">×</button>
        </div>
        <div class="modal-body">
          <div class="selected-poem">
            <span class="poem-title">《{{ selectedPoem?.title }}》</span>
            <span class="poem-author">{{ selectedPoem?.author }} · {{ selectedPoem?.dynasty }}</span>
          </div>
          <div class="form-group">
            <label>选择发布平台：</label>
            <div class="platform-options">
              <label v-for="p in publishPlatforms" :key="p.id" class="platform-option" :class="{ 'is-selected': selectedPublishPlatforms.includes(p.id) }">
                <input type="checkbox" :value="p.id" v-model="selectedPublishPlatforms" />
                <span>{{ p.icon }} {{ p.name }}</span>
              </label>
            </div>
            <div class="form-hint">可多选；已选 {{ selectedPublishPlatforms.length }} 个平台，将分别为每个平台生成对应尺寸的视频。</div>
          </div>
          <div class="form-group">
            <label>选择文案风格：</label>
            <select v-model="selectedStyle" class="style-select">
              <option value="">自动推荐（按热点主题推断，无热点用默认风格）</option>
              <option v-for="s in styleOptions" :key="s.name" :value="s.name">
                {{ s.name }} - {{ s.description }}
              </option>
            </select>
            <div class="form-hint">风格决定文案语调、画面提示词与字幕样式；不指定则自动推荐。</div>
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
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()

// 状态
const loading = ref(false)
const poems = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const searchKeyword = ref('')
const filterDynasty = ref('')
const filterGenre = ref('')
const expandedId = ref(null)

// 弹窗状态
const showCreateModal = ref(false)
const selectedPoem = ref(null)
const selectedPublishPlatforms = ref(['douyin', 'xiaohongshu', 'kuaishou', 'bilibili', 'youtube'])
// 文案风格：'' = 自动推荐（后端按热点主题推断；无热点回退默认风格）
const selectedStyle = ref('')
const styleOptions = ref([])

// 朝代和体裁选项（从API动态获取，这里提供默认值）
const dynastyOptions = ['唐', '宋', '元', '明', '清', '先秦', '汉', '魏晋', '南北朝', '隋', '辽', '金', '遠古', '未知']
const genreOptions = ['诗', '词', '文', '赋', '曲', '论', '其他']

const publishPlatforms = [
  { id: 'douyin', name: '抖音', icon: '🎵' },
  { id: 'xiaohongshu', name: '小红书', icon: '📕' },
  { id: 'kuaishou', name: '快手', icon: '⚡' },
  { id: 'bilibili', name: 'B站', icon: '📺' },
  { id: 'youtube', name: 'YouTube', icon: '▶️' },
]

// 计算总页数
const totalPages = computed(() => Math.ceil(total.value / pageSize.value))

// 防抖搜索
let searchTimer = null
const debounceSearch = () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    page.value = 1
    loadPoems()
  }, 300)
}

// 清除搜索
const clearSearch = () => {
  searchKeyword.value = ''
  page.value = 1
  loadPoems()
}

// 加载诗词
const loadPoems = async () => {
  loading.value = true
  try {
    const data = await api.getPoems({
      page: page.value,
      page_size: pageSize.value,
      search: searchKeyword.value,
      dynasty: filterDynasty.value,
      genre: filterGenre.value,
    })
    poems.value = data.items || []
    total.value = data.total || 0
  } catch (error) {
    console.error('加载诗词失败', error)
  } finally {
    loading.value = false
  }
}

// 切换展开
const toggleExpand = (id) => {
  expandedId.value = expandedId.value === id ? null : id
}

// 切换页码
const changePage = (newPage) => {
  if (newPage < 1 || newPage > totalPages.value) return
  page.value = newPage
  loadPoems()
}

// 打开创建任务弹窗
const createTask = (poem) => {
  selectedPoem.value = poem
  showCreateModal.value = true
}

// 确认创建任务
const confirmCreateTask = async () => {
  if (!selectedPoem.value) return
  if (selectedPublishPlatforms.value.length === 0) {
    alert('请至少选择一个发布平台')
    return
  }
  try {
    await api.createTask(
      selectedPoem.value.id,
      selectedPublishPlatforms.value,
      null,
      selectedStyle.value || null
    )
    showCreateModal.value = false
    router.push('/tasks')
  } catch (error) {
    console.error('创建任务失败', error)
    alert('创建任务失败：' + error.message)
  }
}

// 加载可选风格列表
const loadStyles = async () => {
  try {
    const data = await api.getStyles()
    styleOptions.value = data.items || []
  } catch (error) {
    console.error('加载风格列表失败', error)
  }
}

// 初始化
onMounted(() => {
  loadPoems()
  loadStyles()
})
</script>

<style scoped>
/* 布局 */
.poetry-library { width: 100%; }

/* 页面头部 */
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: var(--spacing-5); }
.header-subtitle { color: var(--color-text-muted); font-size: 13px; margin-top: var(--spacing-2); }
.header-actions { display: flex; gap: var(--spacing-2); }

/* 统计卡片 */
.stats-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--spacing-4); margin-bottom: var(--spacing-5); }
.stat-card { background: linear-gradient(135deg, #fff 0%, #faf8f5 100%); border: 1px solid rgba(180, 140, 90, 0.15); border-radius: var(--radius-lg); padding: var(--spacing-4) var(--spacing-5); display: flex; align-items: center; gap: var(--spacing-4); box-shadow: 0 2px 12px rgba(139, 90, 43, 0.06), 0 1px 3px rgba(0,0,0,0.04); transition: all 0.25s ease; position: relative; overflow: hidden; }
.stat-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: linear-gradient(90deg, #c9a66b, #e8d5a8, #c9a66b); opacity: 0.7; }
.stat-card:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(139, 90, 43, 0.1), 0 2px 6px rgba(0,0,0,0.05); border-color: rgba(180, 140, 90, 0.3); }
.stat-card-icon { font-size: 32px; flex-shrink: 0; width: 56px; height: 56px; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, rgba(201, 166, 107, 0.12), rgba(232, 213, 168, 0.08)); border-radius: var(--radius-md); }
.stat-card-body { display: flex; flex-direction: column; min-width: 0; }
.stat-card-label { font-size: 13px; color: #8b7355; font-weight: 500; margin-bottom: 4px; letter-spacing: 0.3px; }
.stat-card-value { font-size: 26px; font-weight: 700; color: #5a4a2f; line-height: 1.2; font-variant-numeric: tabular-nums; }
.stat-card-value small { font-size: 14px; font-weight: 500; color: #9a8a70; margin-left: 2px; }

/* 搜索筛选 */
.filter-bar { display: flex; gap: var(--spacing-3); margin-bottom: var(--spacing-4); flex-wrap: wrap; align-items: center; }
.search-box { flex: 1; min-width: 240px; display: flex; align-items: center; gap: var(--spacing-2); padding: var(--spacing-2) var(--spacing-3); background: var(--color-bg-card); border: 1px solid var(--color-border); border-radius: var(--radius-md); transition: border-color 0.2s; }
.search-box:focus-within { border-color: var(--color-primary); }
.search-icon { color: var(--color-text-muted); font-size: 16px; flex-shrink: 0; }
.search-box input { flex: 1; border: none; outline: none; font-size: 14px; background: transparent; font-family: inherit; min-width: 0; }
.search-clear { flex-shrink: 0; width: 20px; height: 20px; border: none; background: var(--color-bg-secondary); border-radius: 50%; color: var(--color-text-muted); font-size: 12px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.15s; line-height: 1; padding: 0; }
.search-clear:hover { background: var(--color-border); color: var(--color-text); }
.filter-options { display: flex; gap: var(--spacing-2); }
.filter-options select { padding: var(--spacing-2) var(--spacing-3); border: 1px solid var(--color-border); border-radius: var(--radius-md); background: var(--color-bg-card); font-size: 14px; cursor: pointer; min-width: 100px; font-family: inherit; }

/* 诗词卡片列表 */
.poem-list { display: flex; flex-direction: column; gap: var(--spacing-3); }
.poem-card { background: var(--color-bg-card); border: 1px solid var(--color-border-light); border-radius: var(--radius-lg); padding: var(--spacing-4); transition: all 0.2s ease; box-shadow: var(--shadow-sm); }
.poem-card:hover { border-color: var(--color-primary-light); box-shadow: var(--shadow-md); }
.poem-card.is-expanded { border-color: var(--color-primary); }
.poem-card-main { display: flex; justify-content: space-between; align-items: center; gap: var(--spacing-4); }
.poem-info { flex: 1; min-width: 0; }
.poem-title { font-size: 16px; font-weight: 600; color: var(--color-text); margin-bottom: var(--spacing-1); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.poem-meta { display: flex; align-items: center; gap: var(--spacing-2); flex-wrap: wrap; }
.meta-item { font-size: 13px; color: var(--color-text-secondary); }
.meta-item.author { font-weight: 500; }
.meta-item.dynasty { color: var(--color-text-muted); }
.meta-item.genre { color: var(--color-text-muted); background: var(--color-bg-secondary); padding: 2px 8px; border-radius: var(--radius-sm); }
.meta-divider { color: var(--color-text-muted); font-size: 12px; }
.poem-actions { display: flex; gap: var(--spacing-2); flex-shrink: 0; }

/* 详情展开 */
.poem-detail { padding-top: var(--spacing-4); margin-top: var(--spacing-3); border-top: 1px solid var(--color-border-light); }
.detail-content { line-height: 2; color: var(--color-text-secondary); white-space: pre-wrap; margin-bottom: var(--spacing-3); font-size: 15px; background: var(--color-bg); padding: var(--spacing-3); border-radius: var(--radius-md); }
.detail-actions { display: flex; justify-content: flex-end; }

/* 空状态 */
.empty-state { text-align: center; padding: 80px 20px; background: var(--color-bg-card); border-radius: var(--radius-lg); border: 1px dashed var(--color-border); }
.empty-icon { font-size: 56px; margin-bottom: var(--spacing-3); }
.empty-text { font-size: 15px; color: var(--color-text-muted); }

/* 分页 */
.pagination { display: flex; align-items: center; justify-content: center; gap: var(--spacing-3); margin-top: var(--spacing-5); padding: var(--spacing-3) 0; }
.page-info { font-size: 13px; color: var(--color-text-muted); min-width: 120px; text-align: center; }
.page-size-select { padding: var(--spacing-1) var(--spacing-2); border: 1px solid var(--color-border); border-radius: var(--radius-sm); font-size: 13px; background: var(--color-bg-card); font-family: inherit; }
.style-select { width: 100%; padding: var(--spacing-2) var(--spacing-3); border: 1px solid var(--color-border); border-radius: var(--radius-md); background: var(--color-bg-card); color: var(--color-text); font-size: 14px; font-family: inherit; cursor: pointer; }

/* 加载状态 */
.loading-state { text-align: center; padding: 80px; color: var(--color-text-muted); }
.loading-spinner { width: 40px; height: 40px; border: 3px solid var(--color-border); border-top-color: var(--color-primary); border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto var(--spacing-4); }
@keyframes spin { to { transform: rotate(360deg); } }

/* 弹窗 */
.modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal-content { background: var(--color-bg-card); border-radius: var(--radius-lg); width: 440px; max-width: 90vw; box-shadow: var(--shadow-lg); }
.modal-sm { width: 380px; }
.modal-header { display: flex; justify-content: space-between; align-items: center; padding: var(--spacing-4) var(--spacing-5); border-bottom: 1px solid var(--color-border-light); }
.modal-header h3 { font-size: 17px; font-weight: 600; }
.modal-close { background: none; border: none; font-size: 22px; cursor: pointer; color: var(--color-text-muted); line-height: 1; }
.modal-close:hover { color: var(--color-text); }
.modal-body { padding: var(--spacing-5); }
.selected-poem { padding: var(--spacing-3); background: var(--color-bg); border-radius: var(--radius-md); margin-bottom: var(--spacing-4); }
.poem-title { display: block; font-size: 15px; font-weight: 600; color: var(--color-text); margin-bottom: var(--spacing-1); }
.poem-author { font-size: 13px; color: var(--color-text-muted); }
.form-group { margin-bottom: var(--spacing-4); }
.form-group label { display: block; font-size: 14px; font-weight: 500; margin-bottom: var(--spacing-2); color: var(--color-text-secondary); }
.platform-options { display: flex; gap: var(--spacing-3); flex-wrap: wrap; }
.platform-option { display: flex; align-items: center; gap: var(--spacing-2); cursor: pointer; padding: var(--spacing-2) var(--spacing-3); border: 1px solid var(--color-border); border-radius: var(--radius-md); transition: all 0.2s; }
.platform-option:hover { border-color: var(--color-primary-light); background: var(--color-primary-bg); }
.platform-option.is-selected { border-color: var(--color-primary); background: var(--color-primary-bg); font-weight: 500; }
.platform-option input[type="checkbox"] { accent-color: var(--color-primary); }
.modal-footer { display: flex; justify-content: flex-end; gap: var(--spacing-2); padding: var(--spacing-4) var(--spacing-5); border-top: 1px solid var(--color-border-light); }
.import-cmd { display: block; background: var(--color-bg); padding: var(--spacing-3); border-radius: var(--radius-md); font-family: monospace; font-size: 13px; color: var(--color-primary); margin: var(--spacing-3) 0; word-break: break-all; }
.import-status { margin-top: var(--spacing-3); font-size: 13px; color: var(--color-text-muted); }
</style>
