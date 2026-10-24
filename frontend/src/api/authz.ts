import { api } from "../api";
import { knownModules, vocabulary, surfaces as generatedSurfaces, type Rule } from "../authz/rules";

export const AUTHZ_FIELDS = [
  { id: "data_supplier", label: "供应商信息" },
  { id: "data_customer", label: "客户信息" },
  { id: "data_purchase_cost", label: "成本及销售金额" },
  { id: "data_profit", label: "利润与毛利率" },
  { id: "data_pool_price_governance", label: "池价格治理" },
  { id: "data_reconciliation", label: "销售对账信息" },
] as const;
export type SalesScope = "all" | "assigned_customers";
export interface AuthzSnapshot extends AuthzEnvelope {
  principal: {
    id: number;
    username: string;
    display_name: string | null;
  } | null;
  authorization_epoch: number | null;
  actor_capabilities: Record<ActorCapability, Decision>;
  resources: Record<string, ResourceDecision>;
  version: string;
  username?: string | null;
  can_manage: boolean;
  sales_analysis: {
    enabled: boolean;
    read: boolean;
    export: boolean;
    scope: SalesScope | "none";
    customer_count: number;
    reason: string;
    field_permissions: string[];
  };
}
export interface AuthzCustomer {
  id: number;
  name: string;
  raw_name?: string;
}
export interface SalesAuthorization {
  username: string;
  version: number;
  actions: string[];
  scope: SalesScope;
  field_permissions: string[];
  customer_ids: number[];
  customers: AuthzCustomer[];
}
export interface SalesAuthorizationUpdate {
  expected_version: number;
  actions: string[];
  scope: SalesScope;
  field_permissions: string[];
  customer_ids: number[];
}
const config = (token: string, signal?: AbortSignal) => ({
  headers: { Authorization: `Bearer ${token}` },
  signal,
});
export const getAuthzSnapshot = (token: string, signal?: AbortSignal) =>
  api.get<AuthzSnapshot>("/authz/me", config(token, signal));
export const getSalesAuthorization = (
  username: string,
  token: string,
  signal?: AbortSignal,
) =>
  api.get<SalesAuthorization>(
    `/authz/users/${encodeURIComponent(username)}`,
    config(token, signal),
  );
export const updateSalesAuthorization = (
  username: string,
  body: SalesAuthorizationUpdate,
  token: string,
) =>
  api.put<SalesAuthorization>(
    `/authz/users/${encodeURIComponent(username)}`,
    body,
    config(token),
  );
export const searchAuthzCustomers = (
  q: string,
  token: string,
  signal?: AbortSignal,
) =>
  api.get<{ items: AuthzCustomer[] }>("/authz/customers", {
    ...config(token, signal),
    params: { q, limit: 50 },
  });

export function validateAuthzSnapshot(value: AuthzSnapshot): AuthzSnapshot {
  validateEnvelope(value);
  if (
    !record(value.resources) ||
    !record(value.actor_capabilities) ||
    ACTOR_CAPABILITIES.some(
      (k) => !validDecision(value.actor_capabilities[k]),
    ) ||
    (value.authorization_epoch !== null &&
      !safeVersion(value.authorization_epoch))
  )
    protocolError();
  for (const [id, r] of Object.entries(value.resources)) {
    if (
      !(id in knownModules) ||
      knownModules[id] !== r.migration_state ||
      !validResourceState(r) ||
      !safeVersion(r.grant_version) ||
      !record(r.decisions) ||
      !uniqueStrings(r.field_permissions)
    )
      protocolError();
    if (
      r.migration_state === "implemented" &&
      (!vocabulary[id] ||
        Object.keys(r.decisions).some((a) => !(a in vocabulary[id].actions)) ||
        r.field_permissions.some((f) => !(f in vocabulary[id].fields)) ||
        (r.scope_kind !== "none" && !(r.scope_kind in vocabulary[id].scopes)))
    )
      protocolError();
    for (const decision of Object.values(r.decisions))
      if (
        !validDecision(decision) ||
        ((r.migration_state !== "implemented" ||
          r.runtime_state !== "enabled") &&
          decision.effect !== "deny")
      )
        protocolError();
    if (r.runtime_state !== "enabled" && r.field_permissions.length)
      protocolError();
  }
  const s = value?.sales_analysis;
  if (
    typeof value?.version !== "string" ||
    typeof value.can_manage !== "boolean" ||
    !s ||
    [s.enabled, s.read, s.export].some((v) => typeof v !== "boolean") ||
    !["all", "assigned_customers", "none"].includes(s.scope) ||
    !Number.isInteger(s.customer_count) ||
    s.customer_count < 0 ||
    typeof s.reason !== "string" ||
    !Array.isArray(s.field_permissions) ||
    s.field_permissions.some((k) => !AUTHZ_FIELDS.some((f) => f.id === k))
  ) {
    throw new Error("权限响应格式无效");
  }
  return value;
}

// S1 wire: control-plane configuration is separate from the actor's runtime decisions.
export interface AuthzEnvelope {
  schema_version: 1;
  authorization_mode: "legacy_compat" | "unified_modules";
  catalog_version: string;
}
export interface Decision {
  effect: "allow" | "deny" | "requires_context";
  reason: { code: string; message: string } | null;
}
export type ActorCapability =
  | "read_accounts"
  | "edit_account_profiles"
  | "manage_authorization"
  | "manage_templates"
  | "create_accounts"
  | "reset_account_passwords"
  | "manage_account_lifecycle";
export const ACTOR_CAPABILITIES: ActorCapability[] = [
  "read_accounts",
  "edit_account_profiles",
  "manage_authorization",
  "manage_templates",
  "create_accounts",
  "reset_account_passwords",
  "manage_account_lifecycle",
];
export interface ResourceState {
  migration_state: "planned" | "implemented" | "retired";
  runtime_state: "legacy" | "enabled" | "suspended";
  configurable: boolean;
}
export interface ResourceDecision extends ResourceState {
  grant_version: number;
  decisions: Record<string, Decision>;
  scope_kind: string;
  field_permissions: string[];
}
export interface ResourceConfiguration {
  actions: string[];
  scope: string;
  field_permissions: string[];
}
export interface ResourceDescriptor extends ResourceState {
  id: string;
  label: string;
  actions: {
    id: string;
    label: string;
    kind: "data" | "display" | "mutation";
    requires_actions: string[];
    requires_any_actions: string[];
    requires_fields: string[];
  }[];
  scopes: {
    id: string;
    label: string;
    relationship_kind: string | null;
    requires_context: boolean;
  }[];
  denial_scope: string | null;
  field_permissions: { id: string; label: string; requires_fields: string[] }[];
  surfaces: {
    id: string;
    label: string;
    display_actions: string[];
    page_routes: string[];
    route_entry: boolean;
    data_rule: Rule;
    data_requirements: { resource_id: string; actions: string[] }[];
  }[];
  legacy_sources: string[];
  notice: string | null;
}
export interface AuthzCatalog extends AuthzEnvelope {
  modules: ResourceDescriptor[];
}
export interface AuthorizationSubject {
  id: number;
  username: string;
  display_name: string | null;
  role: string;
  salesperson_name: string | null;
  is_active: boolean;
}
export interface ResourceAuthorization {
  version: number;
  configuration: ResourceConfiguration | null;
  source: "personal_override" | "template_snapshot" | "none";
  template_snapshot: {
    code: string;
    version: number;
    configuration: ResourceConfiguration | null;
  } | null;
  personal_override: {
    present: boolean;
    configuration: ResourceConfiguration | null;
  };
}
export interface AccountModules extends AuthzEnvelope {
  subject: AuthorizationSubject;
  authorization_epoch: number;
  template: {
    code: string;
    version: number;
    current_version: number;
    stale: boolean;
  } | null;
  customer_assignments: { ids: number[]; customers: AuthzCustomer[] };
  resources: Record<string, ResourceAuthorization>;
}
export interface TemplateModules extends AuthzEnvelope {
  template: {
    code: string;
    name: string;
    description: string | null;
    base_role: string;
    is_system: boolean;
    is_active: boolean;
    locked: boolean;
    version: number;
  };
  modules: Record<string, ResourceConfiguration>;
}
export interface Target {
  user_id: number;
  username: string;
  expected_epoch: number;
  expected_resource_versions?: Record<string, number>;
}
export type ResourceOperation =
  | {
      resource_id: string;
      expected_version: number;
      operation: "replace_resource" | "revoke_resource";
      configuration: ResourceConfiguration;
    }
  | {
      resource_id: string;
      expected_version: number;
      operation: "clear_override";
    };
export type BulkCommandOperation =
  | {
      kind: "apply_template" | "sync_template";
      template_code: string;
      expected_template_version: number;
      override_policy: "preserve" | "clear";
    }
  | {
      kind: "replace_resources";
      updates: { resource_id: string; configuration: ResourceConfiguration }[];
    }
  | { kind: "revoke_resources" | "clear_overrides"; resource_ids: string[] };
export interface ResourceDiff {
  resource_id: string;
  version_before: number;
  version_after: number;
  configuration_before: ResourceConfiguration | null;
  configuration_after: ResourceConfiguration | null;
  override_before: {
    present: boolean;
    configuration: ResourceConfiguration | null;
  };
  override_after: {
    present: boolean;
    configuration: ResourceConfiguration | null;
  };
  change: string;
  actions_added: string[];
  actions_removed: string[];
  scope_before: string | null;
  scope_after: string | null;
  fields_added: string[];
  fields_removed: string[];
}
export interface AuthorizationPreview extends AuthzEnvelope {
  dry_run: true;
  committed: false;
  fingerprint: string;
  command: Record<string, unknown>;
  affected?: number;
  preview:
    | {
        subject: AuthorizationSubject;
        epoch_before: number;
        epoch_after: number;
        template_before: { code: string; version: number } | null;
        template_after: { code: string; version: number } | null;
        effective_changed: boolean;
        source_changed: boolean;
        session_revoked: boolean;
        resources: ResourceDiff[];
      }[]
    | {
        subject?: AuthorizationSubject;
        epoch_before?: number;
        epoch_after?: number;
        security_before?: Record<string, unknown>;
        security_after?: Record<string, unknown>;
        legacy_role_behavior_may_change?: boolean;
        session_revoked?: boolean;
        [key: string]: unknown;
      };
}
export interface AuthorizationResult extends AuthzEnvelope {
  committed: true;
  actor_session_revoked: boolean;
  result?: AccountModules | TemplateModules;
  account?: AccountModules;
  results?: AccountModules[];
  applied?: number;
  [key: string]: unknown;
}

const record = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const uniqueStrings = (v: unknown): v is string[] =>
  Array.isArray(v) &&
  v.every((x) => typeof x === "string") &&
  new Set(v).size === v.length;
export const safeVersion = (v: unknown): v is number =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const protocolError = (): never => {
  throw new Error("权限协议无效，请重新加载；未采用旧权限回退。");
};
export function validateEnvelope<T extends AuthzEnvelope>(v: T): T {
  if (
    !record(v) ||
    v.schema_version !== 1 ||
    !["legacy_compat", "unified_modules"].includes(v.authorization_mode) ||
    typeof v.catalog_version !== "string" ||
    !v.catalog_version
  )
    protocolError();
  return v;
}
export function validResourceState(r: ResourceState): boolean {
  return (
    !!r &&
    typeof r.configurable === "boolean" &&
    (((r.migration_state === "planned" || r.migration_state === "retired") &&
      r.runtime_state === "legacy" &&
      !r.configurable) ||
      (r.migration_state === "implemented" &&
        ["legacy", "enabled", "suspended"].includes(r.runtime_state) &&
        (r.runtime_state !== "legacy" || !r.configurable)))
  );
}
function validDecision(v: unknown): v is Decision {
  return (
    record(v) &&
    ["allow", "deny", "requires_context"].includes(v.effect) &&
    (v.reason === null ||
      (record(v.reason) &&
        typeof v.reason.code === "string" &&
        typeof v.reason.message === "string"))
  );
}
function ruleSignature(rule: Rule): string {
  if (!rule || !Array.isArray(rule.anyOf)) return protocolError();
  return JSON.stringify(rule.anyOf.map(clause => {
    if (!Array.isArray(clause) || !clause.length) return protocolError();
    return clause.map(need => {
      if (!need || typeof need.resource_id !== "string" || !uniqueStrings(need.actions) || !need.actions.length || !uniqueStrings(need.field_permissions)) return protocolError();
      return [need.resource_id, [...need.actions].sort(), [...need.field_permissions].sort()];
    });
  }));
}
const sameStrings = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every(x => b.includes(x));
export function validateCatalog(v: AuthzCatalog): AuthzCatalog {
  validateEnvelope(v);
  if (
    !Array.isArray(v.modules) ||
    new Set(v.modules.map((m) => m.id)).size !== v.modules.length
  )
    protocolError();
  for (const m of v.modules) {
    if (
      !(m.id in knownModules) ||
      knownModules[m.id] !== m.migration_state ||
      !validResourceState(m) ||
      typeof m.id !== "string" ||
      typeof m.label !== "string" ||
      !uniqueStrings(m.legacy_sources) ||
      !Array.isArray(m.actions) ||
      !Array.isArray(m.field_permissions) ||
      !Array.isArray(m.scopes) ||
      !Array.isArray(m.surfaces)
    )
      protocolError();
    const actions = m.actions.map((a) => a.id),
      fields = m.field_permissions.map((f) => f.id),
      scopes = m.scopes.map((s) => s.id);
    if (
      ![actions, fields, scopes].every(uniqueStrings) ||
      (m.denial_scope !== null && !scopes.includes(m.denial_scope))
    )
      protocolError();
    for (const a of m.actions)
      if (
        !["data", "display", "mutation"].includes(a.kind) ||
        !uniqueStrings(a.requires_actions) ||
        !uniqueStrings(a.requires_any_actions) ||
        !uniqueStrings(a.requires_fields) ||
        a.requires_actions.some((id) => !actions.includes(id)) ||
        a.requires_any_actions.some((id) => !actions.includes(id)) ||
        a.requires_fields.some((id) => !fields.includes(id))
      )
        protocolError();
    for (const f of m.field_permissions)
      if (
        !uniqueStrings(f.requires_fields) ||
        f.requires_fields.some((id) => !fields.includes(id))
      )
        protocolError();
    if (
      m.migration_state === "implemented" &&
      (!vocabulary[m.id] ||
        !sameStrings(actions, Object.keys(vocabulary[m.id].actions)) ||
        !sameStrings(fields, Object.keys(vocabulary[m.id].fields)) ||
        !sameStrings(scopes, Object.keys(vocabulary[m.id].scopes)) ||
        m.denial_scope !== vocabulary[m.id].denial_scope ||
        m.field_permissions.some(f => !sameStrings(f.requires_fields, vocabulary[m.id].fields[f.id])) ||
        m.scopes.some(s => s.requires_context !== vocabulary[m.id].scopes[s.id].requires_context) ||
        m.actions.some(a => {
          const expected = vocabulary[m.id].actions[a.id];
          return a.kind !== expected.kind || !sameStrings(a.requires_actions, expected.requires_actions) ||
            !sameStrings(a.requires_any_actions, expected.requires_any_actions) || !sameStrings(a.requires_fields, expected.requires_fields);
        }))
    )
      protocolError();
  }
  for (const m of v.modules)
    for (const s of m.surfaces) {
      if (
        !uniqueStrings(s.display_actions) ||
        s.display_actions.some((a) => !m.actions.some((x) => x.id === a)) ||
        !Array.isArray(s.data_requirements)
      )
        protocolError();
      if (m.migration_state === "implemented") {
        const expected = generatedSurfaces[s.id];
        if (!expected || !uniqueStrings(s.page_routes) || typeof s.route_entry !== "boolean" ||
            !sameStrings(s.page_routes, expected.page_routes) || s.route_entry !== expected.route_entry ||
            !sameStrings(s.display_actions, expected.display_actions) ||
            ruleSignature(s.data_rule) !== ruleSignature(expected.data_rule)) protocolError();
      }
      for (const r of s.data_requirements) {
        const source = v.modules.find((x) => x.id === r.resource_id);
        if (
          !source ||
          !uniqueStrings(r.actions) ||
          r.actions.some((a) => !source.actions.some((x) => x.id === a))
        )
          protocolError();
      }
    }
  return v;
}
export function configurationErrors(
  m: ResourceDescriptor,
  c: ResourceConfiguration,
): string[] {
  if (
    !c ||
    !uniqueStrings(c.actions) ||
    !uniqueStrings(c.field_permissions) ||
    !m.scopes.some((s) => s.id === c.scope)
  )
    return ["资源配置或数据范围无效"];
  const errors: string[] = [];
  for (const id of c.actions) {
    const a = m.actions.find((x) => x.id === id);
    if (!a) {
      errors.push(`未知动作：${id}`);
      continue;
    }
    for (const need of a.requires_actions)
      if (!c.actions.includes(need))
        errors.push(
          `${a.label}需要动作「${m.actions.find((x) => x.id === need)?.label || need}」`,
        );
    if (a.requires_any_actions.length && !a.requires_any_actions.some(need => c.actions.includes(need)))
      errors.push(`${a.label}需要以下任一动作：${a.requires_any_actions.map(need => m.actions.find(x => x.id === need)?.label || need).join("、")}`);
    for (const need of a.requires_fields)
      if (!c.field_permissions.includes(need))
        errors.push(
          `${a.label}需要字段「${m.field_permissions.find((x) => x.id === need)?.label || need}」`,
        );
  }
  for (const id of c.field_permissions) {
    const f = m.field_permissions.find((x) => x.id === id);
    if (!f) {
      errors.push(`未知字段：${id}`);
      continue;
    }
    for (const need of f.requires_fields)
      if (!c.field_permissions.includes(need))
        errors.push(
          `${f.label}需要字段「${m.field_permissions.find((x) => x.id === need)?.label || need}」`,
        );
  }
  return errors;
}
export function denialConfiguration(
  m: ResourceDescriptor,
): ResourceConfiguration {
  if (!m.denial_scope) protocolError();
  return { actions: [], scope: m.denial_scope!, field_permissions: [] };
}
export function validateAccountModules(
  v: AccountModules,
  catalog: AuthzCatalog,
): AccountModules {
  validateEnvelope(v);
  if (
    v.catalog_version !== catalog.catalog_version ||
    !record(v.subject) ||
    !safeVersion(v.subject.id) ||
    !v.subject.id ||
    !safeVersion(v.authorization_epoch) ||
    !record(v.resources) ||
    !record(v.customer_assignments) ||
    !Array.isArray(v.customer_assignments.ids) ||
    v.customer_assignments.ids.some((id) => !safeVersion(id) || !id) ||
    new Set(v.customer_assignments.ids).size !==
      v.customer_assignments.ids.length ||
    !Array.isArray(v.customer_assignments.customers)
  )
    protocolError();
  for (const [id, r] of Object.entries(v.resources)) {
    const m = catalog.modules.find((x) => x.id === id);
    if (!m) return protocolError();
    if (
      !safeVersion(r.version) ||
      !["personal_override", "template_snapshot", "none"].includes(r.source) ||
      !record(r.personal_override) ||
      typeof r.personal_override.present !== "boolean" ||
      r.personal_override.present !==
        (r.personal_override.configuration !== null)
    )
      protocolError();
    for (const c of [
      r.configuration,
      r.personal_override.configuration,
      r.template_snapshot?.configuration ?? null,
    ])
      if (
        c !== null &&
        (m.migration_state !== "implemented" ||
          configurationErrors(m, c).length)
      )
        protocolError();
  }
  return v;
}
export function validateTemplateModules(
  v: TemplateModules,
  catalog: AuthzCatalog,
): TemplateModules {
  validateEnvelope(v);
  if (
    v.catalog_version !== catalog.catalog_version ||
    !record(v.template) ||
    !safeVersion(v.template.version) ||
    !v.template.version ||
    !record(v.modules)
  )
    protocolError();
  for (const [id, c] of Object.entries(v.modules)) {
    const m = catalog.modules.find((x) => x.id === id);
    if (
      !m ||
      m.migration_state !== "implemented" ||
      configurationErrors(m, c).length
    )
      protocolError();
  }
  return v;
}
export function requestIdentity(token: string, signal?: AbortSignal) {
  if (!token || localStorage.getItem("token") !== token)
    throw new Error("当前登录已改变，请重新打开窗口。");
  return config(token, signal);
}
export const getAuthzCatalog = async (token: string, signal?: AbortSignal) =>
  validateCatalog(
    (
      await api.get<AuthzCatalog>(
        "/authz/catalog",
        requestIdentity(token, signal),
      )
    ).data,
  );
export const getAccountModules = async (
  username: string,
  token: string,
  catalog: AuthzCatalog,
  signal?: AbortSignal,
) => {
  const data = validateAccountModules(
    (
      await api.get<AccountModules>(
        `/authz/users/${encodeURIComponent(username)}/modules`,
        requestIdentity(token, signal),
      )
    ).data,
    catalog,
  );
  if (data.subject.username !== username) protocolError();
  return data;
};
export const getTemplateModules = async (
  code: string,
  token: string,
  catalog: AuthzCatalog,
  signal?: AbortSignal,
) => {
  const data = validateTemplateModules(
    (
      await api.get<TemplateModules>(
        `/authz/templates/${encodeURIComponent(code)}/modules`,
        requestIdentity(token, signal),
      )
    ).data,
    catalog,
  );
  if (data.template.code !== code) protocolError();
  return data;
};
export async function authzCommand(
  path: string,
  method: "post" | "put" | "delete",
  body: Record<string, unknown>,
  token: string,
): Promise<AuthorizationPreview | AuthorizationResult> {
  const { data } = await api.request<
    AuthorizationPreview | AuthorizationResult
  >({ url: path, method, data: body, ...requestIdentity(token) });
  validateEnvelope(data);
  if (
    data.authorization_mode !== "unified_modules" ||
    data.catalog_version !== body.catalog_version
  )
    protocolError();
  if (data.committed === false) {
    if (
      data.dry_run !== true ||
      typeof data.fingerprint !== "string" ||
      !/^[a-f0-9]{64}$/.test(data.fingerprint) ||
      !record(data.command) ||
      !data.preview
    )
      protocolError();
  } else if (
    data.committed !== true ||
    typeof data.actor_session_revoked !== "boolean"
  )
    protocolError();
  return data;
}
