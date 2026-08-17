import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Collapse,
  Empty,
  Input,
  InputNumber,
  List,
  Modal,
  Pagination,
  Result,
  Row,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from "antd";
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  DownloadOutlined,
  ReloadOutlined,
  SearchOutlined,
  SendOutlined,
  ShoppingCartOutlined,
} from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import {
  applyReplenishmentRevision,
  deleteReplenishmentCartDraft,
  downloadSystemScreeningWorkbook,
  getReplenishmentApplication,
  getReplenishmentCapabilities,
  getReplenishmentCartDraft,
  getReplenishmentProjects,
  listReplenishmentApplications,
  replaceReplenishmentCartDraft,
  searchReplenishmentCatalog,
  submitReplenishmentCartDraft,
  type ApplicationSummary,
  type CatalogPart,
  type PriceStats,
  type ReplenishmentApplication,
  type ReplenishmentCapabilities,
  type ReplenishmentLine,
  type ReplenishmentProject,
  type ReplenishmentVersion,
} from "../api/replenishment";
import "./ReplenishmentBetaPage.css";

// 补库申请自 2026-08-17 归入维保项目组：前置态返回按钮指向维保主页
const MAINTENANCE_HOME_PATH = "/maintenance";

const { Text, Title } = Typography;

/** 浏览器落盘：导出 Excel 用（#11）。 */
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

const STATUS: Record<ReplenishmentApplication["status"], { label: string; color: string }> = {
  draft: { label: "草稿", color: "blue" },
  submitted: { label: "已提交", color: "gold" },
  needs_revision: { label: "需复核", color: "red" },
  approved: { label: "已通过", color: "green" },
};

/** 待提交行：明细事实在云端草稿保存，价格事实不重复塞进购物车。 */
interface DraftLine {
  part_id: number;
  pn_std: string;
  description: string | null;
  unit: string | null;
  quantity: number;
  special_note: string;
  /** 退回编辑时标记被打回行（#10） */
  rejected?: boolean;
  rejectedReason?: string | null;
  /** 打回行的推荐替换候选（池内相似 PN） */
  recommendations?: Array<{ part_id: number; pn_std: string; pool_name?: string | null }>;
}

function errorText(error: unknown): string {
  const payload = error as { response?: { data?: { detail?: string | { message?: string } } } };
  const detail = payload.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return "操作失败，请刷新后重试";
}

function priceText(stats: PriceStats | null, unit: string | null): string {
  if (!stats || stats.weighted_avg == null) return "半年内无有效样本";
  const avg = Number(stats.weighted_avg);
  if (!Number.isFinite(avg)) return "半年内无有效样本";
  const quantity = unit
    ? `${stats.total_qty ?? 0} ${unit}`
    : `数量 ${stats.total_qty ?? 0}`;
  return `¥${avg.toFixed(2)} · ${quantity} · ${stats.order_count} 单`;
}

function PriceFacts({ part }: { part: Pick<CatalogPart, "purchase" | "sales" | "price_window" | "unit"> }) {
  return (
    <div className="replenishment-price-facts">
      <div><span>近半年采购</span><b>{priceText(part.purchase, part.unit)}</b></div>
      <div><span>近半年销售</span><b>{priceText(part.sales, part.unit)}</b></div>
      <Text type="secondary" style={{ fontSize: 11 }}>
        {part.price_window.date_from} 至 {part.price_window.date_to} · 未税数量加权
      </Text>
    </div>
  );
}

/** 提交时冻结的三查快照摘要（证据层）。 */
function ScreeningSummary({ line }: { line: Pick<ReplenishmentLine, "screening" | "pool_floor_ex_tax" | "latest_sales"> }) {
  if (!line.screening) return null;
  const { as_of, lookback_days, anomaly_count, checks } = line.screening;
  const details = checks
    .filter((check) => !check.passed)
    .map((check) => check.name)
    .join("、");
  // 后端可能返回字符串金额（Decimal 序列化），统一转数字再展示
  const floor = line.pool_floor_ex_tax == null ? null : Number(line.pool_floor_ex_tax);
  return (
    <div className="replenishment-screening-summary">
      <Space wrap size={4}>
        <Tag color={anomaly_count > 0 ? "orange" : "green"}>
          三查{anomaly_count > 0 ? ` ${anomaly_count} 项提示` : "通过"}
        </Tag>
        {floor != null && Number.isFinite(floor) && (
          <Text type="secondary" style={{ fontSize: 11 }}>
            池内最低价参考 ¥{floor.toFixed(2)}
          </Text>
        )}
        {!!line.latest_sales?.date && (
          <Text type="secondary" style={{ fontSize: 11 }}>
            最近销售 {String(line.latest_sales.date)}
          </Text>
        )}
      </Space>
      {details && <Text type="secondary" style={{ fontSize: 11, display: "block" }}>{details}</Text>}
      <Text type="secondary" style={{ fontSize: 11, display: "block" }}>
        快照 {as_of} · 回看 {lookback_days} 天 · 提交时冻结
      </Text>
    </div>
  );
}

function CatalogCard({
  part,
  disabled,
  onAdd,
}: {
  part: CatalogPart;
  disabled: boolean;
  onAdd: (quantity: number) => void;
}) {
  const [quantity, setQuantity] = useState(1);
  return (
    <Card className="replenishment-part-card" size="small">
      <div className="replenishment-part-heading">
        <div>
          <Text strong copyable>{part.pn_std}</Text>
          <div className="replenishment-part-desc">{part.description || "暂无产品描述"}</div>
        </div>
        <Space size={4} wrap>
          <Tag color={part.pool.group_id ? "geekblue" : "default"}>
            {part.pool.name || "未加入互通池"}
          </Tag>
          {part.needs_review && <Tag color="orange">主数据待复核</Tag>}
        </Space>
      </div>
      <PriceFacts part={part} />
      <Space.Compact block>
        <InputNumber
          min={1}
          max={999999}
          precision={0}
          value={quantity}
          onChange={(value) => setQuantity(Number(value || 1))}
          style={{ flex: 1 }}
        />
        <Button disabled>{part.unit || "件"}</Button>
        <Button
          type="primary"
          icon={<ShoppingCartOutlined />}
          disabled={disabled}
          onClick={() => onAdd(quantity)}
        >
          加入申请
        </Button>
      </Space.Compact>
    </Card>
  );
}

/** 提交结果里的一条冻结明细（只读证据层）。 */
function SubmittedLine({ line }: { line: ReplenishmentLine }) {
  const rejected = line.review?.decision === "rejected";
  const recommendations = line.screening?.recommendations || [];
  return (
    <div className={`replenishment-cart-line${rejected ? " is-revision" : ""}`}>
      <div className="replenishment-cart-line-top">
        <div>
          <Text strong>{line.line_no}. {line.pn_std}</Text>
          <div className="replenishment-part-desc">{line.description || "暂无产品描述"}</div>
        </div>
        <Space size={4} wrap>
          <Tag>{line.pool.name || "未加入互通池"}</Tag>
          <Text strong>数量 {line.quantity} {line.unit || "件"}</Text>
        </Space>
      </div>
      <PriceFacts part={line} />
      <ScreeningSummary line={line} />
      {line.special_note && <Text>特殊情况：{line.special_note}</Text>}
      {line.review?.reason && <Text type="danger">审核原因：{line.review.reason}</Text>}
      {rejected && recommendations.length > 0 && (
        <Text type="warning">相似候选：{recommendations.map((item) => `${item.pn_std}（${item.match_reason || "相似"}）`).join("、")}</Text>
      )}
    </div>
  );
}

function VersionHistory({ versions }: { versions: ReplenishmentVersion[] }) {
  return (
    <div className="replenishment-version-history">
      <Text strong>版本留存（{versions.length}）</Text>
      <Text type="secondary" style={{ fontSize: 12 }}>每次提交内容与冻结证据只读保留</Text>
      <Collapse
        size="small"
        items={versions.map((version) => ({
          key: version.version_id,
          label: (
            <Space wrap>
              <Text strong>v{version.version_no}</Text>
              <Tag color={version.status === "draft" ? "blue" : "default"}>
                {version.status === "draft" ? "草稿" : "已提交"}
              </Tag>
            </Space>
          ),
          children: (
            <div className="replenishment-history-version">
              <Text type="secondary">
                {version.submitted_at
                  ? `提交时间：${new Date(version.submitted_at).toLocaleString()}`
                  : "尚未提交"}
              </Text>
              {version.request_note && <Text>整单备注：{version.request_note}</Text>}
              {version.lines.map((line) => (
                <SubmittedLine key={line.line_id} line={line} />
              ))}
            </div>
          ),
        }))}
      />
    </div>
  );
}

function newRevisionRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `revision-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function RevisionPanel({
  application,
  onUpdated,
}: {
  application: ReplenishmentApplication;
  onUpdated: (next: ReplenishmentApplication) => void;
}) {
  const version = application.versions[0];
  const rejected = version?.lines.filter((line) => line.review?.decision === "rejected") || [];
  const [choices, setChoices] = useState<Record<string, {
    action: "replace" | "remove";
    part_id?: number;
    quantity?: number;
    special_note?: string;
  }>>({});
  const [saving, setSaving] = useState(false);
  if (application.status !== "needs_revision" || !version || !rejected.length) return null;
  const setChoice = (requestLineId: string, patch: Partial<{ action: "replace" | "remove"; part_id?: number; quantity?: number; special_note?: string }>) => {
    setChoices((prev) => ({ ...prev, [requestLineId]: { ...prev[requestLineId], ...patch } }));
  };
  const submit = async () => {
    if (rejected.some((line) => !choices[line.request_line_id])) return;
    setSaving(true);
    try {
      const { data } = await applyReplenishmentRevision(application.application_id, {
        expected_application_version: application.version,
        client_request_id: newRevisionRequestId(),
        resolutions: rejected.map((line) => {
          const choice = choices[line.request_line_id];
          // remove 只传 action；replace 传 part_id + 可调数量/特殊情况备注
          if (choice.action === "remove") return { request_line_id: line.request_line_id, action: "remove" };
          return {
            request_line_id: line.request_line_id,
            action: "replace",
            part_id: choice.part_id,
            quantity: choice.quantity ?? line.quantity,
            special_note: choice.special_note?.trim() ? choice.special_note.trim() : null,
          };
        }),
      });
      onUpdated(data);
      message.success("打回行已重新提交");
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setSaving(false);
    }
  };
  return (
    <Card size="small" title="处理自动审核打回行" style={{ marginTop: 12 }}>
      <Space direction="vertical" style={{ width: "100%" }}>
        {rejected.map((line) => {
          const candidates = line.screening?.recommendations || [];
          const choice = choices[line.request_line_id];
          return (
            <div key={line.request_line_id} className="replenishment-cart-line is-revision">
              <Space wrap style={{ width: "100%" }}>
                <Text strong>{line.pn_std}</Text>
                <Text type="danger">{line.review?.reason || "无近182天采购/销售记录"}</Text>
                {candidates.slice(0, 3).map((candidate) => (
                  <Button key={candidate.part_id} size="small"
                    type={choice?.part_id === candidate.part_id ? "primary" : "default"}
                    onClick={() => setChoice(line.request_line_id, {
                      action: "replace", part_id: candidate.part_id, quantity: line.quantity,
                    })}>
                    替换为 {candidate.pn_std}
                  </Button>
                ))}
                <Button size="small" danger
                  type={choice?.action === "remove" ? "primary" : "default"}
                  onClick={() => setChoice(line.request_line_id, { action: "remove" })}>
                  移除
                </Button>
              </Space>
              {choice?.action === "replace" && (
                <Space wrap style={{ marginTop: 8 }}>
                  <InputNumber
                    min={1}
                    max={999999}
                    precision={0}
                    value={choice.quantity ?? line.quantity}
                    onChange={(value) => setChoice(line.request_line_id, { quantity: Number(value || 1) })}
                    placeholder="数量"
                    style={{ width: 100 }}
                  />
                  <Input
                    value={choice.special_note ?? ""}
                    maxLength={4000}
                    onChange={(event) => setChoice(line.request_line_id, { special_note: event.target.value })}
                    placeholder="特殊情况说明（选填，替换后需说明使用原因）"
                    style={{ width: 320 }}
                  />
                </Space>
              )}
              {choice && <Tag color="green" style={{ marginTop: 8 }}>
                {choice.action === "remove" ? "将移除" : `将替换为 ${choice.part_id ? candidates.find((c) => c.part_id === choice.part_id)?.pn_std ?? "已选 PN" : ""}`}
              </Tag>}
            </div>
          );
        })}
        <Button type="primary" loading={saving} disabled={rejected.some((line) => !choices[line.request_line_id])} onClick={() => void submit()}>
          提交处理结果
        </Button>
      </Space>
    </Card>
  );
}

export default function ReplenishmentBetaPage() {
  const navigate = useNavigate();
  const [capabilities, setCapabilities] = useState<ReplenishmentCapabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);

  const [projects, setProjects] = useState<ReplenishmentProject[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [requestNote, setRequestNote] = useState("");
  const [draftLines, setDraftLines] = useState<DraftLine[]>([]);
  const [draftVersion, setDraftVersion] = useState<number | null>(null);
  const [editingRevision, setEditingRevision] = useState<ReplenishmentApplication | null>(null);
  const hydratingCart = useRef(false);
  const cartSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [query, setQuery] = useState("");
  const [catalog, setCatalog] = useState<CatalogPart[]>([]);
  const [catalogTotal, setCatalogTotal] = useState(0);
  const [catalogPage, setCatalogPage] = useState(1);

  const [applications, setApplications] = useState<ApplicationSummary[]>([]);
  const [applicationPage, setApplicationPage] = useState(1);
  const [applicationTotal, setApplicationTotal] = useState(0);
  const [current, setCurrent] = useState<ReplenishmentApplication | null>(null);
  const [exporting, setExporting] = useState(false);

  const exportCurrent = async () => {
    if (!current) return;
    setExporting(true);
    try {
      const response = await downloadSystemScreeningWorkbook(current.application_id);
      saveBlob(response.data, `${current.application_no}-复核包.xlsx`);
      message.success("复核包已导出，可发给领导/采购过目");
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setExporting(false);
    }
  };

  const selectedProject = projects.find((project) => project.project_id === selectedProjectId) || null;

  useEffect(() => {
    if (!selectedProjectId) return;
    // #10：退回编辑模式不加载云端草稿——行来自被打回申请，避免覆盖预填内容
    if (editingRevision) return;
    hydratingCart.current = true;
    setDraftVersion(null);
    setRequestNote("");
    setDraftLines([]);
    void getReplenishmentCartDraft(selectedProjectId)
      .then(({ data }) => {
        const draft = data.draft;
        setDraftVersion(draft?.version ?? null);
        setRequestNote(draft?.request_note ?? "");
        setDraftLines((draft?.lines ?? []).map((line) => ({
          part_id: line.part_id,
          pn_std: line.pn_std,
          description: line.description,
          unit: line.unit,
          quantity: Number(line.quantity),
          special_note: line.special_note ?? "",
        })));
      })
      .catch((error) => message.error(errorText(error)))
      .finally(() => { hydratingCart.current = false; });
    return () => { hydratingCart.current = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId, editingRevision]);

  useEffect(() => {
    // #10：退回编辑模式不写云端草稿（编辑的是申请行，提交走 revisions）
    if (!selectedProjectId || hydratingCart.current || editingRevision) return undefined;
    if (cartSaveTimer.current) clearTimeout(cartSaveTimer.current);
    if (!draftLines.length) {
      if (draftVersion == null) return undefined;
      cartSaveTimer.current = setTimeout(() => {
        void deleteReplenishmentCartDraft(selectedProjectId, draftVersion)
          .then(() => setDraftVersion(null))
          .catch((error) => message.error(errorText(error)));
      }, 500);
      return () => {
        if (cartSaveTimer.current) clearTimeout(cartSaveTimer.current);
      };
    }
    cartSaveTimer.current = setTimeout(() => {
      void replaceReplenishmentCartDraft(selectedProjectId, {
        expected_version: draftVersion,
        request_note: requestNote || null,
        lines: draftLines.map((line) => ({
          part_id: line.part_id,
          quantity: line.quantity,
          special_note: line.special_note || null,
        })),
      }).then(({ data }) => setDraftVersion(data.draft.version))
        .catch((error) => message.error(errorText(error)));
    }, 500);
    return () => {
      if (cartSaveTimer.current) clearTimeout(cartSaveTimer.current);
    };
  }, [selectedProjectId, draftLines, requestNote]);

  const flushCart = async (): Promise<number> => {
    if (!selectedProjectId) throw new Error("请先选择维保项目");
    if (cartSaveTimer.current) clearTimeout(cartSaveTimer.current);
    const { data } = await replaceReplenishmentCartDraft(selectedProjectId, {
      expected_version: draftVersion,
      request_note: requestNote || null,
      lines: draftLines.map((line) => ({
        part_id: line.part_id,
        quantity: line.quantity,
        special_note: line.special_note || null,
      })),
    });
    setDraftVersion(data.draft.version);
    return data.draft.version;
  };

  const refreshList = async (preferredId?: string, page = applicationPage) => {
    const { data } = await listReplenishmentApplications(page, 20);
    setApplications(data.items);
    setApplicationPage(data.page);
    setApplicationTotal(data.total);
    const targetId = preferredId || current?.application_id || data.items[0]?.application_id;
    if (targetId) {
      const detail = await getReplenishmentApplication(targetId);
      setCurrent(detail.data);
    } else {
      setCurrent(null);
    }
  };

  const loadCatalog = async (page = 1, q = query) => {
    const { data } = await searchReplenishmentCatalog(q, page);
    setCatalog(data.items);
    setCatalogTotal(data.total);
    setCatalogPage(data.page);
  };

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const { data } = await getReplenishmentCapabilities();
        if (!active) return;
        setCapabilities(data);
        if (data.enabled && data.can_view_price) {
          const [projectsResponse] = await Promise.all([
            getReplenishmentProjects(),
            loadCatalog(1, ""),
            refreshList(),
          ]);
          if (!active) return;
          setProjects(projectsResponse.data.items);
        }
      } catch (error) {
        if (active) message.error(errorText(error));
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
    // Initial capability and data load only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const addDraftLine = (part: CatalogPart, quantity: number) => {
    if (draftLines.some((line) => line.part_id === part.part_id)) {
      message.info(`${part.pn_std} 已在申请中`);
      return;
    }
    setDraftLines((lines) => [
      ...lines,
      {
        part_id: part.part_id,
        pn_std: part.pn_std,
        description: part.description,
        unit: part.unit,
        quantity: Math.max(1, Math.floor(quantity)),
        special_note: "",
      },
    ]);
  };

  const updateDraftLine = (partId: number, patch: Partial<Pick<DraftLine, "quantity" | "special_note">>) => {
    setDraftLines((lines) => lines.map((line) => (
      line.part_id === partId ? { ...line, ...patch } : line
    )));
  };

  const removeDraftLine = (partId: number) => {
    setDraftLines((lines) => lines.filter((line) => line.part_id !== partId));
  };

  const submitApplication = () => {
    if (!selectedProjectId) {
      message.warning("请先选择维保项目");
      return;
    }
    if (!draftLines.length) {
      message.warning("请先加入至少一个 PN");
      return;
    }
    Modal.confirm({
      title: "提交维保补库申请？",
      content: (
        <div>
          <p>将一次性提交 <Text strong>{draftLines.length}</Text> 条明细到项目「{selectedProject?.display_name}」。</p>
          <p>提交后立即冻结三查证据与半年价格快照，不可修改；不改变库存、不自动定价。</p>
        </div>
      ),
      okText: "确认提交",
      cancelText: "继续检查",
      onOk: () => void runSubmit(),
    });
  };

  const runSubmit = async () => {
    setWorking(true);
    try {
      if (editingRevision) {
        // #10 退回编辑后重新提交：原申请新版本 + 全量行重新审核
        const { data } = await applyReplenishmentRevision(editingRevision.application_id, {
          expected_application_version: editingRevision.version,
          client_request_id: newRevisionRequestId(),
          lines: draftLines.map((line) => ({
            part_id: line.part_id,
            quantity: line.quantity,
            special_note: line.special_note?.trim() ? line.special_note.trim() : null,
          })),
        });
        setCurrent(data);
        setEditingRevision(null);
        setDraftLines([]);
        setRequestNote("");
        setSelectedProjectId("");
        await refreshList(data.application_id);
        message.success(data.idempotent ? "已返回既有申请" : `已重新提交 ${data.application_no}`);
        return;
      }
      const version = await flushCart();
      const { data } = await submitReplenishmentCartDraft(selectedProjectId, version);
      setCurrent(data);
      setDraftLines([]);
      setRequestNote("");
      setSelectedProjectId("");
      await refreshList(data.application_id);
      message.success(data.idempotent ? "已返回既有申请" : `已提交 ${data.application_no}`);
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setWorking(false);
    }
  };

  /** #10：打回申请「退回编辑」——把原申请行载入购物车编辑态（打回行标红+推荐）。 */
  const startEditingRevision = (application: ReplenishmentApplication) => {
    const version = application.versions[0];
    const lines = (version?.lines ?? []).map((line) => ({
      part_id: line.part_id,
      pn_std: line.pn_std,
      description: line.description,
      unit: line.unit,
      quantity: line.quantity,
      special_note: line.special_note ?? "",
      rejected: line.review?.decision === "rejected",
      rejectedReason: line.review?.reason ?? null,
      recommendations: line.screening?.recommendations ?? [],
    }));
    setEditingRevision(application);
    setSelectedProjectId(application.project?.project_id ?? "");
    setDraftLines(lines);
    setRequestNote(version?.request_note ?? "");
  };

  const cancelEditingRevision = () => {
    setEditingRevision(null);
    setDraftLines([]);
    setRequestNote("");
    setSelectedProjectId("");
  };

  if (loading) return <Spin fullscreen tip="正在打开补库申请…" />;
  if (!capabilities) return <Result status="error" title="无法读取补库申请状态" />;
  if (!capabilities.enabled) {
    return (
      <Result
        status="info"
        title="补库申请当前未开放"
        subTitle="服务端功能开关已关闭，历史申请仍安全保留，库存与原业务流程不受影响。"
        extra={<Button type="primary" icon={<ArrowLeftOutlined />} onClick={() => navigate(MAINTENANCE_HOME_PATH)}>返回维保主页</Button>}
      />
    );
  }
  if (!capabilities.can_view_price) {
    return (
      <Result
        status="403"
        title="补库申请暂不可用"
        subTitle="当前账号没有半年采购/销售价格事实的查看权限，请联系管理员授权。"
        extra={<Button type="primary" icon={<ArrowLeftOutlined />} onClick={() => navigate(MAINTENANCE_HOME_PATH)}>返回维保主页</Button>}
      />
    );
  }

  return (
    <div className="replenishment-beta-page">
      <PageHeader
        title="维保补库申请"
        subtitle="选择维保项目、选购 PN 后一次性提交；系统三查与半年价格证据在提交时冻结，不会自动定价或改变库存。"
        extra={(
          <Space wrap>
            <Button icon={<ReloadOutlined />} onClick={() => void refreshList()}>刷新</Button>
          </Space>
        )}
      />

      {/* ① 新建补库申请：决断层（选项目 → 组明细 → 一次性提交） */}
      <Card
        className="replenishment-cart-card"
        title={(
          <Space>
            <SendOutlined />
            {editingRevision
              ? <Text strong>编辑被打回申请：{editingRevision.application_no}</Text>
              : "新建维保补库申请"}
          </Space>
        )}
        extra={editingRevision ? (
          <Button onClick={cancelEditingRevision} disabled={working}>取消编辑</Button>
        ) : null}
      >
        <Spin spinning={working}>
          {projects.length === 0 ? (
            <Alert
              showIcon
              type="warning"
              message="当前账号没有可选的维保项目"
              description="销售经理需要先在账号中配置销售人员映射并挂维保项目；管理员可见全部活动项目。"
            />
          ) : (
            <>
              <Space direction="vertical" size={12} style={{ width: "100%" }}>
                <Space wrap>
                  <Select
                    showSearch
                    value={selectedProjectId || undefined}
                    placeholder="选择维保项目（必选）"
                    style={{ width: 360 }}
                    optionFilterProp="label"
                    options={projects.map((project) => ({
                      value: project.project_id,
                      label: `${project.project_code} · ${project.display_name}`,
                    }))}
                    onChange={setSelectedProjectId}
                    disabled={working}
                  />
                  <Input.TextArea
                    value={requestNote}
                    disabled={working}
                    maxLength={4000}
                    autoSize={{ minRows: 1, maxRows: 4 }}
                    style={{ width: 420 }}
                    placeholder="整单备注（选填）"
                    onChange={(event) => setRequestNote(event.target.value)}
                  />
                </Space>

                <div className="replenishment-cart-lines">
                  {draftLines.map((line) => (
                    <div className={`replenishment-cart-line${line.rejected ? " is-revision" : ""}`} key={line.part_id}>
                      <div className="replenishment-cart-line-top replenishment-draft-line-row">
                        <div>
                          <Text strong>{line.pn_std}</Text>
                          <div className="replenishment-part-desc">{line.description || "暂无产品描述"}</div>
                        </div>
                        <Space size={4} wrap>
                          {line.rejected && (
                            <Tag color="red">被打回：{line.rejectedReason || "无近182天采购/销售记录"}</Tag>
                          )}
                          <Text type="secondary">数量 {line.quantity} {line.unit || "件"}</Text>
                        </Space>
                      </div>
                      {line.rejected && !!line.recommendations?.length && (
                        <Space wrap size={4} style={{ margin: "4px 0" }}>
                          <Text type="secondary" style={{ fontSize: 12 }}>推荐替换（池内）：</Text>
                          {line.recommendations.slice(0, 3).map((candidate) => (
                            <Button
                              key={candidate.part_id}
                              size="small"
                              type="primary"
                              ghost
                              onClick={() => {
                                const source = draftLines.find((d) => d.part_id === line.part_id);
                                setDraftLines((lines) => {
                                  const rest = lines.filter((d) => d.part_id !== line.part_id);
                                  return [
                                    ...rest,
                                    {
                                      part_id: candidate.part_id,
                                      pn_std: candidate.pn_std,
                                      description: line.description,
                                      unit: line.unit,
                                      quantity: source?.quantity ?? 1,
                                      special_note: line.special_note,
                                    },
                                  ];
                                });
                              }}
                            >
                              替换为 {candidate.pn_std}
                            </Button>
                          ))}
                        </Space>
                      )}
                      <Row gutter={[8, 8]}>
                        <Col xs={24} sm={5}>
                          <Space.Compact block>
                            <InputNumber
                              min={1}
                              max={999999}
                              precision={0}
                              value={line.quantity}
                              disabled={working}
                              onChange={(value) => updateDraftLine(line.part_id, { quantity: Number(value || 1) })}
                              style={{ flex: 1 }}
                            />
                            <Button disabled>{line.unit || "件"}</Button>
                          </Space.Compact>
                        </Col>
                        <Col xs={24} sm={17}>
                          <Input
                            value={line.special_note}
                            maxLength={4000}
                            disabled={working}
                            placeholder="特殊情况说明（选填）"
                            onChange={(event) => updateDraftLine(line.part_id, { special_note: event.target.value })}
                          />
                        </Col>
                        <Col xs={24} sm={2} style={{ textAlign: "right" }}>
                          <Button
                            danger
                            icon={<DeleteOutlined />}
                            disabled={working}
                            onClick={() => removeDraftLine(line.part_id)}
                          >
                            移除
                          </Button>
                        </Col>
                      </Row>
                    </div>
                  ))}
                  {!draftLines.length && (
                    <Empty
                      description="还没有明细——从下方选购 PN 加入"
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                    />
                  )}
                </div>

                <Space wrap>
                  <Button
                    type="primary"
                    icon={<SendOutlined />}
                    disabled={!selectedProjectId || !draftLines.length || working}
                    title={!selectedProjectId ? "先选择维保项目" : !draftLines.length ? "先加入至少一个 PN" : undefined}
                    onClick={submitApplication}
                  >
                    提交补库申请
                  </Button>
                  {!capabilities.can_create && (
                    <Text type="secondary">当前账号无提交权限，仅可查看</Text>
                  )}
                </Space>
              </Space>
            </>
          )}
        </Spin>
      </Card>

      {/* ② 选购 PN */}
      <Card
        title={<Space><SearchOutlined />选购 PN</Space>}
        extra={<Text type="secondary">没有所属池或半年价格的 PN 仍会完整显示</Text>}
        style={{ marginTop: 16 }}
      >
        <Input.Search
          allowClear
          enterButton="搜索"
          size="large"
          value={query}
          placeholder="搜索 PN、产品描述或品牌"
          onChange={(event) => setQuery(event.target.value)}
          onSearch={() => void loadCatalog(1)}
          style={{ marginBottom: 14, maxWidth: 560 }}
        />
        <div className="replenishment-catalog-grid">
          {catalog.map((part) => (
            <CatalogCard
              key={part.part_id}
              part={part}
              disabled={!capabilities.can_create || working || !selectedProjectId}
              onAdd={(quantity) => addDraftLine(part, quantity)}
            />
          ))}
        </div>
        {!catalog.length && <Empty description="没有匹配的 PN" />}
        {catalogTotal > 20 && (
          <Pagination
            current={catalogPage}
            pageSize={20}
            total={catalogTotal}
            showSizeChanger={false}
            onChange={(page) => void loadCatalog(page)}
            style={{ marginTop: 16 }}
          />
        )}
      </Card>

      {/* ③ 最近提交 / 历史申请 */}
      <Card
        title={<Space><Badge count={applicationTotal} color="#d9d9d9" />补库申请记录</Space>}
        style={{ marginTop: 16 }}
      >
        {current ? (
          <>
            <div className="replenishment-current-application">
              <Space wrap>
                <Title level={5} copyable style={{ margin: 0 }}>{current.application_no}</Title>
                <Tag color={STATUS[current.status].color}>{STATUS[current.status].label}</Tag>
                {current.project && (
                  <Tag color="geekblue">{current.project.project_code} · {current.project.display_name}</Tag>
                )}
                <Text type="secondary">v{current.latest_version_no} · {current.owner_display_name}</Text>
              </Space>
              <Space wrap style={{ marginTop: 12 }}>
                <Button
                  type="primary"
                  icon={<DownloadOutlined />}
                  loading={exporting}
                  onClick={() => void exportCurrent()}
                >
                  导出复核包 Excel
                </Button>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  无论审核结果如何均可导出（#11）；内容为提交时冻结的 PN/数量/价格与三查证据
                </Text>
              </Space>
              {current.stage === "screening_complete" && (
                <Alert
                  showIcon
                  type="success"
                  icon={<ShoppingCartOutlined />}
                  message="已提交，系统三查与价格证据已冻结"
                  description="申请只作为记录与人工复核包，不自动审批、不改变库存；如需调整请重新提交新申请。"
                  style={{ marginTop: 12 }}
                />
              )}
              {current.versions[0]?.lines.map((line) => (
                <SubmittedLine key={line.line_id} line={line} />
              ))}
              {current.status === "needs_revision" && (
                <Alert
                  showIcon
                  type="warning"
                  message="申请被自动审核打回——可退回编辑后重新提交"
                  description="退回编辑后可添加/删减备件、更换 PN、调整数量或填写特殊情况说明，重新提交将生成新版本并重新审核。"
                  action={(
                    <Button type="primary" danger onClick={() => startEditingRevision(current)}>
                      退回编辑
                    </Button>
                  )}
                  style={{ marginTop: 12 }}
                />
              )}
              <VersionHistory versions={current.versions} />
            </div>
            <Collapse
              ghost
              style={{ marginTop: 8 }}
              items={[{
                key: "history",
                label: <Space>历史申请<Badge count={applicationTotal} color="#d9d9d9" /></Space>,
                children: (
                  <List
                    dataSource={applications}
                    locale={{ emptyText: "暂无申请" }}
                    renderItem={(item) => (
                      <List.Item
                        className={item.application_id === current.application_id ? "is-current-application" : ""}
                        onClick={() => void getReplenishmentApplication(item.application_id).then((response) => setCurrent(response.data))}
                      >
                        <List.Item.Meta
                          title={
                            <Space wrap>
                              <span>{item.application_no}</span>
                              <Badge color={STATUS[item.status].color} text={STATUS[item.status].label} />
                              {item.project && <Text type="secondary">{item.project.project_code} · {item.project.display_name}</Text>}
                            </Space>
                          }
                          description={`v${item.latest_version_no} · ${new Date(item.updated_at).toLocaleString()}`}
                        />
                      </List.Item>
                    )}
                  />
                ),
              }]}
            />
            {applicationTotal > 20 && (
              <Pagination
                current={applicationPage}
                pageSize={20}
                total={applicationTotal}
                showSizeChanger={false}
                onChange={(page) => {
                  setApplicationPage(page);
                  void refreshList(undefined, page);
                }}
                style={{ marginTop: 12, textAlign: "right" }}
              />
            )}
          </>
        ) : (
          <Empty description="还没有提交记录——填好项目与明细后提交第一份申请" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
      </Card>

      {/* 口径边界：依据层，页尾小字 */}
      <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 12 }}>
        口径边界：{capabilities.data_contract}
      </Text>
    </div>
  );
}
