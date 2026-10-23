import type { AuthzSnapshot, Decision } from "../api/authz";
import { EXECUTABLE_RESOURCES, EXECUTABLE_SURFACES, KNOWN_MODULES } from "./generated";

export interface Need {
  readonly resource_id: string;
  readonly actions: readonly string[];
  readonly field_permissions: readonly string[];
}
/** OR of complete AND clauses. Empty clauses never mean unrestricted. */
export interface Rule { readonly anyOf: readonly (readonly Need[])[] }
export interface ExecutableDefinition {
  readonly actions: Readonly<Record<string, {
    readonly kind: string;
    readonly requires_actions: readonly string[];
    readonly requires_any_actions: readonly string[];
    readonly requires_fields: readonly string[];
  }>>;
  readonly fields: Readonly<Record<string, readonly string[]>>;
  readonly scopes: Readonly<Record<string, { readonly requires_context: boolean }>>;
  readonly denial_scope: string;
}
export interface SurfaceDefinition {
  readonly resource_id: string;
  readonly display_actions: readonly string[];
  readonly page_routes: readonly string[];
  readonly route_entry: boolean;
  readonly data_rule: Rule;
}
export const vocabulary: Readonly<Record<string, ExecutableDefinition>> = EXECUTABLE_RESOURCES;
export const surfaces: Readonly<Record<string, SurfaceDefinition>> = EXECUTABLE_SURFACES;
export const knownModules: Readonly<Record<string, string>> = KNOWN_MODULES;
export const DENY_RULE: Rule = { anyOf: [] };
export const actionRule = (resource_id: string, ...actions: string[]): Rule => ({
  anyOf: [[{ resource_id, actions, field_permissions: [] }]],
});
export const anyRule = (...rules: Rule[]): Rule => ({ anyOf: rules.flatMap(r => r.anyOf) });
export function allRule(...rules: Rule[]): Rule {
  if (!rules.length) return DENY_RULE;
  return { anyOf: rules.reduce<readonly (readonly Need[])[]>(
    (clauses, rule) => clauses.flatMap(a => rule.anyOf.map(b => [...a, ...b])), [[]],
  ) };
}
export function surfaceRule(id: string): Rule {
  const surface = surfaces[id];
  return surface ? surface.display_actions.length ? allRule(
    actionRule(surface.resource_id, ...surface.display_actions), surface.data_rule,
  ) : surface.data_rule : DENY_RULE;
}
/** The index is derived from the manifest. Embedded surfaces never open a host route. */
export function routeRule(path: string): Rule {
  return anyRule(...Object.entries(surfaces)
    .filter(([, s]) => s.route_entry && s.page_routes.includes(path))
    .map(([id]) => surfaceRule(id)));
}
export function hasControlledRoute(path: string): boolean {
  return Object.values(surfaces).some(s => s.page_routes.includes(path));
}
export function actionAllowed(snapshot: AuthzSnapshot | null, resource: string, action: string): boolean {
  const definition = vocabulary[resource], state = snapshot?.resources[resource];
  if (snapshot?.schema_version !== 1 || !definition || !state ||
      state.migration_state !== "implemented" || state.runtime_state !== "enabled") return false;
  const visit = (id: string, visited: ReadonlySet<string>): boolean => {
    const descriptor = definition.actions[id];
    if (!descriptor || visited.has(id) || state.decisions[id]?.effect !== "allow") return false;
    const next = new Set(visited).add(id);
    return descriptor.requires_fields.every(f => state.field_permissions.includes(f)) &&
      descriptor.requires_actions.every(a => visit(a, next)) &&
      (!descriptor.requires_any_actions.length || descriptor.requires_any_actions.some(a => visit(a, next)));
  };
  return visit(action, new Set());
}
export function ruleAllowed(snapshot: AuthzSnapshot | null, rule: Rule,
  legacy: Readonly<Record<string, boolean>> = {}): boolean {
  if (snapshot?.schema_version !== 1) return false;
  return rule.anyOf.some(clause => clause.length > 0 && clause.every(need => {
    const state = snapshot.resources[need.resource_id], definition = vocabulary[need.resource_id];
    if (!definition || !state || !need.actions.length ||
        need.actions.some(a => !definition.actions[a]) ||
        need.field_permissions.some(f => !(f in definition.fields))) return false;
    if (state.runtime_state === "legacy" && state.migration_state !== "retired")
      return legacy[need.resource_id] === true;
    return need.actions.every(a => actionAllowed(snapshot, need.resource_id, a)) &&
      need.field_permissions.every(f => state.field_permissions.includes(f));
  }));
}
export type BlockAccess<K extends string = string> = Record<K, Decision>;
/** A response projection is local to that response. It cannot grant actor access. */
export function blockAllowed<K extends string>(blocks: BlockAccess<K> | undefined, key: K): boolean {
  return blocks?.[key]?.effect === "allow";
}
export function validateBlockAccess<K extends string>(value: unknown, keys: readonly K[]): BlockAccess<K> {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).length !== keys.length || keys.some(k => !(k in value)))
    throw new Error("数据块权限响应无效");
  for (const decision of Object.values(value)) {
    if (!decision || !["allow", "deny", "requires_context"].includes(decision.effect) ||
        !(decision.reason === null || (typeof decision.reason?.code === "string" && typeof decision.reason?.message === "string")))
      throw new Error("数据块权限响应无效");
  }
  return value as BlockAccess<K>;
}
