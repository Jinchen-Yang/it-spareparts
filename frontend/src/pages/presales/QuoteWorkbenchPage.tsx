import { useState } from "react";
import { App, Empty, Tabs } from "antd";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import WorkInbox from "../../components/workflow/WorkInbox";
import { ProcessContent } from "./ProcessPage";
import "./quoting.css";

export default function QuoteWorkbenchPage() {
  const navigate = useNavigate();
  const { modal } = App.useApp();
  const [dirty, setDirty] = useState(false);
  const [params] = useSearchParams();
  const inquiryId = Number(params.get("id"));
  const [subjectType, setSubjectType] = useState("presales_inquiry");
  const [refreshKey, setRefreshKey] = useState(0);
  return <div className="quote-workbench">
    <aside className="quote-queue" aria-label="待办队列">
      <div className="quote-heading"><h2>报价工作台</h2><Link to="/presales/offers/new">主动报价 →</Link></div>
      <Tabs size="small" activeKey={subjectType} onChange={setSubjectType} items={[
        { key: "presales_inquiry", label: "采购询价" }, { key: "presales_offer_notice", label: "报价通知" },
      ]} />
      <WorkInbox beforeOpen={async (item) => {
        if (!dirty) return true;
        if (item.subject_type === "presales_inquiry" && item.subject_id === inquiryId) return false;
        return new Promise<boolean>((resolve) => modal.confirm({ title: "当前单据有未保存的修改", content: "切换会放弃尚未保存的报价、沿用选择和不报原因。",
          okText: "放弃修改并切换", cancelText: "继续填写", onOk: () => resolve(true), onCancel: () => resolve(false) }));
      }} compact subjectType={subjectType} refreshKey={refreshKey} conflictMessage="已被他人修改/领取"
        renderRow={(item) => <span title={String(item.summary.inquiry_no ?? "询价单")} className="quote-number">
          {String(item.summary.inquiry_no ?? "询价单")} · {String(item.summary.line_count ?? "—")} 行</span>}
        onOpen={(item) => {
          if (item.subject_type === "presales_offer_notice") navigate(`/presales/inquiries/${item.subject_id}`, { state: { workItem: item } });
          else navigate(`/presales/workbench?id=${item.subject_id}`, { state: { workItem: item } });
        }} />
    </aside>
    <main>{inquiryId > 0 ? <ProcessContent key={inquiryId} inquiryId={inquiryId} onDirtyChange={setDirty} onTaskChange={() => setRefreshKey((previous) => previous + 1)} />
      : <Empty description="从左侧选择询价单，公共池询价需先领取" />}</main>
  </div>;
}
