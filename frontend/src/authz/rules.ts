import type { AuthzSnapshot, Decision } from "../api/authz";
import { EXECUTABLE_RESOURCES, EXECUTABLE_SURFACES, KNOWN_MODULES } from "./generated";
import type { AuthorizationContext } from "./contextTypes";

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
export type RuleState = "deny" | "resolve" | "allow";
function everyState(values: readonly RuleState[]): RuleState {
  return !values.length || values.includes("deny") ? "deny" : values.includes("resolve") ? "resolve" : "allow";
}
function someState(values: readonly RuleState[]): RuleState {
  return values.includes("allow") ? "allow" : values.includes("resolve") ? "resolve" : "deny";
}
export function actionState(snapshot: AuthzSnapshot | null, resource: string, action: string,
  context?: AuthorizationContext | null): RuleState {
  const definition = vocabulary[resource], state = snapshot?.resources[resource];
  if (snapshot?.schema_version !== 1 || !definition || !state ||
      state.migration_state !== "implemented" || state.runtime_state !== "enabled") return "deny";
  const contextual = definition.scopes[state.scope_kind]?.requires_context === true;
  const bound = context?.resources[resource];
  const contextMatches = !!context && context.catalog_version === snapshot.catalog_version &&
    context.authorization_epoch === snapshot.authorization_epoch && !!bound &&
    (bound.grant_version ?? 0) === state.grant_version;
  const visit = (id: string, visited: ReadonlySet<string>, moduleDependency = false): RuleState => {
    const descriptor = definition.actions[id];
    const probe = state.decisions[id]?.effect;
    if (!descriptor || visited.has(id) || !probe || probe === "deny") return "deny";
    // Display is an explicit module switch. It never borrows read access or grants facts.
    const needsContext = !moduleDependency && descriptor.kind !== "display" && (contextual || probe === "requires_context");
    let own: RuleState = "allow";
    if (needsContext) {
      if (!context) own = "resolve";
      else if (!contextMatches || bound?.decisions[id]?.effect !== "allow") return "deny";
    } else if (!moduleDependency && probe !== "allow") return "deny";
    const fields = needsContext && context ? bound?.field_permissions ?? [] : state.field_permissions;
    if (!descriptor.requires_fields.every(f => state.field_permissions.includes(f) && fields.includes(f))) return "deny";
    const next = new Set(visited).add(id);
    // Display dependencies describe the module configuration, not a capability for
    // another object/operation. Surface data_rule and the entry independently resolve facts.
    const declarationOnly = moduleDependency || descriptor.kind === "display";
    return everyState([own, ...descriptor.requires_actions.map(a => visit(a, next, declarationOnly)),
      ...(descriptor.requires_any_actions.length ? [someState(descriptor.requires_any_actions.map(a => visit(a, next, declarationOnly)))] : [])]);
  };
  return visit(action, new Set());
}
export function actionAllowed(snapshot: AuthzSnapshot | null, resource: string, action: string,
  context?: AuthorizationContext | null): boolean {
  return actionState(snapshot, resource, action, context) === "allow";
}
/** Resolve only admits a fixed authorization view, never an ordinary business request. */
export function ruleState(snapshot: AuthzSnapshot | null, rule: Rule,
  legacy: Readonly<Record<string, boolean>> = {}, context?: AuthorizationContext | null): RuleState {
  if (snapshot?.schema_version !== 1) return "deny";
  return someState(rule.anyOf.map(clause => everyState(clause.map(need => {
    const state = snapshot.resources[need.resource_id], definition = vocabulary[need.resource_id];
    if (!definition || !state || !need.actions.length ||
        need.actions.some(a => !definition.actions[a]) ||
        need.field_permissions.some(f => !(f in definition.fields))) return "deny";
    if (state.runtime_state === "legacy" && state.migration_state !== "retired")
      return legacy[need.resource_id] === true ? "allow" : "deny";
    const contextual = definition.scopes[state.scope_kind]?.requires_context === true;
    const fields = contextual && context ? context.resources[need.resource_id]?.field_permissions ?? [] : state.field_permissions;
    if (!need.field_permissions.every(f => state.field_permissions.includes(f) && fields.includes(f))) return "deny";
    return everyState(need.actions.map(a => actionState(snapshot, need.resource_id, a, context)));
  }))));
}
export function ruleAllowed(snapshot: AuthzSnapshot | null, rule: Rule,
  legacy: Readonly<Record<string, boolean>> = {}, context?: AuthorizationContext | null): boolean {
  return ruleState(snapshot, rule, legacy, context) === "allow";
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
