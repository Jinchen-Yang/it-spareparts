/** 维保数据看板各页签的共享展示件（R33 / D-0022 + R34 / D-0024）。
 *
 * 铁律：无金额权时金额卡片/列/图不渲染（不是 CSS 隐藏、不是 ¥0 冒充）；
 * 数量缺失与真 0 不混淆（null → "—"）；返还率分母≤0 显示 "—" 而非 0%。
 *
 * 会话守卫（DL-010）：请求发出前捕获 localStorage token，响应落地前直接
 * 重新读取比对；不一致（切换账号/登出）就丢弃该响应，不依赖重渲染时序。
 * 迟到结果另由各页签的序号（seq）拒绝，两者缺一不可。
 */
import { App, Button, Skeleton, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { moneyExact, qty as qtyFmt } from "../../../utils/format";
import { COST_SOURCE_LABEL } from "../panel/panelUtils";
import "./maintenanceDashboard.css";
import {
  downloadDashboardExport,
  type DashboardExportKind,
  type DashboardFilters,
  type DomainReferencePayload,
  type DomainValuationPayload,
  type UnitQtyEntry,
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
  /** 页面所属会话：options 落地时捕获的 localStorage token（null=未登录态）。
   * 请求发出前与响应落地前都以它校验（不是点击时才读新 token 当所属，B-F03/04）；
   * 失配时调用 onSessionMismatch，由壳层清除/停用旧会话已渲染的金额与项目上下文。 */
  ownerToken: string | null;
  onSessionMismatch: () => void;
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

export type KpiTone = "accent" | "danger" | "success" | "warning" | "neutral";

/** KPI 磁贴（2026-10-22 重设计）：左侧色条标语义，主值大字等宽数字，副行
 * 原样承载口径说明与单位分桶（不藏进 Tooltip——数据事实必须直接可见）。
 * extra 位于主值与副行之间（如返还率进度条）；wide 跨满整行。 */
export function KpiCard({ label, value, sub, loading, testId, tone = "neutral", wide, extra }: {
  label: React.ReactNode; value: React.ReactNode; sub?: React.ReactNode; loading?: boolean;
  testId?: string; tone?: KpiTone; wide?: boolean; extra?: React.ReactNode;
}) {
  return (
    <div
      className={`mdb-kpi mdb-tone-${tone}${wide ? " is-wide" : ""}`}
      data-testid={testId}
    >
      <div className="mdb-kpi-label">{label}</div>
      <div className="mdb-kpi-value">
        {loading ? <Skeleton.Button active size="small" style={{ width: 140 }} /> : value ?? "—"}
      </div>
      {extra && !loading ? extra : null}
      {sub && !loading ? <div className="mdb-kpi-sub">{sub}</div> : null}
    </div>
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

// ---- 单位分桶展示（契约 v2 §0/§4/§5，B-F09）：数量与分母永不分单位混算 ----

/** 待核桶文案（unit=null/空 = 主数据缺单位，不猜「件」）。 */
export const UNIT_UNRESOLVED_TEXT = "待核";

/** 混合单位文案（≥2 种单位：平面数量/覆盖率不可加总，分桶呈现）。 */
export const UNIT_MIXED_TEXT = "混合单位";

export function unitBucketText(unit: string | null | undefined): string {
  return unit ? unit : UNIT_UNRESOLVED_TEXT;
}

/** 行级「单位」列：唯一单位 → 单位名；混合 → 混合单位；仅待核/缺 → 待核。 */
export function unitColumnText(
  unit: string | null | undefined,
  breakdown?: readonly { unit: string | null }[] | null,
): string {
  if (unit) return unit;
  if (breakdown && breakdown.length > 1) return UNIT_MIXED_TEXT;
  return UNIT_UNRESOLVED_TEXT;
}

/** 单位分桶小字行（后端已按单位名升序、待核恒最后；前端原样呈现，不重排）。 */
export function UnitBreakdown<T extends { unit: string | null }>({ entries, render, testId }: {
  entries?: T[] | null;
  render: (entry: T) => React.ReactNode;
  testId?: string;
}) {
  if (!entries?.length) return null;
  return (
    <span data-testid={testId}>
      {entries.map((entry) => (
        <Text
          key={entry.unit ?? "__unresolved__"}
          type="secondary"
          style={{ fontSize: 12, display: "block" }}
        >
          {unitBucketText(entry.unit)}：{render(entry)}
        </Text>
      ))}
    </span>
  );
}

/** 表格数量单元格：平面值 → 单一数量；null + 分桶 → 分单位行（不跨单位
 * 求和）；皆缺 → "—"。 */
export function QtyBreakdownCell<T extends { unit: string | null }>({ flat, breakdown, pick }: {
  flat: string | null | undefined;
  breakdown?: T[] | null;
  pick: (entry: T) => string | null | undefined;
}) {
  if (flat !== null && flat !== undefined) return <>{qtyText(flat)}</>;
  if (breakdown?.length) {
    return (
      <span>
        {breakdown.map((entry) => (
          <Text key={entry.unit ?? "__unresolved__"} style={{ fontSize: 12, display: "block" }}>
            {unitBucketText(entry.unit)}：{qtyText(pick(entry))}
          </Text>
        ))}
      </span>
    );
  }
  return <>—</>;
}

/** B2-F05：无制单日期下单说明（M01 卡副行）。order_qty 非空 → 平面一行；
 * null + unit_breakdown → 混合单位 + 每单位真实下单量（不猜日期、不入
 * 期间、不跨单位求和）；null 且无分桶 → 数量缺失「—」（不是 0）。 */
export function UndatedOrdersNote({ undated }: {
  undated: {
    order_qty: string | null;
    order_count: number;
    note: string;
    unit_breakdown?: UnitQtyEntry[];
  };
}) {
  if (undated.order_qty != null) {
    return (
      <span>
        无制单日期下单量 {qtyText(undated.order_qty)}
        ／单数 {qtyText(String(undated.order_count))}
        （未计入期间，不猜日期）
      </span>
    );
  }
  if (undated.unit_breakdown?.length) {
    return (
      <Space direction="vertical" size={0}>
        <span>
          无制单日期下单量 {UNIT_MIXED_TEXT}
          ／单数 {qtyText(String(undated.order_count))}
          （未计入期间，不猜日期）
        </span>
        <UnitBreakdown
          entries={undated.unit_breakdown}
          render={(e) => qtyText(e.order_qty)}
        />
      </Space>
    );
  }
  return (
    <span>
      无制单日期下单量 —／单数 {qtyText(String(undated.order_count))}
      （未计入期间，不猜日期）
    </span>
  );
}

// ---- 三域返件估值覆盖（契约 v2 §2，B-F07）----

/** 域估值视角标签：成本域在此是估值分域（与三域参考列的「领用已计成本」
 * 参考 issued_lines 口径是两个事实，命名上分开）。 */
const DOMAIN_VALUATION_LABELS: Record<string, string> = {
  purchase: DOMAIN_LABELS.purchase,
  sales: DOMAIN_LABELS.sales,
  cost: "成本域",
};

/** 双税并列（B2-F01）：后端 ".2f" 原串直接展示（¥113.00 不是 ¥113——收到的
 * 原值即证据），两侧各自接收到的都显示；只有一侧就只显示那一侧（未知侧
 * 缺失，绝不补 0、绝不用含税÷1.13 推算）。两侧都没有 → null（由调用方给
 * 缺失语义）。 */
export function dualTaxText(
  incTax: string | null | undefined,
  exTax: string | null | undefined,
): string | null {
  const sides: string[] = [];
  if (incTax !== null && incTax !== undefined) sides.push(`¥${incTax}（含税）`);
  if (exTax !== null && exTax !== undefined) sides.push(`¥${exTax}（不含税）`);
  return sides.length ? sides.join("／") : null;
}

/** 三域返件估值覆盖行：分值双税并列 + 域内已估/待估 + 覆盖率（bad_receipts
 * 返件估值口径）。域无金额键且域内确无已估量 → 待估（域内无已估金额）；
 * 有已估量但金额键缺失 → 金额未提供（不误报域全无事实）。混合单位 →
 * 平面已估/待估/覆盖 null + unit_breakdown 分单位行，不跨单位求和（B2-F02）。 */
export function DomainCoverageLines({ domains, testId }: {
  domains: Record<string, DomainValuationPayload>;
  testId?: string;
}) {
  const entries = Object.entries(domains);
  if (!entries.length) return null;
  return (
    <span data-testid={testId}>
      {entries.map(([domain, payload]) => {
        const hasEstimatedFact =
          Number(payload.estimated_bad_qty ?? 0) > 0 ||
          (payload.unit_breakdown ?? []).some(
            (bucket) => Number(bucket.estimated_bad_qty ?? 0) > 0,
          );
        const money = dualTaxText(payload.value_inc_tax, payload.value_ex_tax)
          ?? (hasEstimatedFact ? "金额未提供" : "待估（域内无已估金额）");
        const flatCoverageKnown =
          payload.estimated_bad_qty !== null ||
          payload.pending_bad_qty !== null ||
          payload.coverage_pct !== null;
        return (
          <span key={domain} style={{ display: "block" }}>
            <Text style={{ fontSize: 12 }}>
              {DOMAIN_VALUATION_LABELS[domain] ?? domain} {money}
              {flatCoverageKnown
                ? ` · 已估 ${qtyText(payload.estimated_bad_qty)}`
                  + ` / 待估 ${qtyText(payload.pending_bad_qty)}`
                  + ` · 覆盖 ${rateText(payload.coverage_pct)}（返件估值口径）`
                : null}
            </Text>
            {!flatCoverageKnown && payload.unit_breakdown?.length
              ? payload.unit_breakdown.map((bucket) => (
                <Text
                  key={bucket.unit ?? "__unresolved__"}
                  type="secondary"
                  style={{ fontSize: 12, display: "block" }}
                >
                  {unitBucketText(bucket.unit)}：已估 {qtyText(bucket.estimated_bad_qty)}
                  {" / 待估 "}{qtyText(bucket.pending_bad_qty)}
                  {" · 覆盖 "}{rateText(bucket.coverage_pct)}（返件估值口径）
                </Text>
              ))
              : null}
            {!flatCoverageKnown && !payload.unit_breakdown?.length ? (
              <Text style={{ fontSize: 12 }}>
                {" "}· 已估 {qtyText(payload.estimated_bad_qty)}
                {" / 待估 "}{qtyText(payload.pending_bad_qty)}
                {" · 覆盖 "}{rateText(payload.coverage_pct)}（返件估值口径）
              </Text>
            ) : null}
          </span>
        );
      })}
    </span>
  );
}

/** 单域市场参考一行：可用 → 双税并列 + 样本数（B2-F01）；不可用 → 原因
 * （不显示 ¥0；两侧都没收到 → ¥— 缺失，不互相推算）。 */
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
  const money = dualTaxText(reference.inc_tax, reference.ex_tax);
  return (
    <Text style={{ fontSize: 12 }}>
      {DOMAIN_LABELS[domain] ?? domain} {money ?? "¥—"}
      {reference.order_count !== undefined ? `（${reference.order_count} 单）` : null}
      {reference.low_confidence ? (
        <Text type="warning" style={{ fontSize: 12, color: "var(--mb-text-2)" }}> 低样本</Text>
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
 * 试算比例 value_retention_ratio：发请求前显式剥离，不依赖服务端忽略。
 * （quantity-checks 契约 v2 §6 起支持 q 整单选择——该端点不走此剥离。） */
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

/** 登记人下钻：在原筛选上追加登记人条件（只收窄，不替换原范围）。 */
export function drillByRegistrant(
  params: DashboardFilters, createdBy: string,
): DashboardFilters {
  return { ...params, created_by: createdBy };
}

/** 返件来源码 → 中文（未收录码原样显示）。 */
export const RECEIPT_SOURCE_LABELS: Record<string, string> = {
  manual: "手工登记",
  rkd_import: "RKD 导入",
};

export function receiptSourceText(code: string | null | undefined): string {
  if (!code) return "—";
  return RECEIPT_SOURCE_LABELS[code] ?? code;
}

/** 早期导入行未记录操作人时 created_by 落库默认值（模型 server_default）。 */
export const UNRECORDED_IMPORTER = "rkd_import";

/** 登记人显示名：有显示名用显示名；默认导入占位账号标明未记录操作人；否则账号。 */
export function registrantLabel(
  username: string, name?: string | null,
): string {
  if (name) return name;
  return username === UNRECORDED_IMPORTER ? "导入（未记录操作人）" : username;
}

/** 登记人展示：有显示名 →「显示名（账号）」；否则账号原样（占位账号标注）。 */
export function registrantText(
  username: string | null | undefined, name?: string | null,
): string {
  if (!username) return "—";
  if (name) return `${name}（${username}）`;
  return registrantLabel(username, name);
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
 * 会话所有权（B-F04/DL-010）：ownerToken 是按钮所属页面会话（数据落地时捕获），
 * 请求发出前先校验它仍当前——换号后点击旧按钮直接拒绝，不发旧组件请求；
 * 响应落地前再次校验，旧会话数据不落盘。 */
export function DashboardExportButtons({
  params, kind, maxExportRows, ownerToken, onSessionMismatch,
}: {
  params: DashboardFilters;
  kind: DashboardExportKind;
  maxExportRows: number;
  ownerToken: string | null;
  onSessionMismatch?: () => void;
}) {
  const { message } = App.useApp();
  const [busy, setBusy] = useState<string | null>(null);
  const download = async (format: "csv" | "xlsx") => {
    // 请求前：绑定所属会话（不是点击时才读新 token 然后当作所属）
    if (!sessionUnchanged(ownerToken)) {
      onSessionMismatch?.();
      message.warning("登录会话已变更，已取消导出；请刷新页面后重试");
      return;
    }
    setBusy(format);
    const token = ownerToken;
    try {
      const { blob, filename } = await downloadDashboardExport({ ...params, kind, format });
      if (!sessionUnchanged(token)) {
        onSessionMismatch?.();
        message.warning("会话已变更，已取消下载；请重新登录后重试");
        return;
      }
      saveBlob(blob, filename);
      message.success(`已导出 ${filename}`);
    } catch (err) {
      if (sessionUnchanged(token)) message.error(apiErrorMessage(err));
      else onSessionMismatch?.();
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
