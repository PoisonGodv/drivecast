/**
 * 轻量本地状态持久化。
 *
 * docs/04-frontend.md §5.2：页面级模块内状态 + localStorage 持久化。
 * 这里只放跨页面需要保留的偏好与播放进度。
 */

const PREFIX = 'drivecast:'

export const store = {
  getPlaybackRate() {
    const v = localStorage.getItem(`${PREFIX}playbackRate`)
    return v ? parseFloat(v) : 1.25
  },

  setPlaybackRate(rate) {
    localStorage.setItem(`${PREFIX}playbackRate`, String(rate))
  },

  getPosition(episodeId) {
    const v = localStorage.getItem(`${PREFIX}pos:${episodeId}`)
    return v ? parseInt(v, 10) : 0
  },

  setPosition(episodeId, sec) {
    if (!episodeId || sec == null) return
    localStorage.setItem(`${PREFIX}pos:${episodeId}`, String(Math.floor(sec)))
  },

  getActiveEpisode() {
    return localStorage.getItem(`${PREFIX}activeEpisode`) || ''
  },

  setActiveEpisode(episodeId) {
    if (episodeId) localStorage.setItem(`${PREFIX}activeEpisode`, episodeId)
    else localStorage.removeItem(`${PREFIX}activeEpisode`)
  },

  getLastPlayedPlan() {
    const v = localStorage.getItem(`${PREFIX}lastPlan`)
    return v ? parseInt(v, 10) : 0
  },

  setLastPlayedPlan(planId) {
    if (planId) localStorage.setItem(`${PREFIX}lastPlan`, String(planId))
  },
}
