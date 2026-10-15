import { useCallback, useEffect, useRef, useState } from "react";
import ReturnStatusColumn from "../../components/maintenance/ReturnStatusColumn";
import {
  Alert,
  Button,
  Card,
  Input,
  Modal,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  Upload,
  message,
} from "antd";
import { UploadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import type {
  MaintenanceDemandSummary,
  MaintenanceDemandSearchRow,
} from "../../api/maintenanceDemands";
import {
  restoreMaintenanceDemand,
  searchMaintenanceDemands,
  voidFastMaintenanceDemands,
} from "../../api/maintenanceDemands";
import type { WbddImportReport, WbddMissing } from "../../api/maintenanceWbddImport";
import { getWbddMissing, uploadWbdd } from "../../api/maintenanceWbddImport";
import { readPermissionMap } from "../../nav";
import OrderContactInfo from "../../components/maintenance/OrderContactInfo";
import WbddReconciliation from "../../components/maintenance/WbddReconciliation";
import WbddUploadFeedback from "../../components/maintenance/WbddUploadFeedback";
import DemandLineBatchCreate from "./DemandLineBatchCreate";
import DemandLineCreate from "./DemandLineCreate";
import DemandLinesEditor from "./DemandLinesEditor";

const { Title, Text } = Typography;

const DEMAND_PAGE_SIZE = 20;

function idempotencyKey(prefix: string): string {
  const suffix = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}

/**
 * 需求单与数据同步（#267 前端任务 1+2）。
 *
 * 上传原件更新资料 → 核对本次范围的整单与明细缺项 → 选择并确认同步。
 * D-27 差异操作绑定不可变来源与预览摘要；普通手工作废/恢复保留各自入口。
 *
 * 确认同步同时需要上传与作废动作权限；页面可见性由 page_maintenance 控制。
 */
export function MaintenanceDemandsPage() {
  const permissions = readPermissionMap();
  const canVoid = !!permissions.action_maintenance_demand_delete;
  const canImport = !!permissions.action_maintenance_wbdd_import;
  // 恢复接口后端硬性要求 admin（require_admin），非 admin 给了按钮也只会吃 403
  const isAdmin = localStorage.getItem("role") === "admin";
  const canRestore = canVoid && isAdmin;
  // v1.36：需求单行页面直改（override 保护）
  const canEditLines = !!permissions.action_maintenance_demand_manage;
  const [linesEditorFor, setLinesEditorFor] = useState<MaintenanceDemandSummary | null>(null);
  // v1.36 Phase E：页面直建手工需求行（独立新单）
  const [creatingLine, setCreatingLine] = useState(false);
  const [creatingBatch, setCreatingBatch] = useState(false);

  // ---- 区块一：氚云快照同步 + 差异清单 ----
  const [missing, setMissing] = useState<WbddMissing | null>(null);
  const [missingLoading, setMissingLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadReport, setUploadReport] = useState<WbddImportReport | null>(null);
  const addressReport = uploadReport?.address_report ?? null;
  const [uploadInputVersion, setUploadInputVersion] = useState(0);
  const uploadAttempt = useRef<{ fingerprint: string; key: string } | null>(null);
  const missingRequestSeq = useRef(0);

  // ---- 区块二：需求单查询 ----
  const [keyword, setKeyword] = useState("");
  const [includeVoided, setIncludeVoided] = useState(false);
  const [demands, setDemands] = useState<MaintenanceDemandSearchRow[]>([]);
  const [demandsTotal, setDemandsTotal] = useState(0);
  const [demandPage, setDemandPage] = useState(1);
  const [demandsLoading, setDemandsLoading] = useState(false);
  const demandRequestSeq = useRef(0);
  const latestDemandQuery = useRef({ page: 1, keyword: "", includeVoided: false });

  // ---- 作废原因弹窗（批量与单张共用） ----
  const [voidTarget, setVoidTarget] = useState<string[] | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voiding, setVoiding] = useState(false);

  // ---- 恢复原因弹窗（后端强制 reason 非空） ----
  const [restoreTarget, setRestoreTarget] = useState<MaintenanceDemandSummary | null>(null);
  const [restoreReason, setRestoreReason] = useState("");
  const [restoring, setRestoring] = useState(false);

  const loadMissing = useCallback(async (
    notifyFailure = true,
    acceptNotFound = true,
  ) => {
    const seq = ++missingRequestSeq.current;
    setMissingLoading(true);
    try {
      const resp = await getWbddMissing();
      if (seq !== missingRequestSeq.current) return false;
      setMissing(resp.data);
      return true;
    } catch (error) {
      if (seq !== missingRequestSeq.current) return false;
      // 还没传过快照（404）是合法空态；其他失败也必须失效旧差异清单。
      setMissing(null);
      const status = (error as { response?: { status?: number } })?.response?.status;
      if (status !== 404 && notifyFailure) {
        message.error(readError(error, "差异清单加载失败"));
      }
      return status === 404 && acceptNotFound;
    } finally {
      if (seq === missingRequestSeq.current) setMissingLoading(false);
    }
  }, []);

  const loadDemands = useCallback(
    async (page: number, q: string, withVoided: boolean, notifyFailure = true) => {
      const seq = ++demandRequestSeq.current;
      latestDemandQuery.current = { page, keyword: q, includeVoided: withVoided };
      setDemandsLoading(true);
      try {
        const resp = await searchMaintenanceDemands({
          page,
          page_size: DEMAND_PAGE_SIZE,
          ...(q.trim() ? { q: q.trim() } : {}),
          ...(withVoided ? { include_voided: true } : {}),
        });
        if (seq === demandRequestSeq.current) {
          setDemands(resp.data.items);
          setDemandsTotal(resp.data.total);
          setDemandPage(resp.data.page ?? page);
        }
        return seq === demandRequestSeq.current;
      } catch (error) {
        if (seq === demandRequestSeq.current) {
          setDemands([]);
          setDemandsTotal(0);
          setDemandPage(page);
          if (notifyFailure) message.error(readError(error, "需求单列表加载失败"));
        }
        return false;
      } finally {
        if (seq === demandRequestSeq.current) setDemandsLoading(false);
      }
    },
    [],
  );

  const refreshLatestDemands = useCallback((notifyFailure = true) => {
    const latest = latestDemandQuery.current;
    return loadDemands(latest.page, latest.keyword, latest.includeVoided, notifyFailure);
  }, [loadDemands]);

  const refreshAfterCommit = useCallback(async (
    includeMissing: boolean,
    requireMissingSnapshot = false,
  ) => {
    const refreshes = [refreshLatestDemands(false)];
    if (includeMissing) refreshes.push(loadMissing(false, !requireMissingSnapshot));
    const results = await Promise.allSettled(refreshes);
    return results.every((result) =>
      result.status === "fulfilled" && result.value !== false);
  }, [loadMissing, refreshLatestDemands]);

  useEffect(() => {
    void loadMissing();
    void loadDemands(1, "", false);
    return () => {
      demandRequestSeq.current += 1;
      missingRequestSeq.current += 1;
    };
  }, [loadMissing, loadDemands]);

  const handleWbddUpload = async (file: File) => {
    setUploading(true);
    setUploadReport(null);
    const fingerprint = `${file.name}:${file.size}:${file.lastModified}`;
    if (uploadAttempt.current?.fingerprint !== fingerprint) {
      uploadAttempt.current = {
        fingerprint,
        key: idempotencyKey("wbdd-upload"),
      };
    }
    try {
      const resp = await uploadWbdd(file, uploadAttempt.current.key);
      setUploadReport(resp.data);
      const missingOrders = resp.data.snapshot_diff?.missing_orders ?? 0;
      const missingLines = resp.data.snapshot_diff?.missing_lines ?? 0;
      const differences = [
        ...(missingOrders > 0 ? [`${missingOrders} 张缺失整单`] : []),
        ...(missingLines > 0 ? [`${missingLines} 行缺失明细`] : []),
      ];
      const summary = `快照已同步（批次 #${resp.data.batch_id}）` +
        (differences.length ? `，发现 ${differences.join("、")}，请核对下方差异清单` : "");
      uploadAttempt.current = null;
      // 上传接口刚返回 batch_id，此时 missing 读回 404 不能当合法空态。
      if (await refreshAfterCommit(true, true)) {
        message.success(`${summary}，页面已刷新`);
      } else {
        message.warning(`${summary}，但页面刷新失败；旧列表已失效，请重试。`);
      }
    } catch (error) {
      message.error(readError(error, "快照上传失败"));
    } finally {
      setUploading(false);
      // antd Upload 会保留原生 input 的 value；失败后若用户重选同一文件，浏览器
      // 不再触发 change。重建 input，同时保留上面的幂等键供安全重放。
      setUploadInputVersion((current) => current + 1);
    }
    return false;
  };

  const openVoidModal = (ids: string[]) => {
    setVoidTarget(ids);
    setVoidReason("");
  };

  const confirmVoid = async () => {
    if (!voidTarget) return;
    setVoiding(true);
    try {
      const resp = await voidFastMaintenanceDemands({
        source_order_ids: voidTarget,
        reason: voidReason.trim(),
        idempotency_key: idempotencyKey("wbdd-void"),
      });
      const already = resp.data.results.filter(
        (r) => r.status === "already_voided",
      ).length;
      const summary = `已作废 ${resp.data.voided} 张需求单` +
        (already ? `（其中 ${already} 张此前已是作废状态）` : "");
      setVoidTarget(null);
      // 从差异清单发起作废时，提交前已有快照；读回 404 同样属于刷新失败。
      if (await refreshAfterCommit(true, missing !== null)) {
        message.success(`${summary}，页面已刷新`);
      } else {
        message.warning(`${summary}，但页面刷新失败；旧列表已失效，请重试。`);
      }
    } catch (error) {
      message.error(readVoidError(error));
    } finally {
      setVoiding(false);
    }
  };

  const openRestoreModal = (row: MaintenanceDemandSummary) => {
    setRestoreTarget(row);
    setRestoreReason("");
  };

  const confirmRestore = async () => {
    if (!restoreTarget) return;
    setRestoring(true);
    try {
      const orderNo = restoreTarget.order_no;
      await restoreMaintenanceDemand(restoreTarget.source_order_id, restoreReason.trim());
      setRestoreTarget(null);
      if (await refreshAfterCommit(false)) {
        message.success(`已恢复需求单 ${orderNo}，页面已刷新`);
      } else {
        message.warning(`已恢复需求单 ${orderNo}，但页面刷新失败；旧列表已失效，请重试。`);
      }
    } catch (error) {
      message.error(readError(error, "恢复失败"));
    } finally {
      setRestoring(false);
    }
  };

  const demandColumns: ColumnsType<MaintenanceDemandSearchRow> = [
    {
      title: "需求单号",
      dataIndex: "order_no",
      render: (value, row) => (
        <Space size={4}>
          <span>{value}</span>
          {isVoided(row) ? <Tag>已作废</Tag> : null}
        </Space>
      ),
    },
    { title: "制单日期", dataIndex: "order_date", render: (v) => v ?? "—" },
    {
      title: "项目",
      dataIndex: "project",
      render: (_, row) => row.project ?? row.project_raw ?? "—",
    },
    {
      title: "关联销售单号",
      dataIndex: "linked_sales_order_no",
      render: (v) => v ?? "—",
    },
    { title: "明细行数", dataIndex: "line_count", width: 90, align: "right" },
    { title: "数量核对（当前累计）", key: "return_summary", width: 215, render: (_, row) => <Space direction="vertical" size={0}>
      <ReturnStatusColumn summary={row.return_summary} />
      {!canEditLines && !isVoided(row) ? <Button type="link" size="small" onClick={() => setLinesEditorFor(row)}>查看明细</Button> : null}
    </Space> },
    {
      title: "联系信息",
      key: "contact_info",
      width: 340,
      render: (_: unknown, row) => <OrderContactInfo contact={row} />,
    },
    ...(canVoid || canEditLines
      ? ([
          {
            title: "操作",
            key: "actions",
            width: 160,
            render: (_: unknown, row: MaintenanceDemandSummary) =>
              isVoided(row) ? (
                canRestore ? (
                  <Button size="small" onClick={() => openRestoreModal(row)}>
                    恢复
                  </Button>
                ) : null
              ) : (
                <Space size={4}>
                  {canEditLines ? (
                    <Button size="small" onClick={() => setLinesEditorFor(row)}>
                      编辑明细
                    </Button>
                  ) : null}
                  {canVoid ? (
                    <Button
                      size="small"
                      danger
                      onClick={() => openVoidModal([row.source_order_id])}
                    >
                      作废
                    </Button>
                  ) : null}
                </Space>
              ),
          },
        ] as ColumnsType<MaintenanceDemandSearchRow>)
      : []),
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {/* 已作废行整行灰显（沿用默认主题的中性禁用色，不引入新色板） */}
      <style>{".demand-row-voided > td { color: rgba(0, 0, 0, 0.25); }"}</style>

      <div>
        <Title level={4} style={{ marginBottom: 4 }}>
          需求单与数据同步
        </Title>
        <Text type="secondary">
          上传氚云需求单原件，更新已有资料，并核对本次上传范围内的缺失单据和明细。
        </Text>
      </div>

      <Card title="氚云快照同步">
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Space wrap size={12} align="center">
            {canEditLines ? (
              <>
                <Button type="primary" onClick={() => setCreatingLine(true)}>
                  新增手工需求
                </Button>
                <Button onClick={() => setCreatingBatch(true)}>
                  批量新增需求
                </Button>
              </>
            ) : null}
            {canImport ? (
              <Upload
                key={`wbdd-upload-${uploadInputVersion}`}
                accept=".xlsx"
                maxCount={1}
                showUploadList={false}
                beforeUpload={(file) => handleWbddUpload(file as unknown as File)}
              >
                <Button
                  type="primary"
                  icon={<UploadOutlined />}
                  loading={uploading}
                >
                  上传氚云需求单快照
                </Button>
              </Upload>
            ) : null}
            {missing?.batch_id != null ? (
              <Text type="secondary">
                批次 #{missing.batch_id}
                {missing.uploaded_at ? ` · 上传于 ${missing.uploaded_at}` : ""}
              </Text>
            ) : null}
          </Space>

          {canImport ? (
            <Text type="secondary">
              收货地址或地址列可放在任意位置；缺列、留空保留原地址。需要清空时，增加“地址操作”列填写“清空”。同单地址矛盾时保留原地址并提示。
            </Text>
          ) : null}
          {canImport && uploadReport ? <WbddUploadFeedback report={uploadReport} /> : null}
          {canImport && addressReport ? (
            <Alert
              type={addressReport.conflict ? "warning" : "info"}
              showIcon
              message={`地址处理：填写 ${addressReport.set} 张，未提供或保留 ${addressReport.keep} 张，清空 ${addressReport.clear} 张，冲突 ${addressReport.conflict} 张`}
              description={addressReport.warnings.length ? (
                <div style={{ maxHeight: 240, overflowY: "auto", overflowWrap: "anywhere" }}>
                  {addressReport.warnings.map((warning, index) => (
                    <div key={`${warning.order_no}-${index}`}>{warning.message}</div>
                  ))}
                  <Text type="secondary">数据行从表头后的第 1 行开始计数。</Text>
                </div>
              ) : null}
            />
          ) : null}

          <WbddReconciliation
            data={missing}
            loading={missingLoading}
            canReconcile={canVoid && canImport}
            onRefresh={loadMissing}
            onCommitted={() => refreshAfterCommit(true, true)}
          />
        </Space>
      </Card>

      <Card title="需求单">
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Space wrap size={12} align="center">
            <Input.Search
              allowClear
              placeholder="搜需求单号 / 项目"
              style={{ width: 260 }}
              onSearch={(value) => {
                setKeyword(value);
                void loadDemands(1, value, includeVoided);
              }}
            />
            <Space size={6} align="center">
              <Switch
                checked={includeVoided}
                onChange={(checked) => {
                  setIncludeVoided(checked);
                  void loadDemands(1, keyword, checked);
                }}
              />
              <Text type="secondary">含已作废</Text>
            </Space>
          </Space>

          <Table<MaintenanceDemandSearchRow>
            rowKey="source_order_id"
            size="small"
            loading={demandsLoading}
            dataSource={demands}
            columns={demandColumns}
            scroll={{ x: 1300 }}
            rowClassName={(row) => (isVoided(row) ? "demand-row-voided" : "")}
            locale={{ emptyText: "没有符合条件的需求单；换个关键词，或打开「含已作废」再试" }}
            pagination={{
              current: demandPage,
              total: demandsTotal,
              pageSize: DEMAND_PAGE_SIZE,
              showSizeChanger: false,
              onChange: (page) => void loadDemands(page, keyword, includeVoided),
            }}
          />
        </Space>
      </Card>

      <Modal
        title={voidTarget && voidTarget.length > 1 ? `批量作废 ${voidTarget.length} 张需求单` : "作废需求单"}
        open={voidTarget !== null}
        okText="确认作废"
        cancelText="取消"
        okButtonProps={{
          danger: true,
          loading: voiding,
          disabled: !voidReason.trim() || voidReason.trim().length > 1000,
        }}
        onOk={() => void confirmVoid()}
        onCancel={() => setVoidTarget(null)}
        maskClosable={false}
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Text>
            作废会把这些单从成本与统计里剔除，之后如需找回可以在列表里恢复。请填写作废原因（必填，1000
            字以内）：
          </Text>
          <Input.TextArea
            rows={3}
            maxLength={1000}
            showCount
            placeholder="作废原因，例如：氚云侧已删除该单"
            value={voidReason}
            onChange={(event) => setVoidReason(event.target.value)}
          />
        </Space>
      </Modal>

      <Modal
        title={`恢复需求单 ${restoreTarget?.order_no ?? ""}`}
        open={restoreTarget !== null}
        okText="确认恢复"
        cancelText="取消"
        okButtonProps={{
          loading: restoring,
          disabled: !restoreReason.trim() || restoreReason.trim().length > 1000,
        }}
        onOk={() => void confirmRestore()}
        onCancel={() => setRestoreTarget(null)}
        maskClosable={false}
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Text>
            恢复会清掉这张单的派生成本并标记为待重算（ADR-0003）。请填写恢复原因（必填，1000
            字以内）：
          </Text>
          <Input.TextArea
            rows={3}
            maxLength={1000}
            showCount
            placeholder="恢复原因，例如：误作废，氚云侧单仍然有效"
            value={restoreReason}
            onChange={(event) => setRestoreReason(event.target.value)}
          />
        </Space>
      </Modal>
      {linesEditorFor ? (
        <DemandLinesEditor
          key={linesEditorFor.source_order_id}
          sourceOrderId={linesEditorFor.source_order_id}
          orderNo={linesEditorFor.order_no}
          readOnly={!canEditLines}
          onChanged={() => refreshAfterCommit(false)}
          onClose={() => setLinesEditorFor(null)}
        />
      ) : null}
      {creatingLine ? (
        <DemandLineCreate
          onClose={() => setCreatingLine(false)}
          onCreated={() => { void refreshAfterCommit(false); }}
        />
      ) : null}
      {creatingBatch ? (
        <DemandLineBatchCreate
          onClose={() => setCreatingBatch(false)}
          onCommitted={() => refreshAfterCommit(false)}
        />
      ) : null}
    </Space>
  );
}

/**
 * 行类型里没有作废状态字段（#265 契约外，后端后续补），宽松读常见 key；
 * 读不到就当未作废——只显示「作废」按钮，不误给「恢复」。
 */
function isVoided(row: MaintenanceDemandSummary): boolean {
  const extra = row as unknown as Record<string, unknown>;
  if (extra.is_voided === true || extra.voided === true) return true;
  const status = extra.data_status ?? extra.status;
  return status === "voided" || status === "已作废";
}

/** void-fast 的失败文案按状态码区分（#265：409/404 都是整批零写入，但原因不同）。 */
function readVoidError(error: unknown): string {
  const resp = (
    error as { response?: { status?: number; data?: { detail?: unknown } } }
  )?.response;
  if (resp?.status === 409) {
    return "有需求单数据刚被改动，整批未作废；请刷新列表后重试";
  }
  if (resp?.status === 404) {
    return "清单里有系统查不到的单号，整批未作废；请刷新差异清单再试";
  }
  return readError(error, "作废失败");
}


function readError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response
    ?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return fallback;
}

export default MaintenanceDemandsPage;
