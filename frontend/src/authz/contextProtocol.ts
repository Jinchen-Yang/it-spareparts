import type { AuthzSnapshot, Decision } from "../api/authz";
import {
  CONTEXT_KINDS, CONTEXT_COLLECTION_REFS, CONTEXT_ENTRY_CONTRACTS, CONTEXT_AUTHORIZATION_VIEWS,
} from "./generated";
import { ruleAllowed, vocabulary, type Rule } from "./rules";
import { contextRefKey, type AuthorizationContext, type AuthorizationResolution, type ContextRef } from "./contextTypes";

interface ContextSlot {
  readonly kind: string;
  readonly object_type?: string;
  readonly collection_ref?: string;
  readonly authorizing?: boolean;
}
interface EntryContract {
  readonly branches: Readonly<Record<string, {
    readonly requirements: Readonly<Record<string, readonly string[]>>;
    readonly carrier_slots: readonly string[];
    readonly slots: readonly string[];
    readonly authorization_view_only?: boolean;
    readonly admission_rule?: { readonly anyOf: readonly { readonly resource_id: string; readonly action: string; readonly required_fields: readonly string[] }[] };
  }>>;
  readonly slots: Readonly<Record<string, ContextSlot>>;
}
interface AuthorizationView {
  readonly method: string;
  readonly path: string;
  readonly view: string;
  readonly only_entry_actions: boolean;
  readonly no_business_payload: boolean;
  readonly authorization_requirements?: Readonly<Record<string, readonly string[]>>;
  readonly target_entry_ids?: readonly string[];
  readonly locator_path_parameter?: string;
}
export const contextEntries: Readonly<Record<string, EntryContract>> = CONTEXT_ENTRY_CONTRACTS;
export const authorizationViews: Readonly<Record<string, AuthorizationView>> = CONTEXT_AUTHORIZATION_VIEWS;
export class ContextProtocolError extends Error {
  readonly response = { status: 503 };
  constructor() { super("对象权限响应无效，请重新检查访问权限"); this.name = "ContextProtocolError"; }
}
export class ContextChanged extends Error {
  readonly response = { status: 409 };
  constructor() { super("对象或权限已改变，请重新核对；本次操作未自动重试"); this.name = "ContextChanged"; }
}
export class ContextDenied extends Error {
  readonly response = { status: 403 };
  constructor() { super("没有此对象的操作权限"); this.name = "ContextDenied"; }
}
function fail(): never { throw new ContextProtocolError(); }
function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function keys(value: Record<string, unknown>, expected: readonly string[]) {
  return Object.keys(value).length === expected.length && expected.every(k => Object.hasOwnProperty.call(value, k));
}
function text(value: unknown, max = 256): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= max;
}
function uniqueStrings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(v => typeof v === "string") && new Set(value).size === value.length;
}
function decision(value: unknown): value is Decision {
  return record(value) && keys(value, ["effect", "reason"]) &&
    ["allow", "deny", "requires_context"].includes(String(value.effect)) &&
    (value.reason === null || (record(value.reason) && keys(value.reason, ["code", "message"]) &&
      typeof value.reason.code === "string" && typeof value.reason.message === "string"));
}
export interface ContextExpectation {
  /** A literal registered backend entry; never an arbitrary client action list. */
  entryId: string;
  /** Stable locator known from the actual path. operation_input IDs are server generated. */
  locator?: { kind: "project" | "collection" | "object"; id: string; object_type?: string };
  authorizationView?: boolean;
  pathParams?: Readonly<Record<string, string | number>>;
}
function slotMatches(slot: ContextSlot | undefined, ref: ContextRef) {
  return slot?.kind === ref.kind && (ref.kind !== "object" || slot.object_type === ref.object_type) &&
    (ref.kind !== "collection" || slot.collection_ref === ref.id);
}
export function entryRule(entryId: string): Rule {
  const entry = contextEntries[entryId];
  return { anyOf: entry ? Object.values(entry.branches).filter(branch => !branch.authorization_view_only).flatMap(branch => branch.admission_rule
    ? branch.admission_rule.anyOf.map(need => [{ resource_id: need.resource_id, actions: [need.action], field_permissions: [...need.required_fields] }])
    : [Object.entries(branch.requirements).map(([resource_id, actions]) => ({ resource_id, actions: [...actions], field_permissions: [] }))]) : [] };
}
/** Validate before storing or projecting; no fields or decisions are borrowed from another context. */
export function validateAuthorizationContext(value: unknown, snapshot: AuthzSnapshot,
  expected: ContextExpectation): AuthorizationContext {
  if (!record(value) || !keys(value, ["schema_version", "catalog_version", "authorization_epoch", "context", "resources"]) ||
      value.schema_version !== 1 || value.catalog_version !== snapshot.catalog_version ||
      value.authorization_epoch !== snapshot.authorization_epoch || !Number.isSafeInteger(value.authorization_epoch) ||
      Number(value.authorization_epoch) < 0 || !record(value.context) || !record(value.resources)) fail();
  const raw = value.context;
  if (!["project", "collection", "object"].includes(String(raw.kind)) || !text(raw.id) ||
      !text(raw.authorization_revision, 2048) || !keys(raw, raw.kind === "object" ?
        ["kind", "object_type", "id", "authorization_revision"] : ["kind", "id", "authorization_revision"])) fail();
  if (raw.kind === "object" && (typeof raw.object_type !== "string" ||
      !(CONTEXT_KINDS as readonly string[]).includes(raw.object_type) || ["project", "collection"].includes(raw.object_type))) fail();
  if (raw.kind === "collection" && !(CONTEXT_COLLECTION_REFS as readonly string[]).includes(raw.id)) fail();
  const ref = raw as unknown as ContextRef;
  const locatorParameter = authorizationViews[expected.entryId]?.locator_path_parameter;
  if (locatorParameter && expected.pathParams && ref.id !== String(expected.pathParams[locatorParameter])) fail();
  const locator = expected.locator;
  if (locator && (ref.kind !== locator.kind || ref.id !== locator.id ||
      (ref.kind === "object" && ref.object_type !== locator.object_type))) fail();
  const entry = contextEntries[expected.entryId];
  if (!entry) fail();
  const branches = Object.values(entry.branches).filter(branch =>
    (expected.authorizationView || !branch.authorization_view_only) &&
    branch.carrier_slots.some(slot => slotMatches(entry.slots[slot], ref)));
  if (!branches.length) fail();
  // The actual server-selected branch may contain deny decisions. It must still be a complete registered contract.
  const permitted = branches.some(branch => {
    const resources = value.resources as Record<string, unknown>;
    if (!keys(resources, Object.keys(branch.requirements))) return false;
    return Object.entries(branch.requirements).every(([resource, actions]) => {
      const data = resources[resource];
      return record(data) && record(data.decisions) && keys(data.decisions, actions);
    });
  });
  if (!permitted) fail();
  for (const [resource, data] of Object.entries(value.resources)) {
    const definition = vocabulary[resource], actor = snapshot.resources[resource];
    if (!record(data) || !keys(data, ["grant_version", "decisions", "field_permissions"]) ||
        !definition || !actor || !record(data.decisions) || !uniqueStrings(data.field_permissions) ||
        (data.grant_version !== null && (!Number.isSafeInteger(data.grant_version) || Number(data.grant_version) <= 0)) ||
        (data.grant_version ?? 0) !== actor.grant_version ||
        data.field_permissions.some(f => !(f in definition.fields) || !actor.field_permissions.includes(f))) fail();
    for (const [action, result] of Object.entries(data.decisions)) {
      if (!definition.actions[action] || definition.actions[action].kind === "display" || !decision(result)) fail();
      if (result.effect !== "deny" && (actor.migration_state !== "implemented" || actor.runtime_state !== "enabled" ||
          !actor.decisions[action] || actor.decisions[action].effect === "deny")) fail();
    }
    if ((actor.migration_state !== "implemented" || actor.runtime_state !== "enabled") && data.field_permissions.length) fail();
  }
  return value as unknown as AuthorizationContext;
}
export function validateAuthorizationResolution(value: unknown, snapshot: AuthzSnapshot,
  expected: ContextExpectation): AuthorizationResolution {
  if (!record(value) || !keys(value, ["locator", "authorization_context"]) || !record(value.locator)) fail();
  const context = validateAuthorizationContext(value.authorization_context, snapshot, { ...expected, authorizationView: true });
  const { locator } = value, ref = context.context;
  if (ref.kind === "project" ? !keys(locator, ["project_id"]) || locator.project_id !== ref.id :
      ref.kind === "collection" ? !keys(locator, ["collection_ref"]) || locator.collection_ref !== ref.id :
        !keys(locator, ["object_type", "object_id"]) || locator.object_type !== ref.object_type || locator.object_id !== ref.id) fail();
  return { locator, authorization_context: context } as AuthorizationResolution;
}
export function validateContextualPayload<T>(value: T, snapshot: AuthzSnapshot, expected: ContextExpectation,
  prior: AuthorizationContext): T {
  if (!record(value)) fail();
  const next = validateAuthorizationContext(value.authorization_context, snapshot, expected);
  if (contextRefKey(next.context) !== contextRefKey(prior.context)) throw new ContextChanged();
  if (!ruleAllowed(snapshot, entryRule(expected.entryId), {}, next)) throw new ContextDenied();
  return value;
}
/** Path substitution uses the static registry; a URL cannot select an action or another resolver. */
export function authorizationPath(entryId: string, params: Readonly<Record<string, string | number>> = {}): string {
  const view = authorizationViews[entryId];
  if (!view || view.view !== "authorization" || !view.no_business_payload) fail();
  const names = [...view.path.matchAll(/\{([^}]+)\}/g)].map(match => match[1]);
  if (names.length !== Object.keys(params).length || names.some(name => !Object.hasOwnProperty.call(params, name))) fail();
  return view.path.replace(/^\/api(?=\/)/, "").replace(/\{([^}]+)\}/g, (_, name: string) => {
    const id = params[name];
    if (!(typeof id === "number" && Number.isSafeInteger(id)) && !text(id)) fail();
    return encodeURIComponent(String(id));
  });
}
