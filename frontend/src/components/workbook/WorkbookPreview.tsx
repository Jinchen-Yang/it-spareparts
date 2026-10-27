import { Alert, Checkbox, Space, Table, Tag, Typography, type TableProps } from "antd";
import type { WorkbookIssue, WorkbookPlan, WorkbookPlanRow, WorkbookRowStatus, WorkbookValue } from "../../api/workbooks";
import { tokens } from "../../tokens";

export const ROW_COLORS: Record<WorkbookRowStatus, string> = {
  added: tokens.color.tag.success.background, modified: tokens.color.tag.warning.background,
  unchanged: tokens.color.surface, deleted: tokens.color.tag.danger.background, conflict: tokens.color.borderSoft,
};
const LABELS: Record<WorkbookRowStatus, string> = {
  added: "新增", modified: "修改", unchanged: "无变化", deleted: "删除候选", conflict: "冲突",
};

const valueText = (value: WorkbookValue | undefined) => value == null || value === "" ? "（空）"
  : typeof value === "boolean" ? (value ? "是" : "否") : String(value);

export function workbookIssueText(issue: WorkbookIssue): string {
  return [issue.sheet, issue.row_number == null ? null : `第 ${issue.row_number} 行`, issue.column, issue.message]
    .filter(Boolean).join(" · ");
}

interface Props {
  plan: WorkbookPlan;
  confirmDeletes: string[];
  onConfirmDeletes: (keys: string[]) => void;
  disabled?: boolean;
}

export function WorkbookPreview({ plan, confirmDeletes, onConfirmDeletes, disabled }: Props) {
  const fields = new Map<string, { sheet: string; key: string; header: string }>();
  const fieldId = (sheet: string, key: string) => JSON.stringify([sheet, key]);
  const multipleSheets = new Set(plan.rows.map((row) => row.sheet)).size > 1;
  plan.rows.forEach((row) => row.changes.forEach((change) => fields.set(fieldId(row.sheet, change.key), {
    sheet: row.sheet, key: change.key, header: change.header,
  })));
  plan.rows.forEach((row) => {
    if (row.status === "deleted") {
      Object.keys(row.values || {}).forEach((key) => {
        const id = fieldId(row.sheet, key);
        if (!fields.has(id)) fields.set(id, { sheet: row.sheet, key, header: key });
      });
      Object.keys(row.custom_fields || {}).forEach((name) => fields.set(fieldId(row.sheet, `+${name}`), {
        sheet: row.sheet, key: `+${name}`, header: `+${name}`,
      }));
    }
    row.errors.forEach((issue) => {
      if (issue.column && ![...fields.values()].some((field) => field.sheet === row.sheet
        && (field.header === issue.column || field.key === issue.column))) {
        fields.set(fieldId(row.sheet, issue.column), { sheet: row.sheet, key: issue.column, header: issue.column });
      }
    });
  });
  const columns: TableProps<WorkbookPlanRow>["columns"] = [
    { title: "状态", key: "status", width: 115, render: (_, row) => <Tag>{LABELS[row.status]}</Tag> },
    { title: "位置", key: "position", width: 160, render: (_, row) => `${row.sheet} · ${row.row_number == null ? "导出原行" : `第 ${row.row_number} 行`}` },
    { title: "记录", dataIndex: "entity_id", key: "entity_id", width: 110 },
    ...[...fields.entries()].map(([id, { sheet, key, header }]) => ({
      title: multipleSheets ? `${sheet} · ${header}` : header, key: id,
      render: (_: unknown, row: WorkbookPlanRow) => {
        if (row.sheet !== sheet) return "—";
        const change = row.changes.find((item) => item.key === key);
        const errors = row.errors.filter((issue) => issue.column === key || issue.column === header);
        const warnings = row.warnings.filter((issue) => issue.column === key || issue.column === header);
        return <div data-field={key} data-invalid={errors.length > 0 || undefined}
          style={{ padding: 4, minWidth: 110, border: errors.length ? `2px solid ${tokens.color.danger}` : undefined, overflowWrap: "anywhere" }}>
          {change ? <><span aria-label="修改前">{valueText(change.before)}</span><span> → </span><strong aria-label="修改后">{valueText(change.after)}</strong></>
            : row.values && key in row.values ? valueText(row.values[key])
              : key.startsWith("+") && row.custom_fields && key.slice(1) in row.custom_fields ? valueText(row.custom_fields[key.slice(1)]) : "—"}
          {errors.map((issue, index) => <div key={`error-${index}`} style={{ color: tokens.color.danger }}>{issue.message}</div>)}
          {warnings.map((issue, index) => <div key={`warning-${index}`} style={{ color: tokens.color.warning }}>{issue.message}</div>)}
        </div>;
      },
    })),
    { title: "删除确认", key: "delete", width: 170, render: (_, row) => row.status === "deleted" && row.delete_key
      ? <Checkbox disabled={disabled} checked={confirmDeletes.includes(row.delete_key)}
        aria-label={`确认删除 ${row.sheet} ${row.entity_id}`}
        onChange={(event) => onConfirmDeletes(event.target.checked ? [...confirmDeletes, row.delete_key!]
          : confirmDeletes.filter((key) => key !== row.delete_key))}>确认删除此行</Checkbox> : null },
    { title: "校验提示", key: "issues", width: 240, render: (_, row) => <>
      {row.errors.map((issue, index) => <div key={`e${index}`} style={{ color: tokens.color.danger }}>{workbookIssueText(issue)}</div>)}
      {row.warnings.map((issue, index) => <div key={`w${index}`} style={{ color: tokens.color.warning }}>{workbookIssueText(issue)}</div>)}
    </> },
  ];

  return <Space direction="vertical" size={12} style={{ width: "100%" }}>
    <Space wrap>{Object.entries(LABELS).map(([status, label]) => <Tag key={status} style={{ backgroundColor: ROW_COLORS[status as WorkbookRowStatus] }}>
      {label} {plan.counts[status as WorkbookRowStatus] || 0}
    </Tag>)}</Space>
    {plan.errors.length > 0 && <Alert type="error" showIcon message="请修正错误后重新上传"
      description={plan.errors.map((issue, index) => <div key={index}>{workbookIssueText(issue)}</div>)} />}
    {plan.warnings.length > 0 && <Alert type="warning" showIcon message="上传提示"
      description={plan.warnings.map((issue, index) => <div key={index}>{workbookIssueText(issue)}</div>)} />}
    {plan.counts.conflict > 0 && <Alert type="warning" message="存在版本冲突，请重新导出最新工作簿后修改。" />}
    {plan.counts.deleted > 0 && <Typography.Text type="secondary">删除候选需要逐条勾选；未勾选的行会保留。</Typography.Text>}
    <Table<WorkbookPlanRow> size="small" rowKey="key" columns={columns} dataSource={plan.rows}
      pagination={{ pageSize: 20, showSizeChanger: true }} scroll={{ x: "max-content" }}
      onRow={(row) => ({ "data-status": row.status, style: { backgroundColor: ROW_COLORS[row.status],
        outline: row.errors.length ? `2px solid ${tokens.color.danger}` : undefined, outlineOffset: -2 } } as React.HTMLAttributes<HTMLTableRowElement>)} />
  </Space>;
}

export default WorkbookPreview;
