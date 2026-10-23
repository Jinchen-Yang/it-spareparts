import { useEffect, useState } from "react";
import { Alert, Tabs } from "antd";
import type { Capabilities } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
import ProfileEditor from "./ProfileEditor";
import ContentEditor from "./ContentEditor";
import GalleryPanel from "./GalleryPanel";
import CandidatesPanel from "./CandidatesPanel";
import AuditPanel from "./AuditPanel";
import PartAccessBoundary from "./PartAccessBoundary";

export default function CatalogPartPanel({ partId, onChanged }: { partId: number; onChanged?: () => void }) {
  return <PartAccessBoundary key={partId} partId={partId}><CatalogPartTabs partId={partId} onChanged={onChanged} /></PartAccessBoundary>;
}

function CatalogPartTabs({ partId, onChanged }: { partId: number; onChanged?: () => void }) {
  const { client, can, field } = useCatalogAccess();
  const [capabilities, setCapabilities] = useState<Capabilities>({ media: false, bundle: false, research: false, recognition: false });
  const [configurationError, setConfigurationError] = useState(false);
  useEffect(() => { let alive = true; client.get<Capabilities>("/capabilities").then(({ data }) => { if (alive) setCapabilities(data); }).catch(() => { if (alive) setConfigurationError(true); }); return () => { alive = false; }; }, [client]);
  const tabs = [
    ...(can("part_master", "read") ? [{ key: "profile", label: "主资料", children: <ProfileEditor partId={partId} onChanged={onChanged} /> }] : []),
    ...(["good", "bad"] as const).filter(grade => can("part_content", "read") && ["title", "description"].some(k => field("part_content", `content.${grade}.${k}`))).map(grade => ({ key: grade, label: `${grade === "good" ? "好件" : "坏件"}文案`, children: <ContentEditor partId={partId} grade={grade} /> })),
    ...(can("part_gallery", "gallery.read") && field("part_gallery", "gallery") ? [{ key: "gallery", label: "PN 图库", children: <GalleryPanel partId={partId} capabilities={capabilities} /> }] : []),
    ...(can("part_master", "read_candidates") && field("part_master", "candidates") ? [{ key: "candidates", label: "资料候选", children: <CandidatesPanel partId={partId} /> }] : []),
    ...(can("part_master", "read_audit") ? [{ key: "audit", label: "审计记录", children: <AuditPanel partId={partId} /> }] : []),
  ];
  return <>{configurationError && <Alert type="warning" message="辅助服务配置暂不可读，上传与辅助任务入口已关闭。" />}<Tabs items={tabs} /></>;
}
