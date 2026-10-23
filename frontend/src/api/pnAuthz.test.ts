import { expect, it } from "vitest";
import { validateAuthzSnapshot } from "./authz";
import { resourceAllowed } from "../authz/AuthzProvider";
import { authzSnapshot } from "../test/authzFixtures";
import definitions from "../authz/pnResources.json";
const snapshot = () => {
  const value = authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0, reason: "", field_permissions: [] });
  for (const [id, definition] of Object.entries(definitions)) value.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true, grant_version: 1, decisions: Object.fromEntries(definition.actions.map(action => [action, { effect: "allow", reason: null }])), scope_kind: "all", field_permissions: definition.fields };
  return value;
};
it("接受四种已实现 PN 资源，未知动作或字段仍关闭权限快照", () => {
  const value = snapshot(); expect(validateAuthzSnapshot(value)).toBe(value); expect(resourceAllowed(value, "part_gallery", "gallery.read")).toBe(true);
  value.resources.part_gallery.decisions["gallery.backdoor"] = { effect: "allow", reason: null }; expect(() => validateAuthzSnapshot(value)).toThrow();
  const fields = snapshot(); fields.resources.part_master.field_permissions = ["description", "costs"]; expect(() => validateAuthzSnapshot(fields)).toThrow();
});
it("暂停模块和未识别资源不会通过 action allow 回退到角色权限", () => {
  const value = snapshot(); value.resources.part_master.runtime_state = "suspended";
  expect(resourceAllowed(value, "part_master", "read")).toBe(false); expect(resourceAllowed(value, "unknown", "read")).toBe(false);
});
