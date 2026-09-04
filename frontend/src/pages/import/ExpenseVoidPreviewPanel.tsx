import { Alert, Card, Space, Table, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { ExpenseVoidPreview, ExpenseVoidRow } from "../../api/imports";

const REASON: Record<string, string> = {
  dropped_no_contract: "本表有行因缺少销售订单被排除",
  multi_contract: "本表触及多个销售订单",
  unanchored: "本表没有页级「销售订单」锚",
};

const columns: ColumnsType<ExpenseVoidRow> = [
  { title: "报销日期", dataIndex: "expense_date", width: 110 },
  { title: "单号", dataIndex: "bxd_no", width: 150 },
  { title: "序号", dataIndex: "line_no", width: 60 },
  { title: "报销人员", dataIndex: "person", width: 90 },
  { title: "支出事由", dataIndex: "reason", ellipsis: true },
  { title: "金额", dataIndex: "amount", width: 110, align: "right" },
  { title: "当前状态", dataIndex: "data_status", width: 90 },
];

function Summary({ preview }: { preview: ExpenseVoidPreview }) {
  if (preview.status === "ready" && preview.void) {
    const { rows, amount, already_void_rows } = preview.void;
    if (rows === 0) {
      return (
        <Alert type="success" showIcon
          message={`合同 ${preview.contract}：本次不会作废任何旧报销行`}
          description={already_void_rows > 0
            ? `该合同名下另有 ${already_void_rows} 行早已作废，不重复计。本表覆盖了其余全部报销行。`
            : "本表覆盖了该合同名下全部报销行。"} />
      );
    }
    return (
      <Alert type="warning" showIcon
        message={`合同 ${preview.contract}：将作废 ${rows} 条旧报销行，合计 ${amount}`}
        description={(
          <Space direction="vertical" size={2}>
            <span>这些行在系统里存在、但不在本表里。「以本表为准」会把它们作废，成本随之从项目卡片上扣除。请逐行核对下方清单。</span>
            {already_void_rows > 0 && <span>另有 {already_void_rows} 行早已作废，不重复计。</span>}
            <span>预演有效期 30 分钟；预演之后若相关报销行被他人改动，或本文件被修改，导入会被拒绝并要求重新预演——不会按你没看过的清单执行。</span>
          </Space>
        )} />
    );
  }
  if (preview.status === "suppressed") {
    return (
      <Alert type="info" showIcon
        message="本次不会作废任何旧报销行"
        description={`${REASON[preview.reason ?? ""] ?? preview.reason ?? "删除侧被抑制"}；修复模式只做同键覆盖（改金额照常生效）。`} />
    );
  }
  if (preview.status === "will_be_rejected") {
    return (
      <Alert type="error" showIcon
        message="导入将被整批拒绝"
        description={`报销页含会拦批的错误行：${preview.blocking_error_types.join("、") || "见预检结果"}。`} />
    );
  }
  if (preview.status === "unreadable") {
    return <Alert type="error" showIcon message="预演解析失败" description={preview.error ?? undefined} />;
  }
  return <Alert type="info" showIcon message="本文件不涉及修复模式删除侧（无报销页或非修复模式）" />;
}

export default function ExpenseVoidPreviewPanel({ previews }: { previews: ExpenseVoidPreview[] }) {
  if (!previews.length) return null;
  return (
    <Card title="作废预演（修复模式 · 以本表为准）" size="small" style={{ marginTop: 16 }}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {previews.map((preview) => (
          <Card key={preview.filename} type="inner" title={preview.filename} size="small">
            <Summary preview={preview} />
            {preview.status === "ready" && preview.void_rows.length > 0 && (
              <>
                <Table<ExpenseVoidRow>
                  size="small" rowKey="raw_line_id" columns={columns}
                  dataSource={preview.void_rows} pagination={{ pageSize: 10 }}
                  scroll={{ x: 720 }} style={{ marginTop: 12 }} />
                {preview.void_rows_truncated && (
                  <Typography.Text type="secondary">
                    清单只展示前 {preview.void_rows.length} 行，合计数以上方为准。
                  </Typography.Text>
                )}
              </>
            )}
          </Card>
        ))}
      </Space>
    </Card>
  );
}
