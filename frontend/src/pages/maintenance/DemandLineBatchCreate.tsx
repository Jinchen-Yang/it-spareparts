import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, Button, DatePicker, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Dayjs } from "dayjs";
import dayjs from "dayjs";
import type { MaintenanceProject } from "../../api/maintenanceProjects";
import { listMaintenanceProjects, searchMaintenanceProjects } from "../../api/maintenanceProjects";
import type { DemandLineCreateInput } from "../../api/maintenanceDemands";
import { createDemandLine } from "../../api/maintenanceDemands";
import PartPicker from "../../components/PartPicker";

const { Text } = Typography;
const TEXT_FIELD_MAX = 32767;

type CreateStatus = "queued" | "running" | "succeeded" | "failed";

interface DraftRow {
  id: string;
  part: { part_id: number; pn_std: string } | null;
  qty: number | null;
  returnQty: number | null;
  serialNumbers: string;
  description: string;
}

interface FrozenRow {
  id: string;
  payload: DemandLineCreateInput;
  status: CreateStatus;
  orderNo?: string;
  error?: string;
  unknown?: boolean;
}

function uuid(prefix: string): string {
  const suffix = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}

function newDraft(): DraftRow {
  return {
    id: uuid("draft"), part: null, qty: null, returnQty: 0,
    serialNumbers: "", description: "",
  };
}

function readError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" && detail ? detail : fallback;
}

function isUnknownOutcome(error: unknown): boolean {
  const status = (error as { response?: { status?: number } })?.response?.status;
  return status === undefined || status >= 500;
}

const statusTag: Record<CreateStatus, { color: string; text: string }> = {
  queued: { color: "default", text: "等待提交" },
  running: { color: "processing", text: "提交中" },
  succeeded: { color: "success", text: "成功" },
  failed: { color: "error", text: "失败" },
};

/**
 * 页面批量直建：每一行调用一次既有 create API，因此每行都是独立 PAGE 需求单。
 * 首次提交会冻结整批 normalized payload 与逐行幂等键；失败重试只重放原请求。
 */
export default function DemandLineBatchCreate({
  fixedProjectId,
  fixedProjectLabel,
  onClose,
  onCommitted,
}: {
  fixedProjectId?: string;
  fixedProjectLabel?: string;
  onClose: () => void;
  onCommitted: () => void | Promise<unknown>;
}) {
  const [projectId, setProjectId] = useState<string | undefined>(fixedProjectId);
  const [orderDate, setOrderDate] = useState<Dayjs | null>(dayjs());
  const [reason, setReason] = useState("");
  const [drafts, setDrafts] = useState<DraftRow[]>([newDraft()]);
  const [results, setResults] = useState<Record<string, FrozenRow>>({});
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [frozen, setFrozen] = useState(false);
  const frozenRef = useRef<FrozenRow[] | null>(null);
  const writeLock = useRef(false);
  const epochRef = useRef(0);

  const [projects, setProjects] = useState<MaintenanceProject[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectError, setProjectError] = useState<string | null>(null);
  const projectGen = useRef(0);
  const projectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadProjects = useCallback(async (query?: string) => {
    const generation = ++projectGen.current;
    setProjectsLoading(true);
    setProjectError(null);
    try {
      if (query?.trim()) {
        const response = await searchMaintenanceProjects({
          q: query.trim(), page: 1, page_size: 50, include_inactive: false,
        });
        if (generation === projectGen.current) setProjects(response.data.rows);
        return;
      }
      const all: MaintenanceProject[] = [];
      let page = 1;
      let total = 0;
      do {
        const response = await listMaintenanceProjects({
          page, page_size: 200, include_inactive: false,
        });
        if (generation !== projectGen.current) return;
        total = response.data.total;
        all.push(...response.data.rows);
        if (!response.data.rows.length) break;
        page += 1;
      } while (all.length < total);
      if (generation === projectGen.current) setProjects(all);
    } catch (cause) {
      if (generation === projectGen.current) {
        setProjects([]);
        setProjectError(readError(cause, "项目列表加载失败"));
      }
    } finally {
      if (generation === projectGen.current) setProjectsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!fixedProjectId) void loadProjects();
    return () => { projectGen.current += 1; };
  }, [fixedProjectId, loadProjects]);

  useEffect(() => {
    epochRef.current += 1;
    writeLock.current = false;
    frozenRef.current = null;
    setProjectId(fixedProjectId);
    setDrafts([newDraft()]);
    setResults({});
    setFrozen(false);
    setRunning(false);
    setError(null);
  }, [fixedProjectId]);

  useEffect(() => () => {
    epochRef.current += 1;
    if (projectTimer.current) clearTimeout(projectTimer.current);
  }, []);

  const updateDraft = (id: string, patch: Partial<DraftRow>) => {
    setDrafts((current) => current.map((row) => row.id === id ? { ...row, ...patch } : row));
  };

  const syncFrozen = (id: string, patch: Partial<FrozenRow>) => {
    const current = frozenRef.current;
    if (!current) return;
    const next = current.map((row) => row.id === id ? { ...row, ...patch } : row);
    frozenRef.current = next;
    setResults(Object.fromEntries(next.map((row) => [row.id, row])));
  };

  const runRows = async (epoch: number, targets: FrozenRow[]) => {
    let wroteAny = false;
    for (const target of targets) {
      if (epochRef.current !== epoch) return;
      syncFrozen(target.id, { status: "running", error: undefined });
      try {
        const response = await createDemandLine(target.payload);
        if (epochRef.current !== epoch) return;
        wroteAny = true;
        syncFrozen(target.id, {
          status: "succeeded", orderNo: response.data.order_no,
          error: undefined, unknown: false,
        });
      } catch (cause) {
        if (epochRef.current !== epoch) return;
        const unknown = isUnknownOutcome(cause);
        syncFrozen(target.id, {
          status: "failed",
          unknown,
          error: unknown
            ? `${readError(cause, "网络异常，结果未知")}；请保留当前结果并点“重试失败行”，系统只会继续处理失败行，避免重复创建成功行`
            : readError(cause, "创建失败；请核对项目、PN 与字段范围"),
        });
      }
    }
    if (epochRef.current !== epoch || !wroteAny) return;
    try {
      const refreshed = await onCommitted();
      if (epochRef.current !== epoch) return;
      if (refreshed === false) setError("需求已保存，但列表刷新失败；请稍后手动刷新查看最新数据");
    } catch {
      if (epochRef.current === epoch) {
        setError("需求已保存，但列表刷新失败；请稍后手动刷新查看最新数据");
      }
    }
  };

  const submit = async () => {
    if (writeLock.current) return;
    writeLock.current = true;
    const epoch = epochRef.current;
    setRunning(true);
    setError(null);
    try {
      let targets: FrozenRow[];
      if (!frozenRef.current) {
        const actualProjectId = fixedProjectId ?? projectId;
        if (!actualProjectId) {
          setError("请选择项目");
          return;
        }
        if (!orderDate) {
          setError("请选择制单日期");
          return;
        }
        if (!reason.trim()) {
          setError("创建原因必填，且不能只填空白");
          return;
        }
        if (!drafts.length) {
          setError("至少保留一行需求");
          return;
        }
        const invalid = drafts.findIndex((row) =>
          !row.part || row.qty == null || !Number.isFinite(row.qty) || row.qty <= 0
          || row.returnQty == null || !Number.isFinite(row.returnQty) || row.returnQty < 0
          || row.serialNumbers.trim().length > TEXT_FIELD_MAX
          || row.description.trim().length > TEXT_FIELD_MAX);
        if (invalid >= 0) {
          setError(`第 ${invalid + 1} 行不完整：请选择 PN，需求数量须 > 0、退货数量须 ≥ 0，文本不能超过 ${TEXT_FIELD_MAX} 字符`);
          return;
        }
        targets = drafts.map((row) => ({
          id: row.id,
          status: "queued" as const,
          payload: {
            project_id: actualProjectId,
            order_date: orderDate.format("YYYY-MM-DD"),
            pn_std: row.part!.pn_std,
            qty: row.qty!,
            return_qty: row.returnQty!,
            serial_numbers: row.serialNumbers.trim() || null,
            description: row.description.trim() || null,
            reason: reason.trim(),
            idempotency_key: uuid("demand-line-batch"),
          },
        }));
        frozenRef.current = targets;
        setResults(Object.fromEntries(targets.map((row) => [row.id, row])));
        setFrozen(true);
      } else {
        targets = frozenRef.current.filter((row) => row.status === "failed");
        if (!targets.length) return;
        targets.forEach((row) => syncFrozen(row.id, { status: "queued", error: undefined }));
      }
      await runRows(epoch, targets);
    } finally {
      if (epochRef.current === epoch) {
        setRunning(false);
        writeLock.current = false;
      }
    }
  };

  const columns: ColumnsType<DraftRow> = [
    { title: "#", width: 44, render: (_v, _row, index) => index + 1 },
    {
      title: "型号（PN）", width: 240,
      render: (_v, row) => frozen ? <Text>{row.part?.pn_std ?? "—"}</Text> : (
        <PartPicker
          value={row.part?.part_id ?? null}
          onChange={(partId, item) => updateDraft(row.id, {
            part: item && partId != null ? { part_id: partId, pn_std: item.pn_std } : null,
          })}
          disabled={running}
        />
      ),
    },
    {
      title: "需求数量", width: 125,
      render: (_v, row) => <InputNumber
        aria-label={`第${drafts.indexOf(row) + 1}行需求数量`}
        value={row.qty} min={0} precision={3} disabled={frozen || running}
        onChange={(value) => updateDraft(row.id, { qty: value })}
      />,
    },
    {
      title: "退货数量", width: 125,
      render: (_v, row) => <InputNumber
        aria-label={`第${drafts.indexOf(row) + 1}行退货数量`}
        value={row.returnQty} min={0} precision={3} disabled={frozen || running}
        onChange={(value) => updateDraft(row.id, { returnQty: value })}
      />,
    },
    {
      title: "SN（每行一个）", width: 220,
      render: (_v, row) => <Input.TextArea
        aria-label={`第${drafts.indexOf(row) + 1}行SN`}
        value={row.serialNumbers} rows={2} disabled={frozen || running}
        onChange={(event) => updateDraft(row.id, { serialNumbers: event.target.value })}
      />,
    },
    {
      title: "描述", width: 190,
      render: (_v, row) => <Input.TextArea
        aria-label={`第${drafts.indexOf(row) + 1}行描述`}
        value={row.description} rows={2} disabled={frozen || running}
        onChange={(event) => updateDraft(row.id, { description: event.target.value })}
      />,
    },
    {
      title: "结果", width: 230,
      render: (_v, row) => {
        const result = results[row.id];
        return result ? (
          <Space direction="vertical" size={2}>
            <Tag color={statusTag[result.status].color}>{statusTag[result.status].text}</Tag>
            {result.orderNo ? <Text copyable>{result.orderNo}</Text> : null}
            {result.error ? <Text type="danger" style={{ fontSize: 12 }}>{result.error}</Text> : null}
          </Space>
        ) : "—";
      },
    },
    {
      title: "操作", width: 70,
      render: (_v, row) => <Button
        size="small" danger disabled={frozen || running || drafts.length <= 1}
        onClick={() => setDrafts((current) => current.filter((item) => item.id !== row.id))}
      >删除</Button>,
    },
  ];

  const failedCount = Object.values(results).filter((row) => row.status === "failed").length;
  const succeededCount = Object.values(results).filter((row) => row.status === "succeeded").length;

  return (
    <Modal
      open width={1200} title="批量新增需求"
      footer={[
        <Button key="close" disabled={running} onClick={() => {
          epochRef.current += 1;
          writeLock.current = false;
          onClose();
        }}>关闭</Button>,
        <Button
          key="submit" type="primary" loading={running}
          disabled={frozen && failedCount === 0}
          onClick={() => { void submit(); }}
        >{frozen ? `重试失败行${failedCount ? `（${failedCount}）` : ""}` : "提交整批"}</Button>,
      ]}
      onCancel={() => {
        if (running) return;
        epochRef.current += 1;
        writeLock.current = false;
        onClose();
      }}
      maskClosable={false}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Alert
          type="info" showIcon
          message="每行生成一个独立需求单，系统逐行保存，部分行失败不影响成功行；开始后本批内容固定，继续重试失败行不会重复创建成功行。"
        />
        {error ? <Alert type="error" showIcon message={error} /> : null}
        {frozen ? (
          <Alert
            type={failedCount ? "warning" : "success"} showIcon
            message={`批次结果：成功 ${succeededCount} 行，失败 ${failedCount} 行。结果会保留；继续重试只处理失败行，不会重复创建成功行。`}
          />
        ) : null}
        <Space wrap align="start">
          {fixedProjectId ? (
            <div><Text type="secondary">项目（固定）</Text><br /><Text>{fixedProjectLabel ?? "当前项目"}</Text></div>
          ) : (
            <div>
              <Text type="secondary">项目</Text><br />
              <Select
                showSearch filterOption={false} style={{ width: 280 }}
                value={projectId} loading={projectsLoading} disabled={frozen || running}
                placeholder="选择项目（可按名称/编码搜索）"
                onSearch={(value) => {
                  if (projectTimer.current) clearTimeout(projectTimer.current);
                  projectTimer.current = setTimeout(() => { void loadProjects(value); }, 300);
                }}
                onChange={(value) => {
                  epochRef.current += 1;
                  writeLock.current = false;
                  frozenRef.current = null;
                  setProjectId(value);
                  setDrafts([newDraft()]);
                  setResults({});
                  setFrozen(false);
                  setError(null);
                }}
                notFoundContent={projectError ? "项目列表加载失败" : projectsLoading ? "加载中…" : "没有匹配项目"}
                options={projects.map((project) => ({
                  value: project.project_id,
                  label: `${project.display_name}（${project.project_code}）`,
                }))}
              />
            </div>
          )}
          <div>
            <Text type="secondary">制单日期</Text><br />
            <DatePicker value={orderDate} disabled={frozen || running} onChange={setOrderDate} />
          </div>
        </Space>
        {projectError && !fixedProjectId ? (
          <Alert type="error" showIcon message={projectError}
            action={<Button size="small" onClick={() => { void loadProjects(); }}>重试</Button>} />
        ) : null}
        <Table<DraftRow>
          rowKey="id" size="small" dataSource={drafts} columns={columns}
          pagination={false} scroll={{ x: 1180 }}
        />
        <Button
          disabled={frozen || running}
          onClick={() => setDrafts((current) => [...current, newDraft()])}
        >添加一行</Button>
        <div>
          <Text type="secondary">共同创建原因（必填，审计留痕）</Text>
          <Input.TextArea
            aria-label="共同创建原因" rows={2} value={reason} disabled={frozen || running}
            placeholder="如：氚云漏单，按现场需求批量补录"
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
      </Space>
    </Modal>
  );
}
