// dsh-itdata — host half.
//
// IT 备件管理系统企业插件（宿主面）：
//  - 登录桥：用户经设置页「IT 备件系统」登录 IT 后端，token 仅保存在 host 进程内存，
//    供后续权限门工具（db_query / run_script / call_api）复用。
//  - /itd/ 前缀反代：/itd/api/* → IT 后端 /api/*；其余 /itd/* 服务于内嵌面板前端
//    （config.frontendDist 指向 vite base=/itd/ 的构建产物）。
//  - 模型工具 itdata_status：报告登录态 / 权限 / 后端可达性。
//
// 拓扑：每人本机一份 dsh（单用户），auth 为进程级单例；如未来改为共享拓扑，
// 需按 DSH session id 键控 token（见 dsh-enterprise/DSH企业定制实施计划.md §2.1）。

import http from 'node:http'
import https from 'node:https'
import { promises as fsp } from 'node:fs'
import path from 'node:path'

export const name = 'itdata'
export const inject = ['tools', 'webServer']

const RPC_PATH = '/plugins/itdata/rpc'
const PANEL_PREFIX = '/itd/'

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.map': 'application/json',
}

function jsonReply(res, status, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
  res.end(body)
}

export function apply(ctx, config) {
  const tools = ctx.tools
  const webServer = ctx.webServer
  const systemPrompt = ctx.get('systemPrompt')

  const backendOrigin = new URL(config?.backendUrl ?? 'http://127.0.0.1:8000')
  const backendModule = backendOrigin.protocol === 'https:' ? https : http
  const frontendDist = typeof config?.frontendDist === 'string' && config.frontendDist.trim() !== ''
    ? config.frontendDist.trim()
    : null

  // ── auth state（host-only；token 永不进入模型可见输出） ──────────────────
  const auth = {
    token: null,
    role: null,
    name: null,
    expiresAt: 0,
    permissions: null,
    loggedInAt: null,
  }

  function authSummary() {
    const now = Math.floor(Date.now() / 1000)
    return {
      loggedIn: auth.token !== null && auth.expiresAt > now,
      role: auth.role,
      name: auth.name,
      expiresAt: auth.expiresAt,
      loggedInAt: auth.loggedInAt,
      permissions: auth.permissions,
    }
  }

  // ── backend HTTP helper ───────────────────────────────────────────────
  function backendRequest(method, backendPath, { body, token, timeoutMs = 15000 } = {}) {
    return new Promise((resolve) => {
      const headers = { host: backendOrigin.host, accept: 'application/json' }
      let payload = null
      if (body !== undefined) {
        payload = JSON.stringify(body)
        headers['content-type'] = 'application/json'
        headers['content-length'] = Buffer.byteLength(payload)
      }
      if (token !== undefined && token !== null) headers.authorization = `Bearer ${token}`
      const req = backendModule.request(
        new URL(backendPath, backendOrigin),
        { method, headers, timeout: timeoutMs },
        (up) => {
          const chunks = []
          up.on('data', (c) => chunks.push(c))
          up.on('end', () => {
            const text = Buffer.concat(chunks).toString('utf8')
            let json = null
            try { json = JSON.parse(text) } catch { /* non-JSON body */ }
            resolve({ status: up.statusCode, json, text })
          })
        },
      )
      req.on('timeout', () => req.destroy(new Error('backend timeout')))
      req.on('error', (err) => resolve({ status: 0, json: null, text: String(err?.message ?? err) }))
      if (payload !== null) req.write(payload)
      req.end()
    })
  }

  // 后端健康探测（30s 缓存）：GET /api/auth/beta-features 无需鉴权
  let health = { state: 'unknown', at: 0, detail: null }
  async function probeHealth(force = false) {
    const now = Date.now()
    if (!force && now - health.at < 30000) return health
    const r = await backendRequest('GET', '/api/auth/beta-features', { timeoutMs: 4000 })
    health = r.status > 0 && r.status < 500
      ? { state: 'up', at: now, detail: null }
      : { state: 'down', at: now, detail: r.status === 0 ? r.text : `HTTP ${r.status}` }
    return health
  }

  // ── RPC 路由（client → host，包私有） ───────────────────────────────────
  ctx.effect(() => webServer.register({
    kind: 'exact',
    path: RPC_PATH,
    handler(req, res) {
      if (req.method !== 'POST') {
        res.writeHead(405, { allow: 'POST' })
        res.end()
        return
      }
      let body = ''
      req.on('data', (chunk) => {
        body += chunk
        if (body.length > 20000) req.destroy()
      })
      req.on('end', async () => {
        let msg = null
        try { msg = JSON.parse(body) } catch { jsonReply(res, 400, { error: 'malformed JSON' }); return }
        try {
          if (msg?.action === 'status') {
            const backend = await probeHealth()
            jsonReply(res, 200, { ok: true, auth: authSummary(), backend })
            return
          }
          if (msg?.action === 'login') {
            const username = typeof msg.username === 'string' ? msg.username.trim() : ''
            const password = typeof msg.password === 'string' ? msg.password : ''
            if (username === '' || password === '') {
              jsonReply(res, 400, { ok: false, error: '用户名和密码不能为空' })
              return
            }
            const r = await backendRequest('POST', '/api/auth/login', { body: { username, password } })
            if (r.status === 200 && r.json && typeof r.json.token === 'string') {
              auth.token = r.json.token
              auth.role = r.json.role ?? null
              auth.name = r.json.name ?? null
              auth.expiresAt = Number(r.json.expires_at ?? 0)
              auth.permissions = r.json.permissions ?? null
              auth.loggedInAt = Math.floor(Date.now() / 1000)
              jsonReply(res, 200, { ok: true, auth: authSummary() })
            } else {
              const detail = r.json?.detail ?? (r.status === 0 ? `后端不可达：${r.text}` : `HTTP ${r.status}`)
              jsonReply(res, 200, { ok: false, error: String(detail) })
            }
            return
          }
          if (msg?.action === 'logout') {
            auth.token = null
            auth.role = null
            auth.name = null
            auth.expiresAt = 0
            auth.permissions = null
            auth.loggedInAt = null
            jsonReply(res, 200, { ok: true, auth: authSummary() })
            return
          }
          jsonReply(res, 400, { ok: false, error: `unknown action: ${String(msg?.action)}` })
        } catch (error) {
          jsonReply(res, 500, { ok: false, error: String(error?.message ?? error) })
        }
      })
    },
  }))

  // ── /itd/ 面板前缀：/itd/api/* → 后端；其余 → 前端静态（SPA fallback） ───
  async function serveStatic(res, subPath) {
    if (frontendDist === null) {
      jsonReply(res, 501, { error: '面板前端未部署：请在插件配置 frontendDist 指向 vite base=/itd/ 构建产物' })
      return
    }
    const rel = subPath.replace(/^\/+/, '')
    const safeRel = path.normalize(rel).replace(/^(\.\.[/\\])+/, '')
    let file = path.join(frontendDist, safeRel)
    try {
      const stat = await fsp.stat(file).catch(() => null)
      if (stat === null || stat.isDirectory()) file = path.join(frontendDist, 'index.html')
      const data = await fsp.readFile(file)
      res.writeHead(200, { 'content-type': MIME[path.extname(file)] ?? 'application/octet-stream' })
      res.end(data)
    } catch {
      jsonReply(res, 404, { error: `not found: /itd/${rel}` })
    }
  }

  function proxyBackend(req, res, backendPath) {
    const headers = { ...req.headers, host: backendOrigin.host }
    delete headers['accept-encoding'] // 统一由 node 处理压缩，避免透传编码不匹配
    const upstream = backendModule.request(
      new URL(backendPath, backendOrigin),
      { method: req.method, headers, timeout: 120000 },
      (up) => {
        const outHeaders = { ...up.headers }
        delete outHeaders['transfer-encoding']
        delete outHeaders['connection']
        res.writeHead(up.statusCode, outHeaders)
        up.pipe(res)
      },
    )
    upstream.on('timeout', () => upstream.destroy(new Error('backend timeout')))
    upstream.on('error', (err) => {
      if (!res.headersSent) jsonReply(res, 502, { error: '后端不可达', detail: String(err?.message ?? err) })
      else try { res.end() } catch { /* already gone */ }
    })
    req.pipe(upstream)
  }

  ctx.effect(() => webServer.register({
    kind: 'prefix',
    path: PANEL_PREFIX,
    handler(req, res) {
      const sub = req.url.replace(/^\/itd/, '') || '/'
      if (sub === '/api' || sub.startsWith('/api/')) {
        proxyBackend(req, res, sub)
        return
      }
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405, { allow: 'GET, HEAD' })
        res.end()
        return
      }
      void serveStatic(res, sub)
    },
  }))

  // ── model tool: itdata_status ─────────────────────────────────────────
  const output = {
    schema: { type: 'object', properties: {}, required: [], additionalProperties: true },
    render(_args, value) {
      const text = JSON.stringify(value)
      return [{ type: 'text', text: text.length > 4000 ? `${text.slice(0, 4000)}\n…(truncated)` : text }]
    },
  }

  ctx.effect(() => tools.register({
    name: 'itdata_status',
    description: [
      'Report the IT spare-parts system integration state: whether a user is logged in (the permission gate for all itdata data tools),',
      'the logged-in role and permission summary, and backend reachability.',
      'Data tools (db_query / run_script / call_api) refuse to run while logged out; guide the user to Settings → IT 备件系统 to log in.',
    ].join(' '),
    parameters: {
      type: 'object',
      properties: {},
      required: [],
      additionalProperties: false,
    },
    output,
    async execute() {
      const backend = await probeHealth(true)
      const s = authSummary()
      return {
        ok: true,
        ...s,
        backend: { state: backend.state, detail: backend.detail },
        hint: s.loggedIn
          ? '权限门已就绪：数据工具将以该用户身份执行。'
          : '未登录：请让用户在 设置 → IT 备件系统 登录后重试。',
      }
    },
  }))

  // ── permission-gated data tools（P3） ──────────────────────────────────
  // 三个工具都要求登录：未登录返回 ok:false + 引导文案；后端按当前用户
  // RBAC（action_agent_sql / page_chat / data_* 脱敏 / own_customers_only）裁决。

  function notLoggedIn() {
    return {
      ok: false,
      error: 'NOT_LOGGED_IN',
      hint: '用户尚未登录 IT 备件系统。请让用户在 设置 → IT 备件系统 登录后重试（itdata_status 可查状态）。',
    }
  }

  function backendError(action, r) {
    const detail = r.json && r.json.detail ? String(r.json.detail) : `HTTP ${r.status}`
    const friendly = r.status === 401
      ? '登录已过期，请让用户重新登录（设置 → IT 备件系统）。'
      : r.status === 403
        ? `权限不足（${detail}）`
        : `${action} 失败：${detail}`
    return { ok: false, status: r.status, error: friendly }
  }

  let schemaCache = { token: null, at: 0, data: null }

  ctx.effect(() => tools.register({
    name: 'db_schema',
    description: [
      'Get the IT spare-parts database schema (curated metadata: tables, columns, types) for writing SQL.',
      'Sensitive system tables are excluded. Requires the user to be logged in (Settings → IT 备件系统).',
      'Use together with db_query to run the SQL you write.',
    ].join(' '),
    parameters: {
      type: 'object',
      properties: {
        refresh: { type: 'boolean', description: 'Force refresh the cached schema (default false).' },
      },
      required: [],
      additionalProperties: false,
    },
    output,
    async execute(args) {
      if (!authSummary().loggedIn) return notLoggedIn()
      const now = Date.now()
      if (args?.refresh !== true && schemaCache.token === auth.token && schemaCache.data !== null && now - schemaCache.at < 600000) {
        return { ok: true, cached: true, ...schemaCache.data }
      }
      const r = await backendRequest('GET', '/api/agent/schema', { token: auth.token, timeoutMs: 30000 })
      if (r.status !== 200) return backendError('db_schema', r)
      const data = { table_count: r.json?.table_count, tables: r.json?.tables }
      schemaCache = { token: auth.token, at: now, data }
      return { ok: true, cached: false, ...data }
    },
  }))

  ctx.effect(() => tools.register({
    name: 'db_query',
    description: [
      'Execute ONE read-only SQL statement (SELECT / WITH) against the IT spare-parts database (text2sql executor).',
      'Runs as the logged-in user: results are field-masked by their data permissions, sensitive system tables and writes are rejected,',
      'accounts restricted to their own customers (own_customers_only) cannot use this tool at all — use call_api instead.',
      'Write db_schema first to learn tables/columns. max_rows caps returned rows (default 100, max 500); truncated=true means more rows existed.',
      'This tool is READ-ONLY: never attempt UPDATE/INSERT/DELETE here.',
    ].join(' '),
    parameters: {
      type: 'object',
      properties: {
        sql: { type: 'string', description: 'A single read-only SELECT / WITH statement. No trailing semicolon needed.' },
        max_rows: { type: 'integer', description: 'Maximum rows to return (default 100, cap 500).' },
      },
      required: ['sql'],
      additionalProperties: false,
    },
    output,
    async execute(args) {
      if (!authSummary().loggedIn) return notLoggedIn()
      const body = { sql: args.sql }
      if (args.max_rows !== undefined) body.max_rows = args.max_rows
      const r = await backendRequest('POST', '/api/agent/sql', { token: auth.token, body, timeoutMs: 30000 })
      if (r.status !== 200) return backendError('db_query', r)
      const j = r.json
      const trimmedRows = j.rows.map((row) => {
        const out = {}
        for (const [k, v] of Object.entries(row)) {
          const s = typeof v === 'string' ? (v.length > 500 ? `${v.slice(0, 500)}…` : v) : v
          out[k] = s
        }
        return out
      })
      return {
        ok: true,
        columns: j.columns,
        rows: trimmedRows,
        row_count: j.row_count,
        truncated: j.truncated,
        elapsed_ms: j.elapsed_ms,
        hint: j.truncated ? '结果被截断：请加 WHERE / LIMIT 收窄查询。' : undefined,
      }
    },
  }))

  ctx.effect(() => tools.register({
    name: 'call_api',
    description: [
      'Call a whitelisted read-only business query tool on the IT spare-parts backend (search_parts, get_part_overview,',
      'lookup_prices_bulk, list_recent_purchases, get_profit_ranking, get_purchase_analysis, get_inventory,',
      'get_maintenance_board, get_maintenance_projects, get_maintenance_lines, get_cancellation_stats).',
      'These apply full business-layer permission filtering (row-level customer anonymization included) —',
      'prefer this over db_query for standard business questions.',
    ].join(' '),
    parameters: {
      type: 'object',
      properties: {
        tool: { type: 'string', description: 'Whitelisted tool name.' },
        args: { type: 'object', additionalProperties: true, description: 'Tool arguments (e.g. {"query": "轴承"}).' },
      },
      required: ['tool'],
      additionalProperties: false,
    },
    output,
    async execute(args) {
      if (!authSummary().loggedIn) return notLoggedIn()
      const r = await backendRequest('POST', '/api/agent/call', {
        token: auth.token,
        body: { tool: args.tool, args: args.args ?? {} },
        timeoutMs: 30000,
      })
      if (r.status !== 200) return backendError('call_api', r)
      return { ok: true, result: r.json }
    },
  }))

  // ── prompt section ────────────────────────────────────────────────────
  if (systemPrompt !== undefined) {
    ctx.effect(() => systemPrompt.section({
      name: 'itdata-guidance',
      order: 90,
      text: [
        '[IT 备件系统企业插件 — 权限门与数据访问规则]',
        '- 所有 IT 备件系统的数据访问必须走 itdata 系列工具（itdata_status / db_schema / db_query / call_api），它们携带当前登录用户的 token，由后端 RBAC（字段级/页面/动作/行级权限）过滤结果。',
        '- 写 SQL 前先 db_schema 看表结构；标准业务问题优先 call_api（业务工具带行级客户匿名化），自由分析才用 db_query（只读、字段脱敏）。',
        '- 未登录时不要尝试任何数据访问；引导用户到 设置 → IT 备件系统 登录（itdata_status 可随时查询状态）。',
        '- 严禁绕过权限门：不得直连数据库、不得伪造/复用他人凭据、不得把 token 写入脚本或文件。',
        '- Excel/Word/PDF 处理通过 bash 运行 Python 脚本完成（openpyxl / python-docx / pdfplumber 等）。',
        '- 数据库写操作（UPDATE/INSERT/覆盖）只能通过白名单脚本执行；临时脚本仅限只读查询。',
      ].join('\n'),
    }))
  }
}
