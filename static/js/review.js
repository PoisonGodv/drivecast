/**
 * M5 复习闭环前端。
 *
 * 保持无构建 ES Module：今日到期排序、薄弱点重听推荐、面试日和三日冲刺
 * 都收拢在本模块，避免把复习页的接口细节扩散到 main.js。
 */

import { reactive } from '../vendor/petite-vue.js'
import { api } from './api.js?v=20260911'

const SPRINT_STORAGE_PREFIX = 'drivecast.review.sprint.completed.'

function listFrom(payload, keys = []) {
  if (Array.isArray(payload)) return payload
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload[key]
  }
  return []
}

function parseDate(value) {
  if (!value) return null
  const normalized = /^\d{4}-\d{2}-\d{2} /.test(value) ? value.replace(' ', 'T') : value
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? null : date
}

function localDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return parseDate(value)
  const [year, month, day] = value.split('-').map(Number)
  return new Date(year, month - 1, day)
}

function startOfToday() {
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return today
}

function dateInputValue(date = new Date()) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function dueSortValue(card) {
  const due = parseDate(card.next_review_at)
  return due ? due.getTime() : Number.NEGATIVE_INFINITY
}

function sortDueCards(cards) {
  const masteryOrder = { unknown: 0, fuzzy: 1, mastered: 2 }
  return [...cards].sort((a, b) => {
    const byDue = dueSortValue(a) - dueSortValue(b)
    if (byDue !== 0) return byDue
    const byMastery = (masteryOrder[a.mastery] ?? 0) - (masteryOrder[b.mastery] ?? 0)
    if (byMastery !== 0) return byMastery
    return Number(a.id || 0) - Number(b.id || 0)
  })
}

function normalizeRecommendation(item) {
  const episode = item.recommended_episode || item.episode || {}
  return {
    ...item,
    kp_id: item.kp_id || item.knowledge_point_id || '',
    kp_name: item.kp_name || item.knowledge_point_name || item.knowledge_point || '薄弱知识点',
    weak_question_count: Number(item.weak_question_count ?? item.weak_count ?? item.question_count ?? 0),
    episode_id: item.episode_id ?? episode.id ?? episode.episode_id ?? null,
    episode_title: item.episode_title || episode.title || episode.episode_title || '相关播客',
    plan_id: item.plan_id ?? episode.plan_id ?? null,
    plan_title: item.plan_title || episode.plan_title || '',
    duration_sec: Number(item.duration_sec ?? episode.duration_sec ?? 0),
  }
}

function sprintGroupsFrom(payload) {
  const directGroups = listFrom(payload, ['groups', 'directions'])
  if (directGroups.length) {
    return directGroups.map((group) => ({
      ...group,
      direction: group.direction || group.key || 'other',
      weak_question_count: Number(group.weak_question_count ?? group.question_count ?? 0),
      items: listFrom(group, ['items', 'episodes', 'checklist']).map(normalizeRecommendation),
    }))
  }

  if (payload?.directions && !Array.isArray(payload.directions)) {
    return Object.entries(payload.directions).map(([direction, value]) => {
      const group = Array.isArray(value) ? { items: value } : value || {}
      return {
        ...group,
        direction,
        weak_question_count: Number(group.weak_question_count ?? group.question_count ?? 0),
        items: listFrom(group, ['items', 'episodes', 'checklist']).map(normalizeRecommendation),
      }
    })
  }

  const items = listFrom(payload, ['items', 'checklist']).map(normalizeRecommendation)
  const grouped = new Map()
  for (const item of items) {
    const direction = item.direction || 'other'
    if (!grouped.has(direction)) {
      grouped.set(direction, { direction, weak_question_count: 0, items: [] })
    }
    const group = grouped.get(direction)
    group.items.push(item)
    group.weak_question_count += item.weak_question_count
  }
  return Array.from(grouped.values())
}

function sprintItemKey(item, direction, index) {
  return String(
    item.checklist_id ||
      item.id ||
      item.episode_id ||
      `${direction}:${item.episode_title || 'item'}:${index}`
  )
}

export function useReview() {
  const state = reactive({
    cards: [],
    recommendations: [],
    replayQueue: [],
    sprintGroups: [],
    sprintMeta: {},
    planIdsByTitle: {},
    interviewDate: '',
    savedInterviewDate: '',
    sprintCompleted: {},

    // 遗忘曲线排期:今日新学集与到期/逾期/新题摘要
    daily: { date: '', new_episodes: [], due_count: 0, overdue_count: 0, new_count: 0 },
    // 方向覆盖仪表盘
    coverage: [],
    coverageExpanded: {},

    loading: false,
    recommendationsLoading: false,
    replayQueueLoading: false,
    sprintLoading: false,
    dailyLoading: false,
    coverageLoading: false,
    dateSaving: false,
    error: '',
    recommendationsError: '',
    replayQueueError: '',
    sprintError: '',
    dailyError: '',
    coverageError: '',
    dateError: '',
    notice: '',

    filter: 'all', // 'all' | 'weak' | direction
    flipped: {},
    grading: {},

    get filteredCards() {
      let cards = this.cards
      if (this.filter === 'weak') {
        cards = cards.filter((card) => (card.mastery || 'unknown') !== 'mastered')
      } else if (this.filter !== 'all') {
        cards = cards.filter((card) => (card.direction || '') === this.filter)
      }
      return sortDueCards(cards)
    },

    get dueTodayCount() {
      return this.cards.filter((card) => isDue(card.next_review_at)).length
    },

    get overdueCount() {
      return this.cards.filter((card) => dueStatus(card).kind === 'overdue').length
    },

    get directions() {
      return Array.from(
        new Set([
          ...this.cards.map((card) => card.direction || ''),
          ...this.recommendations.map((item) => item.direction || ''),
          ...this.replayQueue.map((item) => item.direction || ''),
          ...this.sprintGroups.map((group) => group.direction || ''),
        ].filter(Boolean))
      )
    },

    get dateMin() {
      return dateInputValue()
    },

    get dateDirty() {
      return this.interviewDate !== this.savedInterviewDate
    },

    get daysUntilInterview() {
      if (!this.interviewDate) return null
      const target = localDate(this.interviewDate)
      if (!target) return null
      return Math.ceil((target.getTime() - startOfToday().getTime()) / 86400000)
    },

    get interviewHint() {
      const days = this.daysUntilInterview
      if (days == null) return '设置后会在考前 3 天生成按方向聚合的重听清单。'
      if (days < 0) return '面试日已过，请更新日期。'
      if (days === 0) return '面试就在今天，优先扫清不会和模糊的题。'
      if (days === 1) return '距面试 1 天，进入冲刺窗口。'
      if (days <= 3) return `距面试 ${days} 天，进入冲刺窗口。`
      return `距面试 ${days} 天，考前 3 天将进入冲刺窗口。`
    },

    get sprintItemCount() {
      return this.sprintGroups.reduce((sum, group) => sum + group.items.length, 0)
    },

    get sprintDoneCount() {
      return this.sprintGroups.reduce(
        (sum, group) =>
          sum + group.items.filter((item, index) => this.isSprintDone(item, group.direction, index)).length,
        0
      )
    },

    async load() {
      this.loading = true
      this.error = ''
      this.notice = ''
      try {
        const data = await api.getReviewToday()
        this.cards = sortDueCards(listFrom(data, ['items', 'questions']))
        await this.hydratePlanIds(this.cards)
        this.flipped = {}
      } catch (err) {
        this.error = err.message
      } finally {
        this.loading = false
      }

      await Promise.all([
        this.loadInterviewDate(),
        this.loadRecommendations(),
        this.loadReplayQueue(),
        this.loadDaily(),
        this.loadCoverage(),
      ])
      await this.loadSprint()
    },

    async loadDaily() {
      this.dailyLoading = true
      this.dailyError = ''
      try {
        const data = await api.getReviewDaily()
        // 旧后端/缺字段容错:逐项兜底,单个计数缺失不拖垮整个面板
        this.daily = {
          date: data?.date || '',
          new_episodes: listFrom(data, ['new_episodes']),
          due_count: Number(data?.due_count) || 0,
          overdue_count: Number(data?.overdue_count) || 0,
          new_count: Number(data?.new_count) || 0,
        }
      } catch (err) {
        this.daily = { date: '', new_episodes: [], due_count: 0, overdue_count: 0, new_count: 0 }
        this.dailyError = err.message
      } finally {
        this.dailyLoading = false
      }
    },

    async loadCoverage() {
      this.coverageLoading = true
      this.coverageError = ''
      try {
        const data = await api.getReviewCoverage()
        this.coverage = listFrom(data, ['directions']).map((dir) => ({
          direction: dir?.direction || 'other',
          name: dir?.name || '',
          total: Number(dir?.total) || 0,
          covered: Number(dir?.covered) || 0,
          // percent 钳制在 0~100,防止后端脏数据把进度条撑破
          percent: Math.max(0, Math.min(100, Number(dir?.percent) || 0)),
          uncovered_high: Array.isArray(dir?.uncovered_high) ? dir.uncovered_high : [],
        }))
      } catch (err) {
        this.coverage = []
        this.coverageError = err.message
      } finally {
        this.coverageLoading = false
      }
    },

    toggleCoverage(direction) {
      this.coverageExpanded[direction] = !this.coverageExpanded[direction]
    },

    async hydratePlanIds(items) {
      const missing = items.filter((item) => item.plan_id == null && item.plan_title)
      if (!missing.length) return items

      try {
        if (!Object.keys(this.planIdsByTitle).length) {
          const plans = await api.listPlans()
          const listed = listFrom(plans, ['items', 'plans'])
          for (const plan of listed) {
            if (!plan?.title || plan?.id == null) continue
            this.planIdsByTitle[plan.title] = plan.id
          }
          const unresolvedTitles = new Set(
            missing
              .filter((item) => this.planIdsByTitle[item.plan_title] == null)
              .map((item) => item.plan_title)
          )
          for (const plan of listed) {
            if (plan?.id == null || !unresolvedTitles.size) continue
            try {
              const detail = await api.getPlan(plan.id)
              if (detail?.title) this.planIdsByTitle[detail.title] = plan.id
              unresolvedTitles.delete(detail?.title)
            } catch {
              // 单个计划详情失败时继续补全其他计划。
            }
          }
        }
        for (const item of missing) {
          item.plan_id = this.planIdsByTitle[item.plan_title] ?? null
        }
      } catch {
        // 播放器仍可通过集卡来源提供的 plan_id；补全失败只隐藏播放入口。
      }
      return items
    },

    async loadRecommendations() {
      this.recommendationsLoading = true
      this.recommendationsError = ''
      try {
        const data = await api.getReviewWeakPoints()
        const items = listFrom(data, ['items', 'recommendations'])
          .map(normalizeRecommendation)
          .sort((a, b) => b.weak_question_count - a.weak_question_count)
        this.recommendations = await this.hydratePlanIds(items)
      } catch (err) {
        this.recommendations = []
        this.recommendationsError = err.message
      } finally {
        this.recommendationsLoading = false
      }
    },

    async loadReplayQueue() {
      this.replayQueueLoading = true
      this.replayQueueError = ''
      try {
        const data = await api.getReviewReplayQueue()
        const items = listFrom(data, ['items', 'queue'])
          .map(normalizeRecommendation)
          .sort((a, b) =>
            String(a.scheduled_for || '').localeCompare(String(b.scheduled_for || '')) ||
            b.weak_question_count - a.weak_question_count
          )
        this.replayQueue = await this.hydratePlanIds(items)
      } catch (err) {
        this.replayQueue = []
        this.replayQueueError = err.message
      } finally {
        this.replayQueueLoading = false
      }
    },

    async loadInterviewDate() {
      this.dateError = ''
      try {
        const data = await api.getInterviewDate()
        const value = data?.interview_date || data?.date || ''
        this.interviewDate = value ? String(value).slice(0, 10) : ''
        this.savedInterviewDate = this.interviewDate
        this.loadSprintCompletion()
      } catch (err) {
        this.dateError = err.message
      }
    },

    async saveInterviewDate() {
      if (this.interviewDate && !localDate(this.interviewDate)) {
        this.dateError = '请输入有效的面试日期'
        return
      }
      if (this.interviewDate && this.interviewDate < this.dateMin) {
        this.dateError = '面试日期不能早于今天'
        return
      }

      this.dateSaving = true
      this.dateError = ''
      try {
        const data = await api.setInterviewDate(this.interviewDate || null)
        const value = data?.interview_date ?? data?.date ?? this.interviewDate
        this.interviewDate = value ? String(value).slice(0, 10) : ''
        this.savedInterviewDate = this.interviewDate
        this.loadSprintCompletion()
        this.notice = this.interviewDate ? '面试日期已保存' : '已关闭面试冲刺'
        await this.loadSprint()
      } catch (err) {
        this.dateError = err.message
      } finally {
        this.dateSaving = false
      }
    },

    async clearInterviewDate() {
      this.interviewDate = ''
      await this.saveInterviewDate()
    },

    async loadSprint() {
      this.sprintLoading = true
      this.sprintError = ''
      try {
        const data = await api.getReviewSprint()
        if (!this.interviewDate) {
          const value = data?.interview_date || data?.date || ''
          if (value) {
            this.interviewDate = String(value).slice(0, 10)
            this.savedInterviewDate = this.interviewDate
            this.loadSprintCompletion()
          }
        }
        this.sprintMeta = data && !Array.isArray(data) ? data : {}
        const groups = sprintGroupsFrom(data).sort(
          (a, b) => b.weak_question_count - a.weak_question_count || b.items.length - a.items.length
        )
        await this.hydratePlanIds(groups.flatMap((group) => group.items))
        this.sprintGroups = groups
      } catch (err) {
        if (!this.savedInterviewDate && err.status === 409) {
          this.sprintGroups = []
          this.sprintMeta = {}
          this.sprintError = ''
        } else {
          this.sprintGroups = []
          this.sprintMeta = {}
          this.sprintError = err.message
        }
      } finally {
        this.sprintLoading = false
      }
    },

    toggleFlip(id) {
      this.flipped[id] = !this.flipped[id]
    },

    async grade(id, mastery) {
      if (this.grading[id]) return
      this.grading[id] = true
      this.error = ''
      try {
        const updated = await api.gradeQuestion(id, mastery)
        const index = this.cards.findIndex((card) => String(card.id) === String(id))
        if (index >= 0) {
          const card = { ...this.cards[index], ...updated }
          if (isDue(card.next_review_at)) {
            this.cards[index] = card
          } else {
            this.cards.splice(index, 1)
          }
        }
        this.flipped[id] = false
        this.notice = `已标记为${masteryLabels[mastery]}，下次按计划复习`
        void Promise.all([this.loadRecommendations(), this.loadReplayQueue(), this.loadSprint()])
      } catch (err) {
        this.error = err.message
      } finally {
        this.grading[id] = false
      }
    },

    playHref(item) {
      const direct = item.play_href || item.player_url
      if (typeof direct === 'string' && (direct.startsWith('#/') || direct.startsWith('/#/'))) {
        const normalized = direct.replace(/^\//, '')
        if (/^#\/play\/[^?]+\?[^#]*\bplan=\d+(?:&|$)/.test(normalized)) return normalized
      }
      const episodeId = Number(item.episode_id)
      const planId = Number(item.plan_id)
      if (!Number.isInteger(episodeId) || episodeId <= 0) return ''
      if (!Number.isInteger(planId) || planId <= 0) return ''
      return `#/play/${encodeURIComponent(episodeId)}?plan=${encodeURIComponent(planId)}`
    },

    sprintItemKey,

    isSprintDone(item, direction, index) {
      if (item.completed === true || item.status === 'done') return true
      return Boolean(this.sprintCompleted[sprintItemKey(item, direction, index)])
    },

    toggleSprintItem(item, direction, index) {
      const key = sprintItemKey(item, direction, index)
      this.sprintCompleted[key] = !this.sprintCompleted[key]
      this.persistSprintCompletion()
    },

    loadSprintCompletion() {
      this.sprintCompleted = {}
      if (!this.interviewDate) return
      try {
        const saved = localStorage.getItem(`${SPRINT_STORAGE_PREFIX}${this.interviewDate}`)
        this.sprintCompleted = saved ? JSON.parse(saved) : {}
      } catch {
        this.sprintCompleted = {}
      }
    },

    persistSprintCompletion() {
      if (!this.interviewDate) return
      try {
        localStorage.setItem(
          `${SPRINT_STORAGE_PREFIX}${this.interviewDate}`,
          JSON.stringify(this.sprintCompleted)
        )
      } catch {
        // 本地清单进度写入失败不影响播放与复习主流程。
      }
    },
  })

  // main.js 与现有模板都以 review.load()/review.grade() 方式调用。
  return {
    state,
    load: () => state.load(),
    loadReplayQueue: () => state.loadReplayQueue(),
    loadDaily: () => state.loadDaily(),
    loadCoverage: () => state.loadCoverage(),
    toggleCoverage: (direction) => state.toggleCoverage(direction),
    toggleFlip: (id) => state.toggleFlip(id),
    grade: (id, mastery) => state.grade(id, mastery),
    dueStatus,
    formatReviewTime,
    playHref: (item) => state.playHref(item),
    saveInterviewDate: () => state.saveInterviewDate(),
    clearInterviewDate: () => state.clearInterviewDate(),
    sprintItemKey,
    isSprintDone: (item, direction, index) => state.isSprintDone(item, direction, index),
    toggleSprintItem: (item, direction, index) => state.toggleSprintItem(item, direction, index),
  }
}

export function isDue(nextReviewAt) {
  if (!nextReviewAt) return true
  const date = parseDate(nextReviewAt)
  return !date || date <= new Date()
}

export function dueStatus(card) {
  const next = parseDate(card?.next_review_at)
  if (!next) {
    return { kind: 'new', className: 'new', label: '首次复习' }
  }

  const today = startOfToday()
  const dueDay = new Date(next)
  dueDay.setHours(0, 0, 0, 0)
  const dayDiff = Math.round((dueDay.getTime() - today.getTime()) / 86400000)

  if (dayDiff < 0) {
    return { kind: 'overdue', className: 'overdue', label: `逾期 ${Math.abs(dayDiff)} 天` }
  }
  if (dayDiff === 0) {
    return { kind: 'today', className: 'today', label: '今天到期' }
  }
  return { kind: 'scheduled', className: 'scheduled', label: `${dayDiff} 天后` }
}

export function formatDate(iso) {
  if (!iso) return ''
  const date = parseDate(iso)
  if (!date) return iso
  return `${date.getMonth() + 1}/${date.getDate()}`
}

export function formatReviewTime(iso) {
  if (!iso) return '尚未复习'
  const date = parseDate(iso)
  if (!date) return iso
  return `${date.getMonth() + 1}月${date.getDate()}日 ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

export const masteryLabels = {
  unknown: '不会',
  fuzzy: '模糊',
  mastered: '掌握',
}

export const directionNames = {
  backend_arch: '后端/架构',
  ai_llm: 'AI/LLM',
  tech_news: '技术资讯',
  source_dive: '源码/原理',
  other: '其他方向',
}
