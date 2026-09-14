/** 会话生命周期独立于 API/UI，避免循环依赖。每次换凭证都使旧异步工作失效。 */
let session = new AbortController()
const listeners = new Set()

export function getSession() {
  return session
}

export function isCurrentSession(value) {
  return value === session && !value.signal.aborted
}

export function assertSession(value) {
  if (!isCurrentSession(value)) throw new DOMException('登录会话已变更', 'AbortError')
}

export function onSessionChange(callback) {
  listeners.add(callback)
  return () => listeners.delete(callback)
}

export function resetSession() {
  const previous = session
  session = new AbortController()
  previous.abort()
  for (const callback of listeners) callback()
}

/** 退出时撤销 UI 延迟任务，已排队的回调也检查会话。 */
export function sessionTimeout(callback, delay) {
  const owner = getSession()
  const cancel = () => clearTimeout(timer)
  const timer = setTimeout(() => {
    owner.signal.removeEventListener('abort', cancel)
    if (isCurrentSession(owner)) callback()
  }, delay)
  owner.signal.addEventListener('abort', cancel, { once: true })
  return timer
}
