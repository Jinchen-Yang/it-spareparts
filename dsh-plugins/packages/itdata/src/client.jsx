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

      // ── 白名单脚本管理（仅 admin 角色展示；后端 require_admin 把关） ──
      if (auth.loggedIn && auth.role === 'admin') {
        children.push(React.createElement(ScriptsSection, { key: 'scripts' }))
      }

      return React.createElement('div', { style: { maxWidth: 720 } }, children)
    },
  ))
}

function ScriptsSection() {
  const [scripts, setScripts] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [form, setForm] = useState({
    name: '', description: '', required_action: '', content: '', timeout_seconds: '60', enabled: true,
  })
  const [editing, setEditing] = useState(null)
  const [outputs, setOutputs] = useState({})

  const refresh = useCallback(async () => {
    try {
      const r = await rpc('scripts_list')
      setScripts(r.ok === true ? r.scripts : [])
      setMsg(r.ok === true ? null : String(r.error ?? '加载失败'))
    } catch (e) {
      setMsg(String(e?.message ?? e))
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const save = async () => {
    if (form.name.trim() === '' || form.content.trim() === '') { setMsg('名称与内容必填'); return }
    setBusy(true)
    setMsg(null)
    try {
      const r = await rpc(editing ? 'script_update' : 'script_create', {
        ...form,
        required_action: form.required_action.trim() === '' ? null : form.required_action.trim(),
        timeout_seconds: Math.min(Math.max(Number(form.timeout_seconds) || 60, 5), 600),
      })
      if (r.ok !== true) setMsg(String(r.error ?? '保存失败'))
      else {
        setForm({ name: '', description: '', required_action: '', content: '', timeout_seconds: '60', enabled: true })
        setEditing(null)
        void refresh()
      }
    } catch (e) {
      setMsg(String(e?.message ?? e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (name) => {
    setBusy(true)
    try {
      const r = await rpc('script_delete', { name })
      if (r.ok !== true) setMsg(String(r.error ?? '删除失败'))
      void refresh()
    } finally { setBusy(false) }
  }

  const run = async (name) => {
    setBusy(true)
    setMsg(null)
    try {
      const r = await rpc('script_run', { name, args: {} })
      setOutputs((prev) => ({ ...prev, [name]: r }))
    } finally { setBusy(false) }
  }

  const head = React.createElement('div', { key: 'head', style: { fontSize: 14, fontWeight: 600, marginTop: 16 } },
    '白名单脚本（agent 写库的唯一通道，服务端执行）')

  const listItems = Array.isArray(scripts)
    ? scripts.map((s) => React.createElement('div', {
        key: s.name,
        style: { ...card, padding: '10px 12px' },
      },
        React.createElement('div', { style: { display: 'flex', gap: 8, alignItems: 'center' } },
          React.createElement('code', { style: { fontSize: 13 } }, s.name),
          React.createElement('span', { style: { fontSize: 12, opacity: 0.65 } }, s.description || ''),
          s.enabled ? null : React.createElement('span', { style: { fontSize: 12, color: '#d05663' } }, '已停用'),
          React.createElement('span', { style: { fontSize: 12, opacity: 0.65, marginLeft: 'auto' } },
            s.required_action ? `需权限: ${s.required_action}` : '仅需登录'),
        ),
        React.createElement('div', { style: { display: 'flex', gap: 8, marginTop: 8 } },
          React.createElement('button', {
            style: button, onClick: () => run(s.name), disabled: busy,
          }, '执行'),
          React.createElement('button', {
            style: button, onClick: () => {
              setEditing(s.name)
              setForm({
                name: s.name, description: s.description || '', required_action: s.required_action || '',
                content: s.content || '', timeout_seconds: String(s.timeout_seconds ?? 60), enabled: s.enabled !== false,
              })
            }, disabled: busy,
          }, '编辑'),
          React.createElement('button', {
            style: { ...button, color: '#d05663', borderColor: 'rgba(208,86,99,0.5)' },
            onClick: () => remove(s.name), disabled: busy,
          }, '删除'),
        ),
        outputs[s.name] ? React.createElement('pre', {
          style: {
            marginTop: 8, padding: 8, borderRadius: 6, fontSize: 11, lineHeight: 1.5,
            background: 'rgba(128,128,128,0.1)', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
            maxHeight: 200, overflow: 'auto',
          },
        }, `rc=${outputs[s.name].returncode ?? '?'}\n${outputs[s.name].stdout ?? ''}${outputs[s.name].stderr ? `\n[stderr]\n${outputs[s.name].stderr}` : ''}`) : null,
      ))
    : []

  const formEl = React.createElement('div', { key: 'form', style: card },
    React.createElement('div', { style: { ...label, fontSize: 13, opacity: 1 } },
      editing ? `编辑脚本：${editing}` : '新建脚本'),
    React.createElement('label', { style: label }, '名称（小写字母/数字/_/-）'),
    React.createElement('input', {
      style: input, value: form.name, disabled: editing !== null,
      onChange: (e) => setForm({ ...form, name: e.target.value }),
    }),
    React.createElement('label', { style: label }, '说明'),
    React.createElement('input', {
      style: input, value: form.description,
      onChange: (e) => setForm({ ...form, description: e.target.value }),
    }),
    React.createElement('label', { style: label }, '所需权限键（留空=仅需登录；如 action_maintenance_ledger_import）'),
    React.createElement('input', {
      style: input, value: form.required_action,
      onChange: (e) => setForm({ ...form, required_action: e.target.value }),
    }),
    React.createElement('label', { style: label }, '超时秒数（5-600）'),
    React.createElement('input', {
      style: input, value: form.timeout_seconds,
      onChange: (e) => setForm({ ...form, timeout_seconds: e.target.value }),
    }),
    React.createElement('label', { style: label }, 'Python 源码（env: ITD_DB_URL / ITD_USER / ITD_ROLE / ITD_ARGS_JSON）'),
    React.createElement('textarea', {
      style: { ...input, minHeight: 140, fontFamily: 'monospace' },
      value: form.content,
      onChange: (e) => setForm({ ...form, content: e.target.value }),
    }),
    React.createElement('div', { style: { display: 'flex', gap: 10 } },
      React.createElement('button', { style: button, onClick: save, disabled: busy }, '保存'),
      editing ? React.createElement('button', {
        style: button, onClick: () => {
          setEditing(null)
          setForm({ name: '', description: '', required_action: '', content: '', timeout_seconds: '60', enabled: true })
        },
      }, '取消') : null,
    ),
  )

  return React.createElement(React.Fragment, null,
    head,
    formEl,
    listItems.length > 0 ? React.createElement('div', { key: 'list' }, listItems) : null,
    msg ? React.createElement('div', { key: 'msg', style: { fontSize: 12, opacity: 0.7, marginTop: 8 } }, msg) : null,
  )
}
