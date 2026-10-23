import { Alert } from "antd";
import { AuthzUnavailable, useAuthz } from "../authz/AuthzProvider";
import { useCatalogAccess } from "../components/pnCatalog/access";
import InboxWorkspace from "../components/pnCatalog/InboxWorkspace";
import PageHeader from "../components/PageHeader";

export default function PnInboxRoute() {
  const { status } = useAuthz(); const { can, field } = useCatalogAccess();
  if (status !== "ready") return <AuthzUnavailable />;
  if (!can("part_gallery", "inbox.read") || !field("part_gallery", "inbox")) return <Alert type="warning" message="收件箱尚未启用或当前无读取权限。" />;
  return <><PageHeader title="图片收件箱" subtitle="接收、核对和归档到共享 PN 图库" /><InboxWorkspace /></>;
}
