/**
 * 自助修改密码(POST /api/auth/password)。
 *
 * 成功后服务端吊销该用户此前的全部 token 并回发新 token;本页换上新凭证。
 * 换凭证走 store.setToken → resetSession:旧会话的在途请求/SSE/音频(URL 里带旧 token)
 * 一并作废,随后恢复登录态并重新应用当前路由,由新会话重新拉取。
 */

import { getSession, isCurrentSession } from './session.js'
import { api } from './api.js?v=20260930d'
import { store } from './store.js?v=20260930d'

export const PASSWORD_MIN_LEN = 8

const emptyForm = () => ({ open: false, oldPassword: '', newPassword: '', confirmPassword: '' })

/** 混入 main.js 的 appState;方法里的 this 即 appState。 */
export function accountMixin() {
  return {
    passwordForm: emptyForm(),
    passwordError: '',
    passwordSaving: false,

    openPasswordForm() {
      this.passwordForm = { ...emptyForm(), open: true }
      this.passwordError = ''
    },

    closePasswordForm() {
      if (this.passwordSaving) return
      this.passwordForm = emptyForm()
      this.passwordError = ''
    },

    async submitPasswordChange() {
      if (this.passwordSaving) return
      const { oldPassword, newPassword, confirmPassword } = this.passwordForm
      const invalid = !oldPassword
        ? '请输入原密码'
        : newPassword.length < PASSWORD_MIN_LEN
          ? `新密码至少 ${PASSWORD_MIN_LEN} 位`
          : newPassword !== confirmPassword
            ? '两次输入的新密码不一致'
            : newPassword === oldPassword
              ? '新密码不能与原密码相同'
              : ''
      if (invalid) {
        this.passwordError = invalid
        return
      }
      const session = getSession()
      this.passwordSaving = true
      this.passwordError = ''
      try {
        const data = await api.changePassword(oldPassword, newPassword)
        if (!isCurrentSession(session)) return
        if (!data?.token || typeof data.token !== 'string') throw new Error('响应缺少凭证')
        const user = data.user || this.user
        store.setToken(data.token, user?.username || store.getUsername())
        // 换凭证已重置全部用户态(含本表单),恢复登录态并重载当前视图
        this.user = user
        this.authed = true
        this.applyRoute()
        this.showToast('密码已修改，其他设备需重新登录')
      } catch (err) {
        if (!isCurrentSession(session)) return
        this.passwordError = err.message || '修改失败'
      } finally {
        if (isCurrentSession(session)) this.passwordSaving = false
      }
    },
  }
}
