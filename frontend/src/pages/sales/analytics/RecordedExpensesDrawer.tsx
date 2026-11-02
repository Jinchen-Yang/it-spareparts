import { useState } from "react";
import { Alert, Button, Drawer, Table } from "antd";
import { fetchRecordedSalesExpenses, type SalesAnalyticsFilters } from "../../../api/salesAnalytics";
import { fmtMoney } from "../shared";
import { useAsyncData } from "./useAsyncData";

export default function RecordedExpensesDrawer({ filters, ownerToken, onOwnerStale, onClose }: {
  filters: SalesAnalyticsFilters; ownerToken: string | null; onOwnerStale: () => void; onClose: () => void;
}) {
  const [page, setPage] = useState(1);
  const { data, loading, error, retry } = useAsyncData(
    () => fetchRecordedSalesExpenses(filters, page), [JSON.stringify(filters), page],
    () => "费用记录加载失败，请重试。", true, ownerToken, onOwnerStale,
  );
  return <Drawer open title="已记录费用" width={900} onClose={onClose}>
    <Alert type="info" showIcon style={{ marginBottom: 16 }} message="入选销售单的整单费用"
      description="包含已结束的源报销及已确认归属的人工费用，累计到今天；未记录不等于没有费用。商品筛选仅选择销售单，费用不分摊到商品。" />
    {error ? <Alert type="error" message={error} action={<Button onClick={retry}>重试</Button>} /> :
      <Table rowKey={row => `${row.source}:${row.id}`} loading={loading} dataSource={data?.items || []}
        size="small" scroll={{ x: 800 }} locale={{ emptyText: "未找到费用记录，不能据此认定费用为零" }}
        pagination={{ current: page, pageSize: 50, total: data?.total ?? 0, showSizeChanger: false, onChange: setPage }}
        columns={[
          { title: "费用日期", dataIndex: "expense_date" },
          { title: "销售单号", dataIndex: "order_no" },
          { title: "费用单号", dataIndex: "reference", render: value => value || "/" },
          { title: "分类", dataIndex: "category", render: value => value || "/" },
          { title: "来源", dataIndex: "source", render: value => value === "manual" ? "人工登记" : "源报销" },
          { title: "含税金额", dataIndex: "amount_inc_tax", align: "right", render: fmtMoney },
        ]} />}
  </Drawer>;
}
