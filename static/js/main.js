/**
 * 应用入口：hash 路由 + 视图切换 + 页面状态。
 *
 * docs/04-frontend.md §1/§5：无构建原生 ES Module + petite-vue，
 * 视图通过 hash 路由切换；未登录时任何视图先落 #/login(§5.4)，
 * 登录成功后回跳原视图。
 */

import { createApp, reactive } from '../vendor/petite-vue.js'
import { getSession, isCurrentSession, onSessionChange, sessionTimeout } from './session.js'
import { api, connectPlanEvents } from './api.js?v=20260930b'
import { resetPlayer, player, loadEpisode, togglePlay, seek, seekToRatio, cycleRate, toggleTranscript, formatTime, nextEpisode, previousEpisode, dismissEndedPanel, replayFromEnded } from './player.js?v=20260930b'
import { store } from './store.js?v=20260930b'
import { useReview, isDue, formatDate, masteryLabels, directionNames } from './review.js?v=20260930b'

const DIRECTIONS = [
  { key: 'backend_arch', label: '后端/架构面试' },
  { key: 'ai_llm', label: 'AI/LLM 工程' },
  { key: 'tech_news', label: '前沿技术资讯' },
  { key: 'source_dive', label: '源码/原理深读' },
]

const STATUS_LABELS = {
  pending: '排队中',
  scripted: '脚本已完成，等待音频',
  tts_ready: '待合成',
  synth_partial: '合成中',
  ready: '可播放',
  failed: '失败',
  cancelled: '已取消',
}

const PLAN_STATUS_LABELS = {
  draft: '草稿',
  generating: '大纲生成中',
  active: '进行中',
  done: '已完成',
  failed: '生成失败',
  cancelled: '已取消',
}

const appState = reactive({
  // 启动先停在登录态(loading 遮罩),boot() 校验 token 后再进入真实路由
  route: 'login',
  params: {},
  toast: '',

  // 认证(docs/04-frontend.md §5.4):JWT 存 localStorage,401 统一踢回登录
  authReady: true, // 启动校验中,视图显示 loading
  authed: false,
  user: null, // { id, username }
  pendingHash: '', // 未登录被拦下的原视图,登录成功后回跳
  loginForm: { username: store.getUsername(), password: '' },
  loggingIn: false,
  loginError: '',

  // 首页
  plans: [],
  plansLoading: false,
  todayDueCount: 0,
  deletingPlanId: null,

  // 创建
  createForm: {
    title: '',
    direction: 'backend_arch',
    episodeCount: 6,
    targetMinutes: 15,
  },
  creating: false,
  lastCreateAt: 0,

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
  PLAN_STATUS_LABELS,
  masteryLabels,
  directionNames,

  /* ---------- getter ---------- */
  get inProgressPlans() {
    return this.plans
      .filter((p) => ['active', 'draft', 'generating', 'failed'].includes(p.status))
      .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))
  },

  planStatusLabel(plan) {
    return PLAN_STATUS_LABELS[plan?.status] || plan?.status || '进行中'
  },

  get hasActivePlayer() {
    return Boolean(this.player.episodeId)
  },

  /* 播放页选集面板:当前计划全部集按 seq 排序(含未就绪,置灰展示状态) */
  get sortedPlanEpisodes() {
    return (this.player.plan?.episodes || []).slice().sort((a, b) => a.seq - b.seq)
  },

  /* ---------- 路由 ---------- */
  parseHash(hash) {
    const raw = (hash || window.location.hash || '#/').replace(/^#/, '') || '/'
    const [path, query = ''] = raw.split('?')
    const parts = path.split('/').filter(Boolean)
    const q = Object.fromEntries(new URLSearchParams(query).entries())

    if (parts[0] === 'login') return { route: 'login', params: q }
    if (parts[0] === 'create') return { route: 'create', params: q }
    if (parts[0] === 'plan' && parts[1]) return { route: 'plan', params: { ...q, id: parts[1] } }
    if (parts[0] === 'play' && parts[1]) return { route: 'play', params: { ...q, episodeId: parts[1] } }
    if (parts[0] === 'review') return { route: 'review', params: q }
    return { route: 'home', params: q }
  },

  applyRoute() {
    if (this.authReady) return // 启动校验期间忽略路由变化,boot() 完成后统一应用
    const { route, params } = this.parseHash(window.location.hash)

    // 未登录:任何视图都先落登录视图,并记住来路待登录后回跳
    if (!this.authed) {
      if (route !== 'login') this.pendingHash = window.location.hash || '#/'
      this.route = 'login'
      this.params = {}
      return
    }

    // 已登录访问 #/login:回到原目的地(或首页)
    if (route === 'login') {
      const target = this.pendingHash && this.pendingHash !== '#/login' ? this.pendingHash : '/'
      this.pendingHash = ''
      this.go(target)
      return
    }

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
      const session = getSession()
      this.review.load().then(() => {
        if (!isCurrentSession(session)) return
        const targetId = params.section === 'queue'
          ? 'replay-queue'
          : params.section === 'questions'
            ? 'review-questions'
            : ''
        if (!targetId) return
        requestAnimationFrame(() => {
          if (!isCurrentSession(session)) return
          document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth' })
        })
      })
    }
  },

  go(path) {
    window.location.hash = path
  },

  /* ---------- 认证 ---------- */
  /** 启动校验:有 token 则 GET /api/auth/me 验活,失败/无 token 落登录视图 */
  async boot() {
    const session = getSession()
    this.authReady = true
    this.authed = false
    this.user = null
    this.loginError = ''

    const token = store.getToken()
    if (token) {
      try {
        const result = await api.getMe()
        if (!isCurrentSession(session)) return
        this.user = result
        this.authed = true
      } catch (err) {
        if (!isCurrentSession(session)) return
        // 401 时 api 层已清 token;网络异常保留 token,刷新后可重试
        this.user = null
        this.authed = false
        if (err.status !== 401) this.loginError = `无法校验登录状态：${err.message}`
      }
    }

    this.authReady = false
    this.applyRoute()
  },

  async submitLogin() {
    const session = getSession()
    const username = this.loginForm.username.trim()
    const password = this.loginForm.password
    if (!username || !password) {
      this.loginError = '请输入用户名和密码'
      return
    }
    if (this.loggingIn) return
    this.loggingIn = true
    this.loginError = ''
    try {
      const data = await api.login(username, password)
      if (!isCurrentSession(session)) return
      if (!data?.token || typeof data.token !== 'string') throw new Error('登录响应缺少凭证')
      store.setToken(data.token, data.user?.username || username)
      this.user = data.user || { id: null, username }
      this.authed = true
      this.loginForm.password = ''
      const target = this.pendingHash && this.pendingHash !== '#/login' ? this.pendingHash : '#/'
      this.pendingHash = ''
      if ((window.location.hash || '#/') === target) {
        this.applyRoute() // 目标即当前 hash(如首页)时 hashchange 不触发,手动应用
      } else {
        this.go(target)
      }
    } catch (err) {
      if (!isCurrentSession(session)) return
      this.loginError = err.message || '登录失败'
    } finally {
      if (!isCurrentSession(session)) return
      this.loggingIn = false
    }
  },

  /** 退出登录:停播/断开 SSE,清凭证回登录视图 */
  logout() {
    store.clearToken()
    this.pendingHash = ''
    this.go('/login')
  },

  /** api 层 401 广播:登录过期,统一踢回登录视图(幂等) */
  handleUnauthorized() {
    this.pendingHash = window.location.hash && window.location.hash !== '#/login' ? window.location.hash : ''
    // API 已使会话失效；也支持直接调用此入口。
    if (store.getToken()) store.clearToken()
    this.go('/login')
  },

  /* ---------- 工具 ---------- */
  showToast(msg) {
    this.toast = msg
    sessionTimeout(() => {
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
    const session = getSession()
    this.plansLoading = true
    try {
      const summaries = await api.listPlans()
      if (!isCurrentSession(session)) return
      const plans = await Promise.all(
        summaries.map(async (summary) => {
          try {
            return await api.getPlan(summary.id)
          } catch {
            if (!isCurrentSession(session)) return
            return summary
          }
        })
      )
      if (!isCurrentSession(session)) return
      this.plans = plans
      const cards = await api.getReviewToday().catch(() => [])
      if (!isCurrentSession(session)) return
      this.todayDueCount = Array.isArray(cards) ? cards.filter((c) => isDue(c.next_review_at)).length : 0
    } catch (err) {
      if (!isCurrentSession(session)) return
      this.showToast(err.message)
    } finally {
      if (!isCurrentSession(session)) return
      this.plansLoading = false
    }
  },

  continuePlan(plan) {
    const ready = plan.episodes
      ?.filter((e) => e.status === 'ready')
      .sort((a, b) => a.seq - b.seq)
    // 优先续听第一集"未听完"的;全部听完则回到第一集(从头复听)
    const ep = ready?.find((e) => e.listen_status !== 'done') || ready?.[0]
    if (ep) {
      this.go(`/play/${ep.id}?plan=${plan.id}`)
    } else {
      this.go(`/plan/${plan.id}`)
    }
  },

  /* ---------- 创建计划 ---------- */
  async submitCreate() {
    const session = getSession()
    const { title, direction, episodeCount, targetMinutes } = this.createForm
    if (!title.trim()) {
      this.showToast('请输入学习主题')
      return
    }
    // 防抖:请求在途(creating)或 1.5s 内的重复提交一律忽略;
    // 服务端另有同主题 generating 去重兜底,双击/连点不会产生重复计划
    const now = Date.now()
    if (this.creating || now - this.lastCreateAt < 1500) return
    this.lastCreateAt = now
    this.creating = true
    try {
      const topic = await api.createTopic(title.trim(), direction)
      if (!isCurrentSession(session)) return
      const plan = await api.createPlan(topic.id, Number(episodeCount), Number(targetMinutes))
      if (!isCurrentSession(session)) return
      this.createForm.title = ''
      this.showToast('已加入生成队列，大纲约需 1~5 分钟')
      this.go(`/plan/${plan.plan_id}`)
    } catch (err) {
      if (!isCurrentSession(session)) return
      this.showToast(err.message)
    } finally {
      if (!isCurrentSession(session)) return
      this.creating = false
    }
  },

  /* ---------- 计划详情 ---------- */
  async loadPlan(id) {
    const session = getSession()
    this.planLoading = true
    this.planError = ''
    try {
      const result = await api.getPlan(id)
      if (!isCurrentSession(session)) return
      this.plan = result
      this.startPlanSSE(id)
    } catch (err) {
      if (!isCurrentSession(session)) return
      this.planError = err.message
    } finally {
      if (!isCurrentSession(session)) return
      this.planLoading = false
    }
  },

  /* 只刷新详情数据,不重启 SSE(大纲生成完成/状态翻转时由 plan_status 事件触发) */
  async refreshPlan(id) {
    const session = getSession()
    try {
      const result = await api.getPlan(id)
      if (!isCurrentSession(session)) return
      this.plan = result
    } catch (err) {
      if (!isCurrentSession(session)) return
      console.warn('刷新计划详情失败', err)
    }
  },

  startPlanSSE(planId) {
    if (this.sse) this.sse.close()
    // EventSource 401 会静默循环重连且无法被 fetch 拦截:
    // 出错时探测一次 /api/auth/me,失效则由 api 层广播踢回登录视图
    const session = getSession()
    let probing = false
    this.sse = connectPlanEvents(planId, {
      onEpisode: (data) => this.handleEpisodeStatus(data),
      onPlan: (data) => this.handlePlanStatus(data),
      onError: () => {
        if (probing || !this.authed) return
        probing = true
        api.getMe().catch(() => {}).finally(() => {
          if (!isCurrentSession(session)) return
          sessionTimeout(() => {
            probing = false
          }, 3000)
        })
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
      sessionTimeout(() => {
        ep._highlight = false
      }, 3000)
    } else if (data.status === 'failed' && !ep.error) {
      this.refreshPlan(plan.id)
    }
  },

  handlePlanStatus(data) {
    if (!this.plan) return
    if (data.all_done != null) this.plan.all_done = data.all_done
    // 大纲异步生成完成(或失败)时计划状态翻转、集列表从无到有,拉详情刷新
    const episodeCount = this.plan.episodes?.length || 0
    if ((data.status && data.status !== this.plan.status) || (data.total || 0) > episodeCount) {
      this.refreshPlan(data.plan_id)
    }
  },

  async retryEpisode(id) {
    const session = getSession()
    try {
      await api.generateEpisode(id)
      if (!isCurrentSession(session)) return
      this.showToast('已加入生成队列')
    } catch (err) {
      if (!isCurrentSession(session)) return
      this.showToast(err.message)
    }
  },

  async retryFailedEpisodes(plan) {
    const session = getSession()
    try {
      await api.generatePlanAll(plan.id)
      if (!isCurrentSession(session)) return
      plan.episodes.forEach((e) => {
        if (e.status === 'failed') {
          e.status = 'pending'
          e.error = ''
        }
      })
      this.showToast('失败集已重新加入生成队列')
    } catch (err) {
      if (!isCurrentSession(session)) return
      this.showToast(err.message)
    }
  },

  async retryPlan(plan) {
    const session = getSession()
    try {
      await api.retryPlan(plan.id)
      if (!isCurrentSession(session)) return
      plan.status = 'generating'
      this.showToast('已重新生成大纲')
    } catch (err) {
      if (!isCurrentSession(session)) return
      this.showToast(err.message)
    }
  },

  /* 永久删除计划:停止执行中任务,彻底删除全部脚本/音频/题库与文件 */
  async deletePlan(plan) {
    const session = getSession()
    const ok = window.confirm(
      `确定永久删除计划「${plan.title}」？\n\n警告：将立即停止当前任务，并彻底删除该计划的全部脚本、音频、自测题、复习重听与播放进度，不可恢复！`
    )
    if (!ok) return
    this.deletingPlanId = plan.id
    try {
      await api.deletePlan(plan.id)
      if (!isCurrentSession(session)) return
      this.showToast('计划及全部内容已永久删除')
      if (this.plan && String(this.plan.id) === String(plan.id)) {
        if (this.sse) {
          this.sse.close()
          this.sse = null
        }
        this.plan = null
        this.go('/')
      }
      if (this.player.planId && String(this.player.planId) === String(plan.id)) {
        resetPlayer()
      }
      await this.loadPlans()
      if (!isCurrentSession(session)) return
    } catch (err) {
      if (!isCurrentSession(session)) return
      this.showToast(err.message)
    } finally {
      if (!isCurrentSession(session)) return
      this.deletingPlanId = null
    }
  },

  /* 勘误重生成(docs/07 §5):旧产物自动备份,确认时填写原因入勘误史 */
  async regenerateEpisode(id) {
    const session = getSession()
    const reason = window.prompt('重新生成本集,原因(记入勘误历史,可留空):')
    if (reason === null) return
    try {
      await api.regenerateEpisode(id, reason.trim())
      if (!isCurrentSession(session)) return
      this.showToast('已备份旧产物并重新入队')
    } catch (err) {
      if (!isCurrentSession(session)) return
      this.showToast(err.message)
    }
  },

  playFromPlan(epId) {
    if (!this.plan) return
    loadEpisode(epId, this.plan)
    this.go(`/play/${epId}?plan=${this.plan.id}`)
  },

  /* 播放页选集:复用播放器里已加载的 plan,切集不整页刷新 */
  playEpisode(epId) {
    const plan = this.player.plan || this.plan
    if (!plan) return
    if (String(this.player.episodeId) !== String(epId)) {
      loadEpisode(epId, plan)
    }
    this.go(`/play/${epId}?plan=${plan.id}`)
  },

  /* ---------- 播放器 ---------- */
  async enterPlayer(episodeId, planId) {
    const session = getSession()
    if (String(player.state.episodeId) === String(episodeId)) return

    const plan = this.plan?.id == planId ? this.plan : null
    if (plan) {
      await loadEpisode(episodeId, plan)
      if (!isCurrentSession(session)) return
      return
    }

    if (!planId) {
      this.showToast('缺少计划信息')
      this.go('/')
      return
    }
    try {
      const fetched = await api.getPlan(planId)
      if (!isCurrentSession(session)) return
      this.plan = fetched
      await loadEpisode(episodeId, fetched)
      if (!isCurrentSession(session)) return
    } catch (err) {
      if (!isCurrentSession(session)) return
      this.showToast(err.message)
      this.go('/')
    }
  },

  /* 进度条按下即定位,按住拖动持续 seek(docs/04 §2) */
  onProgressDown(event) {
    const session = getSession()
    const wrap = event.currentTarget
    const seekFromEvent = (e) => {
      if (!isCurrentSession(session)) return
      const rect = wrap.getBoundingClientRect()
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
      seekToRatio(ratio)
    }
    seekFromEvent(event)
    const onMove = (e) => seekFromEvent(e)
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
      session.signal.removeEventListener('abort', onUp)
    }
    session.signal.addEventListener('abort', onUp, { once: true })
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
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
  nextEpisode,
  previousEpisode,
  dismissEndedPanel,
  replayFromEnded,
})

// 只保存可序列化的用户态默认值，不复制 getter、播放器和复习实例。
const defaults = {}
for (const [key, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(appState))) {
  if (!('value' in descriptor) || typeof descriptor.value === 'function' || ['player', 'review', 'pendingHash'].includes(key)) continue
  defaults[key] = descriptor.value
}
const initialState = JSON.stringify(defaults)
onSessionChange(() => {
  appState.sse?.close()
  Object.assign(appState, JSON.parse(initialState), {
    authReady: false, loginForm: { username: '', password: '' },
  })
})

window.addEventListener('drivecast:unauthorized', () => appState.handleUnauthorized())
window.addEventListener('hashchange', () => appState.applyRoute())
if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', () => appState.boot())
} else {
  appState.boot()
}

createApp(appState).mount('#app')

// 暴露到全局，方便调试
window.__DRIVECAST__ = appState
