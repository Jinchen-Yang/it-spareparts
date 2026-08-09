import {
  Alert,
  Button,
  Card,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  List,
  Modal,
  Progress,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from "antd";
import { useEffect, useRef, useState } from "react";

import {
  confirmMaintenanceBadReturnWarehouse,
  createMaintenanceBadReturnDraft,
  listMaintenanceReturnCategories,
  markMaintenanceBadReturnInTransit,
  resolveMaintenanceReturnObligationCategory,
  searchMaintenanceBadReturns,
  searchMaintenanceReturnObligations,
  submitMaintenanceBadReturn,
  voidMaintenanceBadReturn,
  type MaintenanceBadReturn,
  type MaintenanceReturnCategory,
  type MaintenanceReturnObligation,
  type MaintenanceReturnRate,
} from "../../api/maintenanceOperations";

const { Text } = Typography;

const quantity = (value: string | null | undefined) => value ?? "—";

const commandKey = (prefix: string) => {
  const nativeUuid = globalThis.crypto?.randomUUID?.();
  return `${prefix}-${nativeUuid ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
};

const classificationTag = (row: MaintenanceReturnObligation) => {
  if (row.classification === "exempt") return <Tag color="green">硬盘免返</Tag>;
  if (row.classification === "pending_category") return <Tag color="orange">品类待判定</Tag>;
  return <Tag color="blue">明确应返</Tag>;
};

const returnStatusLabel: Record<string, { label: string; color?: string }> = {
  draft: { label: "草稿", color: "blue" },
  submitted: { label: "已登记", color: "gold" },
  in_transit: { label: "在途", color: "purple" },
  warehouse_confirmed: { label: "仓库已确认", color: "green" },
  void: { label: "已作废", color: "default" },
};

export default function BadReturnPanel({
  projectId,
  returnRate,
  canManage,
  onChanged,
}: {
  projectId: string;
  returnRate: MaintenanceReturnRate | null | undefined;
  canManage: boolean;
  onChanged: () => void;
}) {
  const [obligations, setObligations] = useState<MaintenanceReturnObligation[]>([]);
  const [returns, setReturns] = useState<MaintenanceBadReturn[]>([]);
  const [obligationPage, setObligationPage] = useState(1);
  const [obligationPageSize, setObligationPageSize] = useState(50);
  const [obligationTotal, setObligationTotal] = useState(0);
  const [returnPage, setReturnPage] = useState(1);
  const [returnPageSize, setReturnPageSize] = useState(20);
  const [returnTotal, setReturnTotal] = useState(0);
  const [obligationsLoadingMore, setObligationsLoadingMore] = useState(false);
  const [returnsLoadingMore, setReturnsLoadingMore] = useState(false);
  const [loadedRate, setLoadedRate] = useState<MaintenanceReturnRate | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const generation = useRef(0);
  const factsGeneration = useRef(0);
  const activeProject = useRef(projectId);
  const [draftOpen, setDraftOpen] = useState(false);
  const [replacementFor, setReplacementFor] = useState<MaintenanceBadReturn | null>(null);
  const [selected, setSelected] = useState<Record<string, number>>({});
  const [draftNote, setDraftNote] = useState("");
  const [draftReason, setDraftReason] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  const [draftSaving, setDraftSaving] = useState(false);
  const [submitTarget, setSubmitTarget] = useState<MaintenanceBadReturn | null>(null);
  const [submitReason, setSubmitReason] = useState("");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitSaving, setSubmitSaving] = useState(false);
  const [transitTarget, setTransitTarget] = useState<MaintenanceBadReturn | null>(null);
  const [logisticsReference, setLogisticsReference] = useState("");
  const [transitReason, setTransitReason] = useState("");
  const [transitError, setTransitError] = useState<string | null>(null);
  const [transitSaving, setTransitSaving] = useState(false);
  const [warehouseTarget, setWarehouseTarget] = useState<MaintenanceBadReturn | null>(null);
  const [warehouseReference, setWarehouseReference] = useState("");
  const [inboundReference, setInboundReference] = useState("");
  const [warehouseReason, setWarehouseReason] = useState("");
  const [warehouseError, setWarehouseError] = useState<string | null>(null);
  const [warehouseSaving, setWarehouseSaving] = useState(false);
  const [voidTarget, setVoidTarget] = useState<MaintenanceBadReturn | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voidError, setVoidError] = useState<string | null>(null);
  const [voidSaving, setVoidSaving] = useState(false);
  const [categoryTarget, setCategoryTarget] = useState<MaintenanceReturnObligation | null>(null);
  const [returnCategories, setReturnCategories] = useState<MaintenanceReturnCategory[]>([]);
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [categoryReason, setCategoryReason] = useState("");
  const [categoryError, setCategoryError] = useState<string | null>(null);
  const [categoryLoading, setCategoryLoading] = useState(false);
  const [categorySaving, setCategorySaving] = useState(false);
  const [canResolveCategory] = useState(() => localStorage.getItem("role") === "admin");
  const draftCommandKey = useRef<string | null>(null);
  const submitCommandKey = useRef<string | null>(null);
  const transitCommandKey = useRef<string | null>(null);
  const warehouseCommandKey = useRef<string | null>(null);
  const voidCommandKey = useRef<string | null>(null);
  const categoryCommandKey = useRef<string | null>(null);
  const categoryLoadGeneration = useRef(0);

  activeProject.current = projectId;

  const load = async () => {
    const request = ++generation.current;
    const requestedProject = projectId;
    setLoading(true);
    setError(false);
    try {
      const [obligationResponse, returnResponse] = await Promise.all([
        searchMaintenanceReturnObligations({
          project_id: requestedProject,
          page: 1,
          page_size: 50,
        }),
        searchMaintenanceBadReturns({
          project_id: requestedProject,
          page: 1,
          page_size: 20,
        }),
      ]);
      if (request !== generation.current) return;
      setObligations(obligationResponse.data.rows ?? []);
      setReturns(returnResponse.data.rows ?? []);
      setObligationPage(obligationResponse.data.page);
      setObligationPageSize(obligationResponse.data.page_size);
      setObligationTotal(obligationResponse.data.total);
      setReturnPage(returnResponse.data.page);
      setReturnPageSize(returnResponse.data.page_size);
      setReturnTotal(returnResponse.data.total);
      setLoadedRate(obligationResponse.data.return_rate ?? null);
    } catch {
      if (request !== generation.current) return;
      setObligations([]);
      setReturns([]);
      setObligationPage(1);
      setObligationTotal(0);
      setReturnPage(1);
      setReturnTotal(0);
      setLoadedRate(null);
      setError(true);
    } finally {
      if (request === generation.current) setLoading(false);
    }
  };

  const refreshObligationFacts = async () => {
    const request = ++factsGeneration.current;
    const requestedProject = projectId;
    try {
      const { data } = await searchMaintenanceReturnObligations({
        project_id: requestedProject,
        page: 1,
        page_size: 50,
      });
      if (
        request !== factsGeneration.current
        || activeProject.current !== requestedProject
      ) return;
      setObligations(data.rows ?? []);
      setObligationPage(data.page);
      setObligationPageSize(data.page_size);
      setObligationTotal(data.total);
      setLoadedRate(data.return_rate ?? null);
    } catch {
      if (
        request === factsGeneration.current
        && activeProject.current === requestedProject
      ) message.warning("返还数量刷新失败，请重试");
    }
  };

  useEffect(() => {
    setObligations([]);
    setReturns([]);
    setLoadedRate(null);
    setDraftOpen(false);
    setReplacementFor(null);
    setDraftReason("");
    setDraftError(null);
    setDraftSaving(false);
    setSubmitTarget(null);
    setSubmitReason("");
    setSubmitError(null);
    setSubmitSaving(false);
    setTransitTarget(null);
    setTransitReason("");
    setTransitError(null);
    setTransitSaving(false);
    setWarehouseTarget(null);
    setWarehouseReason("");
    setWarehouseError(null);
    setWarehouseSaving(false);
    setVoidTarget(null);
    setVoidReason("");
    setVoidError(null);
    setVoidSaving(false);
    setCategoryTarget(null);
    setReturnCategories([]);
    setCategoryId(null);
    setCategoryReason("");
    setCategoryError(null);
    setCategoryLoading(false);
    setCategorySaving(false);
    draftCommandKey.current = null;
    submitCommandKey.current = null;
    transitCommandKey.current = null;
    warehouseCommandKey.current = null;
    voidCommandKey.current = null;
    categoryCommandKey.current = null;
    categoryLoadGeneration.current += 1;
    setSelected({});
    setObligationPage(1);
    setObligationPageSize(50);
    setObligationTotal(0);
    setReturnPage(1);
    setReturnPageSize(20);
    setReturnTotal(0);
    setObligationsLoadingMore(false);
    setReturnsLoadingMore(false);
    factsGeneration.current += 1;
    void load();
    return () => {
      generation.current += 1;
      factsGeneration.current += 1;
    };
    // projectId is the complete loading identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const rate = loadedRate ?? returnRate;
  const hasMoreObligations = obligationPage * obligationPageSize < obligationTotal;
  const hasMoreReturns = returnPage * returnPageSize < returnTotal;
  const eligibleObligations = obligations.filter((row) => (
    row.classification === "required"
    && row.is_active
    && Number(row.remaining_quantity) > 0
  ));

  const openCategoryResolution = async (row: MaintenanceReturnObligation) => {
    if (!canResolveCategory || row.classification !== "pending_category") return;
    const request = ++categoryLoadGeneration.current;
    const requestedProject = projectId;
    setCategoryTarget(row);
    setReturnCategories([]);
    setCategoryId(null);
    setCategoryReason("");
    setCategoryError(null);
    setCategoryLoading(true);
    categoryCommandKey.current = commandKey("bad-return-category");
    try {
      const { data } = await listMaintenanceReturnCategories();
      if (
        request !== categoryLoadGeneration.current
        || activeProject.current !== requestedProject
      ) return;
      setReturnCategories(data.categories ?? []);
    } catch {
      if (
        request === categoryLoadGeneration.current
        && activeProject.current === requestedProject
      ) setCategoryError("标准品类加载失败，请重试");
    } finally {
      if (
        request === categoryLoadGeneration.current
        && activeProject.current === requestedProject
      ) setCategoryLoading(false);
    }
  };

  const closeCategoryResolution = () => {
    if (categorySaving) return;
    categoryLoadGeneration.current += 1;
    setCategoryTarget(null);
    categoryCommandKey.current = null;
  };

  const resolveCategory = async () => {
    const cleanReason = categoryReason.trim();
    const idempotencyKey = categoryCommandKey.current;
    if (!canResolveCategory || !categoryTarget || categoryId == null
      || !cleanReason || !idempotencyKey) return;
    const requestedProject = projectId;
    setCategorySaving(true);
    setCategoryError(null);
    try {
      const { data } = await resolveMaintenanceReturnObligationCategory(
        categoryTarget.obligation_id,
        {
          project_id: requestedProject,
          version: categoryTarget.version,
          category_id: categoryId,
          idempotency_key: idempotencyKey,
          reason: cleanReason,
        },
      );
      if (activeProject.current !== requestedProject) return;
      setObligations((current) => current.map((row) => (
        row.obligation_id === data.obligation_id ? data : row
      )));
      setCategoryTarget(null);
      categoryCommandKey.current = null;
      message.success("标准品类已关联，返还口径已重新判定");
      void refreshObligationFacts();
      onChanged();
    } catch {
      if (activeProject.current === requestedProject) {
        setCategoryError("品类处理失败，义务版本或标准品类可能已变化，请刷新后重试");
      }
    } finally {
      if (activeProject.current === requestedProject) setCategorySaving(false);
    }
  };

  const openDraft = (source: MaintenanceBadReturn | null = null) => {
    const sourceSelection = source == null ? {} : Object.fromEntries(
      source.lines.flatMap((line) => {
        const obligation = eligibleObligations.find(
          (row) => row.obligation_id === line.obligation_id,
        );
        if (!obligation) return [];
        const maximum = Number(obligation.remaining_quantity);
        return maximum > 0
          ? [[line.obligation_id, Math.min(Number(line.quantity), maximum)]]
          : [];
      }),
    );
    setSelected(sourceSelection);
    setDraftNote(source == null ? "" : `替代已作废返还单 ${source.return_no}`);
    setDraftReason("");
    setDraftError(null);
    setReplacementFor(source);
    draftCommandKey.current = commandKey("bad-return-draft");
    setDraftOpen(true);
  };

  const closeDraft = () => {
    if (draftSaving) return;
    setDraftOpen(false);
    setReplacementFor(null);
    draftCommandKey.current = null;
  };

  const saveDraft = async () => {
    const lines = Object.entries(selected)
      .filter(([, value]) => Number.isFinite(value) && value > 0)
      .map(([obligation_id, value]) => ({ obligation_id, quantity: value }));
    const cleanReason = draftReason.trim();
    const idempotencyKey = draftCommandKey.current;
    if (lines.length === 0 || !cleanReason || !idempotencyKey) {
      setDraftError("请至少选择一条正数量应返义务，并填写建立草稿原因");
      return;
    }
    const requestedProject = projectId;
    setDraftSaving(true);
    setDraftError(null);
    try {
      const { data } = await createMaintenanceBadReturnDraft({
        project_id: requestedProject,
        idempotency_key: idempotencyKey,
        ...(replacementFor ? { replaces_return_id: replacementFor.return_id } : {}),
        lines,
        ...(draftNote.trim() ? { note: draftNote.trim() } : {}),
        reason: cleanReason,
      });
      if (activeProject.current !== requestedProject) return;
      setReturns((current) => [
        data,
        ...current.filter((item) => item.return_id !== data.return_id),
      ]);
      setReturnTotal((current) => current + 1);
      setDraftOpen(false);
      setReplacementFor(null);
      draftCommandKey.current = null;
      message.success("坏件返还草稿已保存");
      onChanged();
    } catch {
      if (activeProject.current === requestedProject) {
        setDraftError("坏件返还草稿保存失败，义务余额可能已变化，请刷新后重试");
      }
    } finally {
      if (activeProject.current === requestedProject) setDraftSaving(false);
    }
  };

  const openSubmit = (item: MaintenanceBadReturn) => {
    setSubmitTarget(item);
    setSubmitReason("");
    setSubmitError(null);
    submitCommandKey.current = commandKey("bad-return-submit");
  };

  const submitReturn = async () => {
    const cleanReason = submitReason.trim();
    const idempotencyKey = submitCommandKey.current;
    if (!submitTarget || !cleanReason || !idempotencyKey) return;
    const requestedProject = projectId;
    setSubmitSaving(true);
    setSubmitError(null);
    try {
      const { data } = await submitMaintenanceBadReturn(submitTarget.return_id, {
        project_id: requestedProject,
        version: submitTarget.version,
        idempotency_key: idempotencyKey,
        reason: cleanReason,
      });
      if (activeProject.current !== requestedProject) return;
      setReturns((current) => current.map((item) => (
        item.return_id === data.return_id ? data : item
      )));
      setSubmitTarget(null);
      submitCommandKey.current = null;
      message.success("坏件返还已提交登记");
      void refreshObligationFacts();
      onChanged();
    } catch {
      if (activeProject.current === requestedProject) {
        setSubmitError("提交失败，单据版本或返还义务余额可能已变化，请刷新后重试");
      }
    } finally {
      if (activeProject.current === requestedProject) setSubmitSaving(false);
    }
  };

  const openTransit = (item: MaintenanceBadReturn) => {
    setTransitTarget(item);
    setLogisticsReference("");
    setTransitReason("");
    setTransitError(null);
    transitCommandKey.current = commandKey("bad-return-transit");
  };

  const markInTransit = async () => {
    const cleanReference = logisticsReference.trim();
    const cleanReason = transitReason.trim();
    const idempotencyKey = transitCommandKey.current;
    if (!transitTarget || !cleanReference || !cleanReason || !idempotencyKey) return;
    const requestedProject = projectId;
    setTransitSaving(true);
    setTransitError(null);
    try {
      const { data } = await markMaintenanceBadReturnInTransit(transitTarget.return_id, {
        project_id: requestedProject,
        version: transitTarget.version,
        idempotency_key: idempotencyKey,
        logistics_reference: cleanReference,
        reason: cleanReason,
      });
      if (activeProject.current !== requestedProject) return;
      setReturns((current) => current.map((item) => (
        item.return_id === data.return_id ? data : item
      )));
      setTransitTarget(null);
      transitCommandKey.current = null;
      message.success("坏件返还已标记在途");
      onChanged();
    } catch {
      if (activeProject.current === requestedProject) {
        setTransitError("在途登记失败，单据版本可能已变化，请刷新后重试");
      }
    } finally {
      if (activeProject.current === requestedProject) setTransitSaving(false);
    }
  };

  const openWarehouseConfirm = (item: MaintenanceBadReturn) => {
    setWarehouseTarget(item);
    setWarehouseReference("");
    setInboundReference("");
    setWarehouseReason("");
    setWarehouseError(null);
    warehouseCommandKey.current = commandKey("bad-return-warehouse-confirm");
  };

  const confirmWarehouse = async () => {
    const cleanWarehouseReference = warehouseReference.trim();
    const cleanInboundReference = inboundReference.trim();
    const cleanReason = warehouseReason.trim();
    const idempotencyKey = warehouseCommandKey.current;
    if (!warehouseTarget || !cleanWarehouseReference || !cleanReason || !idempotencyKey) return;
    const requestedProject = projectId;
    setWarehouseSaving(true);
    setWarehouseError(null);
    try {
      const { data } = await confirmMaintenanceBadReturnWarehouse(
        warehouseTarget.return_id,
        {
          project_id: requestedProject,
          version: warehouseTarget.version,
          idempotency_key: idempotencyKey,
          warehouse_reference: cleanWarehouseReference,
          ...(cleanInboundReference ? { inbound_reference: cleanInboundReference } : {}),
          reason: cleanReason,
        },
      );
      if (activeProject.current !== requestedProject) return;
      setReturns((current) => current.map((item) => (
        item.return_id === data.return_id ? data : item
      )));
      setWarehouseTarget(null);
      warehouseCommandKey.current = null;
      message.success("仓库已确认坏件返还");
      void refreshObligationFacts();
      onChanged();
    } catch {
      if (activeProject.current === requestedProject) {
        setWarehouseError("仓库确认失败，单据版本或确认数量可能已变化，请刷新后重试");
      }
    } finally {
      if (activeProject.current === requestedProject) setWarehouseSaving(false);
    }
  };

  const openVoid = (item: MaintenanceBadReturn) => {
    setVoidTarget(item);
    setVoidReason("");
    setVoidError(null);
    voidCommandKey.current = commandKey("bad-return-void");
  };

  const voidReturn = async () => {
    const cleanReason = voidReason.trim();
    const idempotencyKey = voidCommandKey.current;
    if (!voidTarget || !cleanReason || !idempotencyKey) return;
    const requestedProject = projectId;
    setVoidSaving(true);
    setVoidError(null);
    try {
      const { data } = await voidMaintenanceBadReturn(voidTarget.return_id, {
        project_id: requestedProject,
        version: voidTarget.version,
        idempotency_key: idempotencyKey,
        reason: cleanReason,
      });
      if (activeProject.current !== requestedProject) return;
      setReturns((current) => current.map((item) => (
        item.return_id === data.return_id ? data : item
      )));
      await refreshObligationFacts();
      if (activeProject.current !== requestedProject) return;
      setVoidTarget(null);
      voidCommandKey.current = null;
      message.success("坏件返还单已追加式作废");
      onChanged();
    } catch {
      if (activeProject.current === requestedProject) {
        setVoidError("作废失败；如已关联正式入库或版本已变化，请刷新后核对");
      }
    } finally {
      if (activeProject.current === requestedProject) setVoidSaving(false);
    }
  };

  const loadMoreObligations = async () => {
    if (!hasMoreObligations || obligationsLoadingMore) return;
    const requestedProject = projectId;
    const request = generation.current;
    setObligationsLoadingMore(true);
    try {
      const { data } = await searchMaintenanceReturnObligations({
        project_id: requestedProject,
        page: obligationPage + 1,
        page_size: obligationPageSize,
      });
      if (request !== generation.current || activeProject.current !== requestedProject) return;
      setObligations((current) => {
        const rows = new Map(current.map((row) => [row.obligation_id, row]));
        for (const row of data.rows ?? []) rows.set(row.obligation_id, row);
        return [...rows.values()];
      });
      setObligationPage(data.page);
      setObligationPageSize(data.page_size);
      setObligationTotal(data.total);
      setLoadedRate(data.return_rate ?? null);
    } catch {
      if (request === generation.current && activeProject.current === requestedProject) {
        message.error("更多返还义务加载失败，请重试");
      }
    } finally {
      if (request === generation.current && activeProject.current === requestedProject) {
        setObligationsLoadingMore(false);
      }
    }
  };

  const loadMoreReturns = async () => {
    if (!hasMoreReturns || returnsLoadingMore) return;
    const requestedProject = projectId;
    const request = generation.current;
    setReturnsLoadingMore(true);
    try {
      const { data } = await searchMaintenanceBadReturns({
        project_id: requestedProject,
        page: returnPage + 1,
        page_size: returnPageSize,
      });
      if (request !== generation.current || activeProject.current !== requestedProject) return;
      setReturns((current) => {
        const rows = new Map(current.map((row) => [row.return_id, row]));
        for (const row of data.rows ?? []) rows.set(row.return_id, row);
        return [...rows.values()];
      });
      setReturnPage(data.page);
      setReturnPageSize(data.page_size);
      setReturnTotal(data.total);
    } catch {
      if (request === generation.current && activeProject.current === requestedProject) {
        message.error("更多坏件返还单加载失败，请重试");
      }
    } finally {
      if (request === generation.current && activeProject.current === requestedProject) {
        setReturnsLoadingMore(false);
      }
    }
  };

  return (
    <Card
      data-testid="bad-return-panel"
      title="坏件返还"
      extra={canManage ? (
        <Button
          type="primary"
          disabled={eligibleObligations.length === 0}
          onClick={() => openDraft()}
        >
          新建坏件返还单
        </Button>
      ) : null}
    >
      <Space direction="vertical" size={14} style={{ width: "100%" }}>
        <Alert
          type="info"
          showIcon
          message="返还登记不冲减项目成本，也不直接增加库存"
          description="官方返还率只按仓库确认量统计；正式库存变化由后续稳定入库事实负责。"
        />
        {rate && (
          <>
            <div className="bad-return-metric-grid">
              <Text data-testid="return-required">应返<strong>{quantity(rate.required_quantity)}</strong></Text>
              <Text data-testid="return-registered">已登记<strong>{quantity(rate.registered_quantity)}</strong></Text>
              <Text data-testid="return-confirmed">仓库确认<strong>{quantity(rate.warehouse_confirmed_quantity)}</strong></Text>
              <Text data-testid="return-outstanding">
                待仓库确认<strong>{quantity(rate.outstanding_quantity)}</strong>
              </Text>
              <Text data-testid="return-exempt">硬盘免返<strong>{quantity(rate.exempt_quantity)}</strong></Text>
              <Text data-testid="return-pending">品类待判定<strong>{quantity(rate.pending_quantity)}</strong></Text>
            </div>
            {rate.status === "available" && rate.official_rate_pct != null ? (
              <div>
                <Text strong>仓库确认返还率</Text>
                <Progress percent={Number(rate.official_rate_pct)} />
              </div>
            ) : rate.status === "basis_incomplete" ? (
              <Alert
                type="warning"
                showIcon
                message="返还率暂不可判定"
                description="存在品类待判定明细，系统不会输出看似精确的百分比。"
              />
            ) : (
              <Alert type="success" showIcon message="无应返项" description="分母为 0，返还率不适用。" />
            )}
          </>
        )}
        {error && (
          <Alert
            type="error"
            showIcon
            message="坏件返还信息加载失败"
            action={(
              <Button aria-label="重试" size="small" danger onClick={() => void load()}>
                重试
              </Button>
            )}
          />
        )}
        {loading && obligations.length === 0 ? (
          <div style={{ padding: 24, textAlign: "center" }}><Spin /></div>
        ) : obligations.length === 0 && !error ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无返还义务" />
        ) : (
          <List
            dataSource={obligations}
            renderItem={(row) => (
              <List.Item key={row.obligation_id}>
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  <Space wrap style={{ width: "100%", justifyContent: "space-between" }}>
                    <Space wrap>
                      <Text strong>{row.pn}</Text>
                      <Text type="secondary">来源 {row.issue_no}</Text>
                      {classificationTag(row)}
                    </Space>
                    <Text type="secondary">
                      应返 {row.required_quantity} · 已登记 {row.registered_quantity}
                      {" · "}仓库确认 {row.warehouse_confirmed_quantity}
                      {" · "}未登记 {row.remaining_quantity}
                    </Text>
                  </Space>
                  {row.classification === "pending_category" && (
                    <Space wrap style={{ justifyContent: "space-between" }}>
                      <Text type="warning">
                        当前不计入返还率分母，管理员关联标准品类后才能判定是否应返。
                      </Text>
                      {canResolveCategory && (
                        <Button size="small" onClick={() => void openCategoryResolution(row)}>
                          处理品类
                        </Button>
                      )}
                    </Space>
                  )}
                </Space>
              </List.Item>
            )}
          />
        )}
        {hasMoreObligations && !error && (
          <Button
            block
            loading={obligationsLoadingMore}
            onClick={() => void loadMoreObligations()}
          >
            加载更多返还义务
          </Button>
        )}
        {returns.length > 0 && (
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Text strong>坏件返还单</Text>
            <List
              grid={{ gutter: 12, xs: 1, md: 2, xl: 3 }}
              dataSource={returns}
              renderItem={(item) => {
                const state = returnStatusLabel[item.status] ?? { label: item.status };
                const replacement = returns.find(
                  (candidate) => candidate.replaces_return_id === item.return_id,
                );
                const canVoid = item.status !== "void"
                  && (item.status !== "warehouse_confirmed" || item.inbound_reference == null);
                return (
                  <List.Item key={item.return_id}>
                    <Card size="small" data-testid={`bad-return-card-${item.return_id}`}>
                      <Space direction="vertical" size={6} style={{ width: "100%" }}>
                        <Space wrap>
                          <Text strong>{item.return_no}</Text>
                          <Tag color={state.color}>{state.label}</Tag>
                        </Space>
                        <Text type="secondary">{item.lines.length} 行 · 登记不影响库存与成本</Text>
                        {item.logistics_reference && (
                          <Text type="secondary">物流参考：{item.logistics_reference}</Text>
                        )}
                        {item.warehouse_reference && (
                          <Text type="secondary">仓库参考：{item.warehouse_reference}</Text>
                        )}
                        {item.inbound_reference && (
                          <Text type="secondary">正式入库引用：{item.inbound_reference}</Text>
                        )}
                        {item.replaces_return_id && (
                          <Text type="secondary">替代原返还单：{item.replaces_return_id}</Text>
                        )}
                        {replacement && (
                          <Text type="secondary">替代单：{replacement.return_no}</Text>
                        )}
                        {canManage && item.status === "draft" && (
                          <Space wrap>
                            <Button size="small" type="primary" onClick={() => openSubmit(item)}>
                              提交返还单
                            </Button>
                            <Button size="small" danger onClick={() => openVoid(item)}>
                              作废返还单
                            </Button>
                          </Space>
                        )}
                        {canManage && item.status === "submitted" && (
                          <Space wrap>
                            <Button size="small" type="primary" onClick={() => openTransit(item)}>
                              标记在途
                            </Button>
                            <Button size="small" onClick={() => openWarehouseConfirm(item)}>
                              仓库确认
                            </Button>
                            <Button size="small" danger onClick={() => openVoid(item)}>
                              作废返还单
                            </Button>
                          </Space>
                        )}
                        {canManage && item.status === "in_transit" && (
                          <Space wrap>
                            <Button size="small" type="primary" onClick={() => openWarehouseConfirm(item)}>
                              仓库确认
                            </Button>
                            <Button size="small" danger onClick={() => openVoid(item)}>
                              作废返还单
                            </Button>
                          </Space>
                        )}
                        {canManage && item.status === "warehouse_confirmed" && canVoid && (
                          <Button size="small" danger onClick={() => openVoid(item)}>
                            作废返还单
                          </Button>
                        )}
                        {item.status === "warehouse_confirmed" && !canVoid && (
                          <Text type="secondary">已关联正式入库，不可直接作废</Text>
                        )}
                        {canManage && item.status === "void" && !replacement && (
                          <Button size="small" type="primary" onClick={() => openDraft(item)}>
                            建立替代单
                          </Button>
                        )}
                      </Space>
                    </Card>
                  </List.Item>
                );
              }}
            />
            {hasMoreReturns && (
              <Button
                block
                loading={returnsLoadingMore}
                onClick={() => void loadMoreReturns()}
              >
                加载更多返还单
              </Button>
            )}
          </Space>
        )}
      </Space>

      <Modal
        title="处理品类待判定"
        open={categoryTarget != null && canResolveCategory}
        okText="确认关联"
        cancelText="取消"
        confirmLoading={categorySaving}
        okButtonProps={{
          disabled: categoryLoading || categoryId == null || !categoryReason.trim(),
        }}
        onOk={() => void resolveCategory()}
        onCancel={closeCategoryResolution}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="warning"
            showIcon
            message="仅管理员可关联标准品类"
            description="系统只按标准一级品类精确判定：硬盘免返，其他品类明确应返；选择和原因会保留审计。"
          />
          {categoryTarget && (
            <Text>待处理：{categoryTarget.pn} · 来源 {categoryTarget.issue_no}</Text>
          )}
          {categoryError && <Alert type="error" showIcon message={categoryError} />}
          <label>
            标准品类
            <Select
              aria-label="标准品类"
              showSearch
              loading={categoryLoading}
              value={categoryId ?? undefined}
              placeholder="选择标准品类"
              optionFilterProp="label"
              options={returnCategories.map((category) => ({
                value: category.category_id,
                label: category.category_minor
                  ? `${category.category_major} / ${category.category_minor}`
                  : category.category_major,
              }))}
              onChange={(value) => setCategoryId(value)}
              style={{ width: "100%" }}
            />
          </label>
          <label>
            判定原因
            <Input.TextArea
              aria-label="判定原因"
              value={categoryReason}
              maxLength={1000}
              rows={3}
              onChange={(event) => setCategoryReason(event.target.value)}
            />
          </label>
        </Space>
      </Modal>

      <Modal
        title={replacementFor ? "建立替代坏件返还草稿" : "新建坏件返还草稿"}
        open={draftOpen}
        width={760}
        okText="保存草稿"
        cancelText="取消"
        confirmLoading={draftSaving}
        onOk={() => void saveDraft()}
        onCancel={closeDraft}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message={replacementFor
              ? `替代已作废返还单 ${replacementFor.return_no}`
              : "仅可选择明确应返且仍有余额的义务"}
            description="系统生成返还单号和行号；替代关系会保留审计，登记不会直接增加库存或冲减成本。"
          />
          {draftError && <Alert type="error" showIcon message={draftError} />}
          {eligibleObligations.map((row) => {
            const checked = Object.prototype.hasOwnProperty.call(selected, row.obligation_id);
            const maximum = Number(row.remaining_quantity);
            return (
              <Card key={row.obligation_id} size="small">
                <Space direction="vertical" size={8} style={{ width: "100%" }}>
                  <Checkbox
                    checked={checked}
                    aria-label={row.pn}
                    onChange={(event) => setSelected((current) => {
                      const next = { ...current };
                      if (event.target.checked) next[row.obligation_id] = Math.min(1, maximum);
                      else delete next[row.obligation_id];
                      return next;
                    })}
                  >
                    <Text strong>{row.pn}</Text>
                  </Checkbox>
                  <Text type="secondary">来源 {row.issue_no} · 未登记 {row.remaining_quantity}</Text>
                  {checked && (
                    <label>
                      返还数量
                      <InputNumber
                        aria-label={`${row.pn} 返还数量`}
                        min={0.001}
                        max={maximum}
                        precision={3}
                        value={selected[row.obligation_id]}
                        onChange={(value) => setSelected((current) => ({
                          ...current,
                          [row.obligation_id]: Number(value ?? 0),
                        }))}
                        style={{ width: "100%" }}
                      />
                    </label>
                  )}
                </Space>
              </Card>
            );
          })}
          <label>
            返还单备注
            <Input.TextArea
              aria-label="返还单备注"
              value={draftNote}
              maxLength={1000}
              rows={2}
              onChange={(event) => setDraftNote(event.target.value)}
            />
          </label>
          <label>
            建立草稿原因
            <Input
              aria-label="建立草稿原因"
              value={draftReason}
              maxLength={1000}
              onChange={(event) => setDraftReason(event.target.value)}
            />
          </label>
        </Space>
      </Modal>

      <Modal
        title="提交坏件返还单"
        open={submitTarget != null}
        okText="确认提交"
        cancelText="取消"
        confirmLoading={submitSaving}
        okButtonProps={{ disabled: !submitReason.trim() }}
        onOk={() => void submitReturn()}
        onCancel={() => {
          if (!submitSaving) {
            setSubmitTarget(null);
            submitCommandKey.current = null;
          }
        }}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert type="info" showIcon message="提交后计入已登记数量，但仍不代表仓库已确认。" />
          {submitError && <Alert type="error" showIcon message={submitError} />}
          <label>
            操作原因
            <Input.TextArea
              aria-label="操作原因"
              value={submitReason}
              maxLength={1000}
              rows={3}
              onChange={(event) => setSubmitReason(event.target.value)}
            />
          </label>
        </Space>
      </Modal>

      <Modal
        title="标记坏件返还在途"
        open={transitTarget != null}
        okText="确认在途"
        cancelText="取消"
        confirmLoading={transitSaving}
        okButtonProps={{ disabled: !logisticsReference.trim() || !transitReason.trim() }}
        onOk={() => void markInTransit()}
        onCancel={() => {
          if (!transitSaving) {
            setTransitTarget(null);
            transitCommandKey.current = null;
          }
        }}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message="物流参考由人工填写"
            description="系统不假设已接入真实物流，也不会把在途状态算作仓库确认。"
          />
          {transitError && <Alert type="error" showIcon message={transitError} />}
          <label>
            物流参考
            <Input
              aria-label="物流参考"
              value={logisticsReference}
              maxLength={128}
              onChange={(event) => setLogisticsReference(event.target.value)}
            />
          </label>
          <label>
            操作原因
            <Input.TextArea
              aria-label="操作原因"
              value={transitReason}
              maxLength={1000}
              rows={3}
              onChange={(event) => setTransitReason(event.target.value)}
            />
          </label>
        </Space>
      </Modal>

      <Modal
        title="仓库确认坏件返还"
        open={warehouseTarget != null}
        okText="确认仓库收件"
        cancelText="取消"
        confirmLoading={warehouseSaving}
        okButtonProps={{ disabled: !warehouseReference.trim() || !warehouseReason.trim() }}
        onOk={() => void confirmWarehouse()}
        onCancel={() => {
          if (!warehouseSaving) {
            setWarehouseTarget(null);
            warehouseCommandKey.current = null;
          }
        }}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message="仓库确认才进入官方返还率分子"
            description="外部入库引用可以留空；即使填写，本模块也只建立稳定关联，不直接写库存。"
          />
          {warehouseError && <Alert type="error" showIcon message={warehouseError} />}
          <label>
            仓库确认参考
            <Input
              aria-label="仓库确认参考"
              value={warehouseReference}
              maxLength={128}
              onChange={(event) => setWarehouseReference(event.target.value)}
            />
          </label>
          <label>
            外部入库稳定引用（可选）
            <Input
              aria-label="外部入库稳定引用（可选）"
              value={inboundReference}
              maxLength={128}
              onChange={(event) => setInboundReference(event.target.value)}
            />
          </label>
          <label>
            操作原因
            <Input.TextArea
              aria-label="操作原因"
              value={warehouseReason}
              maxLength={1000}
              rows={3}
              onChange={(event) => setWarehouseReason(event.target.value)}
            />
          </label>
        </Space>
      </Modal>

      <Modal
        title="作废坏件返还单"
        open={voidTarget != null}
        okText="确认追加式作废"
        cancelText="取消"
        confirmLoading={voidSaving}
        okButtonProps={{ danger: true, disabled: !voidReason.trim() }}
        onOk={() => void voidReturn()}
        onCancel={() => {
          if (!voidSaving) {
            setVoidTarget(null);
            voidCommandKey.current = null;
          }
        }}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="warning"
            showIcon
            message="原单和操作审计都会保留"
            description="作废单不再计入已登记或仓库确认数量；需要更正时，请在作废后建立替代单。"
          />
          {voidTarget && (
            <Text>待作废：{voidTarget.return_no}（{voidTarget.lines.length} 行）</Text>
          )}
          {voidError && <Alert type="error" showIcon message={voidError} />}
          <label>
            作废原因
            <Input.TextArea
              aria-label="作废原因"
              value={voidReason}
              maxLength={1000}
              rows={3}
              onChange={(event) => setVoidReason(event.target.value)}
            />
          </label>
        </Space>
      </Modal>
    </Card>
  );
}
