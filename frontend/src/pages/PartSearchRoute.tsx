import { resourceAllowed, resourceUsesLegacy, useAuthz } from "../authz/AuthzProvider";
import PartSearchPage from "./PartSearchPage";

/** Business facts retain their existing route; only the catalog editor switches authority. */
export default function PartSearchRoute() {
  const { status, snapshot } = useAuthz();
  const legacy = status === "ready" && resourceUsesLegacy(snapshot, "part_master");
  return <PartSearchPage catalog={legacy ? { mode: "legacy", canEdit: false } : { mode: "shared", canEdit: status === "ready" && resourceAllowed(snapshot, "part_master", "read") && resourceAllowed(snapshot, "part_master", "edit") }} />;
}
