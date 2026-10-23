import { AuthzUnavailable, useAuthz } from "../authz/AuthzProvider";
import IdentityPanel from "../components/pnCatalog/IdentityPanel";
import PageHeader from "../components/PageHeader";

export default function PnIdentityRoute() {
  const { status } = useAuthz();
  if (status !== "ready") return <AuthzUnavailable />;
  return <><PageHeader title="PN 身份治理" subtitle="预览后确认合并、回滚及别名归属" /><IdentityPanel /></>;
}
