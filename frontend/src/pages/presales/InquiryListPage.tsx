import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, App, Button, Card, DatePicker, Select, Space, Table, Tabs, Tag, Typography } from "antd";
import type { Dayjs } from "dayjs";
import { presales, type Inquiry } from "../../api/presales";
import { listWorkCandidates, type WorkCandidate } from "../../api/workItems";
import { fetchPurchaseAnalyticsOptions } from "../../api/purchaseAnalytics";
import { readPermissionMap } from "../../nav";

const statuses: Record<string, string> = { draft: "草稿", submitted: "已提交", revising: "撤回修改中", closed: "已结束" };
const workStatuses: Record<string, string> = { pending: "待处理", in_progress: "处理中", done: "已回复", declined: "已退回", cancelled: "已取消" };

export default function InquiryListPage() {
  const navigate = useNavigate();
  const { message } = App.useApp();
  const permissions = readPermissionMap();
  const supervisor = !!permissions.action_presales_supervise || ["admin", "boss"].includes(localStorage.getItem("role") || "");
  const [scope, setScope] = useState<"mine" | "all">("mine");
  const [kind, setKind] = useState<Inquiry["kind"]>();
  const [status, setStatus] = useState<Inquiry["status"]>();
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [items, setItems] = useState<Inquiry[]>([]);
  const [details, setDetails] = useState<Record<number, Inquiry>>({});
  const [candidates, setCandidates] = useState<WorkCandidate[]>([]);
  const [customers, setCustomers] = useState<Record<number, string>>({});
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState(false);
  const [reload, setReload] = useState(0);
  const conflictRetried = useRef(false);
  useEffect(() => { conflictRetried.current = false; }, [scope, kind, status, page]);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError(""); setDetails({}); setDetailError(false);
    void presales.listInquiries({ scope, kind, status, page, page_size: 20 }).then(async (data) => {
      if (disposed) return;
      conflictRetried.current = false;
      setItems(data.items); setTotal(data.total);
      const rows = await Promise.allSettled(data.items.map((item) => presales.getInquiry(item.id)));
      if (disposed) return;
      const result: Record<number, Inquiry> = {};
      rows.forEach((row) => { if (row.status === "fulfilled") result[row.value.id] = row.value; });
      setDetails(result); setDetailError(rows.some((row) => row.status === "rejected"));
    }).catch((cause) => {
      if (disposed) return;
      const conflict = cause?.response?.status === 409;
      setError(conflict ? "已被他人修改/领取，请刷新" : "询价单列表加载失败");
      if (conflict && !conflictRetried.current) {
        conflictRetried.current = true;
        void message.warning("已被他人修改/领取，已自动刷新"); setReload((value) => value + 1);
      }
    }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [scope, kind, status, page, reload, message]);
  useEffect(() => {
    let disposed = false;
    void listWorkCandidates("presales_inquiry").then((data) => { if (!disposed) setCandidates(data.items); }).catch(() => {});
    return () => { disposed = true; };
  }, []);
  const customerIds = items.flatMap((item) => item.customer_id ? [item.customer_id] : []).join(",");
  useEffect(() => {
    let disposed = false;
    if (!customerIds) return;
    void fetchPurchaseAnalyticsOptions("", customerIds.split(",").map(Number)).then(({ data }) => {
      if (!disposed) setCustomers(Object.fromEntries([...(data.customers || []), ...(data.selected_customers || [])]
        .map((customer) => [customer.customer_id, customer.customer || String(customer.customer_id)])));
    }).catch(() => {});
    return () => { disposed = true; };
  }, [customerIds]);
  const filtered = useMemo(() => items.filter((item) => {
    const time = Date.parse(item.updated_at);
    return (!range?.[0] || time >= range[0].startOf("day").valueOf()) && (!range?.[1] || time <= range[1].endOf("day").valueOf());
  }), [items, range]);
  const progress = (row: Inquiry) => {
    if (row.status !== "submitted") return statuses[row.status] || row.status;
    const detail = details[row.id];
    if (!detail) return "已提交";
    const work = (detail.work_items || []).filter((item) => !["cancelled", "declined"].includes(item.status));
    const replied = work.filter((item) => item.status === "done").length;
    if (work.length && replied === work.length) return "已回复";
    if (replied || work.some((item) => item.status === "in_progress")) return `处理中「已回复 ${replied}/${work.length}」`;
    if (!work.length && (detail.lines || []).length && (detail.lines || []).every((line) => line.ref_offer_id)) return "已回复";
    return "待处理";
  };
  return <Space direction="vertical" size="large" style={{ width: "100%" }}>
    <Space wrap style={{ justifyContent: "space-between", width: "100%" }}>
      <Typography.Title level={3} style={{ margin: 0 }}>询价单</Typography.Title>
      <Space><Button onClick={() => setReload((value) => value + 1)}>刷新</Button>
        <Button type="primary" onClick={() => navigate("/presales/inquiries/new")}>新建询价</Button></Space>
    </Space>
    <Card>
      <Tabs activeKey={scope} onChange={(value) => { setScope(value as "mine" | "all"); setPage(1); }}
        items={[{ key: "mine", label: "我的询价" }, ...(supervisor ? [{ key: "all", label: "全部" }] : [])]} />
      <Space wrap style={{ marginBottom: 16 }}>
        <Select aria-label="询价类型" allowClear placeholder="类型" style={{ width: 150 }} value={kind}
          options={[{ value: "sales_inquiry", label: "销售询价" }, { value: "proactive_offer", label: "主动报价" }]}
          onChange={(value) => { setKind(value); setPage(1); }} />
        <Select aria-label="询价状态" allowClear placeholder="状态" style={{ width: 150 }} value={status}
          options={Object.entries(statuses).map(([value, label]) => ({ value, label }))}
          onChange={(value) => { setStatus(value); setPage(1); }} />
        <DatePicker.RangePicker aria-label="最近动态日期" value={range} onChange={(value) => setRange(value)} />
        <Typography.Text type="secondary">日期筛选作用于当前页最近动态时间。</Typography.Text>
      </Space>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      {detailError && <Alert type="warning" message="部分处理人状态加载失败，请刷新后查看。" style={{ marginBottom: 12 }} />}
      <Table<Inquiry> rowKey="id" dataSource={filtered} loading={loading} scroll={{ x: 950 }}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }}
        columns={[
          { title: "单号", dataIndex: "inquiry_no", render: (value, row) => <Button type="link" onClick={() => navigate(`/presales/inquiries/${row.id}`)}>{value}</Button> },
          { title: "类型", dataIndex: "kind", render: (value) => value === "proactive_offer" ? "主动报价" : "销售询价" },
          { title: "客户", render: (_, row) => "customer_id" in row && row.customer_id != null ? (customers[row.customer_id] || `客户 #${row.customer_id}`) : null },
          { title: "行数", dataIndex: "line_count" },
          { title: "进度", render: (_, row) => <Tag>{progress(row)}</Tag> },
          { title: "处理人", render: (_, row) => <Space wrap>{(details[row.id]?.work_items || []).map((item) => {
            const userId = item.claimed_by ?? item.assignee_id;
            const person = candidates.find((candidate) => candidate.id === userId);
            return <Tag key={item.id}>{userId != null ? `${person?.display_name || `账号 #${userId}`} · ` : item.assignee_id === null && item.claimed_by === null ? "公共池 · " : ""}
              {item.paused ? "已暂停" : workStatuses[item.status] || item.status}</Tag>;
          })}</Space> },
          { title: "最近动态", dataIndex: "updated_at", render: (value) => value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : null },
        ]} />
    </Card>
  </Space>;
}
