import { ACTOR_CAPABILITIES, type AuthzSnapshot } from "../api/authz";
/** S0 compatibility shape embedded in the current, validated S1 envelope. */
export function authzSnapshot(
  sales: AuthzSnapshot["sales_analysis"],
  options: {
    version?: string;
    canManage?: boolean;
    username?: string;
    mode?: AuthzSnapshot["authorization_mode"];
  } = {},
): AuthzSnapshot {
  const enabled = sales.enabled,
    allow = enabled && sales.read;
  const decision = (yes: boolean) => ({
    effect: yes ? ("allow" as const) : ("deny" as const),
    reason: null,
  });
  return {
    schema_version: 1,
    authorization_mode: options.mode || "legacy_compat",
    catalog_version: "authz-s1-v1",
    version: options.version || "fixture",
    username: options.username || "operator",
    principal: {
      id: 1,
      username: options.username || "operator",
      display_name: null,
    },
    authorization_epoch: 0,
    can_manage: options.canManage ?? false,
    actor_capabilities: Object.fromEntries(
      ACTOR_CAPABILITIES.map((k) => [k, decision(options.canManage ?? false)]),
    ) as AuthzSnapshot["actor_capabilities"],
    resources: {
      sales_analysis: {
        migration_state: "implemented",
        runtime_state: enabled ? "enabled" : "legacy",
        configurable: enabled,
        grant_version: allow ? 1 : 0,
        decisions: {
          read: decision(allow),
          export: decision(enabled && sales.export),
        },
        scope_kind: allow ? sales.scope : "none",
        field_permissions: allow ? sales.field_permissions : [],
      },
    },
    sales_analysis: sales,
  };
}
