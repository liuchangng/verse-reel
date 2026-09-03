<template>
  <div class="settings">
    <!-- 页面头部 -->
    <div class="page-header">
      <div>
        <div class="decorative-line"></div>
        <h1 class="page-title">⚙️ 系统设置</h1>
        <p class="page-subtitle">配置已持久化到数据库（重启后保留），修改即时生效。</p>
      </div>
      <div class="btn-group">
        <button class="btn btn-secondary" @click="resetSettings" :disabled="saving">重置</button>
        <button class="btn btn-primary" @click="saveSettings" :disabled="saving">
          {{ saving ? '⏳ 保存中…' : '💾 保存设置' }}
        </button>
      </div>
    </div>

    <!-- 顶部状态条 / 加载/保存提示 -->
    <div v-if="saveHint" class="top-hint" :class="saveHint.kind">
      {{ saveHint.text }}
    </div>

    <!-- 模型配置 -->
    <div class="model-configs">
      <!-- ====== 文本模型 ====== -->
      <div class="section-card model-card">
        <div class="section-header">
          <div class="section-title">
            <span class="model-icon">📝</span> 文本模型
          </div>
          <span class="model-tag tag-free">免费</span>
        </div>
        <div class="model-fields">
          <div class="form-item">
            <div class="form-label">API Key</div>
            <input class="form-input" type="password" v-model="settings.text_api_key" placeholder="sk-..." />
          </div>
          <div class="form-item">
            <div class="form-label">Base URL</div>
            <input class="form-input" v-model="settings.text_base_url" placeholder="https://api.xxx.com/v1" />
          </div>
          <div class="form-item">
            <div class="form-label">模型名称</div>
            <input class="form-input" v-model="settings.text_model" placeholder="model-name" />
            <div class="form-hint">已实测可用：<b>agnes-2.5-flash</b>（官方 FAQ 未列入，已通过）。备选：<code>agnes-2.0-flash</code>。</div>
          </div>
          <div class="form-item">
            <div class="form-label">并发数</div>
            <div class="input-row">
              <button class="num-btn" @click="dec('text_concurrency')" :disabled="settings.text_concurrency <= 1">−</button>
              <input class="form-input num-input" type="number" v-model.number="settings.text_concurrency" min="1" max="10" />
              <button class="num-btn" @click="inc('text_concurrency')" :disabled="settings.text_concurrency >= 10">+</button>
              <button class="btn btn-test" @click="testConcurrency('text')" :disabled="testing === 'text'">
                {{ testing === 'text' ? '⏳ 测试中…' : '🧪 测试' }}
              </button>
            </div>
            <div class="form-hint">
              💡 官方 RPM 约 <b>60 次/分钟</b>（文本档）；单次响应通常 2-8s，
              <b>推荐 2–5</b>。设 5 时 RPM≈40 安全；设 10 时理论 RPM≈75+ 长期会撞 429。
            </div>
          </div>
          <div v-if="testResults.text" class="test-result" :class="testResults.text.failed > 0 ? 'result-warn' : 'result-ok'">
            ✅ 成功 {{ testResults.text.success }}/{{ testResults.text.concurrency }}
            <span v-if="testResults.text.failed > 0"> ❌ 失败 {{ testResults.text.failed }}</span>
            ⏱️ {{ testResults.text.total_time }}s（端点 {{ testResults.text.endpoint }}）
          </div>
        </div>
      </div>

      <!-- ====== 图片模型 ====== -->
      <div class="section-card model-card">
        <div class="section-header">
          <div class="section-title">
            <span class="model-icon">🖼️</span> 图片模型
          </div>
          <span class="model-tag tag-free">免费</span>
        </div>
        <div class="model-fields">
          <div class="form-item">
            <div class="form-label">API Key</div>
            <input class="form-input" type="password" v-model="settings.image_api_key" placeholder="sk-..." />
          </div>
          <div class="form-item">
            <div class="form-label">Base URL</div>
            <input class="form-input" v-model="settings.image_base_url" placeholder="https://api.xxx.com/v1" />
          </div>
          <div class="form-item">
            <div class="form-label">模型名称</div>
            <input class="form-input" v-model="settings.image_model" placeholder="model-name" />
            <div class="form-hint">推荐 <code>agnes-image-2.1-flash</code>。请求体规范：<code>size:"1K"+ratio</code>，图生图走 <code>extra_body.image</code>。</div>
          </div>
          <div class="form-item">
            <div class="form-label">并发数</div>
            <div class="input-row">
              <button class="num-btn" @click="dec('image_concurrency')" :disabled="settings.image_concurrency <= 1">−</button>
              <input class="form-input num-input" type="number" v-model.number="settings.image_concurrency" min="1" max="10" />
              <button class="num-btn" @click="inc('image_concurrency')" :disabled="settings.image_concurrency >= 10">+</button>
              <button class="btn btn-test" @click="testConcurrency('image')" :disabled="testing === 'image'">
                {{ testing === 'image' ? '⏳ 测试中…' : '🧪 测试' }}
              </button>
            </div>
            <div class="form-hint">
              💡 官方 RPM：<b>1K=20/min, 2K=12/min, 3K=8/min, 4K=4/min</b>。
              单图耗时 10–25s，<b>推荐 2–3</b>。设 5 时 1K 档 RPM≈12–25 接近上限，
              设 8+ 几乎必撞 429。
            </div>
          </div>
          <div v-if="testResults.image" class="test-result" :class="testResults.image.failed > 0 ? 'result-warn' : 'result-ok'">
            ✅ 成功 {{ testResults.image.success }}/{{ testResults.image.concurrency }}
            <span v-if="testResults.image.failed > 0"> ❌ 失败 {{ testResults.image.failed }}</span>
            ⏱️ {{ testResults.image.total_time }}s（端点 {{ testResults.image.endpoint }}）
          </div>
        </div>
      </div>

      <!-- ====== 视频模型 ====== -->
      <div class="section-card model-card">
        <div class="section-header">
          <div class="section-title">
            <span class="model-icon">🎬</span> 视频模型
          </div>
          <span class="model-tag tag-free">免费</span>
        </div>
        <div class="model-fields">
          <div class="form-item">
            <div class="form-label">API Key</div>
            <input class="form-input" type="password" v-model="settings.video_api_key" placeholder="sk-..." />
          </div>
          <div class="form-item">
            <div class="form-label">Base URL</div>
            <input class="form-input" v-model="settings.video_base_url" placeholder="https://api.xxx.com/v1" />
          </div>
          <div class="form-item">
            <div class="form-label">模型名称</div>
            <input class="form-input" v-model="settings.video_model" placeholder="model-name" />
            <div class="form-hint">已对齐 SKILL 规范为 <code>agnes-video-v2.0</code>。请求体：<code>width/height/num_frames(8n+1)/frame_rate</code> + 顶层 <code>image</code>。</div>
          </div>
          <div class="form-item">
            <div class="form-label">
              并发数
              <span class="inline-hint warn">⚠ 官方硬限流 1 次/分钟</span>
            </div>
            <div class="input-row">
              <button class="num-btn" @click="dec('video_concurrency')" :disabled="settings.video_concurrency <= 1">−</button>
              <input class="form-input num-input" type="number" v-model.number="settings.video_concurrency" min="1" max="3" />
              <button class="num-btn" @click="inc('video_concurrency')" :disabled="settings.video_concurrency >= 3">+</button>
              <button class="btn btn-test" @click="testConcurrency('video')" :disabled="testing === 'video'">
                {{ testing === 'video' ? '⏳ 测试中…' : '🧪 测试' }}
              </button>
            </div>
            <div class="form-hint">
              💡 <b>必须 1</b>（视频两次提交间隔 < 60s 必返 400/429 <code>rate_limit_exceeded</code>）；
              后端已内置退避重试（≥65s、最多 6 次）。改高于 1 只是浪费配额。
            </div>
          </div>
          <div v-if="testResults.video" class="test-result" :class="testResults.video.failed > 0 ? 'result-warn' : 'result-ok'">
            ✅ 成功 {{ testResults.video.success }}/{{ testResults.video.concurrency }}
            <span v-if="testResults.video.failed > 0"> ❌ 失败 {{ testResults.video.failed }}</span>
            ⏱️ {{ testResults.video.total_time }}s（端点 {{ testResults.video.endpoint }}）
          </div>
        </div>
      </div>
    </div>

    <!-- 资源并发 -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">🧰 资源并发</div>
      </div>
      <p class="section-sub">TTS（已合并进后端，进程内运行；默认 edge-tts，CosyVoice2 权重就绪后自动优先）和字幕烧录（本地 ffmpeg，CPU 密集）独立配置；不会触发 agnes 限流。</p>
      <div class="threshold-row">
        <div class="form-item">
          <div class="form-label">TTS 并发</div>
          <div class="input-row">
            <button class="num-btn" @click="dec('tts_concurrency')" :disabled="settings.tts_concurrency <= 1">−</button>
            <input class="form-input num-input" type="number" v-model.number="settings.tts_concurrency" min="1" max="8" />
            <button class="num-btn" @click="inc('tts_concurrency')" :disabled="settings.tts_concurrency >= 8">+</button>
          </div>
        </div>
        <div class="form-item">
          <div class="form-label">字幕烧录并发</div>
          <div class="input-row">
            <button class="num-btn" @click="dec('subtitle_concurrency')" :disabled="settings.subtitle_concurrency <= 1">−</button>
            <input class="form-input num-input" type="number" v-model.number="settings.subtitle_concurrency" min="1" max="4" />
            <button class="num-btn" @click="inc('subtitle_concurrency')" :disabled="settings.subtitle_concurrency >= 4">+</button>
          </div>
        </div>
        <div class="form-item">
          <div class="form-label">评测 Critic 并发</div>
          <div class="input-row">
            <button class="num-btn" @click="dec('critic_concurrency')" :disabled="settings.critic_concurrency <= 1">−</button>
            <input class="form-input num-input" type="number" v-model.number="settings.critic_concurrency" min="1" max="10" />
            <button class="num-btn" @click="inc('critic_concurrency')" :disabled="settings.critic_concurrency >= 10">+</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 质检阈值 -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">🎯 质检阈值</div>
      </div>
      <div class="threshold-row">
        <div class="form-item">
          <div class="form-label">文案最低分 (满分10)</div>
          <div class="input-row">
            <button class="num-btn" @click="dec('min_script_score', 0.5)" :disabled="settings.min_script_score <= 1">−</button>
            <input class="form-input num-input" type="number" v-model.number="settings.min_script_score" min="1" max="10" step="0.5" />
            <button class="num-btn" @click="inc('min_script_score', 0.5)" :disabled="settings.min_script_score >= 10">+</button>
          </div>
        </div>
        <div class="form-item">
          <div class="form-label">图片最低分 (满分10)</div>
          <div class="input-row">
            <button class="num-btn" @click="dec('min_image_score', 0.5)" :disabled="settings.min_image_score <= 1">−</button>
            <input class="form-input num-input" type="number" v-model.number="settings.min_image_score" min="1" max="10" step="0.5" />
            <button class="num-btn" @click="inc('min_image_score', 0.5)" :disabled="settings.min_image_score >= 10">+</button>
          </div>
        </div>
        <div class="form-item">
          <div class="form-label">最大重试次数</div>
          <div class="input-row">
            <button class="num-btn" @click="dec('max_retries')" :disabled="settings.max_retries <= 1">−</button>
            <input class="form-input num-input" type="number" v-model.number="settings.max_retries" min="1" max="5" />
            <button class="num-btn" @click="inc('max_retries')" :disabled="settings.max_retries >= 5">+</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 发布平台（全局默认，创建任务未指定时回退到此） -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">📡 发布平台</div>
      </div>
      <p class="section-sub">勾选后，新建任务若不单独指定平台，将按此列表为每个平台生成对应尺寸的视频。默认全选。</p>
      <div class="platform-grid">
        <label
          v-for="p in allPlatforms"
          :key="p.id"
          class="platform-chip"
          :class="{ 'is-on': settings.output_platforms.includes(p.id) }"
        >
          <input type="checkbox" :value="p.id" v-model="settings.output_platforms" />
          <span>{{ p.icon }} {{ p.name }}</span>
          <span class="plat-ratio">{{ p.ratio }}</span>
        </label>
      </div>
    </div>

    <!-- 水印配置 -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">💧 水印配置</div>
        <label class="switch-inline">
          <input type="checkbox" v-model="settings.watermark_enabled" />
          <span>启用</span>
        </label>
      </div>
      <p class="section-sub" v-if="!settings.watermark_enabled">已关闭：成片与分镜图将不再叠加水印文字。</p>
      <div class="watermark-fields" v-else>
        <div class="form-item">
          <div class="form-label">水印文字</div>
          <input
            class="form-input"
            type="text"
            v-model="settings.watermark_text"
            maxlength="4"
            placeholder="昊康动漫"
          />
          <div class="form-hint">最多 4 个字（默认「昊康动漫」），用于分镜图与视频的品牌标识。</div>
        </div>
        <div class="wm-row">
          <div class="form-item">
            <div class="form-label">颜色</div>
            <select class="form-input" v-model="settings.watermark_colour">
              <option value="white">白色</option>
              <option value="black">黑色</option>
              <option value="yellow">黄色</option>
              <option value="red">红色</option>
            </select>
          </div>
          <div class="form-item">
            <div class="form-label">位置</div>
            <select class="form-input" v-model="settings.watermark_position">
              <option value="right_bottom">右下角</option>
              <option value="left_bottom">左下角</option>
            </select>
          </div>
        </div>
        <div class="wm-row">
          <div class="form-item">
            <div class="form-label">字号比例 (相对画面高)</div>
            <div class="input-row">
              <button class="num-btn" @click="dec('watermark_fontsize_ratio', 0.01, 0.03)" :disabled="settings.watermark_fontsize_ratio <= 0.03">−</button>
              <input class="form-input num-input" type="number" v-model.number="settings.watermark_fontsize_ratio" min="0.03" max="0.12" step="0.01" />
              <button class="num-btn" @click="inc('watermark_fontsize_ratio', 0.01)" :disabled="settings.watermark_fontsize_ratio >= 0.12">+</button>
            </div>
          </div>
          <div class="form-item">
            <div class="form-label">边距比例 (相对画面高)</div>
            <div class="input-row">
              <button class="num-btn" @click="dec('watermark_margin_ratio', 0.01, 0.01)" :disabled="settings.watermark_margin_ratio <= 0.01">−</button>
              <input class="form-input num-input" type="number" v-model.number="settings.watermark_margin_ratio" min="0.01" max="0.1" step="0.01" />
              <button class="num-btn" @click="inc('watermark_margin_ratio', 0.01)" :disabled="settings.watermark_margin_ratio >= 0.1">+</button>
            </div>
          </div>
          <div class="form-item">
            <div class="form-label">透明度 (0–1)</div>
            <div class="input-row">
              <button class="num-btn" @click="dec('watermark_alpha', 0.1, 0.1)" :disabled="settings.watermark_alpha <= 0.1">−</button>
              <input class="form-input num-input" type="number" v-model.number="settings.watermark_alpha" min="0.1" max="1" step="0.1" />
              <button class="num-btn" @click="inc('watermark_alpha', 0.1)" :disabled="settings.watermark_alpha >= 1">+</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref, onMounted } from 'vue'

const DEFAULT_API_KEY = 'sk-AOzSrTPz1GNuZR3XxJcEmhloPkvUAsMCZUWcUExotFdjOrAN'
const DEFAULT_BASE_URL = 'https://api.agnes-ai.cn/v1'

// 设置数据（默认与后端 Pydantic 默认一致，启动后会被服务器返回覆盖）
const settings = reactive({
  // 文本
  text_api_key: DEFAULT_API_KEY,
  text_base_url: DEFAULT_BASE_URL,
  text_model: 'agnes-2.5-flash',
  text_concurrency: 5,
  // 图片
  image_api_key: DEFAULT_API_KEY,
  image_base_url: DEFAULT_BASE_URL,
  image_model: 'agnes-image-2.1-flash',
  image_concurrency: 3,
  // 视频
  video_api_key: DEFAULT_API_KEY,
  video_base_url: DEFAULT_BASE_URL,
  video_model: 'agnes-video-v2.0',
  video_concurrency: 1,
  // 资源并发
  tts_concurrency: 3,
  subtitle_concurrency: 2,
  critic_concurrency: 5,
  // 质检阈值（前端命名 min_*, 后端字段是 *score_threshold）
  min_script_score: 7,
  min_image_score: 7,
  max_retries: 3,
  // 发布平台（全局默认；新建任务未指定时回退到此，用于多平台渲染）
  output_platforms: ['douyin', 'xiaohongshu', 'kuaishou', 'bilibili'],
  // 水印配置
  watermark_enabled: true,
  watermark_text: '昊康动漫',
  watermark_fontsize_ratio: 0.06,
  watermark_colour: 'white',
  watermark_alpha: 0.8,
  watermark_margin_ratio: 0.03,
  watermark_position: 'right_bottom',
})

const saving = ref(false)
const testing = ref('')
const saveHint = ref(null)
const testResults = reactive({ text: null, image: null, video: null })

// 平台全集（设置页"发布平台"与创建任务弹窗共用）
const allPlatforms = [
  { id: 'douyin', name: '抖音', icon: '🎵', ratio: '9:16' },
  { id: 'xiaohongshu', name: '小红书', icon: '📕', ratio: '3:4' },
  { id: 'kuaishou', name: '快手', icon: '⚡', ratio: '9:16' },
  { id: 'bilibili', name: 'B站', icon: '📺', ratio: '16:9' },
  { id: 'youtube', name: 'YouTube', icon: '▶️', ratio: '16:9' },
]

// 加减按钮（限制已在模板 :disabled 上做了，这里只包一层 clamp 防止越界）
const inc = (key, step = 1) => { settings[key] = Math.round((settings[key] + step) * 100) / 100 }
const dec = (key, step = 1, min = 1) => { settings[key] = Math.max(min, Math.round((settings[key] - step) * 100) / 100) }

const showHint = (text, kind = 'ok', ttlMs = 2500) => {
  saveHint.value = { text, kind }
  if (ttlMs > 0) setTimeout(() => { saveHint.value = null }, ttlMs)
}

// ---- 加载/保存 ----
const loadSettings = async () => {
  try {
    const resp = await fetch('/api/settings')
    if (!resp.ok) throw new Error('HTTP ' + resp.status)
    const data = await resp.json()
    Object.entries(data).forEach(([k, v]) => {
      if (k in settings) settings[k] = v
    })
    // 后端字段名映射：script_score_threshold → min_script_score, image_score_threshold → min_image_score
    if ('script_score_threshold' in data) settings.min_script_score = data.script_score_threshold
    if ('image_score_threshold' in data) settings.min_image_score = data.image_score_threshold
    showHint('已从服务器载入最新配置', 'ok', 1800)
  } catch (e) {
    showHint('载入失败，使用本地默认：' + e.message, 'warn', 4000)
  }
}

const saveSettings = async () => {
  saving.value = true
  // 构造后端期望的字段名映射
  const payload = { ...settings }
  payload.script_score_threshold = settings.min_script_score
  payload.image_score_threshold = settings.min_image_score
  delete payload.min_script_score
  delete payload.min_image_score
  try {
    const resp = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!resp.ok) throw new Error('HTTP ' + resp.status)
    const data = await resp.json()
    showHint(`✅ 已保存（${(data.saved_keys || []).length} 项）——重启后端仍保留`, 'ok', 3000)
    // 回写服务器可能做了字段清理后的对象
    Object.entries(data.settings || {}).forEach(([k, v]) => {
      if (k in settings) settings[k] = v
    })
    if ('script_score_threshold' in (data.settings || {})) settings.min_script_score = data.settings.script_score_threshold
    if ('image_score_threshold' in (data.settings || {})) settings.min_image_score = data.settings.image_score_threshold
  } catch (e) {
    showHint('❌ 保存失败：' + e.message, 'warn', 5000)
  } finally {
    saving.value = false
  }
}

const resetSettings = async () => {
  if (!confirm('确认恢复默认设置？（服务器侧的持久化也会清空）')) return
  saving.value = true
  try {
    const resp = await fetch('/api/settings/reset', { method: 'POST' })
    if (!resp.ok) throw new Error('HTTP ' + resp.status)
    const data = await resp.json()
    Object.entries(data.settings || {}).forEach(([k, v]) => {
      if (k in settings) settings[k] = v
    })
    settings.min_script_score = data.settings?.script_score_threshold ?? 7
    settings.min_image_score = data.settings?.image_score_threshold ?? 7
    testResults.text = null; testResults.image = null; testResults.video = null
    showHint('已重置为 Pydantic 默认值', 'ok')
  } catch (e) {
    showHint('❌ 重置失败：' + e.message, 'warn', 5000)
  } finally {
    saving.value = false
  }
}

// ---- 并发测试 ----
const testConcurrency = async (type) => {
  testing.value = type
  const concurrency = settings[`${type}_concurrency`]
  try {
    const resp = await fetch(`/api/settings/test-concurrency?type=${type}&concurrency=${concurrency}`)
    const data = await resp.json()
    testResults[type] = data
  } catch (e) {
    testResults[type] = { success: 0, failed: concurrency, total_time: 0, error: e.message }
  } finally {
    testing.value = ''
  }
}

onMounted(loadSettings)
</script>

<style scoped>
.model-configs {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-5);
  margin-bottom: var(--spacing-5);
}

.model-card {
  margin-bottom: 0;
}

.model-icon {
  font-size: 18px;
  margin-right: var(--spacing-1);
}

.model-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-weight: 500;
}

.tag-free {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.model-fields {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
}

.input-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-1);
}

.num-btn {
  width: 32px;
  height: 32px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-white);
  color: var(--color-text);
  font-size: 16px;
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s ease;
  flex-shrink: 0;
}

.num-btn:hover:not(:disabled) {
  background: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.num-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.num-input {
  width: 56px;
  text-align: center;
  padding: var(--spacing-2) var(--spacing-1);
  -moz-appearance: textfield;
}

.num-input::-webkit-outer-spin-button,
.num-input::-webkit-inner-spin-button {
  -webkit-appearance: none;
  margin: 0;
}

.btn-test {
  padding: var(--spacing-2) var(--spacing-3);
  background: var(--color-bg-white);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s ease;
  white-space: nowrap;
  margin-left: var(--spacing-2);
}

.btn-test:hover:not(:disabled) {
  background: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.btn-test:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.test-result {
  margin-top: var(--spacing-2);
  padding: var(--spacing-2) var(--spacing-3);
  border-radius: var(--radius-md);
  font-size: 13px;
  font-weight: 500;
}

.result-ok {
  background: var(--color-success-bg);
  color: var(--color-success);
  border: 1px solid rgba(46, 125, 50, 0.2);
}

.result-warn {
  background: var(--color-warning-bg);
  color: var(--color-warning);
  border: 1px solid rgba(230, 81, 0, 0.2);
}

.threshold-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-5);
}

.form-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-2);
}

.form-label {
  font-size: 13px;
  color: var(--color-text-secondary);
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
}

.inline-hint {
  font-size: 11px;
  font-weight: 400;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
}

.inline-hint.warn {
  background: var(--color-warning-bg);
  color: var(--color-warning);
}

.form-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  background: var(--color-bg-secondary);
  border-left: 3px solid var(--color-primary);
  padding: var(--spacing-2) var(--spacing-3);
  border-radius: var(--radius-sm);
  line-height: 1.6;
}

.form-hint code {
  background: rgba(139, 69, 19, 0.08);
  padding: 0 4px;
  border-radius: 3px;
  font-family: var(--font-mono);
  font-size: 11.5px;
}

.form-input {
  padding: var(--spacing-2) var(--spacing-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-white);
  color: var(--color-text);
  font-size: 14px;
  font-family: var(--font-sans);
  transition: all 0.2s ease;
}

.form-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px rgba(139, 69, 19, 0.1);
}

.btn {
  padding: var(--spacing-2) var(--spacing-5);
  border-radius: var(--radius-md);
  border: none;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.2s ease;
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-2);
  font-family: var(--font-sans);
}

.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-text-white);
}

.btn-primary:hover:not(:disabled) { background: var(--color-primary-hover); }

.btn-secondary {
  background: var(--color-bg-white);
  color: var(--color-text);
  border: 1px solid var(--color-border);
}

.btn-secondary:hover:not(:disabled) { background: var(--color-bg-secondary); }

.page-subtitle,
.section-sub {
  font-size: 13px;
  color: var(--color-text-secondary);
  margin: 4px 0 0;
}

.top-hint {
  padding: var(--spacing-3) var(--spacing-4);
  border-radius: var(--radius-md);
  font-size: 14px;
  font-weight: 500;
  margin-bottom: var(--spacing-4);
}

.top-hint.ok {
  background: var(--color-success-bg);
  color: var(--color-success);
  border: 1px solid rgba(46, 125, 50, 0.25);
}

.top-hint.warn {
  background: var(--color-warning-bg);
  color: var(--color-warning);
  border: 1px solid rgba(230, 81, 0, 0.25);
}

/* ===== 发布平台（设置页） ===== */
.platform-grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-3);
}
.platform-chip {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
  cursor: pointer;
  padding: var(--spacing-2) var(--spacing-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-white);
  font-size: 14px;
  transition: all 0.2s ease;
}
.platform-chip:hover { border-color: var(--color-primary-light); }
.platform-chip.is-on {
  border-color: var(--color-primary);
  background: var(--color-primary-bg);
  font-weight: 500;
  color: var(--color-primary);
}
.platform-chip input[type="checkbox"] { accent-color: var(--color-primary); }
.platform-chip .plat-ratio {
  margin-left: auto;
  font-size: 11px;
  font-weight: 600;
  color: #096dd9;
  background: #e6f7ff;
  padding: 1px 8px;
  border-radius: var(--radius-full);
}

/* ===== 水印配置（设置页） ===== */
.switch-inline {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
  font-size: 13px;
  color: var(--color-text-secondary);
  cursor: pointer;
}
.switch-inline input[type="checkbox"] { accent-color: var(--color-primary); width: 16px; height: 16px; }
.watermark-fields {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
  margin-top: var(--spacing-2);
}
.wm-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-4);
}
@media (max-width: 720px) {
  .wm-row { grid-template-columns: 1fr; }
}
</style>
