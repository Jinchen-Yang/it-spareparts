import { describe, expect, it } from "vitest";
import manifest from "../../../../backend/app/authz/catalog_manifest.json";
import catalogCases from "../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/01-catalog-me.json";
import accountCases from "../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/02-account-modules.json";
import templateCases from "../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/03-template-commands.json";
import {
  configurationErrors,
  validateAccountModules,
  validateAuthzSnapshot,
  validateCatalog,
  validateTemplateModules,
  type AuthzCatalog,
  type AuthzSnapshot,
  type AccountModules,
  type TemplateModules,
} from "../authz";
import {
  resourceAllowed,
  resourceUsesLegacy,
  salesAccess,
} from "../../authz/AuthzProvider";
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v));
const catalog = catalogCases.cases[0].response.json as unknown as AuthzCatalog;
const me = catalogCases.cases[1].response.json as unknown as AuthzSnapshot;
const account = accountCases.cases[0].response
  .json as unknown as AccountModules;
describe("S1 documented wire consumption", () => {
  it("accepts the full real backend catalog, with only S1 sales executable", () => {
    const real = {
      ...catalog,
      modules: manifest.map((module) => ({
        ...module,
        runtime_state:
          module.migration_state === "implemented" ? "enabled" : "legacy",
        configurable: module.migration_state === "implemented",
      })),
    } as AuthzCatalog;
    expect(real.modules).toHaveLength(87);
    expect(validateCatalog(real)).toEqual(real);
    expect(
      real.modules
        .filter((m) => m.configurable)
        .map((m) => [m.id, m.actions.map((a) => a.id)]),
    ).toEqual([["sales_analysis", ["read", "export"]]]);
    const invalid = clone(real);
    invalid.modules.find(
      (m) => m.surfaces.length,
    )!.surfaces[0].display_actions = [null] as any;
    expect(() => validateCatalog(invalid)).toThrow();
  });
  it("accepts catalog and actor snapshots, preserving empty assigned scope and suspended denial", () => {
    expect(validateCatalog(clone(catalog))).toEqual(catalog);
    expect(validateAuthzSnapshot(clone(me))).toEqual(me);
    const suspended = catalogCases.cases[2].response
      .json as unknown as AuthzSnapshot;
    validateAuthzSnapshot(suspended);
    expect(resourceAllowed(suspended, "sales_analysis", "read")).toBe(false);
    expect(resourceUsesLegacy(suspended, "sales_analysis")).toBe(false);
  });
  it("keeps equal overrides and explicit denial distinct from absence across account goldens", () => {
    for (const c of accountCases.cases) {
      const body = c.response.json as unknown as Record<string, unknown>;
      const data = (body.result || body) as AccountModules;
      expect(validateAccountModules(clone(data), catalog)).toEqual(data);
    }
    expect(account.resources.sales_analysis.personal_override.present).toBe(
      true,
    );
  });
  it("accepts canonical template reads and saves without deriving runtime decisions", () => {
    for (const c of templateCases.cases.slice(0, 6)) {
      const body = c.response.json as unknown as Record<string, unknown>;
      const data = (body.result || body) as TemplateModules;
      expect(validateTemplateModules(clone(data), catalog)).toEqual(data);
    }
  });
  it("rejects missing schema, unknown states and executable resources even if old fields allow", () => {
    for (const mutate of [
      (v: any) => delete v.schema_version,
      (v: any) => (v.schema_version = 99),
      (v: any) => (v.resources.sales_analysis.runtime_state = "staged"),
      (v: any) =>
        (v.resources.unregistered = { ...v.resources.sales_analysis }),
    ]) {
      const v = clone(me);
      mutate(v);
      expect(() => validateAuthzSnapshot(v)).toThrow();
    }
  });
  it("requires closed action-field dependencies and never silently grants fields", () => {
    const bad = clone(catalog);
    delete (bad.modules[0].actions[0] as any).requires_fields;
    expect(() => validateCatalog(bad)).toThrow();
    const unknown = clone(catalog);
    unknown.modules[0].actions[0].requires_fields = ["unknown"];
    expect(() => validateCatalog(unknown)).toThrow();
    const c = clone(catalog);
    c.modules[0].actions[1].requires_fields = ["data_purchase_cost"];
    validateCatalog(c);
    const grant = {
      actions: ["read", "export"],
      scope: "all",
      field_permissions: [],
    };
    expect(configurationErrors(c.modules[0], grant)).toContain(
      "导出需要字段「成本及销售金额」",
    );
    expect(grant.field_permissions).toEqual([]);
  });
  it("unified sales reads resources, never old compatibility allow", () => {
    const v = clone(me);
    v.resources.sales_analysis.decisions.read = {
      effect: "deny",
      reason: null,
    };
    v.sales_analysis.read = true;
    expect(salesAccess(v)?.read).toBe(false);
    expect(resourceAllowed(v, "unknown", "read")).toBe(false);
  });
});
