import axios from 'axios'
import { Message } from '@arco-design/web-vue'

// 创建 axios 实例
const apiClient = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    // 可以在这里添加 token 等
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    return response.data
  },
  (error) => {
    // 统一错误处理
    let message = '请求失败'
    
    if (error.response) {
      // 服务器返回错误
      const { status, data } = error.response
      message = data?.error || data?.message || `服务器错误 (${status})`
      
      if (status === 404) {
        message = '资源不存在'
      } else if (status === 500) {
        message = '服务器内部错误'
      }
    } else if (error.request) {
      // 请求未发出
      message = '网络连接失败，请检查网络'
    } else {
      // 请求配置错误
      message = error.message || '请求配置错误'
    }
    
    // 显示错误提示
    Message.error(message)
    
    console.error('API 错误:', error)
    return Promise.reject(error)
  }
)

// 热搜 API
export const getHotspots = async (platforms = ['weibo', 'douyin', 'kuaishou'], force = false) => {
  const params = { platforms: platforms.join(',') }
  if (force) params.force = 'true'
  const response = await apiClient.get('/hotspots/', { params })
  return response
}

// 诗词 API
export const getPoems = async (params) => {
  const response = await apiClient.get('/poems/', { params })
  return response
}

export const getPoem = async (id) => {
  const response = await apiClient.get(`/poems/${id}`)
  return response
}

export const getPoemStats = async () => {
  const response = await apiClient.get('/poems/stats')
  return response
}

// 任务 API
export const getTasks = async (params) => {
  const response = await apiClient.get('/tasks/', { params })
  return response
}

export const getTask = async (id) => {
  const response = await apiClient.get(`/tasks/${id}`)
  return response
}

export const createTask = async (poemId, platforms = ['douyin']) => {
  // platforms: 数组，例 ['douyin','xiaohongshu','kuaishou','bilibili']
  // 后端 Query(list[str]) 接收；为空数组时回退全局 settings.output_platforms
  // 用 indices:false 序列化为 platforms=a&platforms=b（FastAPI 方能解析为列表）
  const params = { poem_id: poemId }
  if (Array.isArray(platforms) && platforms.length > 0) {
    params.platforms = platforms
  }
  const response = await apiClient.post('/tasks/', null, {
    params,
    paramsSerializer: { indices: false },
  })
  return response
}

export const startTask = async (id) => {
  const response = await apiClient.post(`/tasks/${id}/start`)
  return response
}

export const deleteTask = async (id) => {
  const response = await apiClient.delete(`/tasks/${id}`)
  return response
}

// 审核任务（approve/reject）
export const reviewTask = async (id, action, comment) => {
  const response = await apiClient.post(`/tasks/${id}/review`, null, {
    params: { action, comment }
  })
  return response
}

// 重新生成（重跑指定 stage）
export const regenerateTask = async (id, stage = 'script') => {
  const response = await apiClient.post(`/tasks/${id}/regenerate`, null, {
    params: { stage }
  })
  return response
}

// 发布任务到平台
export const publishTask = async (id, platforms = ['douyin']) => {
  const response = await apiClient.post(`/tasks/${id}/publish`, null, {
    params: { platforms },
    paramsSerializer: { indices: false },
  })
  return response
}

// WebSocket 连接
export const createProgressWebSocket = (taskId, onMessage) => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${protocol}//${window.location.host}/ws/progress/${taskId}`
  
  let ws = null
  let reconnectAttempts = 0
  const maxReconnectAttempts = 5
  
  const connect = () => {
    ws = new WebSocket(wsUrl)
    
    ws.onopen = () => {
      console.log('WebSocket 已连接:', taskId)
      reconnectAttempts = 0
    }
    
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        onMessage(data)
      } catch (e) {
        console.error('WebSocket 消息解析错误:', e)
      }
    }
    
    ws.onerror = (error) => {
      console.error('WebSocket 错误:', error)
    }
    
    ws.onclose = (event) => {
      console.log('WebSocket 已断开:', event.code)
      
      // 尝试重连
      if (reconnectAttempts < maxReconnectAttempts) {
        reconnectAttempts++
        setTimeout(connect, 1000 * reconnectAttempts)
      }
    }
  }
  
  connect()
  
  return {
    close: () => {
      if (ws) {
        ws.close()
      }
    },
    send: (data) => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(data)
      }
    }
  }
}

// 设置 API
export const getSettings = async () => {
  const response = await apiClient.get('/settings')
  return response
}

export const updateSettings = async (data) => {
  const response = await apiClient.post('/settings', data)
  return response
}

export const testConcurrency = async (type, concurrency) => {
  const response = await apiClient.get('/settings/test-concurrency', {
    params: { type, concurrency }
  })
  return response
}

// 导出默认对象
export default {
  getHotspots,
  getPoems,
  getPoem,
  getPoemStats,
  getTasks,
  getTask,
  createTask,
  startTask,
  deleteTask,
  reviewTask,
  regenerateTask,
  publishTask,
  createProgressWebSocket,
  getSettings,
  updateSettings,
  testConcurrency,
}
