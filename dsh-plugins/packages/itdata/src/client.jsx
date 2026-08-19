// dsh-itdata — client half.
//
// 设置页「IT 备件系统」：后端健康状态、用户登录/登出（权限门）、
// 当前权限摘要、数据面板入口（/itd/，P5 部署前端后生效）。

import React, { useCallback, useEffect, useState } from 'react'

const RPC = '/plugins/itdata/rpc'

async function rpc(action, extra = {}) {
  const resp = await fetch(RPC, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ action, ...extra }),
  })
  return resp.json()
}

const card = {
  border: '1px solid rgba(128,128,128,0.25)',
  borderRadius: 10,
  padding: '14px 16px',
  margin: '12px 0',
}
const label = { display: 'block', fontSize: 12, opacity: 0.7, marginBottom: 4 }
const input = {
  width: '100%',
  boxSizing: 'border-box',
  padding: '7px 10px',
  borderRadius: 8,
  border: '1px solid rgba(128,128,128,0.4)',
  background: 'transparent',
  color: 'inherit',
  fontSize: 13,
  marginBottom: 10,
}
const button = {
  padding: '7px 16px',
  borderRadius: 8,
  border: '1px solid rgba(128,128,128,0.4)',
  background: 'rgba(128,128,128,0.12)',
  color: 'inherit',
  fontSize: 13,
  cursor: 'pointer',
}
const dot = (ok) => ({
  display: 'inline-block',
  width: 8,
  height: 8,
  borderRadius: '50%',
  marginRight: 6,
  background: ok ? '#3fb27f' : '#d05663',
})

function fmtTime(unix) {
  if (!unix) return '-'
  return new Date(unix * 1000).toLocaleString()
}

function permSummary(perms) {
  if (perms === null || typeof perms !== 'object') return null
  const entries = Object.entries(perms)
  const on = entries.filter(([, v]) => v === true).map(([k]) => k)
  const off = entries.filter(([, v]) => v !== true).map(([k]) => k)
  return { on, off, total: entries.length }
}

export const inject = ['slots']

export function apply(ctx) {
  const slots = ctx.get('slots')
  if (slots === undefined) return

  slots.inject('settings.section', () => slots.register(
    { name: 'settings.section', id: 'itdata', order: 60, label: 'IT 备件系统' },
    function ItDataSettingsPage() {
      const [state, setState] = useState({ loading: true })
      const [username, setUsername] = useState('')
      const [password, setPassword] = useState('')
      const [busy, setBusy] = useState(false)
      const [error, setError] = useState(null)

      const refresh = useCallback(async () => {
        try {
          const r = await rpc('status')
          setError(null)
          setState({ loading: false, auth: r.auth, backend: r.backend })
        } catch (e) {
          setState({ loading: false })
          setError(String(e?.message ?? e))
        }
      }, [])

      useEffect(() => { void refresh() }, [refresh])

      const doLogin = async () => {
        setBusy(true)
        setError(null)
        try {
          const r = await rpc('login', { username, password })
          if (r.ok !== true) setError(r.error ?? '登录失败')
          else { setPassword(''); void refresh() }
        } catch (e) {
          setError(String(e?.message ?? e))
        } finally {
          setBusy(false)
        }
      }

      const doLogout = async () => {
        setBusy(true)
        try { await rpc('logout') } catch { /* ignore */ }
        finally { setBusy(false); void refresh() }
      }

      if (state.loading) {
        return React.createElement('div', { style: { padding: 24, opacity: 0.7 } }, '加载中…')
      }

      const auth = state.auth ?? { loggedIn: false }
      const backend = state.backend ?? { state: 'unknown' }
      const summary = permSummary(auth.permissions)

      const children = []

      children.push(React.createElement('div', { key: 'head', style: { fontSize: 14, fontWeight: 600 } },
        'IT 备件管理系统 · 权限门'))

      children.push(React.createElement('div', { key: 'backend', style: { ...card, fontSize: 13 } },
        React.createElement('span', { style: dot(backend.state === 'up') }),
        `后端（${backend.state === 'up' ? '在线' : backend.state === 'down' ? '不可达' : '未知'}）`,
        backend.detail ? React.createElement('span', { style: { opacity: 0.6, marginLeft: 8 } }, backend.detail) : null,
      ))

      if (auth.loggedIn) {
        const rows = [
          ['账号', `${auth.name ?? '-'}（${auth.username ?? auth.role ?? '-'}）`],
          ['角色', auth.role ?? '-'],
          ['登录时间', fmtTime(auth.loggedInAt)],
          ['令牌到期', fmtTime(auth.expiresAt)],
        ]
        children.push(React.createElement('div', { key: 'auth', style: card },
          React.createElement('div', { style: { ...label, fontSize: 13, opacity: 1 } },
            React.createElement('span', { style: dot(true) }), '已登录'),
          ...rows.map(([k, v]) => React.createElement('div', { key: k, style: { display: 'flex', fontSize: 13, margin: '4px 0' } },
            React.createElement('span', { style: { width: 84, opacity: 0.6 } }, k),
            React.createElement('span', null, v))),
          summary !== null && summary.total > 0
            ? React.createElement('details', { style: { marginTop: 8, fontSize: 12 } },
                React.createElement('summary', { style: { cursor: 'pointer', opacity: 0.75 } },
                  `权限摘要：${summary.on.length}/${summary.total} 项开启`),
                React.createElement('div', { style: { marginTop: 6, lineHeight: 1.7, wordBreak: 'break-all', opacity: 0.8 } },
                  summary.on.map((k) => React.createElement('code', { key: k, style: { marginRight: 8 } }, k))))
            : null,
          React.createElement('div', { style: { marginTop: 12, display: 'flex', gap: 10 } },
            React.createElement('button', { style: button, onClick: doLogout, disabled: busy }, '退出登录'),
            React.createElement('button', {
              style: button,
              onClick: () => window.open('/itd/', '_blank', 'noopener'),
              title: '嵌入面板（P5 部署前端后可用）',
            }, '打开数据面板'),
          ),
        ))
      } else {
        children.push(React.createElement('div', { key: 'login', style: card },
          React.createElement('div', { style: { ...label, fontSize: 13, opacity: 1 } }, '登录（agent 数据工具将以此身份通过权限门）'),
          React.createElement('label', { style: label }, '用户名'),
          React.createElement('input', {
            style: input, value: username, autoComplete: 'username',
            onChange: (e) => setUsername(e.target.value),
          }),
          React.createElement('label', { style: label }, '密码'),
          React.createElement('input', {
            style: input, type: 'password', value: password, autoComplete: 'current-password',
            onChange: (e) => setPassword(e.target.value),
            onKeyDown: (e) => { if (e.key === 'Enter' && username !== '' && password !== '') void doLogin() },
          }),
          React.createElement('button', {
            style: { ...button, opacity: busy || username === '' || password === '' ? 0.5 : 1 },
            onClick: doLogin, disabled: busy || username === '' || password === '',
          }, busy ? '登录中…' : '登录'),
        ))
      }

      if (error !== null) {
        children.push(React.createElement('div', {
          key: 'error',
          style: { ...card, borderColor: '#d05663', color: '#d05663', fontSize: 13 },
        }, String(error)))
      }

      return React.createElement('div', { style: { maxWidth: 520 } }, children)
    },
  ))
}
