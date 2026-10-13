import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import PartPicker from "../PartPicker";
import SiteIssueAddress from "./SiteIssueAddress";
import {
  createManualSiteIssue,
  getSiteIssueDemandCandidates,
  patchManualSiteIssue,
  previewManualSiteIssue,
  voidSiteIssue,
  type ManualSiteIssueLineInput,
  type ManualSiteIssuePreview,
  type ManualSiteIssuePreviewLine,
  type SiteIssueDemandCandidate,
  type SiteIssueDocument,
} from "../../api/maintenanceOperations";

const { Text } = Typography;

/**
 * 人工登记领用（v1.36）：项目总表 06 人工领用的页面通道。
 *
 * 用户录入的是明确的人工业务事实（日期/接收人/PN/SN/数量/是否应返还），
 * 不需要仓库发货候选——生产里发货适配器未就绪时，这里是日常可用入口。
 * 源语义诚实（page_manual）：不是 Excel 上传、也不是仓库发货领用。
 * 「从仓库发货领用」的草稿/确认闸门在 SiteIssueWorkflowPanel 维持原样，
 * 两通道互不成为必经步骤。
 *
 * 并发纪律（Codex 复审 P1，2026-09-20）：
 * - 幂等键绑定完整归一化请求指纹（头字段+行），改任何字段即换新键；
 *   PATCH 同样冻结完整 payload+key，重试不新建键。
 * - epoch 计数器：项目变化 / 弹窗开闭 / 组件卸载都使旧回调失效，
 *   在途响应不污染新上下文。
 * - 预览后任何编辑使 preview 失效；提交绑定预览冻结的 payload，
 *   不从当前表单重组（防「保存未预览的内容」）。
 * - 提交/预览/作废用同步 ref 锁防双击；await 后先查 epoch 再动状态。
 * - 写成功与刷新失败分开提示：已 200 的登记不因刷新失败被误报为失败。
 *
 * 按 PN 建议需求单 + 地址建议预填（2026-10-12 方案 §3.3）：
 * - 行内「关联需求单」升级为 AutoComplete：按行内 part_id 拉候选、手输仍允许
 *   任意值（服务端校验兜底）；各行命中的候选（含脱敏收货地址）按行上报。
 * - 地址建议只做「预填」不做「覆盖」（D-26 规则 1）：仅新建表单、领用地址为空、
 *   用户从未手动编辑过、且各行已选需求单恰好指向同一张单时，填入该单收货地址
 *   并标注来源；用户一旦手动编辑过，本表单会话内建议永久退位。
 */

const commandKey = (prefix: string) => {
  const nativeUuid = globalThis.crypto?.randomUUID?.();
  return `${prefix}-${nativeUuid ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
};

const today = () => {
  const current = new Date();
  const offset = current.getTimezoneOffset() * 60_000;
  return new Date(current.getTime() - offset).toISOString().slice(0, 10);
};

const amount = (value: string | null | undefined) => value == null
  ? "—"
  : `¥${Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

interface EditorLine {
  key: string;
  /** 更正时保留服务端行身份；新行为 null。 */
  issueLineId: string | null;
  partId: number | null;
  pn: string;
  quantity: number | null;
  serialNumber: string;
  noReturn: "" | "yes" | "no";
  demandOrderNo: string;
  remark: string;
}

const emptyLine = (): EditorLine => ({
  key: commandKey("manual-line"),
  issueLineId: null,
  partId: null,
  pn: "",
  quantity: null,
  serialNumber: "",
  noReturn: "",
  demandOrderNo: "",
  remark: "",
});

interface EditorValues {
  issueDate: string;
  issueNo: string;
  receiver: string;
  issuedBy: string;
  siteLocation: string;
  reason: string;
}

const emptyEditor = (): EditorValues => ({
  issueDate: today(),
  issueNo: "",
  receiver: "",
  issuedBy: "",
  siteLocation: "",
  reason: "人工登记现场领用",
});

const noReturnOptions = [
  { value: "", label: "按规则判断" },
  { value: "no", label: "应返还" },
  { value: "yes", label: "不返还" },
];

/** 归一化完整请求（头+行）——幂等指纹与提交 payload 共用同一来源。 */
const buildRequest = (
  editor: EditorValues,
  lines: EditorLine[],
):
  { ok: true; header: Record<string, unknown>; lines: ManualSiteIssueLineInput[] }
  | { ok: false; error: string } => {
  const header = {
    issue_date: editor.issueDate.trim(),
    ...(editor.issueNo.trim() ? { issue_no: editor.issueNo.trim() } : {}),
    receiver: editor.receiver.trim(),
    issued_by: editor.issuedBy.trim(),
    site_location: editor.siteLocation.trim(),
    reason: editor.reason.trim(),
  };
  if (
    !header.issue_date || !header.receiver || !header.issued_by
    || !header.site_location || !header.reason
  ) {
    return { ok: false, error: "请填写日期、接收人、发出人、领用地址和原因" };
  }
  if ([...header.site_location].length > 256) {
    return { ok: false, error: "领用地址最多 256 个字符，请缩短后再提交" };
  }
  if (!lines.length) return { ok: false, error: "请至少添加一条领用明细" };
  const clean: ManualSiteIssueLineInput[] = [];
  for (const line of lines) {
    if (line.partId == null) {
      return { ok: false, error: "每一行都必须选择型号（PN）" };
    }
    if (line.quantity == null || !(line.quantity > 0)) {
      return { ok: false, error: "每一行的领用数量都必须大于 0" };
    }
    clean.push({
      ...(line.issueLineId ? { issue_line_id: line.issueLineId } : {}),
      part_id: line.partId,
      quantity: line.quantity,
      ...(line.serialNumber.trim() ? { serial_number: line.serialNumber.trim() } : {}),
      no_return: line.noReturn === "" ? null : line.noReturn === "yes",
      ...(line.demandOrderNo.trim() ? { demand_order_no: line.demandOrderNo.trim() } : {}),
      ...(line.remark.trim() ? { remark: line.remark.trim() } : {}),
    });
  }
  return { ok: true, header, lines: clean };
};

const previewColumns: ColumnsType<ManualSiteIssuePreviewLine> = [
  {
    title: "PN / SN",
    render: (_v, row) => (
      <>
        <Text strong style={{ fontFamily: "monospace" }}>{row.pn}</Text>
        <br />
        <Text type="secondary">{row.serial_number || "无 SN"}</Text>
      </>
    ),
  },
  { title: "领用数量", dataIndex: "quantity" },
  { title: "未税成本", dataIndex: "cost_amount_ex_tax", render: amount },
  { title: "含税成本", dataIndex: "cost_amount_inc_tax", render: amount },
  {
    title: "取价依据",
    render: (_v, row) => row.cost_source == null
      ? <Tag color="orange">待补价格，金额留空</Tag>
      : <Tag color={row.cost_is_estimate ? "gold" : "blue"}>{row.cost_source_label}</Tag>,
  },
];

/** 候选行在行内下拉里的展示文本：单号（日期）；order_date 可空，缺日期不带括号。 */
const demandOptionLabel = (row: SiteIssueDemandCandidate) =>
  row.order_date ? `${row.order_no}（${row.order_date}）` : row.order_no;

/**
 * 行内「关联需求单」自动完成（按 PN 建议需求单，2026-10-12 方案 §3.3）：
 * - 行内已选 PN → 按 part_id 拉本项目含该 PN 的活跃需求单候选；手输时 300ms
 *   防抖叠加 q 参数（与 PartPicker 同范式：代次守卫防慢响应竞态）。
 * - 未选 PN → 不拉数据（placeholder 提示先选 PN）；仍允许手输任意值，
 *   服务端 _resolve_demand_project 校验兜底，手输不在候选内也能提交。
 * - 选定候选（或手输值恰好等于某候选单号）时向头级上报该候选（含脱敏收货地址），
 *   供领用地址建议预填使用；restricted 候选三字段为 null，前端不显示脱敏数据。
 */
function DemandOrderAutoComplete({
  projectId,
  lineKey,
  partId,
  ariaLabel,
  value,
  disabled,
  autoLink = true,
  onValueChange,
  onCandidateChange,
}: {
  projectId: string;
  /** 行 key：候选信息按行上报，行删除后父级记录自然失联。 */
  lineKey: string;
  partId: number | null;
  ariaLabel: string;
  value: string;
  disabled?: boolean;
  /** 唯一候选自动关联只允许新建（2026-10-13 裁定范围）；编辑既有单时不自动写入，
   *  避免无关更正悄悄改动历史关联。 */
  autoLink?: boolean;
  onValueChange: (next: string) => void;
  onCandidateChange: (lineKey: string, candidate: SiteIssueDemandCandidate | null) => void;
}) {
  const [options, setOptions] = useState<SiteIssueDemandCandidate[]>([]);
  // 代次守卫：只认最新一次请求，慢响应不覆盖新输入（PartPicker 同范式）。
  const gen = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchCandidates = useCallback((q: string) => {
    if (partId == null) return;
    const g = ++gen.current;
    getSiteIssueDemandCandidates(projectId, {
      partId,
      ...(q ? { q } : {}),
      pageSize: 50,
    })
      .then(({ data }) => {
        if (g !== gen.current) return;
        setOptions(data.rows ?? []);
      })
      .catch(() => {
        if (g !== gen.current) return;
        setOptions([]);
      });
  }, [projectId, partId]);

  // 选定 PN（或项目变化）即拉一次该 PN 的候选；未选 PN 不拉，避免无界下拉。
  useEffect(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    gen.current += 1; // 作废在途请求
    if (partId == null) {
      setOptions([]);
      return;
    }
    fetchCandidates("");
  }, [fetchCandidates, partId]);

  // 卸载：清掉防抖定时器并作废在途响应，不许写已卸载的行。
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
    gen.current += 1;
  }, []);

  const onSearch = (text: string) => {
    if (timer.current) clearTimeout(timer.current);
    const q = text.trim();
    if (partId == null || !q) return; // 未选 PN / 清空搜索：退回已加载的 PN 候选
    timer.current = setTimeout(() => fetchCandidates(q), 300);
  };

  // 值或候选集变化后重算本行命中：选中、清空、手输恰好等于候选单号都同步上报。
  useEffect(() => {
    const key = value.trim();
    onCandidateChange(
      lineKey,
      key ? options.find((row) => row.order_no === key) ?? null : null,
    );
  }, [options, value, lineKey, onCandidateChange]);

  // 2026-10-13 口径（领用地址=需求单联系地址）：含该 PN 的候选恰好一张且本行
  // 还没关联任何单时自动选上——仅限新建（autoLink），编辑既有单不自动写入。
  // 用户仍可清空改选或手输其他单号。多张候选不自动选，避免替用户猜单。
  const autoSelectedRef = useRef(false);
  useEffect(() => {
    if (!autoLink || partId == null || autoSelectedRef.current) return;
    if (value.trim() || options.length !== 1) return;
    autoSelectedRef.current = true;
    onValueChange(options[0].order_no);
  }, [options, value, partId, autoLink, onValueChange]);

  return (
    <AutoComplete
      aria-label={ariaLabel}
      value={value}
      disabled={disabled}
      allowClear
      style={{ width: 240 }}
      placeholder={partId == null ? "先选择 PN 后可按需求单候选" : "可搜需求单号或 PN"}
      notFoundContent={partId == null ? "先选择 PN 后可按需求单候选" : "无匹配需求单，可手输单号"}
      options={options.map((row) => ({ value: row.order_no, label: demandOptionLabel(row) }))}
      onSearch={onSearch}
      onChange={(next) => onValueChange(typeof next === "string" ? next : "")}
    />
  );
}

export default function ManualSiteIssueSection({
  projectId,
  canManage,
  issues,
  onChanged,
  reloadIssues,
}: {
  projectId: string;
  canManage: boolean;
  /** 面板当前的领用单列表（含全部来源），人工登记单的更正/作废入口就挂在行上。 */
  issues: SiteIssueDocument[];
  onChanged: () => void | Promise<void>;
  reloadIssues: () => Promise<void>;
}) {
  // epoch：任何项目变化 / 卸载 / 弹窗重开都自增；await 之后必须复核。
  const epochRef = useRef(0);
  const mountedRef = useRef(true);
  const activeProjectRef = useRef(projectId);
  const submittingRef = useRef(false);
  const previewingRef = useRef(false);
  const voidingRef = useRef(false);
  const previewGenerationRef = useRef(0);
  // 冻结的预览/提交上下文：confirm 只用这份 payload，不再看当前表单。
  const frozenRef = useRef<{
    epoch: number;
    header: Record<string, unknown>;
    lines: ManualSiteIssueLineInput[];
    key: string;
  } | null>(null);
  const voidAttemptRef = useRef<{ content: string; key: string } | null>(null);
  // 领用地址建议预填的 dirty 追踪：用户手动 onChange 过 site_location 即置 dirty，
  // 本表单会话内不再自动预填；打开新建 / 编辑回填时重置（回填值不算 dirty）。
  const siteLocationDirtyRef = useRef(false);
  // 当前由建议预填写入的地址（撤回建议时只清这个值，绝不触碰用户输入）。
  const prefilledAddressRef = useRef<string | null>(null);

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<SiteIssueDocument | null>(null);
  const [editor, setEditor] = useState<EditorValues>(emptyEditor);
  const [lines, setLines] = useState<EditorLine[]>([emptyLine()]);
  // 各行命中的需求单候选（含脱敏收货地址）：派生 UI 状态，不进提交 payload，
  // 更新也不失效预览（值本身的变化才走 updateLine 失效预览）。
  const [demandCandidates, setDemandCandidates] = useState<
    Record<string, SiteIssueDemandCandidate | null>
  >({});
  const [editorError, setEditorError] = useState<string | null>(null);
  const [preview, setPreview] = useState<ManualSiteIssuePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [voidTarget, setVoidTarget] = useState<SiteIssueDocument | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voiding, setVoiding] = useState(false);

  // 项目变化 / 卸载：epoch 前进 + 全部局部状态清空，旧表单不留到新项目。
  useEffect(() => {
    // StrictMode 下 effect 会重放：mount 时必须重新置 true，否则一次
    // cleanup 之后就永久失效（Codex 复审 P1③）。
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      epochRef.current += 1;
      previewGenerationRef.current += 1;
      frozenRef.current = null;
    };
  }, []);
  useEffect(() => {
    if (activeProjectRef.current === projectId) return;
    activeProjectRef.current = projectId;
    epochRef.current += 1;
    previewGenerationRef.current += 1;
    frozenRef.current = null;
    voidAttemptRef.current = null;
    // 旧项目的在途 finally 不许碰新项目状态：锁随 epoch 一起重置。
    submittingRef.current = false;
    previewingRef.current = false;
    voidingRef.current = false;
    setEditorOpen(false);
    setEditing(null);
    setEditor(emptyEditor());
    setLines([emptyLine()]);
    setDemandCandidates({});
    siteLocationDirtyRef.current = false;
    prefilledAddressRef.current = null;
    setPreview(null);
    setEditorError(null);
    setPreviewLoading(false);
    setSubmitting(false);
    setVoidTarget(null);
    setVoidReason("");
    setVoiding(false);
  }, [projectId]);

  // 预览生效后任何编辑（头字段/行字段/增删行）统一失效预览与冻结 payload：
  // 确认按钮只能消费「预览过」的内容，绝不提交未预览的新表单（P1②）。
  // invalidatePreview 同时推进 generation，让在途旧预览彻底作废。
  const invalidatePreview = useCallback(() => {
    previewGenerationRef.current += 1;
    setPreview(null);
    frozenRef.current = null;
  }, []);
  const updateEditor = useCallback((patch: Partial<EditorValues>) => {
    invalidatePreview();
    setEditor((current) => ({ ...current, ...patch }));
  }, [invalidatePreview]);
  const updateLine = useCallback((key: string, patch: Partial<EditorLine>) => {
    invalidatePreview();
    setLines((current) => current.map(
      (line) => (line.key === key ? { ...line, ...patch } : line),
    ));
  }, [invalidatePreview]);
  const addLine = useCallback(() => {
    invalidatePreview();
    setLines((current) => [...current, emptyLine()]);
  }, [invalidatePreview]);
  const removeLine = useCallback((key: string) => {
    invalidatePreview();
    setLines((current) => current.filter((line) => line.key !== key));
  }, [invalidatePreview]);

  // —— 按 PN 建议需求单：各行候选上报 + 领用地址建议预填（方案 §3.3）——
  const recordDemandCandidate = useCallback(
    (lineKey: string, candidate: SiteIssueDemandCandidate | null) => {
      setDemandCandidates((current) => (
        current[lineKey] === candidate ? current : { ...current, [lineKey]: candidate }
      ));
    },
    [],
  );

  // 恰好所有已选需求单的行指向同一张单（非空单号集合 size == 1）才认定「唯一」。
  const uniqueSelectedDemandNo = useMemo(() => {
    const nos = Array.from(new Set(
      lines.map((line) => line.demandOrderNo.trim()).filter(Boolean),
    ));
    return nos.length === 1 ? nos[0] : null;
  }, [lines]);

  const uniqueDemandCandidate = useMemo(() => {
    if (uniqueSelectedDemandNo == null) return null;
    for (const line of lines) {
      const candidate = demandCandidates[line.key];
      if (candidate?.order_no === uniqueSelectedDemandNo) return candidate;
    }
    return null;
  }, [lines, demandCandidates, uniqueSelectedDemandNo]);

  // 有客户信息权限且地址非空才构成建议；restricted 时三字段为 null（fail-closed）。
  const suggestedAddress = uniqueDemandCandidate != null
    && uniqueDemandCandidate.contact_info_state !== "restricted"
    && uniqueDemandCandidate.receiver_address
    ? uniqueDemandCandidate.receiver_address.trim()
    : null;
  const suggestedOrderNo = suggestedAddress != null && uniqueDemandCandidate != null
    ? uniqueDemandCandidate.order_no
    : null;

  // 地址建议预填（D-26：只建议、不覆盖）：仅新建、未手动编辑过时把空地址填为
  // 唯一已选需求单的收货地址；建议条件消失时只撤回我们自己填入且未被改动的值。
  // 任何能触发本效果的行/候选变化都先经过 updateLine（已失效预览），这里再失效
  // 一次兜底，确保冻结 payload 不与表单漂移。
  useEffect(() => {
    if (!editorOpen || editing || siteLocationDirtyRef.current) return;
    const current = editor.siteLocation;
    if (suggestedAddress == null) {
      if (prefilledAddressRef.current != null && current === prefilledAddressRef.current) {
        prefilledAddressRef.current = null;
        invalidatePreview();
        setEditor((state) => ({ ...state, siteLocation: "" }));
      }
      return;
    }
    if (current === suggestedAddress) {
      prefilledAddressRef.current = suggestedAddress;
      return;
    }
    if (current !== "" && prefilledAddressRef.current !== current) return; // 用户/回填内容，不动
    prefilledAddressRef.current = suggestedAddress;
    invalidatePreview();
    setEditor((state) => ({ ...state, siteLocation: suggestedAddress }));
  }, [editorOpen, editing, editor.siteLocation, suggestedAddress, invalidatePreview]);

  const newDialogEpoch = useCallback(() => {
    epochRef.current += 1;
    previewGenerationRef.current += 1;
    frozenRef.current = null;
    setPreview(null);
    setEditorError(null);
  }, []);

  if (!canManage) return null;

  const manualIssues = issues.filter((issue) => issue.source === "page_manual"
    && issue.workflow_status !== "void");

  const openCreate = () => {
    newDialogEpoch();
    setEditing(null);
    setEditor(emptyEditor());
    setLines([emptyLine()]);
    // 新表单会话：地址建议的 dirty / 预填痕迹、行候选记录全部清零。
    siteLocationDirtyRef.current = false;
    prefilledAddressRef.current = null;
    setDemandCandidates({});
    setEditorOpen(true);
  };

  const openEdit = (issue: SiteIssueDocument) => {
    newDialogEpoch();
    setEditing(issue);
    setEditor({
      issueDate: issue.issue_date,
      issueNo: issue.issue_no,
      receiver: issue.receiver ?? "",
      issuedBy: issue.issued_by ?? "",
      siteLocation: issue.site_location ?? "",
      reason: issue.workflow_status === "corrected" ? "继续更正人工领用" : "更正人工领用",
    });
    setLines(issue.lines.map((line) => ({
      key: line.issue_line_id,
      issueLineId: line.issue_line_id,
      partId: line.part_id,
      pn: line.pn,
      quantity: Number(line.quantity),
      serialNumber: line.serial_number ?? "",
      noReturn: line.no_return == null ? "" : line.no_return ? "yes" : "no",
      demandOrderNo: line.demand_order_no ?? "",
      remark: line.remark ?? "",
    })));
    // 编辑回填：既有地址不算 dirty，但编辑态不做地址建议预填；痕迹一并清零。
    siteLocationDirtyRef.current = false;
    prefilledAddressRef.current = null;
    setDemandCandidates({});
    setEditorOpen(true);
  };

  // preview generation：任何编辑（updateEditor/updateLine/addLine/removeLine）与
  // 弹窗 epoch 前进都自增。迟到预览的 generation 落后即丢弃，不得回填
  // frozenRef/preview——否则「预览后改字段」会把旧结果当成可确认依据。
  const runPreview = async () => {
    if (previewingRef.current || submittingRef.current) return;
    const built = buildRequest(editor, lines);
    if (!built.ok) {
      setEditorError(built.error);
      return;
    }
    previewingRef.current = true;
    setPreviewLoading(true);
    setEditorError(null);
    const epoch = epochRef.current;
    const generation = previewGenerationRef.current;
    const requestedProject = projectId;
    try {
      const { data } = await previewManualSiteIssue(requestedProject, {
        issue_date: built.header.issue_date as string,
        receiver: built.header.receiver as string,
        issued_by: built.header.issued_by as string,
        site_location: built.header.site_location as string,
        lines: built.lines,
      });
      if (
        !mountedRef.current
        || epoch !== epochRef.current
        || generation !== previewGenerationRef.current
      ) return;
      // 冻结本次预览对应的完整 payload 与幂等键：confirm 只消费这份。
      frozenRef.current = {
        epoch,
        header: built.header,
        lines: built.lines,
        key: commandKey(editing ? "manual-site-patch" : "manual-site-create"),
      };
      setPreview(data);
    } catch {
      if (
        mountedRef.current
        && epoch === epochRef.current
        && generation === previewGenerationRef.current
      ) {
        setEditorError("预览失败，请检查明细后重试（关联需求单必须属于当前项目）");
      }
    } finally {
      // 同一弹窗内，即使编辑已推进 generation，也要结束这次 loading；
      // 但旧 epoch 的 finally 不能解开新项目/新弹窗正在持有的锁。
      if (mountedRef.current && epoch === epochRef.current) {
        previewingRef.current = false;
        setPreviewLoading(false);
      }
    }
  };

  const confirmSave = async () => {
    if (submittingRef.current) return;
    const frozen = frozenRef.current;
    if (!frozen || frozen.epoch !== epochRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    const epoch = epochRef.current;
    const requestedProject = projectId;
    try {
      if (editing) {
        await patchManualSiteIssue(editing.issue_id, {
          project_id: requestedProject,
          version: editing.version,
          idempotency_key: frozen.key,
          ...(frozen.header as {
            issue_date: string;
            issue_no?: string;
            receiver: string;
            issued_by: string;
            site_location: string;
            reason: string;
          }),
          lines: frozen.lines,
        });
      } else {
        await createManualSiteIssue(requestedProject, {
          idempotency_key: frozen.key,
          ...(frozen.header as {
            issue_date: string;
            issue_no?: string;
            receiver: string;
            issued_by: string;
            site_location: string;
            reason: string;
          }),
          lines: frozen.lines,
        });
      }
      if (!mountedRef.current || epoch !== epochRef.current) return;
      // 写入已成功：无论刷新结果如何，登记本体不能被误报为失败。
      setEditorOpen(false);
      frozenRef.current = null;
      setPreview(null);
      const wrote = editing ? "人工领用单已更正" : "人工领用已登记";
      let refreshFailed = false;
      try {
        await reloadIssues();
      } catch {
        refreshFailed = true;
      }
      // 每段 await 后复核：项目切换/卸载后不再碰新上下文的父回调。
      if (!mountedRef.current || epoch !== epochRef.current) return;
      try {
        await onChanged();
      } catch {
        refreshFailed = true;
      }
      if (!mountedRef.current || epoch !== epochRef.current) return;
      if (refreshFailed) {
        message.warning(`${wrote}，但列表刷新失败；数据已保存，请手动刷新页面`);
      } else {
        message.success(wrote);
      }
    } catch {
      if (mountedRef.current && epoch === epochRef.current) {
        setEditorError(
          editing
            ? "更正失败：单据版本可能已被他人更新，请关闭后重试"
            : "登记失败，请重试；同一内容重试不会重复建单",
        );
      }
    } finally {
      if (mountedRef.current && epoch === epochRef.current) {
        submittingRef.current = false;
        setSubmitting(false);
      }
    }
  };

  const confirmVoid = async () => {
    if (voidingRef.current || !voidTarget || !voidReason.trim()) return;
    voidingRef.current = true;
    setVoiding(true);
    const epoch = epochRef.current;
    const requestedProject = projectId;
    const content = JSON.stringify([
      voidTarget.issue_id,
      voidTarget.version,
      voidReason.trim(),
    ]);
    if (voidAttemptRef.current?.content !== content) {
      voidAttemptRef.current = { content, key: commandKey("manual-site-void") };
    }
    try {
      await voidSiteIssue(voidTarget.issue_id, {
        project_id: requestedProject,
        version: voidTarget.version,
        idempotency_key: voidAttemptRef.current.key,
        reason: voidReason.trim(),
      });
      if (!mountedRef.current || epoch !== epochRef.current) return;
      setVoidTarget(null);
      setVoidReason("");
      voidAttemptRef.current = null;
      let refreshFailed = false;
      try {
        await reloadIssues();
      } catch {
        refreshFailed = true;
      }
      if (!mountedRef.current || epoch !== epochRef.current) return;
      try {
        await onChanged();
      } catch {
        refreshFailed = true;
      }
      if (!mountedRef.current || epoch !== epochRef.current) return;
      if (refreshFailed) {
        message.warning("人工领用单已作废，但列表刷新失败；请手动刷新页面");
      } else {
        message.success("人工领用单已作废，退出统计与导出");
      }
    } catch {
      if (mountedRef.current && epoch === epochRef.current) {
        message.error("作废失败，请刷新后重试");
      }
    } finally {
      if (mountedRef.current && epoch === epochRef.current) {
        voidingRef.current = false;
        setVoiding(false);
      }
    }
  };

  const editingLocked = previewLoading || submitting;
  // 预览请求在途时仍允许改表单；每次编辑都会推进 generation，
  // 因而迟到结果只能被丢弃。真正写入期间才冻结字段。
  const formLocked = submitting;

  // 地址建议的展示文案（仅新建会话；dirty 后全部隐去，restricted 显示中性提示，
  // 不显示 restricted 原文）。
  let addressPrefillNote: string | null = null;
  if (
    !editing
    && !siteLocationDirtyRef.current
    && suggestedOrderNo != null
    && suggestedAddress != null
    && editor.siteLocation === suggestedAddress
  ) {
    addressPrefillNote = `已按需求单〈${suggestedOrderNo}〉收货地址预填，可修改`;
  }
  const restrictedHint = !editing
    && !siteLocationDirtyRef.current
    && uniqueDemandCandidate?.contact_info_state === "restricted"
    && editor.siteLocation.trim() === ""
    ? "无客户信息权限，地址建议不可用"
    : null;

  return (
    <Card
      data-testid="manual-site-issue-section"
      title="人工登记领用"
      size="small"
      extra={(
        <Button
          type="primary"
          aria-label="人工登记领用"
          disabled={editingLocked}
          onClick={openCreate}
        >
          人工登记领用
        </Button>
      )}
    >
      <Space direction="vertical" size={10} style={{ width: "100%" }}>
        <Alert
          type="info"
          showIcon
          message="适用于无仓库发货单的现场领用：直接登记人工业务事实"
          description="选择型号、填数量与领用信息，先预览实际消耗与成本再保存；没有价格时金额留空待补，不修改任何库存。已有明确发货单的领用请用上方的「从仓库发货领用」。"
        />
        {manualIssues.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="本项目还没有页面人工登记的领用单"
          />
        ) : (
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            <Text type="secondary">共 {manualIssues.length} 张人工登记领用单</Text>
            {manualIssues.map((issue) => (
              <Card key={issue.issue_id} size="small" className="manual-issue-card">
                <Space direction="vertical" size={4} style={{ width: "100%" }}>
                  <Space wrap>
                    <Text strong>{issue.issue_no}</Text>
                    <Text type="secondary">{issue.issue_date}</Text>
                    <Tag>{issue.workflow_status === "corrected" ? "已更正" : "已确认"}</Tag>
                  </Space>
                  <SiteIssueAddress value={issue.site_location} />
                  <Text type="secondary">
                    {issue.receiver} 接收 · {issue.issued_by} 发出 · {issue.lines.length} 行
                  </Text>
                  <Space wrap>
                    <Button
                      aria-label={`更正 ${issue.issue_no}`}
                      size="small"
                      disabled={editingLocked}
                      onClick={() => openEdit(issue)}
                    >
                      更正
                    </Button>
                    <Button
                      aria-label={`作废 ${issue.issue_no}`}
                      size="small"
                      danger
                      disabled={voiding}
                      onClick={() => {
                        newDialogEpoch();
                        setVoidTarget(issue);
                        setVoidReason("");
                      }}
                    >
                      作废
                    </Button>
                  </Space>
                </Space>
              </Card>
            ))}
          </Space>
        )}
      </Space>

      <Modal
        title={editing ? `更正人工领用 ${editing.issue_no}` : "人工登记领用"}
        open={editorOpen}
        width={960}
        okText={preview ? (editing ? "提交更正" : "确认登记") : "预览实际消耗"}
        cancelText="取消"
        confirmLoading={previewLoading || submitting}
        okButtonProps={{
          disabled: preview
            ? !editor.reason.trim() || submitting
            : previewLoading,
        }}
        onOk={() => {
          if (preview) void confirmSave();
          else void runPreview();
        }}
        onCancel={() => {
          if (submitting || previewLoading) return;
          // 关闭即失效：epoch 前进，在途预览/迟到响应全部作废。
          newDialogEpoch();
          setEditorOpen(false);
        }}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert type="info" showIcon message="单号留空自动生成；这里只填写业务内容。" />
          {editorError && <Alert type="error" showIcon closable message={editorError} />}
          <div className="site-issue-editor-grid">
            <label>领用日期<Input type="date" aria-label="领用日期" disabled={formLocked} value={editor.issueDate} onChange={(event) => updateEditor({ issueDate: event.target.value })} /></label>
            <label>领用单号（选填）<Input aria-label="领用单号（选填）" disabled={formLocked} value={editor.issueNo} onChange={(event) => updateEditor({ issueNo: event.target.value })} placeholder="留空自动生成" /></label>
            <label>接收人<Input aria-label="接收人" disabled={formLocked} value={editor.receiver} onChange={(event) => updateEditor({ receiver: event.target.value })} /></label>
            <label>发出人<Input aria-label="发出人" disabled={formLocked} value={editor.issuedBy} onChange={(event) => updateEditor({ issuedBy: event.target.value })} /></label>
            <label>领用地址（现场位置）<Input.TextArea aria-label="领用地址（现场位置）" autoSize={{ minRows: 2, maxRows: 4 }} disabled={formLocked} value={editor.siteLocation} onChange={(event) => {
              // 用户手动改动即置 dirty：本表单会话内地址建议预填永久退位（D-26）。
              siteLocationDirtyRef.current = true;
              prefilledAddressRef.current = null;
              updateEditor({ siteLocation: event.target.value });
            }} placeholder="填写实际领用地址，最多 256 个字符" />
              {addressPrefillNote && (
                <Text type="secondary" style={{ display: "block" }}>{addressPrefillNote}</Text>
              )}
              {restrictedHint && (
                <Text type="secondary" style={{ display: "block" }}>{restrictedHint}</Text>
              )}
            </label>
          </div>
          <Text type="secondary">领用地址填写实际使用地点，与需求单收货地址分别保存。</Text>
          <label>操作原因<Input aria-label="操作原因" disabled={formLocked} value={editor.reason} onChange={(event) => updateEditor({ reason: event.target.value })} /></label>
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Space style={{ width: "100%", justifyContent: "space-between" }}>
              <Text strong>领用明细（可多行）</Text>
              <Button
                size="small"
                aria-label="添加明细行"
                disabled={formLocked || lines.length >= 200}
                onClick={addLine}
              >
                添加一行
              </Button>
            </Space>
            {lines.map((line, index) => (
              <Card key={line.key} size="small">
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  <Space wrap>
                    <Text type="secondary">第 {index + 1} 行</Text>
                    <PartPicker
                      value={line.partId}
                      onChange={(partId) => updateLine(line.key, { partId })}
                      size="small"
                      disabled={formLocked}
                    />
                    <Button
                      size="small"
                      danger
                      aria-label={`删除第 ${index + 1} 行`}
                      disabled={formLocked || lines.length <= 1}
                      onClick={() => removeLine(line.key)}
                    >
                      删除行
                    </Button>
                  </Space>
                  <Space wrap size={8}>
                    <label>
                      数量
                      <InputNumber
                        aria-label={`第 ${index + 1} 行数量`}
                        min={0.001}
                        precision={3}
                        disabled={formLocked}
                        value={line.quantity}
                        onChange={(value) => updateLine(line.key, { quantity: value == null ? null : Number(value) })}
                      />
                    </label>
                    <label>
                      SN（选填）
                      <Input
                        aria-label={`第 ${index + 1} 行 SN`}
                        disabled={formLocked}
                        value={line.serialNumber}
                        onChange={(event) => updateLine(line.key, { serialNumber: event.target.value })}
                        style={{ width: 160 }}
                      />
                    </label>
                    <label>
                      是否应返还
                      <Select
                        aria-label={`第 ${index + 1} 行是否应返还`}
                        value={line.noReturn}
                        options={noReturnOptions}
                        disabled={formLocked}
                        onChange={(value) => updateLine(line.key, { noReturn: value })}
                        style={{ width: 140 }}
                      />
                    </label>
                    <label>
                      关联需求单（选填）
                      <DemandOrderAutoComplete
                        projectId={projectId}
                        lineKey={line.key}
                        partId={line.partId}
                        ariaLabel={`第 ${index + 1} 行关联需求单`}
                        value={line.demandOrderNo}
                        disabled={formLocked}
                        autoLink={editing == null}
                        onValueChange={(next) => updateLine(line.key, { demandOrderNo: next })}
                        onCandidateChange={recordDemandCandidate}
                      />
                    </label>
                    <label>
                      备注（选填）
                      <Input
                        aria-label={`第 ${index + 1} 行备注`}
                        disabled={formLocked}
                        value={line.remark}
                        onChange={(event) => updateLine(line.key, { remark: event.target.value })}
                        style={{ width: 200 }}
                      />
                    </label>
                  </Space>
                </Space>
              </Card>
            ))}
          </Space>
          {preview && (
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <Alert type="info" showIcon message="库存影响：无" />
              <Table
                rowKey={(row) => `${row.part_id}:${row.serial_number ?? ""}:${row.quantity}`}
                size="small"
                pagination={false}
                columns={previewColumns}
                dataSource={preview.lines}
              />
              <Text type="secondary">
                合计：未税 {amount(preview.total_cost_ex_tax)} · 含税 {amount(preview.total_cost_inc_tax)}
                （有待补价格行时合计显示 —）
              </Text>
            </Space>
          )}
        </Space>
      </Modal>

      <Modal
        title={voidTarget ? `作废 ${voidTarget.issue_no}` : "作废人工领用单"}
        open={voidTarget != null}
        okText="确认作废"
        cancelText="取消"
        confirmLoading={voiding}
        okButtonProps={{ danger: true, disabled: !voidReason.trim() || voiding }}
        onOk={() => void confirmVoid()}
        onCancel={() => {
          if (voiding) return;
          newDialogEpoch();
          setVoidTarget(null);
          setVoidReason("");
        }}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="warning"
            showIcon
            message="整单软作废：全部行退出成本与返还义务计算，历史与审计保留"
          />
          <Input.TextArea
            aria-label="作废原因"
            value={voidReason}
            maxLength={1000}
            showCount
            rows={3}
            placeholder="请填写可审计的真实业务原因"
            onChange={(event) => setVoidReason(event.target.value)}
          />
        </Space>
      </Modal>
    </Card>
  );
}
