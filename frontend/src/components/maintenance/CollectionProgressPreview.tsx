import { Alert, Collapse, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

import type { MaintenanceCollectionProgress } from "../../api/maintenanceBatchTransfer";

type ProgressState = MaintenanceCollectionProgress["before"];
type Milestone = ProgressState["milestones"][number];
type ProgressRow = { before: Milestone; after?: Milestone };

const STATES: Record<Milestone["payment_state"], { label: string; color: string }> = {
  paid: { label: "已到账", color: "green" },
  partial: { label: "部分到账", color: "orange" },
  unpaid: { label: "未到账", color: "red" },
  not_due: { label: "未到期", color: "blue" },
  not_reported: { label: "未报", color: "default" },
  incomplete: { label: "计划不完整", color: "gold" },
};

/** The server supplies cent-precision decimals; formatting never adds money. */
function money(value: string | null, empty = "未报"): string {
  if (value === null) return empty;
  const match = /^(-?\d+)(?:\.(\d+))?$/.exec(value);
  if (!match) return "—";
  const whole = match[1].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `¥${whole}.${(match[2] ?? "").padEnd(2, "0").slice(0, 2)}`;
}

function periodMoney(milestone: Milestone, field: "received_for_period" | "remaining_amount") {
  return money(milestone[field], milestone.payment_state === "incomplete" ? "无法判断" : "未报");
}

function stateTag(milestone: Milestone) {
  const state = STATES[milestone.payment_state];
  return <Tag color={state.color}>{state.label}</Tag>;
}

function change(before: React.ReactNode, after: React.ReactNode, valid: boolean) {
  return <Space size={4} wrap>{before}<span aria-hidden="true">→</span>{valid ? after : "待完成勾选"}</Space>;
}

function selectedCandidate(progress: MaintenanceCollectionProgress, selected: Set<string>) {
  return progress.candidates.reduce<MaintenanceCollectionProgress["candidates"][number] | undefined>(
    (latest, candidate) => selected.has(candidate.row_key)
      && (!latest || candidate.report_month > latest.report_month) ? candidate : latest,
    undefined,
  );
}

export default function CollectionProgressPreview({
  progress,
  selectedRowKeys,
  selectionValid,
}: {
  progress: MaintenanceCollectionProgress[];
  selectedRowKeys: string[];
  selectionValid: boolean;
}) {
  if (!progress.length) return null;
  const selected = new Set(selectedRowKeys);
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Typography.Text strong>回款期次进度</Typography.Text>
      <Typography.Text type="secondary">
        按合同累计到账依次覆盖各期计划，一笔收款可以覆盖多期；这里展示本次勾选的影响。
      </Typography.Text>
      {!selectionValid && <Alert type="warning" showIcon message="勾选尚不完整，补齐依赖行后才计算导入后进度" />}
      <Collapse items={progress.map((contract) => {
        const candidate = selectedCandidate(contract, selected);
        const olderMonth = !!candidate && !!contract.before.report_month
          && candidate.report_month < contract.before.report_month;
        const after: ProgressState = candidate && !olderMonth ? candidate.after : contract.before;
        const afterById = new Map(after.milestones.map((item) => [item.milestone_id, item]));
        const rows = contract.before.milestones.map((item) => ({ before: item, after: afterById.get(item.milestone_id) }));
        const columns: ColumnsType<ProgressRow> = [
          { title: "期次", key: "sequence", width: 70, render: (_, item) => `第 ${item.before.sequence} 期` },
          { title: "计划日期", key: "planned_date", width: 110, render: (_, item) => item.before.planned_date ?? "未填写" },
          { title: "计划金额", key: "planned_amount", width: 120, render: (_, item) => money(item.before.planned_amount, "未填写") },
          {
            title: "本期到账（前 → 后）", key: "received", width: 190,
            render: (_, item) => change(periodMoney(item.before, "received_for_period"), periodMoney(item.after ?? item.before, "received_for_period"), selectionValid),
          },
          {
            title: "剩余金额（前 → 后）", key: "remaining", width: 190,
            render: (_, item) => change(periodMoney(item.before, "remaining_amount"), periodMoney(item.after ?? item.before, "remaining_amount"), selectionValid),
          },
          {
            title: "状态（前 → 后）", key: "state", width: 210,
            render: (_, item) => change(stateTag(item.before), stateTag(item.after ?? item.before), selectionValid),
          },
        ];
        return {
          key: contract.contract_id,
          label: <Space size={8} wrap>
            <Typography.Text strong>{contract.project_name || contract.contract_no || "维保合同"}</Typography.Text>
            {contract.project_name && contract.contract_no && <Typography.Text type="secondary">{contract.contract_no}</Typography.Text>}
            <span>导入前累计 {money(contract.before.cumulative_received)}</span>
            {selectionValid
              ? <span>→ 勾选后累计 {money(after.cumulative_received)}</span>
              : <Tag color="gold">待完成勾选</Tag>}
          </Space>,
          children: <Space direction="vertical" size={8} style={{ width: "100%" }}>
            {selectionValid && !candidate && <Typography.Text type="secondary">未勾选本合同的收款，金额和期次保持原样。</Typography.Text>}
            {selectionValid && olderMonth && <Typography.Text type="secondary">本次补录较早月份，最新累计和期次进度保持原样。</Typography.Text>}
            {contract.before.milestones.some((item) => item.payment_state === "incomplete") && <Alert type="warning" showIcon message="计划不完整，后续期次也无法准确判断，请先补齐计划。" />}
            {rows.length
              ? <Table<ProgressRow> size="small" rowKey={(item) => item.before.milestone_id} columns={columns} dataSource={rows} pagination={false} scroll={{ x: 890 }} />
              : <Typography.Text type="secondary">未配置回款计划；收款仍可正常导入，暂不计算期次进度。</Typography.Text>}
          </Space>,
        };
      })} />
    </Space>
  );
}
