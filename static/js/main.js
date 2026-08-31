/**
 * 应用入口：hash 路由 + 视图切换 + 页面状态。
 *
 * docs/04-frontend.md §1/§5：无构建原生 ES Module + petite-vue，
 * 5 个视图通过 hash 路由切换。
 */

import { createApp, reactive } from '../vendor/petite-vue.js'
import { api, connectPlanEvents } from './api.js'
import { player, loadEpisode, togglePlay, seek, seekToRatio, cycleRate, toggleTranscript, formatTime } from './player.js'
import { store } from './store.js'
import { useReview, isDue, formatDate, masteryLabels, directionNames } from './review.js'

const DIRECTIONS = [
  { key: 'backend_arch', label: '后端/架构面试' },
  { key: 'ai_llm', label: 'AI/LLM 工程' },
  { key: 'tech_news', label: '前沿技术资讯' },
  { key: 'source_dive', label: '源码/原理深读' },
]

const STATUS_LABELS = {
  pending: '排队中',
  scripted: '写脚本中',
  tts_ready: '待合成',
  synth_partial: '合成中',
  ready: '可播放',
  failed: '失败',
}

const appState = reactive({
  route: 'home',
  params: {},
  toast: '',

  // 首页
  plans: [],
  plansLoading: false,
  todayDueCount: 0,

  // 创建
  createForm: {
    title: '',
    direction: 'backend_arch',
    episodeCount: 6,
  },
  creating: false,

  // 计划详情
  plan: null,
  planLoading: false,
  planError: '',
  sse: null,

  // 播放器（单例状态）
  player: player.state,

  // 复习
  review: useReview(),

  // 常量/映射（模板中可直接用）
  DIRECTIONS,
  STATUS_LABELS,
  masteryLabels,
  directionNames,

  /* ---------- getter ---------- */
  get inProgressPlans() {
    return this.plans
      .filter((p) => p.status === 'active' || p.status === 'draft')
      .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))
  },

  get hasActivePlayer() {
    return Boolean(this.player.episodeId)
  },

  /* ---------- 路由 ---------- */
  parseHash(hash) {
    const raw = (hash || window.location.hash || '#/').replace(/^#/, '') || '/'
    const [path, query = ''] = raw.split('?')
    const parts = path.split('/').filter(Boolean)
    const q = Object.fromEntries(new URLSearchParams(query).entries())

    if (parts[0] === 'create') return { route: 'create', params: q }
    if (parts[0] === 'plan' && parts[1]) return { route: 'plan', params: { ...q, id: parts[1] } }
    if (parts[0] === 'play' && parts[1]) return { route: 'play', params: { ...q, episodeId: parts[1] } }
    if (parts[0] === 'review') return { route: 'review', params: q }
    return { route: 'home', params: q }
  },

  applyRoute() {
    const { route, params } = this.parseHash(window.location.hash)
    this.route = route
    this.params = params

    if (route !== 'plan' && this.sse) {
      this.sse.close()
      this.sse = null
    }

    if (route === 'home') this.loadPlans()
    if (route === 'plan' && params.id) this.loadPlan(params.id)
    if (route === 'play' && params.episodeId) this.enterPlayer(params.episodeId, params.plan)
    if (route === 'review') {
      this.review.load().then(() => {
        const targetId = params.section === 'queue'
          ? 'replay-queue'
          : params.section === 'questions'
            ? 'review-questions'
            : ''
        if (!targetId) return
        requestAnimationFrame(() => {
          document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth' })
        })
      })
    }
  },

  go(path) {
    window.location.hash = path
  },

  /* ---------- 工具 ---------- */
  showToast(msg) {
    this.toast = msg
    setTimeout(() => {
      if (this.toast === msg) this.toast = ''
    }, 2500)
  },

  planProgress(plan) {
    const total = plan?.episodes?.length || 0
    const ready = plan?.episodes?.filter((e) => e.status === 'ready').length || 0
    return { ready, total, percent: total ? Math.round((ready / total) * 100) : 0 }
  },

  listenedCount(plan) {
    return plan?.episodes?.filter((e) => e.listen_status === 'done').length || 0
  },

  /* ---------- 首页 ---------- */
  async loadPlans() {
    this.plansLoading = true
    try {
      const summaries = await api.listPlans()
      this.plans = await Promise.all(
        summaries.map(async (summary) => {
          try {
            return await api.getPlan(summary.id)
          } catch {
            return summary
          }
        })
      )
      const cards = await api.getReviewToday().catch(() => [])
      this.todayDueCount = Array.isArray(cards) ? cards.filter((c) => isDue(c.next_review_at)).length : 0
    } catch (err) {
      this.showToast(err.message)
    } finally {
      this.plansLoading = false
    }
  },

  continuePlan(plan) {
    const ready = plan.episodes
      ?.filter((e) => e.status === 'ready')
      .sort((a, b) => a.seq - b.seq)
    const ep = ready?.[0]
    if (ep) {
      this.go(`/play/${ep.id}?plan=${plan.id}`)
    } else {
      this.go(`/plan/${plan.id}`)
    }
  },

  /* ---------- 创建计划 ---------- */
  async submitCreate() {
    const { title, direction, episodeCount } = this.createForm
    if (!title.trim()) {
      this.showToast('请输入学习主题')
      return
    }
    this.creating = true
    try {
      const topic = await api.createTopic(title.trim(), direction)
      const plan = await api.createPlan(topic.id, Number(episodeCount), 15)
      this.createForm.title = ''
      this.showToast('计划创建成功，正在生成')
      this.go(`/plan/${plan.plan_id}`)
    } catch (err) {
      this.showToast(err.message)
    } finally {
      this.creating = false
    }
  },

  /* ---------- 计划详情 ---------- */
  async loadPlan(id) {
    this.planLoading = true
    this.planError = ''
    try {
      this.plan = await api.getPlan(id)
      this.startPlanSSE(id)
    } catch (err) {
      this.planError = err.message
    } finally {
      this.planLoading = false
    }
  },

  startPlanSSE(planId) {
    if (this.sse) this.sse.close()
    this.sse = connectPlanEvents(planId, {
      onEpisode: (data) => this.handleEpisodeStatus(data),
      onPlan: (data) => this.handlePlanStatus(data),
      onError: () => {
        // EventSource 自动重连，不弹 toast
      },
    })
  },

  handleEpisodeStatus(data) {
    const plan = this.plan
    if (!plan || !plan.episodes) return
    const ep = plan.episodes.find((e) => String(e.id) === String(data.episode_id))
    if (!ep) return

    ep.status = data.status
    if (data.stage) ep.stage = data.stage
    if (data.percent != null) ep.percent = data.percent
    if (data.duration_sec != null) ep.duration_sec = data.duration_sec

    if (data.status === 'ready') {
      ep._highlight = true
      setTimeout(() => {
        ep._highlight = false
      }, 3000)
    }
  },

  handlePlanStatus(data) {
    if (!this.plan) return
    if (data.all_done != null) this.plan.all_done = data.all_done
  },

  async retryEpisode(id) {
    try {
      await api.generateEpisode(id)
      this.showToast('已加入生成队列')
    } catch (err) {
      this.showToast(err.message)
    }
  },

  playFromPlan(epId) {
    if (!this.plan) return
    loadEpisode(epId, this.plan)
    this.go(`/play/${epId}?plan=${this.plan.id}`)
  },

  /* ---------- 播放器 ---------- */
  async enterPlayer(episodeId, planId) {
    if (String(player.state.episodeId) === String(episodeId)) return

    const plan = this.plan?.id == planId ? this.plan : null
    if (plan) {
      await loadEpisode(episodeId, plan)
      return
    }

    if (!planId) {
      this.showToast('缺少计划信息')
      this.go('/')
      return
    }
    try {
      const fetched = await api.getPlan(planId)
      this.plan = fetched
      await loadEpisode(episodeId, fetched)
    } catch (err) {
      this.showToast(err.message)
      this.go('/')
    }
  },

  onProgressClick(event) {
    const rect = event.currentTarget.getBoundingClientRect()
    const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width))
    seekToRatio(ratio)
  },

  /* ---------- 复习 ---------- */
  setReviewFilter(filter) {
    this.review.state.filter = filter
  },

  formatTime,
  formatDate,
  isDue,
  togglePlay,
  seek,
  cycleRate,
  toggleTranscript,
})

window.addEventListener('hashchange', () => appState.applyRoute())
window.addEventListener('DOMContentLoaded', () => appState.applyRoute())

createApp(appState).mount('#app')

// 暴露到全局，方便调试
window.__DRIVECAST__ = appState
