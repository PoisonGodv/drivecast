/**
 * API 封装层。
 *
 * 端点与错误体以 docs/02-architecture.md §4 为权威；
 * 所有 fetch 统一处理 JSON 与网络错误，抛出 Error(message)。
 */

const API_BASE = '' // 同源，FastAPI StaticFiles 与 API 同 host

async function request(path, options = {}) {
  const url = `${API_BASE}${path}`
  const init = {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  }
  if (init.body && typeof init.body === 'object') {
    init.body = JSON.stringify(init.body)
  }

  let res
  try {
    res = await fetch(url, init)
  } catch (err) {
    throw new Error(`网络异常：${err.message || '无法连接后端'}`)
  }

  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = await res.json()
      const detail = body.message || body.detail
      msg = typeof detail === 'object' ? detail?.message || msg : detail || msg
    } catch {
      // 忽略非 JSON 错误体
    }
    const error = new Error(msg)
    error.status = res.status
    throw error
  }

  if (res.status === 204) return null
  return res.json()
}

export const api = {
  // 主题 / 计划
  createTopic: (title, direction) =>
    request('/api/topics', {
      method: 'POST',
      body: { title, direction },
    }),

  createPlan: (topicId, episodeCount, targetMinutes = 15) =>
    request('/api/plans', {
      method: 'POST',
      body: { topic_id: topicId, episode_count: episodeCount, target_minutes: targetMinutes },
    }),

  listPlans: () => request('/api/plans'),

  getPlan: (id) => request(`/api/plans/${id}`),

  // 单集生成与重试
  generateEpisode: (id) =>
    request(`/api/episodes/${id}/generate`, { method: 'POST' }),

  // 脚本 / 音频 / 进度
  getScript: (id) => request(`/api/episodes/${id}/script`),

  getAudioMeta: (id) => request(`/api/episodes/${id}/audio/meta`),

  postProgress: (id, positionSec) =>
    request(`/api/episodes/${id}/progress`, {
      method: 'POST',
      body: { position_sec: positionSec },
    }),

  // 复习
  getReviewToday: () => request('/api/review/today'),

  getReviewWeakPoints: () => request('/api/review/weak-points'),

  getReviewReplayQueue: () => request('/api/review/replay-queue'),

  getInterviewDate: () => request('/api/review/interview-date'),

  setInterviewDate: (interviewDate) =>
    request('/api/review/interview-date', {
      method: 'PUT',
      body: { interview_date: interviewDate || null },
    }),

  getReviewSprint: () => request('/api/review/sprint'),

  gradeQuestion: (id, mastery) =>
    request(`/api/questions/${id}/grade`, {
      method: 'POST',
      body: { mastery },
    }),
}

export function connectPlanEvents(planId, handlers) {
  /**
   * SSE 连接，docs/04-frontend.md §3.2。
   * 返回 { close() }，由调用方在页面卸载时关闭。
   */
  const url = `${API_BASE}/api/plans/${planId}/events`
  const es = new EventSource(url)

  es.addEventListener('episode_status', (e) => {
    try {
      handlers.onEpisode(JSON.parse(e.data))
    } catch (err) {
      console.warn('SSE episode_status 解析失败', err)
    }
  })

  es.addEventListener('plan_status', (e) => {
    try {
      handlers.onPlan(JSON.parse(e.data))
    } catch (err) {
      console.warn('SSE plan_status 解析失败', err)
    }
  })

  es.onerror = (err) => {
    console.warn('SSE error', err)
    if (handlers.onError) handlers.onError(err)
  }

  return {
    close: () => {
      es.close()
    },
  }
}
