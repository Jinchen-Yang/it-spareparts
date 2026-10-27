import { useState } from "react";
import { App, Button, Empty, Tabs } from "antd";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import WorkInbox from "../../components/workflow/WorkInbox";
import { ProcessContent } from "./ProcessPage";
import { presalesWorkbooks } from "../../api/presalesWorkbooks";
import type { WorkItem } from "../../api/workItems";
import { WorkbookExportButton } from "../../components/workbook";
import WorkbookDialog from "./WorkbookDialog";
import "./quoting.css";

export default function QuoteWorkbenchPage() {
  const navigate = useNavigate();
  const { modal, message } = App.useApp();
  const [dirty, setDirty] = useState(false);
  const [params] = useSearchParams();
  const inquiryId = Number(params.get("id"));
  const [subjectType, setSubjectType] = useState("presales_inquiry");
  const [refreshKey, setRefreshKey] = useState(0);
  const [selected, setSelected] = useState<WorkItem[]>([]);
  const [importOpen, setImportOpen] = useState(false);
  return <div className="quote-workbench">
    <aside className="quote-queue" aria-label="待办队列">
      <div className="quote-heading"><h2>报价工作台</h2><Link to="/presales/offers/new">主动报价 →</Link></div>
      <Tabs size="small" activeKey={subjectType} onChange={setSubjectType} items={[
        { key: "presales_inquiry", label: "采购询价" }, { key: "presales_offer_notice", label: "报价通知" },
      ]} />
      {subjectType === "presales_inquiry" && <div className="quote-queue-filters">
        <WorkbookExportButton size="small" label="合并下载" schemaName="presales_quote_sheet" disabled={!selected.length}
          fetchSchema={() => presalesWorkbooks.getQuoteSheetSchema()} filename="合并报价表.xlsx"
          onExport={(visible) => presalesWorkbooks.exportQuoteSheet([...new Set(selected.map((item) => item.subject_id))], visible)} />
        <Button size="small" onClick={() => setImportOpen(true)}>上传合并表</Button>
      </div>}
      <WorkInbox beforeOpen={async (item) => {
        if (!dirty) return true;
        if (item.subject_type === "presales_inquiry" && item.subject_id === inquiryId) return false;
        return new Promise<boolean>((resolve) => modal.confirm({ title: "当前单据有未保存的修改", content: "切换会放弃尚未保存的报价、沿用选择和不报原因。",
          okText: "放弃修改并切换", cancelText: "继续填写", onOk: () => resolve(true), onCancel: () => resolve(false) }));
      }} compact subjectType={subjectType} refreshKey={refreshKey} conflictMessage="已被他人修改/领取"
        onSelectionChange={subjectType === "presales_inquiry" ? setSelected : undefined}
        isRowSelectable={(item) => !item.paused && ["pending", "in_progress"].includes(item.status)
          && (item.assignee_id != null || item.claimed_by != null) && item.allowed_actions.some((action) => ["start", "release", "complete"].includes(action))}
        renderRow={(item) => <span title={String(item.summary.inquiry_no ?? "询价单")} className="quote-number">
          {String(item.summary.inquiry_no ?? "询价单")} · {String(item.summary.line_count ?? "—")} 行</span>}
        onOpen={(item) => {
          if (item.subject_type === "presales_offer_notice") navigate(`/presales/inquiries/${item.subject_id}`, { state: { workItem: item } });
          else navigate(`/presales/workbench?id=${item.subject_id}`, { state: { workItem: item } });
        }} />
    </aside>
    <main>{inquiryId > 0 ? <ProcessContent key={`${inquiryId}:${refreshKey}`} inquiryId={inquiryId} onDirtyChange={setDirty} onTaskChange={() => setRefreshKey((previous) => previous + 1)} />
      : <Empty description="从左侧选择询价单，公共池询价需先领取" />}</main>
    {importOpen && <WorkbookDialog mode="quote" onClose={() => setImportOpen(false)} onApplied={() => {
      message.success("已保存为草稿，请在页面确认后回复"); setImportOpen(false); setDirty(false); setRefreshKey((value) => value + 1);
    }} />}
  </div>;
}
