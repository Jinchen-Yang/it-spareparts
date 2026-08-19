// dsh-itdata-config — host-only 插件：模型锁定与远程下发（P6）。
//
// 启动与轮询企业配置端点（config.configUrl），把返回的唯一模型写入 DSH 的
// settings 命名空间：
//   llm-pi-ai.providers.enterprise-llm  ← 唯一 provider（replace 整节，清掉其余）
//   agent-default-model                 ← {provider, model} 固定
// apiKey 只写入进程环境（config.apiKeyEnv 引用的变量名），不落 settings 明文。
// 拉取失败保留上次成功配置；首次失败打日志并保持现状（不覆盖用户已有配置）。

export const name = 'itdata-config'
export const inject = ['settings', 'agentDefaultModel', 'timer']

export function apply(ctx, config) {
  const settings = ctx.settings
  const agentDefaultModel = ctx.agentDefaultModel

  const configUrl = typeof config?.configUrl === 'string' ? config.configUrl.trim() : ''
  const pollMs = Math.max(Number(config?.pollMs) || 300000, 10000)
  const configToken = typeof config?.configToken === 'string' ? config.configToken.trim() : ''
  const importUrl = typeof config?.importUrl === 'string' ? config.importUrl.trim() : ''

  if (configUrl === '' && importUrl === '') {
    console.log('[itdata-config] 未配置 configUrl/importUrl，模型锁定未启用')
    return
  }

  let lastGood = null // { providerSection, defaultSelection, keyEnv, keyValue }

  async function fetchConfig() {
    const url = configUrl !== '' ? configUrl : importUrl
    const headers = { accept: 'application/json' }
    if (configToken !== '') headers['x-dsh-config-token'] = configToken
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 10000)
    try {
      const resp = await fetch(url, { headers, signal: controller.signal })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      return await resp.json()
    } finally {
      clearTimeout(timer)
    }
  }

  async function applyConfig(cfg) {
    const provider = cfg.provider_id ?? 'enterprise-llm'
    const keyEnv = cfg.api_key_env ?? 'DSH_ENTERPRISE_LLM_KEY'
    const keyValue = typeof cfg.api_key === 'string' ? cfg.api_key : ''
    const models = Array.isArray(cfg.models) && cfg.models.length > 0
      ? cfg.models
      : [{ id: cfg.default_model, name: cfg.default_model }]

    // 1) 模型键写入进程环境（llm-pi-ai 按 apiKeyEnv 引用）
    if (keyValue !== '') process.env[keyEnv] = keyValue

    // 2) llm-pi-ai 整节替换：唯一 provider，清掉用户自定义
    const providerSection = {
      providers: {
        [provider]: {
          apiKeyEnv: keyEnv,
          displayName: cfg.display_name ?? '企业统一模型',
          api: cfg.api ?? 'openai-completions',
          baseURL: cfg.base_url,
          models,
        },
      },
    }
    const cur = (() => {
      try { return settings.get('llm-pi-ai') } catch { return undefined }
    })()
    if (JSON.stringify(cur) !== JSON.stringify(providerSection)) {
      await settings.replace('llm-pi-ai', providerSection)
      console.log(`[itdata-config] llm-pi-ai 已锁定到 provider=${provider} model=${cfg.default_model}`)
    }

    // 3) agent-default-model 固定
    const selection = { provider, model: cfg.default_model }
    const curDefault = agentDefaultModel.currentSelection()
    if (curDefault.provider !== selection.provider || curDefault.model !== selection.model) {
      await agentDefaultModel.saveSelection(selection)
      console.log(`[itdata-config] agent-default-model 已固定为 ${provider}/${cfg.default_model}`)
    }

    lastGood = { providerSection, selection, keyEnv, keyValue }
  }

  async function poll() {
    try {
      const cfg = await fetchConfig()
      if (cfg?.enabled === false || !cfg?.base_url || !cfg?.default_model) {
        console.warn('[itdata-config] 配置响应缺少 base_url/default_model，忽略')
        return
      }
      await applyConfig(cfg)
    } catch (error) {
      console.warn(`[itdata-config] 拉取配置失败（保留上次成功配置）：${error?.message ?? error}`)
    }
  }

  void poll()
  return ctx.timer.interval(() => { void poll() }, pollMs)
}
