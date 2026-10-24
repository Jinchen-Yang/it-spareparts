import { useContextAdmission, useModuleAccess } from "../../../authz/access";
import { actionRule, allRule, surfaceRule } from "../../../authz/rules";

/** Visible surfaces and commands keep independent grants; workspace is composite. */
export function useCollectionPermissions(legacyManage: boolean) {
  const admission = useContextAdmission();
  const module = useModuleAccess("maintenance_collection");
  const canCreate = admission.state(allRule(surfaceRule("maintenance_collection.manage_actuals"),
    actionRule("maintenance_collection", "create"), actionRule("maintenance_contract", "read")), legacyManage) !== "deny";
  const canUpdate = admission.state(allRule(surfaceRule("maintenance_collection.manage_actuals"),
    actionRule("maintenance_collection", "read_snapshots", "update")), legacyManage) !== "deny";
  return { canCreate, canUpdate, legacy: module.legacy,
    canPlan: admission.surface("maintenance_collection.plans", true) !== "deny",
    canActuals: admission.surface("maintenance_collection.actuals", true) !== "deny",
    canReadSnapshots: admission.state(actionRule("maintenance_collection", "read_snapshots"), true) !== "deny",
    canContracts: admission.state(actionRule("maintenance_contract", "read"), true) !== "deny",
    amountsVisible: module.legacy || module.field("data_profit"),
  };
}
