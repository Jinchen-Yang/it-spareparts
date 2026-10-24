import type { AuthzSnapshot } from "../api/authz";
import { actorAllowed } from "./AuthzProvider";
import { ruleAllowed, surfaceRule } from "./rules";

/** Ordinary surfaces use their resource runtime, independent of management cutover. */
export function accountAccess(snapshot: AuthzSnapshot | null) {
  const legacy = {
    account_directory: actorAllowed(snapshot, "read_accounts"),
    account_profile: actorAllowed(snapshot, "edit_account_profiles"),
  };
  const management = ["manage_authorization", "manage_templates", "create_accounts",
    "reset_account_passwords", "manage_account_lifecycle"] as const;
  const control = management.some(cap => actorAllowed(snapshot, cap));
  const list = ruleAllowed(snapshot, surfaceRule("account_directory.list"), legacy);
  const activity = ruleAllowed(snapshot, surfaceRule("account_directory.activity"), legacy);
  const profile = ruleAllowed(snapshot, surfaceRule("account_profile.edit"), legacy);
  return { control, list, activity, profile, enter: control || list || activity || profile, legacy };
}
