import { ACTOR_CAPABILITIES, type AuthzSnapshot } from "../api/authz";
import { knownModules, vocabulary } from "../authz/rules";

let testOwner: { token: string | null; version: string; controller: AbortController; state?: any; signature?: string } | undefined;
/** Explicit provider substitute for isolated legacy rendering tests. Race tests use the real provider. */
export function authzTestState(snapshot: AuthzSnapshot) {
  const token = localStorage.getItem("token");
  if (!testOwner || testOwner.controller.signal.aborted || testOwner.token !== token || testOwner.version !== snapshot.version) {
    testOwner?.controller.abort();
    testOwner = { token, version: snapshot.version, controller: new AbortController() };
  }
  const owner = testOwner;
  const signature = JSON.stringify(snapshot);
  if (owner.state && owner.signature === signature) return owner.state;
  owner.signature = signature;
  return owner.state = { status: "ready" as const, snapshot, token, generation: 0, signal: owner.controller.signal,
    refresh: async () => {}, invalidate: () => owner.controller.abort(),
    isCurrent: (capture: { token: string; snapshotVersion: string }) =>
      !owner.controller.signal.aborted && capture.token === localStorage.getItem("token") && capture.snapshotVersion === owner.version,
  };
}

/** Test-only expectation of the explicit S1→S2 migration; never used by runtime. */
export function migratedSalesActions(actions: readonly string[], fields: readonly string[]): string[] {
  if (!actions.includes("read")) return actions.filter(a => a !== "read");
  const result = ["read_options", "read_summary", "read_groups", "read_trend", "read_orders", "read_order_detail",
    "show_overview", "show_compact", "show_groups", "show_trend", "show_orders", "show_summary_counts", "show_summary_amounts", "show_order_detail"];
  if (fields.includes("data_purchase_cost")) {
    result.push("read_amount_histogram", "show_amount_histogram");
    if (fields.includes("data_profit")) result.push("show_summary_profit");
    if (fields.includes("data_reconciliation")) result.push("show_reconciliation");
  }
  if (actions.includes("export")) result.push("export", "show_export");
  return result;
}
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
      ...Object.fromEntries(Object.entries(knownModules).map(([id, migration_state]) => [id, {
        migration_state: migration_state as "planned" | "implemented" | "retired", runtime_state: "legacy" as const, configurable: false, grant_version: 0,
        decisions: Object.fromEntries(Object.keys(vocabulary[id]?.actions || {}).map(a => [a, decision(false)])), scope_kind: "none", field_permissions: [],
      }])),
      sales_analysis: {
        migration_state: "implemented",
        runtime_state: enabled ? "enabled" : "legacy",
        configurable: enabled,
        grant_version: allow ? 1 : 0,
        decisions: Object.fromEntries(Object.keys(vocabulary.sales_analysis.actions).map(action => [action,
          decision(migratedSalesActions(allow ? ["read", ...(sales.export ? ["export"] : [])] : [], sales.field_permissions).includes(action))])),
        scope_kind: allow ? sales.scope : "none",
        field_permissions: allow ? sales.field_permissions : [],
      },
    },
    sales_analysis: sales,
  };
}
