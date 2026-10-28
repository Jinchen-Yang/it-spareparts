import { useState } from "react";
import { App, Button, Space, Tabs } from "antd";
import { Link, Navigate, useNavigate, useSearchParams, useLocation } from "react-router-dom";
import WorkInbox from "../../components/workflow/WorkInbox";
import PageHeader from "../../components/PageHeader";
import { presalesWorkbooks } from "../../api/presalesWorkbooks";
import type { WorkItem } from "../../api/workItems";
import { WorkbookExportButton } from "../../components/workbook";
import { formatBusinessDateTime } from "../../utils/date";
import WorkbookDialog from "./WorkbookDialog";
import "./presales.css";

export default function QuoteWorkbenchPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [params] = useSearchParams();
  const { message } = App.useApp();
  const [subjectType, setSubjectType] = useState("presales_inquiry");
  const [refreshKey, setRefreshKey] = useState(0);
  const [selected, setSelected] = useState<WorkItem[]>([]);
  const [importOpen, setImportOpen] = useState(false);
  const legacyId = Number(params.get("id"));
  if (legacyId > 0) return <Navigate replace to={`/presales/workbench/${legacyId}`} state={location.state} />;
  return <section className="presales-page">
    <PageHeader title="报价" subtitle="领取询价、填写报价，或主动报出已有货源" extra={<Link to="/presales/offers/new">主动报价</Link>} />
    <div className="presales-panel">
      <Tabs activeKey={subjectType} onChange={(value) => { setSubjectType(value); setSelected([]); }} items={[
        { key: "presales_inquiry", label: "采购询价" }, { key: "presales_offer_notice", label: "报价通知" },
      ]} />
      {subjectType === "presales_inquiry" && <Space className="presales-toolbar">
        <WorkbookExportButton label="合并下载" schemaName="presales_quote_sheet" disabled={!selected.length}
          fetchSchema={() => presalesWorkbooks.getQuoteSheetSchema()} filename="合并报价表.xlsx"
          onExport={(visible) => presalesWorkbooks.exportQuoteSheet([...new Set(selected.map((item) => item.subject_id))], visible)} />
        <Button onClick={() => setImportOpen(true)}>上传合并表</Button>
      </Space>}
      <WorkInbox key={subjectType} subjectType={subjectType} refreshKey={refreshKey}
        onSelectionChange={subjectType === "presales_inquiry" ? setSelected : undefined}
        isRowSelectable={(item) => !item.paused && ["pending", "in_progress"].includes(item.status)
          && (item.assignee_id != null || item.claimed_by != null) && item.allowed_actions.some((action) => ["start", "release", "complete"].includes(action))}
        renderRow={(item) => <div>
          <div>{String(item.summary.created_by_name ?? "业务人员")}</div>
          <div className="presales-muted">{formatBusinessDateTime(String(item.summary.created_at ?? item.created_at))}</div>
        </div>}
        onOpen={(item) => navigate(item.subject_type === "presales_offer_notice"
          ? `/presales/inquiries/${item.subject_id}` : `/presales/workbench/${item.subject_id}`, { state: { workItem: item } })} />
    </div>
    {importOpen && <WorkbookDialog mode="quote" onClose={() => setImportOpen(false)} onApplied={() => {
      message.success("已保存为草稿，请在页面确认后回复"); setImportOpen(false); setRefreshKey((value) => value + 1);
    }} />}
  </section>;
}
