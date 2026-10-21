/** 维保数据看板各页签的共享展示件（R33 / D-0022 + R34 / D-0024）。
 *
 * 铁律：无金额权时金额卡片/列/图不渲染（不是 CSS 隐藏、不是 ¥0 冒充）；
 * 数量缺失与真 0 不混淆（null → "—"）；返还率分母≤0 显示 "—" 而非 0%。
 *
 * 会话守卫（DL-010）：请求发出前捕获 localStorage token，响应落地前直接
 * 重新读取比对；不一致（切换账号/登出）就丢弃该响应，不依赖重渲染时序。
 * 迟到结果另由各页签的序号（seq）拒绝，两者缺一不可。
 */
import { App, Button, Card, Skeleton, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { moneyExact, qty as qtyFmt } from "../../../utils/format";
import { COST_SOURCE_LABEL } from "../panel/panelUtils";
import {
  downloadDashboardExport,
  type DashboardExportKind,
  type DashboardFilters,
  type DomainReferencePayload,
} from "../../../api/maintenanceDashboard";

const { Text } = Typography;

/** 页签公共入参：筛选集由壳层解析；排序/分页改动经 onSortChange/onPageChange 写回 URL。 */
export interface DashboardTabProps {
  params: DashboardFilters;
  amountsVisible: boolean;
  /** 本页签允许的服务端排序键（options 下发；金额键已在无权时被服务端剔除）。 */
  sorts: string[];
  sort: string;
  page: number;
  pageSize: number;
  refreshKey: number;
  maxExportRows: number;
  onSortChange: (sort: string) => void;
  onPageChange: (page: number, pageSize: number) => void;
}

/** 请求发出前的会话令牌（用于落地前比对）。 */
export function sessionToken(): string | null {
  return localStorage.getItem("token");
}

/** 落地前校验：token 仍与发出时一致才允许应用响应（切换账号/登出即丢弃）。 */
export function sessionUnchanged(token: string | null): boolean {
  return localStorage.getItem("token") === token;
}

/** 后端 detail（字符串或 {message, code}）→ 页面可读错误文案。
 * 契约 422 业务码只保证 code（如 invalid_retention_ratio）：message 缺失时
 * 显示 code 本身，绝不吞成笼统「加载失败」。 */
export function apiErrorMessage(err: unknown): string {
  const detail = (err as {
    response?: { data?: { detail?: { message?: string; code?: string } | string } };
  }).response?.data?.detail;
  if (typeof detail === "string") return detail;
  return detail?.message || detail?.code || "加载失败";
}

export function KpiCard({ label, value, sub, loading, testId }: {
  label: string; value: React.ReactNode; sub?: React.ReactNode; loading?: boolean;
  testId?: string;
}) {
  return (
    <Card size="small" style={{ flex: "1 1 180px" }} data-testid={testId}>
      <Text type="secondary" style={{ fontSize: 12 }}>{label}</Text>
      <div style={{ fontSize: 20, fontWeight: 600, marginTop: 4, lineHeight: 1.4 }}>
        {loading ? <Skeleton.Button active size="small" style={{ width: 140 }} /> : value}
      </div>
      {sub && !loading ? <Text type="secondary" style={{ fontSize: 12 }}>{sub}</Text> : null}
    </Card>
  );
}

/** 后端 ".3f" 数量串 → 千分位展示；null/缺失 → "—"（不是 0）。 */
export function qtyText(value: string | null | undefined): string {
  return value === null || value === undefined ? "—" : qtyFmt(Number(value));
}

/** 数量串 → 数字（图表/排序用）；缺失 → null。 */
export function qtyNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** 后端 ".2f" 金额串 → 千分位展示；null/缺失 → "—"（缺价不是 0 元）。 */
export function moneyText(value: string | null | undefined): string {
  return value === null || value === undefined ? "—" : moneyExact(Number(value));
}

/** 返还率（百分比数值）：null = 无率（分母≤0），绝不显示成 0%；>100% 原样。 */
export function rateText(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${value}%`;
}

export const RATE_NO_VALUE_HINT = "无率：同期实际领用≤0";

/** T4 基数状态 → 标签（只描述核对基数，不发布返还状态）。 */
export const BASIS_STATE_TAGS: Record<string, { label: string; color: string }> = {
  complete: { label: "基数完整", color: "success" },
  incomplete: { label: "待核对", color: "warning" },
  inactive: { label: "失效", color: "default" },
  ownership_conflict: { label: "归属冲突", color: "error" },
};

export function BasisStateTag({ state }: { state: string }) {
  const meta = BASIS_STATE_TAGS[state];
  return meta ? <Tag color={meta.color}>{meta.label}</Tag> : <Tag>{state}</Tag>;
}

/** 参考域中文名（三域并列：采购优先、销售回退；领用已计成本独立视角）。 */
export const DOMAIN_LABELS: Record<string, string> = {
  purchase: "采购",
  sales: "销售",
  cost: "领用已计成本",
};

/** 待估/不可用原因码 → 可读文案（与后端 reason 枚举一一对应）。 */
export const REASON_LABELS: Record<string, string> = {
  no_samples: "窗口内无成交样本",
  unit_missing_master: "主数据缺单位",
  unit_missing_sample: "样本缺单位",
  unit_mixed_samples: "样本单位不一致",
  unit_mismatch: "单位与主数据不一致",
  price_unavailable: "样本无有效价格",
  cost_not_recorded: "窗口内领用无已计成本",
  unknown_condition: "件况未知（不估价）",
  pn_unresolved: "PN 未归一（无主数据）",
  purchase_fallback: "采购无样本，回退销售参考",
};

export function reasonText(code: string | null | undefined): string {
  if (!code) return "—";
  return REASON_LABELS[code] ?? code;
}

/** 单域市场参考一行：可用 → 金额 + 样本数；不可用 → 原因（不显示 ¥0）。 */
export function ReferenceLine({ domain, reference }: {
  domain: string; reference: DomainReferencePayload;
}) {
  if (!reference.available) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        {DOMAIN_LABELS[domain] ?? domain}：无参考（{reasonText(reference.unavailable_reason)}）
      </Text>
    );
  }
  return (
    <Text style={{ fontSize: 12 }}>
      {DOMAIN_LABELS[domain] ?? domain} ¥{reference.inc_tax ?? "—"}
      {reference.order_count !== undefined ? `（${reference.order_count} 单）` : null}
      {reference.low_confidence ? (
        <Text type="warning" style={{ fontSize: 12 }}> 低样本</Text>
      ) : null}
    </Text>
  );
}

/** 排序键 → 中文标签（各页签共用；金额键只在有权时出现在候选里）。 */
export const SORT_LABELS: Record<string, string> = {
  bad_receipt_qty_desc: "坏件实收降序",
  receipt_qty_desc: "返件实收降序",
  issued_qty_desc: "实际领用降序",
  project_code_asc: "项目编码升序",
  bad_value_desc: "坏件试算价值降序",
  difference_desc: "差额降序",
  over_received_desc: "超收降序",
  demand_qty_desc: "需求量降序",
};

/** 待试算提示（R34：无比例不出金额，绝不拿 0 冒充）。 */
export const AWAITING_RATIO_TEXT = "待试算（未设比例）";

/** 待估提示（市场两域无样本：与「待试算（缺比例）」「无权限」分开呈现）。 */
export const PENDING_ESTIMATE_TEXT = "待估（无可用样本）";

/** 数量域端点（orders/consumption/consumption-issues）契约上不收关键词 q 与
 * 试算比例 value_retention_ratio：发请求前显式剥离，不依赖服务端忽略。 */
export function qtyOnlyParams(params: DashboardFilters): DashboardFilters {
  const { q: _q, value_retention_ratio: _ratio, ...rest } = params;
  return rest;
}

/** 成本来源码 → 中文：复用项目面板既有可信映射（panelUtils.COST_SOURCE_LABEL，
 * 与后端 MaintenanceSiteIssueLine.cost_source 枚举一一对应）。未收录码保留
 * 原码并明确标注「未收录」，不臆造含义（码表冻结后由后端契约背书）。 */
export function costSourceText(code: string | null | undefined): string {
  if (!code) return "—";
  return COST_SOURCE_LABEL[code] ?? `${code}（未收录来源）`;
}

/** M-AC09 下钻 = 原筛选 ∩ 点击维度（DL-022：绝不用维度值替换/扩大原范围）。
 * 点击的行本身来自当前筛选集，故取点击维度后必然是原范围的子集。 */

/** 项目下钻：project 收敛为被点击项目（原 project 选集包含它；未选时原为全量）。 */
export function drillByProject(
  params: DashboardFilters, projectId: string,
): DashboardFilters {
  return { ...params, project: projectId };
}

/** PN 下钻：关键词收敛为被点击 PN（该 PN 已满足原关键词，不扩大）。 */
export function drillByPn(params: DashboardFilters, pn: string): DashboardFilters {
  return { ...params, q: pn };
}

/** 日期下钻：期间收敛为被点击的上海日历日（原窗口的子集）。 */
export function drillByDate(params: DashboardFilters, date: string): DashboardFilters {
  return { ...params, range: "custom", date_from: date, date_to: date };
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/** 导出按钮对：CSV/XLSX 同一筛选集；无金额权的服务端不含金额列，前端不再裁剪。
 * 下载前同样校验会话（DL-010）：token 变了就不落盘旧会话的数据。 */
export function DashboardExportButtons({ params, kind, maxExportRows }: {
  params: DashboardFilters;
  kind: DashboardExportKind;
  maxExportRows: number;
}) {
  const { message } = App.useApp();
  const [busy, setBusy] = useState<string | null>(null);
  const download = async (format: "csv" | "xlsx") => {
    setBusy(format);
    const token = sessionToken();
    try {
      const { blob, filename } = await downloadDashboardExport({ ...params, kind, format });
      if (!sessionUnchanged(token)) {
        message.warning("会话已变更，已取消下载；请重新登录后重试");
        return;
      }
      saveBlob(blob, filename);
      message.success(`已导出 ${filename}`);
    } catch (err) {
      if (sessionUnchanged(token)) message.error(apiErrorMessage(err));
    } finally {
      if (sessionUnchanged(token)) setBusy(null);
    }
  };
  return (
    <Space size={8}>
      <Button size="small" loading={busy === "csv"} onClick={() => void download("csv")}>
        导出 CSV
      </Button>
      <Button size="small" loading={busy === "xlsx"} onClick={() => void download("xlsx")}>
        导出 XLSX
      </Button>
      <Text type="secondary" style={{ fontSize: 12 }}>上限 {maxExportRows.toLocaleString("zh-CN")} 行</Text>
    </Space>
  );
}
