import { AuthzUnavailable, useAuthz } from "../authz/AuthzProvider";
import MigrationWorkspace from "../components/pnCatalog/MigrationWorkspace";
import PageHeader from "../components/PageHeader";

export default function PnMigrationRoute() {
  const { status } = useAuthz();
  if (status !== "ready") return <AuthzUnavailable />;
  return <><PageHeader title="共享 PN 迁移核对" subtitle="核对来源记录，明确处理历史中断任务" /><MigrationWorkspace /></>;
}
