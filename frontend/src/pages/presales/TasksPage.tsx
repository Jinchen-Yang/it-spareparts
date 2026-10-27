import { useState } from "react";
import { Tabs, Typography } from "antd";
import { useNavigate } from "react-router-dom";
import WorkInbox from "../../components/workflow/WorkInbox";

export default function TasksPage() {
  const navigate = useNavigate();
  const [subjectType, setSubjectType] = useState("presales_inquiry");
  return <>
    <Typography.Title level={3}>采购待办</Typography.Title>
    <Tabs activeKey={subjectType} onChange={setSubjectType} items={[
      { key: "presales_inquiry", label: "采购询价" }, { key: "presales_offer_notice", label: "报价通知" },
    ]} />
    <WorkInbox subjectType={subjectType} conflictMessage="已被他人修改/领取"
      renderRow={(item) => <><Typography.Text strong>{String(item.summary.inquiry_no ?? "询价单")}</Typography.Text>
        {item.summary.line_count !== undefined && <Typography.Text type="secondary"> · {String(item.summary.line_count)} 行</Typography.Text>}</>}
      onOpen={(item) => navigate(item.subject_type === "presales_offer_notice"
        ? `/presales/inquiries/${item.subject_id}` : `/presales/tasks/${item.subject_id}`, { state: { workItem: item } })} />
  </>;
}
