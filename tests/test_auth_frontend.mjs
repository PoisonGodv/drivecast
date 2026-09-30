// node --experimental-vm-modules --test tests/test_auth_frontend.mjs
// Execute the production ES modules in an isolated browser-like realm, with no server/data writes.
import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFile } from 'node:fs/promises'

const root = new URL('../static/', import.meta.url)
const deferred = () => {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}
const response = (body, status = 200) => ({ ok: status < 400, status, json: async () => body })
const tick = () => new Promise((resolve) => setImmediate(resolve))

// Separate realms share values, but storage events go only to other tabs and are queued.
function sharedStorage() {
  const values = Object.create(null)
  const tabs = []
  const queue = []
  return {
    connect(window) {
      const storage = Object.create(null)
      const change = (key, value) => {
        const oldValue = values[key] ?? null
        if (oldValue === value) return
        if (value === null) delete values[key]
        else values[key] = value
        for (const tab of tabs) {
          if (tab.window !== window) queue.push(() => tab.window.dispatchEvent({ type: 'storage', key, oldValue, newValue: value, storageArea: tab.storage }))
        }
      }
      Object.defineProperties(storage, {
        getItem: { value: (key) => values[key] ?? null, configurable: true },
        setItem: { value: (key, value) => change(key, String(value)), configurable: true },
        removeItem: { value: (key) => change(key, null), configurable: true },
      })
      const proxy = new Proxy(storage, {
        ownKeys: () => Reflect.ownKeys(values),
        getOwnPropertyDescriptor: (_, key) => key in values ? { enumerable: true, configurable: true, value: values[key] } : undefined,
        get: (target, key) => key in target ? target[key] : values[key],
      })
      tabs.push({ window, storage: proxy })
      return proxy
    },
    flush() {
      let count = 0
      while (queue.length) {
        assert.ok(++count < 100, 'storage events must not echo indefinitely')
        queue.shift()()
      }
    },
  }
}

async function browser(shared = null) {
  class Events {
    handlers = new Map()
    addEventListener(name, fn) {
      if (!this.handlers.has(name)) this.handlers.set(name, new Set())
      this.handlers.get(name).add(fn)
    }
    removeEventListener(name, fn) { this.handlers.get(name)?.delete(fn) }
    dispatchEvent(event) { for (const fn of this.handlers.get(event.type) || []) fn(event) }
  }
  class Audio extends Events {
    src = ''; currentTime = 0; duration = 60; playbackRate = 1
    buffered = { length: 0 }; loads = 0; pauses = 0
    play() { this.dispatchEvent({ type: 'play' }); return Promise.resolve() }
    pause() { this.pauses++; this.dispatchEvent({ type: 'pause' }) }
    removeAttribute(name) { if (name === 'src') this.src = '' }
    load() { this.loads++; this.currentTime = 0 }
  }
  const streams = []
  class EventSource extends Events {
    constructor(url) { super(); this.url = url; streams.push(this) }
    close() { this.closed = true }
  }
  const window = new Events()
  const storage = shared ? shared.connect(window) : Object.create(null)
  if (!shared) Object.defineProperties(storage, {
    getItem: { value: (key) => storage[key] ?? null },
    setItem: { value: (key, value) => { storage[key] = String(value) } },
    removeItem: { value: (key) => { delete storage[key] } },
  })
  window.location = { hash: '#/login' }
  window.confirm = () => true
  window.prompt = () => ''
  const timers = new Map()
  let timerId = 0
  const schedule = (fn) => { timers.set(++timerId, fn); return timerId }
  const calls = []
  let fetcher = async () => response({})
  const context = vm.createContext({
    window, localStorage: storage, Audio, EventSource, AbortController, DOMException,
    CustomEvent: class { constructor(type) { this.type = type } },
    navigator: { mediaSession: { metadata: null, setActionHandler() {}, setPositionState() {} } },
    MediaMetadata: class { constructor(data) { Object.assign(this, data) } },
    document: { readyState: 'loading', getElementById: () => null },
    URLSearchParams, console: { warn() {} },
    setInterval: schedule, clearInterval: (id) => timers.delete(id),
    setTimeout: schedule, clearTimeout: (id) => timers.delete(id),
    requestAnimationFrame: schedule,
    fetch: (url, init) => { calls.push({ url, init }); return fetcher(url, init) },
  })
  const modules = new Map()
  async function module(url) {
    const key = url.href
    if (modules.has(key)) return modules.get(key)
    const source = url.pathname.endsWith('/vendor/petite-vue.js')
      ? 'export const reactive = (x) => x; export const createApp = () => ({ mount() {} });'
      : await readFile(url, 'utf8')
    const result = new vm.SourceTextModule(source, { context, identifier: key })
    modules.set(key, result)
    await result.link((specifier, owner) => module(new URL(specifier, owner.identifier)))
    return result
  }
  // Link dependencies sequentially to mirror the browser module map (including version query strings).
  for (const name of ['session.js', 'store.js?v=20260930c', 'api.js?v=20260930c', 'player.js?v=20260930c', 'review.js?v=20260930c', 'main.js']) {
    const mod = await module(new URL(`js/${name}`, root))
    await mod.evaluate()
  }
  const ns = (name) => modules.get(new URL(`js/${name}`, root).href).namespace
  return {
    app: window.__DRIVECAST__, window, storage, streams, timers, calls,
    api: ns('api.js?v=20260930c').api,
    withToken: ns('api.js?v=20260930c').withToken,
    store: ns('store.js?v=20260930c').store,
    playerModule: ns('player.js?v=20260930c'),
    fetchWith(fn) { fetcher = fn },
    login(token = 'A') {
      this.store.setToken(token, token)
      this.app.authed = true
      this.app.user = { id: token, username: token }
      this.app.authReady = false
    },
  }
}

const plan = { id: 7, title: 'private A', episodes: [{ id: 9, seq: 1, status: 'ready', duration_sec: 60 }] }
const plain = (value) => JSON.parse(JSON.stringify(value))

function assertClean(b) {
  const { app, playerModule: { player }, timers } = b
  assert.equal(app.authed, false)
  assert.equal(app.route, 'login')
  assert.equal(app.plan, null)
  assert.deepEqual(plain(app.plans), [])
  assert.equal(app.createForm.title, '')
  assert.equal(app.sse, null)
  assert.equal(app.todayDueCount, 0)
  assert.equal(app.loginForm.password, '')
  assert.equal(player.state.episodeId, '')
  assert.equal(player.state.plan, null)
  assert.equal(player.state.script, null)
  assert.equal(player.state.downloadUrl, '')
  assert.deepEqual(plain(player.state.chapters), [])
  assert.deepEqual(plain(player.state.endedRefs), [])
  assert.equal(player.audio.src, '')
  assert.ok(player.audio.loads > 0)
  assert.equal(player.progressTimer, null)
  assert.equal(player.endedTimer, null)
  assert.equal(timers.size, 0)
  assert.deepEqual(plain(app.review.state.cards), [])
  assert.deepEqual(plain(app.review.state.planIdsByTitle), {})
  assert.equal(app.review.state.interviewDate, '')
  assert.equal(b.store.getPosition(9), 0)
  assert.equal(b.storage['drivecast.review.sprint.completed.2026-09-20'], undefined)
}

async function seed(b) {
  b.login()
  b.fetchWith(async () => response({ chapters: [{ start_sec: 0 }], download_url: '/private' }))
  b.app.plan = plan
  b.app.plans = [plan]
  b.app.createForm.title = 'A topic'
  b.app.todayDueCount = 8
  b.app.review.state.cards = [{ id: 42 }]
  b.app.review.state.planIdsByTitle = { A: 7 }
  b.app.review.state.interviewDate = '2026-09-20'
  b.storage.setItem('drivecast.review.sprint.completed.2026-09-20', '{"9":true}')
  b.store.setPosition(9, 20)
  await b.playerModule.loadEpisode(9, plan)
  await tick()
  b.playerModule.player.state.script = { content_md: 'A secret' }
  b.app.startPlanSSE(7)
  b.app.showToast('A toast')
}

test('createPlan uses explicit and default targetMinutes in real request JSON', async () => {
  const b = await browser()
  b.login()
  await b.api.createPlan(11, 6, 40)
  await b.api.createPlan(11, 6)
  assert.deepEqual(JSON.parse(b.calls[0].init.body), { topic_id: 11, episode_count: 6, target_minutes: 40 })
  assert.equal(JSON.parse(b.calls[1].init.body).target_minutes, 15)
  assert.equal(b.calls[0].init.headers.Authorization, 'Bearer A')
  assert.equal(b.withToken('//other.example/file'), '//other.example/file')
})

test('logout unloads media, clears all user caches and closes streams/timers without posting progress', async () => {
  const b = await browser()
  await seed(b)
  const before = b.calls.length
  b.app.logout()
  assertClean(b)
  assert.equal(b.store.getToken(), '')
  assert.equal(b.streams[0].closed, true)
  assert.equal(b.calls.length, before)
  b.login('B')
  b.playerModule.player.audio.dispatchEvent({ type: 'pause' })
  b.playerModule.player.audio.dispatchEvent({ type: 'ended' })
  assert.equal(b.calls.length, before)
})

test('401 performs identical cleanup and preserves login return route, including boot-time expiry', async () => {
  const b = await browser()
  await seed(b)
  b.window.location.hash = '#/play/9?plan=7'
  b.fetchWith(async () => response({ detail: 'expired' }, 401))
  await assert.rejects(b.api.listPlans(), { status: 401 })
  assertClean(b)
  assert.equal(b.app.pendingHash, '#/play/9?plan=7')
  b.app.loginForm = { username: 'B', password: 'secret' }
  b.fetchWith(async () => response({ token: 'B', user: { id: 2, username: 'B' } }))
  await b.app.submitLogin()
  assert.equal(b.store.getToken(), 'B')
  assert.equal(b.window.location.hash, '#/play/9?plan=7')
  assert.equal(b.app.loggingIn, false)
  b.fetchWith(async () => response({}, 401))
  await b.app.boot()
  assert.equal(b.app.authReady, false)
  assert.equal(b.app.authed, false)
})

test('late plan, player, review, create and SSE work cannot revive A or issue chained requests with B credentials', async () => {
  const b = await browser()
  b.login()
  const pending = []
  b.fetchWith((url) => { const d = deferred(); pending.push({ url, ...d }); return d.promise })
  b.app.createForm.title = 'A topic'
  const tasks = [b.app.loadPlans(), b.app.loadPlan(7), b.app.enterPlayer(9, 7), b.app.review.load(), b.app.submitCreate()]
  await b.playerModule.loadEpisode(9, plan)
  b.playerModule.toggleTranscript()
  b.playerModule.player.audio.dispatchEvent({ type: 'ended' })
  b.app.startPlanSSE(7)
  const oldStream = b.streams[0]
  b.app.logout()
  assertClean(b)
  b.login('B')
  b.app.plan = { id: 7, title: 'B', episodes: [{ id: 9, status: 'pending' }] }
  b.app.review.state.cards = [{ id: 'B' }]
  const before = b.calls.length
  for (const item of pending) item.resolve(response(item.url === '/api/plans' ? [plan] : { ...plan, content_md: 'A secret', references: [{ title: 'A' }], id: 7 }))
  oldStream.dispatchEvent({ type: 'episode_status', data: '{"episode_id":9,"status":"ready"}' })
  oldStream.onerror({})
  await Promise.all(tasks)
  await tick()
  assert.equal(b.app.plan.title, 'B')
  assert.equal(b.app.plan.episodes[0].status, 'pending')
  assert.deepEqual(plain(b.app.plans), [])
  assert.deepEqual(plain(b.app.review.state.cards), [{ id: 'B' }])
  assert.equal(b.playerModule.player.state.script, null)
  assert.equal(b.playerModule.player.state.downloadUrl, '')
  assert.equal(b.playerModule.player.audio.src, '')
  assert.equal(b.calls.length, before)
  assert.equal(b.store.getToken(), 'B')
  assert.equal(b.timers.size, 0)
})

test('old 401 and delayed JSON decoding cannot revoke or update the new session', async () => {
  const b = await browser()
  b.login()
  const late401 = deferred()
  const body = deferred()
  b.fetchWith((url) => url.endsWith('/me') ? late401.promise : Promise.resolve({ ok: true, status: 200, json: () => body.promise }))
  const first = b.api.getMe()
  const second = b.app.loadPlan(7)
  await tick()
  b.app.logout()
  b.login('B')
  late401.resolve(response({}, 401))
  body.resolve(plan)
  await assert.rejects(first, { name: 'AbortError' })
  await second
  assert.equal(b.store.getToken(), 'B')
  assert.equal(b.app.authed, true)
  assert.equal(b.app.plan, null)
})


test('logout invalidates late login and storage account changes unload the current tab', async () => {
  const b = await browser()
  const login = deferred()
  b.fetchWith(() => login.promise)
  b.app.loginForm = { username: 'A', password: 'secret' }
  const task = b.app.submitLogin()
  b.app.logout()
  login.resolve(response({ token: 'A', user: { username: 'A' } }))
  await task
  assert.equal(b.store.getToken(), '')
  assert.equal(b.app.authed, false)
  await seed(b)
  b.storage.setItem('drivecast:token', 'B')
  b.storage.setItem('drivecast:user', 'B')
  b.window.dispatchEvent({ type: 'storage', key: 'drivecast:token' })
  assertClean(b)
  assert.equal(b.store.getToken(), '')
})

test('late nested list hydration does not overwrite B state or start follow-up review requests', async () => {
  const b = await browser()
  b.login()
  const detail = deferred()
  b.fetchWith((url) => url === '/api/plans' ? Promise.resolve(response([{ id: 7 }])) : detail.promise)
  const task = b.app.loadPlans()
  await tick()
  assert.equal(b.calls.length, 2)
  b.app.logout()
  b.login('B')
  b.app.plans = [{ id: 'B' }]
  detail.resolve(response(plan))
  await task
  assert.deepEqual(plain(b.app.plans), [{ id: 'B' }])
  assert.equal(b.calls.length, 2)
})

test('A transcript and metadata cannot attach to B even when the new episode ID is identical', async () => {
  const b = await browser()
  b.login()
  const old = []
  b.fetchWith(() => { const d = deferred(); old.push(d); return d.promise })
  await b.playerModule.loadEpisode(9, plan)
  b.playerModule.toggleTranscript()
  b.app.logout()
  b.login('B')
  b.fetchWith(async () => response({ chapters: [{ title: 'B', start_sec: 0 }], download_url: '/B' }))
  await b.playerModule.loadEpisode(9, { ...plan, title: 'B' })
  await tick()
  for (const d of old) d.resolve(response({ content_md: 'A secret', chapters: [{ title: 'A' }], download_url: '/A' }))
  await tick()
  const state = b.playerModule.player.state
  assert.equal(state.plan.title, 'B')
  assert.equal(state.script, null)
  assert.equal(state.downloadUrl, '/B?token=B')
  assert.equal(state.chapters[0].title, 'B')
  b.app.logout()
})


test('late login response cannot overwrite a newer successful login', async () => {
  const b = await browser()
  const oldLogin = deferred()
  b.fetchWith(() => oldLogin.promise)
  b.app.loginForm = { username: 'A', password: 'a-password' }
  const first = b.app.submitLogin()
  b.app.logout()
  b.app.loginForm = { username: 'B', password: 'b-password' }
  b.fetchWith(async () => response({ token: 'B', user: { id: 2, username: 'B' } }))
  await b.app.submitLogin()
  oldLogin.resolve(response({ token: 'A', user: { id: 1, username: 'A' } }))
  await first
  assert.equal(b.store.getToken(), 'B')
  assert.equal(b.app.user.username, 'B')
  assert.equal(b.app.loginError, '')
  assert.equal(b.app.loggingIn, false)
})

test('cross-tab credential changes fail closed and unload active playback', async () => {
  const b = await browser()
  await seed(b)
  const before = b.calls.length
  b.storage.setItem('drivecast:token', 'B')
  b.storage.setItem('drivecast:user', 'B')
  b.window.dispatchEvent({ type: 'storage', key: 'drivecast:token' })
  assertClean(b)
  assert.equal(b.store.getToken(), '')
  assert.equal(b.calls.length, before)
})

test('two tabs invalidate old playback without deleting the newly logged-in shared session', async () => {
  const shared = sharedStorage()
  const old = await browser(shared)
  await seed(old)
  const fresh = await browser(shared)
  shared.flush()
  fresh.login('B')
  const before = old.calls.length
  shared.flush()
  assertClean(old)
  assert.equal(old.store.getToken(), '')
  assert.equal(old.calls.length, before)
  assert.equal(fresh.store.getToken(), 'B')
  assert.equal(fresh.app.authed, true)
  assert.equal(fresh.storage.getItem('drivecast:token'), 'B')
  assert.equal(fresh.storage.getItem('drivecast:user'), 'B')
  // Even clicking logout in the now-stale tab cannot revoke B.
  old.app.logout()
  shared.flush()
  assert.equal(fresh.store.getToken(), 'B')
  assert.equal(fresh.app.authed, true)
  fresh.app.logout()
  shared.flush()
  assert.equal(fresh.storage.getItem('drivecast:token'), null)
})

test('an idle login tab and a late old-tab logout cannot bounce a new login', async () => {
  const shared = sharedStorage()
  const idle = await browser(shared)
  const fresh = await browser(shared)
  fresh.login('B')
  shared.flush()
  assert.equal(idle.store.getToken(), '')
  assert.equal(fresh.store.getToken(), 'B')
  assert.equal(fresh.app.authed, true)
  assert.equal(fresh.storage.getItem('drivecast:token'), 'B')
  // Simulate logout/401 before the queued storage event is delivered.
  idle.login('A')
  shared.flush()
  fresh.login('B')
  idle.app.logout()
  shared.flush()
  assert.equal(fresh.store.getToken(), 'B')
  assert.equal(fresh.app.authed, true)
  assert.equal(fresh.storage.getItem('drivecast:token'), 'B')
})

test('same-episode late metadata and transcript stay discarded after account switch', async () => {
  const b = await browser()
  b.login()
  const pending = []
  b.fetchWith(() => { const d = deferred(); pending.push(d); return d.promise })
  await b.playerModule.loadEpisode(9, plan)
  b.playerModule.toggleTranscript()
  b.app.logout()
  b.login('B')
  b.fetchWith(async () => response({ chapters: [{ start_sec: 12 }], download_url: '/b/download' }))
  await b.playerModule.loadEpisode(9, { ...plan, title: 'B' })
  await tick()
  for (const d of pending) d.resolve(response({ content_md: 'A secret', download_url: '/a/download', chapters: [{ start_sec: 0 }] }))
  await tick()
  assert.equal(b.playerModule.player.state.plan.title, 'B')
  assert.equal(b.playerModule.player.state.script, null)
  assert.equal(b.playerModule.player.state.downloadUrl, '/b/download?token=B')
  assert.deepEqual(plain(b.playerModule.player.state.chapters), [{ start_sec: 12 }])
  b.app.logout()
})


test('cancel regenerate sends no request while confirmed empty reason is allowed', async () => {
  const b = await browser()
  b.login()
  b.window.prompt = () => null
  await b.app.regenerateEpisode(9)
  assert.equal(b.calls.length, 0)
  b.window.prompt = () => ''
  b.fetchWith(() => Promise.resolve(response({ episode_id: 9, job_id: 1, status: 'pending' })))
  await b.app.regenerateEpisode(9)
  assert.equal(b.calls.length, 1)
  assert.equal(JSON.parse(b.calls[0].init.body).reason, '')
})

test('create page shows remaining quota and blocks submit when the request exceeds it', async () => {
  const b = await browser()
  b.login()
  b.fetchWith(async (url) => response(url === '/api/usage' ? { minutes_remaining: 50, minutes_limit: 240 } : {}))
  await b.app.loadUsage()
  assert.equal(b.app.usage.minutes_remaining, 50)
  b.app.createForm = { title: 'T', direction: 'backend_arch', episodeCount: 6, targetMinutes: 15 }
  assert.equal(b.app.createRequestMinutes, 90)
  assert.equal(b.app.createOverQuota, true)
  const before = b.calls.length
  await b.app.submitCreate()
  assert.equal(b.calls.length, before)
  b.app.createForm.episodeCount = 4
  b.app.createForm.targetMinutes = 10
  assert.equal(b.app.createOverQuota, false)
  b.app.usage = { minutes_remaining: null }
  b.app.createForm.targetMinutes = 40
  assert.equal(b.app.createOverQuota, false)
  b.app.logout()
  assert.equal(b.app.usage, null)
  assert.equal(b.timers.size, 0)
})

test('password change validates inline, keeps this tab logged in with the new token and resets old-session work', async () => {
  const b = await browser()
  await seed(b)
  const oldStream = b.streams[0]
  b.app.openPasswordForm()
  assert.equal(b.app.passwordForm.open, true)
  const before = b.calls.length
  Object.assign(b.app.passwordForm, { oldPassword: 'old-password', newPassword: 'short', confirmPassword: 'short' })
  await b.app.submitPasswordChange()
  assert.match(b.app.passwordError, /至少 8 位/)
  Object.assign(b.app.passwordForm, { newPassword: 'new-password-1', confirmPassword: 'new-password-2' })
  await b.app.submitPasswordChange()
  assert.match(b.app.passwordError, /不一致/)
  assert.equal(b.calls.length, before)

  // 原密码错是 400,只在表单内提示,不踢回登录页
  b.app.passwordForm.confirmPassword = 'new-password-1'
  b.fetchWith(async () => response({ detail: { code: 400, message: '原密码错误' } }, 400))
  await b.app.submitPasswordChange()
  assert.equal(b.app.passwordError, '原密码错误')
  assert.equal(b.app.passwordSaving, false)
  assert.equal(b.app.authed, true)
  assert.equal(b.store.getToken(), 'A')
  const request = b.calls.at(-1)
  assert.equal(request.url, '/api/auth/password')
  assert.equal(request.init.method, 'POST')
  assert.deepEqual(JSON.parse(request.init.body), { old_password: 'old-password', new_password: 'new-password-1' })

  b.window.location.hash = '#/review'
  b.fetchWith(async (url) => url === '/api/auth/password'
    ? response({ token: 'A2', user: { id: 1, username: 'A' } })
    : response([]))
  await b.app.submitPasswordChange()
  assert.equal(b.store.getToken(), 'A2')
  assert.equal(b.storage['drivecast:token'], 'A2')
  assert.equal(b.app.authed, true)
  assert.equal(b.app.user.username, 'A')
  assert.equal(b.app.route, 'review')
  assert.equal(b.app.passwordForm.open, false)
  assert.equal(b.app.passwordError, '')
  // 旧会话的 SSE/音频 URL 带旧 token,必须随换凭证一并卸载
  assert.equal(oldStream.closed, true)
  assert.equal(b.playerModule.player.audio.src, '')
  assert.equal(b.app.toast, '密码已修改，其他设备需重新登录')
  await tick()
  assert.equal(b.calls.at(-1).init.headers.Authorization, 'Bearer A2')
  b.app.logout()
  assert.equal(b.timers.size, 0)
})

test('late password-change response cannot install a token after logout', async () => {
  const b = await browser()
  b.login()
  const pending = deferred()
  b.fetchWith(() => pending.promise)
  b.app.openPasswordForm()
  Object.assign(b.app.passwordForm, { oldPassword: 'old-password', newPassword: 'new-password-1', confirmPassword: 'new-password-1' })
  const task = b.app.submitPasswordChange()
  b.app.logout()
  pending.resolve(response({ token: 'A2', user: { id: 1, username: 'A' } }))
  await task
  assert.equal(b.store.getToken(), '')
  assert.equal(b.app.authed, false)
  assert.equal(b.app.passwordForm.open, false)
  assert.equal(b.timers.size, 0)
})
