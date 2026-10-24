import { upgradeS1Fixture } from "../../test/s2WireFixtures";
import { describe, expect, it } from "vitest";
import manifest from "../../../../backend/app/authz/catalog_manifest.json";
import catalogCasesHistorical from "../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/01-catalog-me.json";
import accountCasesHistorical from "../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/02-account-modules.json";
import templateCasesHistorical from "../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/03-template-commands.json";
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
const catalogCases = upgradeS1Fixture(catalogCasesHistorical);
const accountCases = upgradeS1Fixture(accountCasesHistorical);
const templateCases = upgradeS1Fixture(templateCasesHistorical);

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v));
const catalog = catalogCases.cases[0].response.json as unknown as AuthzCatalog;
const me = catalogCases.cases[1].response.json as unknown as AuthzSnapshot;
const account = accountCases.cases[0].response
  .json as unknown as AccountModules;
describe("S1 documented wire after explicit S2 migration", () => {
  it("keeps platform boundaries closed and separate from employee grants", () => {
    expect(catalog.boundaries).toHaveLength(10);
    expect(catalog.boundaries.every(b => !catalog.modules.some(m => m.id === b.id))).toBe(true);
    const bad = clone(catalog);
    bad.boundaries[0].authority = "employee_grant";
    expect(() => validateCatalog(bad)).toThrow();
    const grant = clone(catalog);
    (grant.boundaries[0] as any).configurable = true;
    expect(() => validateCatalog(grant)).toThrow();
    const missing = clone(catalog);
    missing.boundaries.pop();
    expect(() => validateCatalog(missing)).toThrow();
    const duplicate = clone(catalog);
    duplicate.boundaries[1] = duplicate.boundaries[0];
    expect(() => validateCatalog(duplicate)).toThrow();
  });
  it("accepts the full real backend catalog, with all 107 S2/S3/S4 resources executable", () => {
    const real = {
      ...catalog,
      modules: manifest.map((module) => ({
        ...module,
        runtime_state:
          module.migration_state === "implemented" ? "enabled" : "legacy",
        configurable: module.migration_state === "implemented",
      })),
    } as AuthzCatalog;
    expect(real.modules).toHaveLength(112);
    expect(validateCatalog(real)).toEqual(real);
    expect(real.modules.filter(m => m.configurable)).toHaveLength(107);
    expect(real.modules.find(m => m.id === "sales_analysis")!.actions.map(a => a.id)).not.toContain("read");
    const missingSource = clone(real);
    delete (missingSource.modules.find(m => m.id === "maintenance_project") as any).legacy_sources;
    expect(() => validateCatalog(missingSource)).toThrow();
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
    const sales = c.modules.find(m => m.id === "sales_analysis")!;
    const histogram = sales.actions.find(a => a.id === "read_amount_histogram")!;
    expect(histogram.requires_fields).toEqual(["data_purchase_cost"]);
    const drift = clone(catalog);
    drift.modules.find(m => m.id === "sales_analysis")!.actions.find(a => a.id === "export")!.requires_fields = ["data_purchase_cost"];
    expect(() => validateCatalog(drift)).toThrow();
    const grant = { actions: ["read_amount_histogram"], scope: "all", field_permissions: [] };
    expect(configurationErrors(sales, grant)).toContain("读取金额分布需要字段「采购成本」");
    expect(grant.field_permissions).toEqual([]);
  });
  it("unified sales reads resources, never old compatibility allow", () => {
    const v = clone(me);
    for (const decision of Object.values(v.resources.sales_analysis.decisions)) decision.effect = "deny";
    v.sales_analysis.read = true;
    expect(salesAccess(v)?.read).toBe(false);
    expect(resourceAllowed(v, "unknown", "read")).toBe(false);
  });
});
