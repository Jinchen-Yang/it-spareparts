import type { Decision } from "../api/authz";
import type { AuthorizationCapture } from "./AuthzProvider";

/** Vocabulary is checked against the generated backend registry before use. */
export type ContextRef =
  | { kind: "project"; id: string; authorization_revision: string }
  | { kind: "collection"; id: string; authorization_revision: string }
  | { kind: "object"; object_type: string; id: string; authorization_revision: string };
export interface AuthorizationContext {
  schema_version: 1;
  catalog_version: string;
  authorization_epoch: number;
  context: ContextRef;
  resources: Record<string, {
    grant_version: number | null;
    decisions: Record<string, Decision>;
    field_permissions: string[];
  }>;
}
export interface AuthorizationPrecondition {
  catalog_version: string;
  authorization_epoch: number;
  context: ContextRef;
}
export interface AuthorizationResolution {
  locator: { project_id: string } | { collection_ref: string }
    | { object_type: string; object_id: string };
  authorization_context: AuthorizationContext;
}
/** Local lifecycle binding, never a new actor or a server proof. */
export interface BoundAuthorizationContext {
  readonly capture: AuthorizationCapture;
  readonly inputKey: string;
  readonly value: AuthorizationContext;
}
export const contextRefKey = (ref: ContextRef): string => JSON.stringify([
  ref.kind, ref.kind === "object" ? ref.object_type : null, ref.id, ref.authorization_revision,
]);
export const contextPrecondition = (value: AuthorizationContext): AuthorizationPrecondition => ({
  catalog_version: value.catalog_version,
  authorization_epoch: value.authorization_epoch,
  context: { ...value.context },
});
