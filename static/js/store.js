/**
 * 轻量本地状态持久化。
 *
 * docs/04-frontend.md §5.2：页面级模块内状态 + localStorage 持久化。
 * 这里只放跨页面需要保留的偏好与播放进度，以及登录 token。
 */

import { resetSession } from './session.js'

const PREFIX = 'drivecast:'
const TOKEN_KEY = `${PREFIX}token`
const USER_KEY = `${PREFIX}user`

function readStorage(key) {
  try { return localStorage.getItem(key) || '' } catch { return '' }
}
let currentToken = readStorage(TOKEN_KEY)
let currentUsername = readStorage(USER_KEY)

export const store = {
  /* ---------- 登录凭证(用户名+密码 → JWT,docs/04 §5.4) ---------- */
  getToken() {
    return currentToken
  },

  setToken(token, username = '') {
    this.clearToken(false)
    currentToken = token || ''
    currentUsername = username || ''
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token)
      else localStorage.removeItem(TOKEN_KEY)
      if (username) localStorage.setItem(USER_KEY, String(username))
      else localStorage.removeItem(USER_KEY)
    } catch {
      // 隐私模式等写入失败不阻断登录主流程(本次会话内存态仍可用)
    }
  },

  clearToken(clearShared = true) {
    // 旧标签页只撤销自己的会话，不能删除另一页刚写入的新凭证。
    const ownsShared = readStorage(TOKEN_KEY) === currentToken && readStorage(USER_KEY) === currentUsername
    currentToken = ''
    currentUsername = ''
    resetSession()
    try {
      if (clearShared && ownsShared) {
        localStorage.removeItem(TOKEN_KEY)
        localStorage.removeItem(USER_KEY)
      }
      // 旧版未分账号的进度和冲刺清单不能带到下一位用户。
      for (const key of Object.keys(localStorage)) {
        if (key.startsWith(`${PREFIX}pos:`) || key.startsWith('drivecast.review.sprint.completed.')) {
          localStorage.removeItem(key)
        }
      }
    } catch {
      // 同上
    }
  },

  /* 缓存的用户名:登录页/头部展示用,权威以 GET /api/auth/me 为准 */
  getUsername() {
    return currentUsername
  },

  /* ---------- 播放偏好 ---------- */
  getPlaybackRate() {
    const v = readStorage(`${PREFIX}playbackRate`)
    return v ? parseFloat(v) : 1.25
  },

  setPlaybackRate(rate) {
    try { localStorage.setItem(`${PREFIX}playbackRate`, String(rate)) } catch { /* 隐私模式 */ }
  },

  getPosition(episodeId) {
    const v = readStorage(`${PREFIX}pos:${encodeURIComponent(currentUsername)}:${episodeId}`)
    return v ? parseInt(v, 10) : 0
  },

  setPosition(episodeId, sec) {
    if (!episodeId || sec == null) return
    if (!currentToken) return
    try {
      localStorage.setItem(`${PREFIX}pos:${encodeURIComponent(currentUsername)}:${episodeId}`, String(Math.floor(sec)))
    } catch { /* 隐私模式 */ }
  },
}

// 另一标签页换号/退出也必须卸载当前标签页的音频和用户态。
window.addEventListener('storage', (event) => {
  if (event.key !== null && event.key !== TOKEN_KEY && event.key !== USER_KEY) return
  const token = readStorage(TOKEN_KEY)
  const username = readStorage(USER_KEY)
  if (token === currentToken && username === currentUsername) return
  // 不自动信任另一页的新账号，要求重新登录/验活。
  store.clearToken(false)
  window.dispatchEvent(new CustomEvent('drivecast:unauthorized'))
})
