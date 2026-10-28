import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Alert, Button, Table, Tag } from "antd";
import { presales, type PartInquiryRecord } from "../../api/presales";
import { formatBusinessDateTime } from "../../utils/date";
import { formatMoney } from "../../utils/format";
import { taxLabel } from "./shared";

const statuses = { draft: "草稿", submitted: "已提交", revising: "撤回修改中", closed: "已结束" };

export default function PartInquiryHistory({ partId }: { partId: number }) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState<PartInquiryRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => { setPage(1); }, [partId]);
  useEffect(() => {
    const controller = new AbortController();
    setData([]); setTotal(0); setLoading(true); setError(false);
    presales.partInquiries(partId, page, controller.signal).then((result) => {
      if (!controller.signal.aborted) { setData(result.items); setTotal(result.total); }
    }).catch(() => { if (!controller.signal.aborted) setError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [partId, page, reload]);
  return <section aria-label="型号询价记录">
    {error && <Alert type="error" message="询价记录加载失败" action={<Button onClick={() => setReload((value) => value + 1)}>重试</Button>} />}
    <Table aria-label="询价记录" rowKey="line_id" dataSource={data} loading={loading} tableLayout="fixed" pagination={{ current: page, pageSize: 20, total, onChange: setPage, showSizeChanger: false }} columns={[
      { title: "询价时间", dataIndex: "created_at", width: 170, render: formatBusinessDateTime },
      { title: "询价人", dataIndex: "created_by_name", width: 120 },
      { title: "数量", dataIndex: "qty", width: 80 },
      { title: "状态", dataIndex: "status", width: 100, render: (status: PartInquiryRecord["status"]) => <Tag>{statuses[status]}</Tag> },
      { title: "报价情况 / 最低有效报价", key: "quotes", render: (_, row) => <>{row.has_quote ? "已有报价" : "暂无报价"}{row.lowest_valid_quotes.map((quote, index) => <div key={index}>{formatMoney(quote.unit_price, { prefix: "¥" })} · {taxLabel(quote.is_tax_inclusive)}</div>)}</> },
      { title: "操作", key: "actions", width: 110, render: (_, row) => <Link to={`/presales/inquiries/${row.inquiry_id}`}>打开询价单</Link> },
    ]} />
  </section>;
}
