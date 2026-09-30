/**
 * API 封装层。
 *
 * 端点与错误体以 docs/02-architecture.md §4 为权威；
 * 所有 fetch 统一处理 JSON 与网络错误，抛出 Error(message)。
 *
 * 登录凭证(docs/04-frontend.md §5.4)：token 存 localStorage(drivecast:token)，
 * request() 统一注入 Authorization: Bearer 头；收到 401 时清 token 并广播
 * drivecast:unauthorized 事件，由 main.js 切到登录视图(登录请求自身豁免)。
 * EventSource / <audio> 无法自定义请求头，这两类 GET 端点用 withToken() 拼 ?token=。
 */

import { getSession, assertSession, isCurrentSession } from './session.js'
import { store } from './store.js?v=20260930c'

const API_BASE = '' // 同源，FastAPI StaticFiles 与 API 同 host
const LOGIN_PATH = '/api/auth/login'

/** 401 统一广播：main.js 监听后清应用态并切登录视图 */
function notifyUnauthorized() {
  window.dispatchEvent(new CustomEvent('drivecast:unauthorized'))
}

async function request(path, options = {}) {
  const session = getSession()
  const url = `${API_BASE}${path}`
  const init = {
    ...options,
    signal: session.signal,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  }
  if (init.body && typeof init.body === 'object') {
    init.body = JSON.stringify(init.body)
  }
  const token = store.getToken()
  if (token) init.headers.Authorization = `Bearer ${token}`

  let res
  try {
    res = await fetch(url, init)
  } catch (err) {
    assertSession(session)
    throw new Error(`网络异常：${err.message || '无法连接后端'}`)
  }

  assertSession(session)
  if (!res.ok) {
    // 登录过期/无效：清凭证并通知切登录视图；登录请求自身的 401 豁免(防死循环)
    if (res.status === 401 && path !== LOGIN_PATH) {
      store.clearToken()
      notifyUnauthorized()
    }
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
  const body = await res.json()
  assertSession(session)
  return body
}

/**
 * 给无法携带 Authorization 头的 URL(SSE EventSource / <audio> src / <a download)
 * 追加 ?token= 查询参数。仅对本站相对路径生效，token 不外带到外域。
 */
export function withToken(url) {
  if (!url || !url.startsWith('/') || url.startsWith('//') || url.includes('\\')) return url
  const token = store.getToken()
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
}

export const api = {
  // 登录 / 当前用户(docs/04-frontend.md §5.4)
  login: (username, password) =>
    request(LOGIN_PATH, {
      method: 'POST',
      body: { username, password },
    }),

  getMe: () => request('/api/auth/me'),

  getAuthStatus: () => request('/api/auth/status'),

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

  // 今日额度与用量:minutes_limit/minutes_remaining 为 null 表示不限额
  getUsage: () => request('/api/usage'),

  getPlan: (id) => request(`/api/plans/${id}`),

  // 永久删除计划:停止运行中任务、清理所有音频/脚本/题库/文件
  deletePlan: (id) =>
    request(`/api/plans/${id}`, { method: 'DELETE' }),

  // 单集生成与重试
  generateEpisode: (id) =>
    request(`/api/episodes/${id}/generate`, { method: 'POST' }),

  // 批量重试:已就绪集只做本地音频升级,失败/未生成集重新入队
  generatePlanAll: (id) =>
    request(`/api/plans/${id}/generate_all`, { method: 'POST' }),

  retryPlan: (id) =>
    request(`/api/plans/${id}/retry`, { method: 'POST' }),

  // 勘误重生成(docs/07 §5):备份旧产物后重新入队
  regenerateEpisode: (id, reason = '') =>
    request(`/api/episodes/${id}/regenerate`, {
      method: 'POST',
      body: { reason },
    }),

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

  // 遗忘曲线排期:今日新学集 + 到期/逾期/新题计数(docs 复习契约)
  getReviewDaily: () => request('/api/review/daily'),

  // 全局覆盖仪表盘:各方向已覆盖/未覆盖高频题
  getReviewCoverage: () => request('/api/review/coverage'),

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
   * EventSource 不能自定义请求头，用 ?token= 传递凭证(后端白名单 GET 端点)。
   * 返回 { close() }，由调用方在页面卸载时关闭。
   */
  const url = withToken(`${API_BASE}/api/plans/${planId}/events`)
  const session = getSession()
  assertSession(session)
  const es = new EventSource(url)
  let closed = false
  const close = () => {
    closed = true
    es.close()
    session.signal.removeEventListener('abort', close)
  }
  session.signal.addEventListener('abort', close, { once: true })

  es.addEventListener('episode_status', (e) => {
    if (closed || !isCurrentSession(session)) return
    try {
      handlers.onEpisode(JSON.parse(e.data))
    } catch (err) {
      console.warn('SSE episode_status 解析失败', err)
    }
  })

  es.addEventListener('plan_status', (e) => {
    if (closed || !isCurrentSession(session)) return
    try {
      handlers.onPlan(JSON.parse(e.data))
    } catch (err) {
      console.warn('SSE plan_status 解析失败', err)
    }
  })

  es.onerror = (err) => {
    if (closed || !isCurrentSession(session)) return
    console.warn('SSE error', err)
    if (handlers.onError) handlers.onError(err)
  }

  return {
    close,
  }
}
