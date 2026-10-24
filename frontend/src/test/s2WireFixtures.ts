/** Historical wire documents stay immutable; tests explicitly apply the S2 action migration. */
import manifest from "../../../backend/app/authz/catalog_manifest.json";
import { migratedSalesActions } from "./authzFixtures";
import { knownModules, vocabulary } from "../authz/rules";
import { KNOWN_BOUNDARIES } from "../authz/generated";
export function upgradeS1Fixture<T>(source: T): T {
  const visit = (value: any): any => {
    if (Array.isArray(value)) return value.map(visit);
    if (!value || typeof value !== "object") return value;
    const result = Object.fromEntries(Object.entries(value).map(([key, child]) => [key, visit(child)])) as any;
    if (Array.isArray(value.actions) && value.actions.every((a: unknown) => typeof a === "string") && Array.isArray(value.field_permissions))
      result.actions = migratedSalesActions(value.actions, value.field_permissions);
    if (value.decisions && "read" in value.decisions) {
      const allow = migratedSalesActions(value.decisions.read.effect === "allow"
        ? ["read", ...(value.decisions.export?.effect === "allow" ? ["export"] : [])] : [], value.field_permissions || []);
      result.decisions = Object.fromEntries(Object.keys(vocabulary.sales_analysis.actions).map(id => [id,
        {effect: allow.includes(id) ? "allow" : "deny", reason: null}]));
    }
    if (Array.isArray(value.modules)) { result.boundaries = JSON.parse(JSON.stringify(KNOWN_BOUNDARIES)); result.modules = manifest.map(module => {
      const old = value.modules.find((m: any) => m.id === module.id);
      return {...module, runtime_state: module.id === "sales_analysis" ? old?.runtime_state || "enabled" : "legacy",
        configurable: module.id === "sales_analysis" && old?.configurable !== false};
    }); }
    if (result.resources) for (const [id, item] of Object.entries(result.resources) as [string, any][]) {
      if (item && "migration_state" in item) {
        if (!(id in knownModules)) { delete result.resources[id]; continue; }
        item.migration_state = knownModules[id];
        if (id !== "sales_analysis" && vocabulary[id]) item.decisions = Object.fromEntries(Object.keys(vocabulary[id].actions).map(a => [a, {effect: "deny", reason: null}]));
      }
    }
    if (result.resources && result.actor_capabilities) for (const [id, definition] of Object.entries(vocabulary)) {
      if (!result.resources[id]) result.resources[id] = { migration_state: "implemented", runtime_state: "legacy", configurable: false,
        grant_version: 0, scope_kind: "none", field_permissions: [], decisions: Object.fromEntries(Object.keys(definition.actions).map(a => [a, { effect: "deny", reason: null }])) };
    }
    return result;
  };
  return visit(source);
}
