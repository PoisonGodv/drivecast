/**
 * 全局单例播放器。
 *
 * docs/04-frontend.md §2：唯一碰 <audio> 与 Media Session 的地方，
 * 跨页面保持播放（路由切换不销毁 audio）。
 */

import { reactive } from '../vendor/petite-vue.js'
import { api } from './api.js?v=20260911'
import { store } from './store.js?v=20260911'

export const playbackRates = [0.75, 1, 1.25, 1.5, 2]

export const player = {
  state: reactive({
    episodeId: '',
    planId: 0,
    plan: null,
    episode: null,
    playing: false,
    currentTime: 0,
    duration: 0,
    buffered: 0,
    playbackRate: store.getPlaybackRate(),
    transcriptOpen: false,
    script: null,
    scriptLoading: false,
    audioMetaLoading: false,
    audioMetaLoaded: false,
    chapters: [],
    currentChapterIndex: -1,
    quality: null,
    durationEstimate: null,
    downloadUrl: '',
    sizeBytes: 0,
    jumpToChapter: (startSec) => seekToTime(startSec),
    error: '',
  }),

  audio: new Audio(),
  progressTimer: null,
}

const audio = player.audio

function formatTime(totalSeconds) {
  if (!isFinite(totalSeconds) || totalSeconds < 0) return '0:00'
  const s = Math.floor(totalSeconds)
  const m = Math.floor(s / 60)
  const rem = s % 60
  const h = Math.floor(m / 60)
  if (h > 0) return `${h}:${String(m % 60).padStart(2, '0')}:${String(rem).padStart(2, '0')}`
  return `${m}:${String(rem).padStart(2, '0')}`
}

export { formatTime }

function updateMediaSessionPosition() {
  if ('mediaSession' in navigator && navigator.mediaSession.setPositionState) {
    try {
      navigator.mediaSession.setPositionState({
        duration: audio.duration || 0,
        playbackRate: audio.playbackRate || 1,
        position: audio.currentTime || 0,
      })
    } catch {
      // Safari 某些版本会抛，忽略
    }
  }
}

function setMediaMetadata() {
  if (!('mediaSession' in navigator)) return
  const ep = player.state.episode
  const plan = player.state.plan
  navigator.mediaSession.metadata = new MediaMetadata({
    title: ep?.title || 'DriveCast',
    artist: plan?.title || '通勤技术播客',
    album: 'DriveCast',
    artwork: [{ src: '/static/assets/cover.png', sizes: '512x512', type: 'image/png' }],
  })
}

function installMediaSessionHandlers() {
  if (!('mediaSession' in navigator)) return
  navigator.mediaSession.setActionHandler('play', () => play())
  navigator.mediaSession.setActionHandler('pause', () => pause())
  navigator.mediaSession.setActionHandler('seekbackward', () => seek(-15))
  navigator.mediaSession.setActionHandler('seekforward', () => seek(15))
  navigator.mediaSession.setActionHandler('previoustrack', previousEpisode)
  navigator.mediaSession.setActionHandler('nexttrack', nextEpisode)
}

installMediaSessionHandlers()

function saveProgress() {
  const sec = Math.floor(audio.currentTime || 0)
  const id = player.state.episodeId
  if (!id) return
  store.setPosition(id, sec)
  api.postProgress(id, sec).catch((err) => {
    console.warn('进度上报失败', err)
  })
}

function startProgressTimer() {
  stopProgressTimer()
  player.progressTimer = setInterval(() => {
    saveProgress()
  }, 10000)
}

function stopProgressTimer() {
  if (player.progressTimer) {
    clearInterval(player.progressTimer)
    player.progressTimer = null
  }
}

function play() {
  if (!player.state.episodeId) return
  audio.play().catch((err) => {
    // 无手势自动播放被浏览器策略拒绝(NotAllowedError)不是故障:
    // 页面已就绪,用户点 ▶ 即可,不该显示吓人的"播放失败"。
    if (err && (err.name === 'NotAllowedError' || err.name === 'AbortError')) return
    player.state.error = `播放失败：${err.message}`
  })
}

function pause() {
  audio.pause()
}

export function seek(offsetSec) {
  if (!player.state.episodeId) return
  const t = Math.max(0, Math.min(audio.duration || 0, audio.currentTime + offsetSec))
  audio.currentTime = t
  player.state.currentTime = t
  updateCurrentChapter(t)
  updateMediaSessionPosition()
}

export function seekToRatio(ratio) {
  if (!audio.duration) return
  seekToTime(audio.duration * ratio)
}

export function seekToTime(timeSec) {
  if (!player.state.episodeId) return
  const duration = audio.duration || player.state.duration || 0
  const requested = Number(timeSec)
  if (!Number.isFinite(requested)) return
  const t = Math.max(0, duration ? Math.min(duration, requested) : requested)
  audio.currentTime = t
  player.state.currentTime = t
  updateCurrentChapter(t)
  updateMediaSessionPosition()
}

function updateCurrentChapter(currentTime = audio.currentTime || 0) {
  const chapters = player.state.chapters || []
  let current = -1
  for (let index = 0; index < chapters.length; index += 1) {
    if (currentTime + 0.05 < chapters[index].start_sec) break
    current = index
  }
  player.state.currentChapterIndex = current
}

async function loadAudioMeta(episodeId) {
  player.state.audioMetaLoading = true
  player.state.audioMetaLoaded = false
  try {
    const meta = await api.getAudioMeta(episodeId)
    if (String(player.state.episodeId) !== String(episodeId)) return
    player.state.chapters = Array.isArray(meta.chapters) ? meta.chapters : []
    player.state.quality = meta.quality || null
    player.state.durationEstimate = meta.duration_estimate || null
    player.state.downloadUrl = meta.download_url || `/api/episodes/${episodeId}/audio/download`
    player.state.sizeBytes = Number(meta.size_bytes) || 0
    player.state.audioMetaLoaded = true
    if (Number(meta.duration_sec) > 0 && !audio.duration) {
      player.state.duration = Number(meta.duration_sec)
    }
    updateCurrentChapter(player.state.currentTime)
  } catch (err) {
    if (String(player.state.episodeId) !== String(episodeId)) return
    // 元数据是增强信息,失败不阻断播放和旧音频使用。
    console.warn('音频元数据加载失败', err)
    player.state.downloadUrl = `/api/episodes/${episodeId}/audio/download`
  } finally {
    if (String(player.state.episodeId) === String(episodeId)) {
      player.state.audioMetaLoading = false
    }
  }
}

export function togglePlay() {
  if (player.state.playing) pause()
  else play()
}

export function cycleRate() {
  const idx = playbackRates.indexOf(player.state.playbackRate)
  const next = playbackRates[(idx + 1) % playbackRates.length]
  player.state.playbackRate = next
  audio.playbackRate = next
  store.setPlaybackRate(next)
  updateMediaSessionPosition()
}

export function toggleTranscript() {
  player.state.transcriptOpen = !player.state.transcriptOpen
  if (player.state.transcriptOpen && !player.state.script && player.state.episodeId) {
    player.state.scriptLoading = true
    api.getScript(player.state.episodeId)
      .then((s) => {
        player.state.script = s
      })
      .catch((err) => {
        player.state.script = { content_md: `文稿加载失败：${err.message}` }
      })
      .finally(() => {
        player.state.scriptLoading = false
      })
  }
}

function findEpisodeById(id) {
  return player.state.plan?.episodes?.find((e) => String(e.id) === String(id))
}

function sortedReadyEpisodes() {
  return (player.state.plan?.episodes || [])
    .filter((e) => e.status === 'ready')
    .sort((a, b) => a.seq - b.seq)
}

function nextEpisode() {
  const current = player.state.episode
  if (!current || !player.state.plan) return
  const ready = sortedReadyEpisodes()
  const next = ready.find((e) => e.seq > current.seq)
  if (next) loadEpisode(next.id, player.state.plan)
  else pause()
}

function previousEpisode() {
  const current = player.state.episode
  if (!current || !player.state.plan) return
  const ready = sortedReadyEpisodes()
  const prev = ready
    .slice()
    .reverse()
    .find((e) => e.seq < current.seq)
  if (prev) loadEpisode(prev.id, player.state.plan)
  else {
    audio.currentTime = 0
    player.state.currentTime = 0
    updateMediaSessionPosition()
  }
}

export async function loadEpisode(episodeId, plan) {
  /**
   * 加载并准备播放一集。
   * plan 对象须包含 episodes 数组（docs/02 §4 详情契约）。
   */
  if (!episodeId) return
  const ep = plan?.episodes?.find((e) => String(e.id) === String(episodeId))
  if (!ep) {
    player.state.error = '未找到该集'
    return
  }

  player.state.error = ''
  player.state.episodeId = String(episodeId)
  player.state.planId = plan?.id || 0
  player.state.plan = plan
  player.state.episode = ep
  player.state.script = null
  player.state.transcriptOpen = false
  player.state.audioMetaLoading = false
  player.state.audioMetaLoaded = false
  player.state.chapters = []
  player.state.currentChapterIndex = -1
  player.state.quality = null
  player.state.durationEstimate = null
  player.state.downloadUrl = `/api/episodes/${episodeId}/audio/download`
  player.state.sizeBytes = 0

  const saved = store.getPosition(String(episodeId))
  let start = Number(ep.position_sec) || saved || 0
  // 历史脏数据可能 position >= duration(旧版上报未钳制):直接顶到末尾会
  // metadata 加载完立即触发 ended 秒跳下一集,表现为"点了一下就跳走"。已听完的集从头播。
  const epDuration = Number(ep.duration_sec) || 0
  if (epDuration > 0 && start >= epDuration - 1) start = 0
  start = Math.max(0, start)

  audio.src = `/api/episodes/${episodeId}/audio`
  audio.playbackRate = player.state.playbackRate
  audio.currentTime = start
  player.state.currentTime = start
  player.state.duration = epDuration

  setMediaMetadata()
  updateMediaSessionPosition()
  void loadAudioMeta(episodeId)

  play()
}

// audio 事件绑定
audio.addEventListener('play', () => {
  player.state.playing = true
  startProgressTimer()
})

audio.addEventListener('pause', () => {
  player.state.playing = false
  stopProgressTimer()
  saveProgress()
})

audio.addEventListener('timeupdate', () => {
  player.state.currentTime = audio.currentTime || 0
  updateCurrentChapter(player.state.currentTime)
  if (audio.buffered.length) {
    player.state.buffered = audio.buffered.end(audio.buffered.length - 1)
  }
})

audio.addEventListener('durationchange', () => {
  player.state.duration = audio.duration || 0
})

audio.addEventListener('ended', () => {
  saveProgress()
  nextEpisode()
})

audio.addEventListener('error', (e) => {
  player.state.error = `音频加载失败：${audio.error?.message || '未知错误'}`
})

// 锁屏进度节流上报（每 5s 一次）
let lastPositionReport = 0
audio.addEventListener('timeupdate', () => {
  const now = Date.now()
  if (now - lastPositionReport < 5000) return
  lastPositionReport = now
  updateMediaSessionPosition()
})
