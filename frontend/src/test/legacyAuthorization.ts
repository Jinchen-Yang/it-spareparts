import { authzSnapshot, authzTestState } from "./authzFixtures";
import type { AuthzSnapshot } from "../api/authz";
let snapshot: AuthzSnapshot | undefined;
let owner = "";
/** Explicit legacy runtime fixture for tests which exercise existing page behaviour. */
export function legacyAuthorizationState() {
  const key = localStorage.getItem("token") || "";
  if (!snapshot || key !== owner) {
    owner = key;
    snapshot = authzSnapshot({enabled: false, read: false, export: false, scope: "none", field_permissions: [], customer_count: 0, reason: ""}, {version: key});
  }
  return authzTestState(snapshot);
}
export function prepareLegacyPageFixture(permissions: Record<string, boolean> = {}, defaultRole = "readonly") {
  const existing = JSON.parse(localStorage.getItem("permissions") || "{}");
  localStorage.setItem("permissions", JSON.stringify({...permissions, ...existing}));
  if (!localStorage.getItem("token")) localStorage.setItem("token", "legacy-page-fixture-owner");
  if (!localStorage.getItem("role")) localStorage.setItem("role", defaultRole);
}
