import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Input, Modal, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { createReturnReceipt } from "../../../api/maintenanceOperations";
import { raw, readError } from "./panelUtils";

const { Text } = Typography;

/**
 * 与后端 register_receipt/_validate_serial_numbers 的硬约束对齐（只读镜像，改后端时同步）：
 * PN ≤128、SN ≤128/个、单条 SN ≤1000、qty 正整数 <10^11、idempotency_key ≤128
 * （本组件 key = "br-" + 36 位批次 UUID + "-" + 行序号，最长 ~44 字符，天然在上限内）
 */
const PN_MAX = 128;
const SN_MAX = 128;
const SERIALS_MAX = 1000;
const QTY_MAX = 10 ** 11 - 1;

/** 无 SN 数量行的显式前缀：禁止凭“全数字”猜数量——纯数字/前导零一律是 SN 候选 */
const QTY_PREFIX = "qty:";

function uuid(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `f-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;
}

/** 行冻结 payload：首次提交时快照，重试原样复用；成功行的身份永不重算、永不复用到新内容 */
interface LineFingerprint {
  pn: string;
  serials: string[];
  qty: number;
}

function fingerprintOf(line: BatchLine): LineFingerprint {
  return {
    pn: line.pn,
    serials: line.serials,
    qty: line.serials.length || line.qty || 0,
  };
}

/** 批量行（解析后的中间形态） */
interface BatchLine {
  key: string;
  pn: string;
  serials: string[];
  /** 仅无 SN 行可显式数量；有 SN 行数量恒等于 SN 数 */
  qty: number | null;
  error: string | null;
}

/** 提交结果行。unknown=响应丢失（服务器可能已登记）：重试必须复用原 key 走幂等重放 */
interface ResultRow {
  /**
   * 行稳定身份 = 服务端幂等键：批次 UUID + 行序号（同批两行内容完全相同也各自独立，
   * 不含业务原文）。只在首次提交时生成一次，重试直接从本字段复用，绝不重算。
   */
  idempotencyKey: string;
  fingerprint: LineFingerprint;
  pn: string;
  qty: number;
  status: "pending" | "ok" | "replayed" | "failed" | "unknown";
  detail: string;
}

/**
 * 解析批量粘贴文本。每行格式（分隔符支持逗号/制表符/多个空白混用）：
 *   PN,SN1,SN2,SN3        —— SN 行，数量 = SN 个数
 *   PN,qty:数量           —— 无 SN 数量行（必须带 qty: 前缀）
 *   PN                    —— 报错：缺数量或 SN
 *
 * 铁律：禁止凭“全数字”猜数量。纯数字/前导零（如 007、1001）一律按 SN 候选保留字符串；
 * 无 SN 数量必须显式写 qty: 前缀。空行忽略；# 开头视为注释行；SN 行内/跨行去重报错；
 * 首列空 = PN 空（报错，不移位）；SN 中间空列报错（尾随空列容忍，Excel 复制常见）。
 */
export function parseBatchText(text: string): BatchLine[] {
  const lines: BatchLine[] = [];
  const seen = new Map<string, number>(); // sn -> 行号（跨行查重）
  const rows = text.split(/\r?\n/);
  rows.forEach((rowText, index) => {
    const trimmed = rowText.trim();
    if (!trimmed || trimmed.startsWith("#")) return;
    const cells = trimmed
      .split(/[\t,]+/)
      .map((cell) => cell.trim());
    // 只去尾部空列（Excel 复制常带尾逗号）；首列保留原位——空 PN 必须报错，不能移位成 SN 当 PN
    while (cells.length && cells[cells.length - 1] === "") cells.pop();
    if (!cells.length) return;
    const pn = cells[0];
    const rest = cells.slice(1);
    let error: string | null = null;
    let serials: string[] = [];
    let qty: number | null = null;

    if (!pn) {
      error = "PN 不能为空（首列为空）";
    } else if (rest.length === 0) {
      error = `缺少数量或 SN（写 ${QTY_PREFIX}数量 表示无 SN 数量）`;
    } else if (rest[0].toLowerCase().startsWith(QTY_PREFIX)) {
      const valueText = rest[0].slice(QTY_PREFIX.length).trim();
      const value = /^\d+$/.test(valueText) ? Number(valueText) : NaN;
      if (valueText === "" || !(value > 0) || !Number.isInteger(value) || value > QTY_MAX) {
        error = `${QTY_PREFIX} 后必须是正整数（≤${QTY_MAX}）`;
      } else if (rest.length > 1) {
        error = `${QTY_PREFIX} 行不能再带 SN（带 SN 时数量=SN 个数，去掉 ${QTY_PREFIX} 行）`;
      } else {
        qty = value;
      }
    } else {
      // 其余（含纯数字/前导零）一律按 SN 字符串处理，绝不猜数量
      serials = rest;
      const localSeen = new Set<string>();
      for (const sn of serials) {
        if (!sn) { error = "SN 不能为空白"; break; }
        if (localSeen.has(sn)) { error = `SN 重复：${sn}`; break; }
        localSeen.add(sn);
        const firstLine = seen.get(sn);
        if (firstLine !== undefined && firstLine !== index) {
          error = `SN 与第 ${firstLine + 1} 行重复：${sn}`;
          break;
        }
      }
      if (!error) serials.forEach((sn) => seen.set(sn, index));
    }
    lines.push({ key: `${index}-${pn}`, pn, serials, qty, error });
  });
  return lines;
}

/** 单行本地预检（pn/SN 长度、单行 SN 上限、数量上限），与后端约束镜像对齐 */
export function validateLineConstraints(line: BatchLine): string | null {
  if (line.pn.length > PN_MAX) return `PN 长度 ${line.pn.length} 超上限 ${PN_MAX}`;
  if (line.serials.length > SERIALS_MAX) {
    return `SN 数 ${line.serials.length} 超单条上限 ${SERIALS_MAX}，请拆成多行`;
  }
  for (const sn of line.serials) {
    if (sn.length > SN_MAX) return `SN ${sn.slice(0, 16)}… 长度 ${sn.length} 超上限 ${SN_MAX}`;
  }
  const qty = line.serials.length || line.qty || 0;
  if (line.qty !== null && line.qty > QTY_MAX) return `数量超上限 ${QTY_MAX}`;
  if (qty <= 0) return "数量必须大于 0";
  return null;
}

/** axios 错误是否“有服务端响应”（4xx/5xx=明确失败）；无响应=网络层，结果未知 */
function hasResponse(error: unknown): boolean {
  return (error as { response?: unknown })?.response !== undefined;
}

/** 提交会话：isCurrent 通过对象引用同一性判定；任何切换/关闭/unmount 置 null 即全部作废 */
interface SubmitSession {
  projectId: string;
}

/**
 * 批量录入返还（v1.36 Phase B → v1.36.1 重做）：
 * 粘贴多行 → 预览校验（本地约束镜像）→ 逐条登记（行级稳定幂等键）→ 结果表。
 * 部分失败语义：每行独立成败；失败/未知行单独重试（直接复用保存的 key+冻结 payload 走幂等
 * 重放），成功行锁定不可再提交。关闭只隐藏不清结果：重开可继续同批重试，「新批次」显式清空。
 */
export default function ReturnReceiptBatchEntry({ projectId, onDone }: {
  projectId: string;
  onDone: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<"input" | "preview" | "results">("input");
  const [submitting, setSubmitting] = useState(false);
  const [results, setResults] = useState<ResultRow[]>([]);
  /** 当前有效提交会话；projectId 切换 / unmount / close / 新批次将其置 null */
  const sessionRef = useRef<SubmitSession | null>(null);
  /**
   * 同步防双击锁（React setState 异步不可靠）。只在两处清零：会话拥有者的循环正常跑完、
   * 失效方（projectId 切换 / unmount 的 effect）。过期异步任务绝不触碰本 ref——否则
   * 旧项目的迟到 Promise 会把新项目刚拿到的锁误清掉。
   */
  const busyRef = useRef(false);

  const lines = useMemo(() => parseBatchText(text), [text]);
  const lineError = (line: BatchLine) => line.error || validateLineConstraints(line);
  const errorCount = lines.reduce((count, line) => count + (lineError(line) ? 1 : 0), 0);
  /** 预览件数只累计实际可提交行（parse 与约束预检都通过），避免夸大 */
  const totalQty = lines.reduce(
    (sum, line) => sum + (lineError(line) ? 0 : (line.serials.length || line.qty || 0)),
    0,
  );

  // unmount：立即失效在途会话——旧 Promise 迟到时循环直接退出，不再发后续行、不写状态
  useEffect(() => () => { sessionRef.current = null; }, []);

  // projectId 变化 = 组件被复用于新项目：立即失效旧会话（在途回调作废、锁由这里清）、
  // 清空并关闭界面。旧项目保留的结果/幂等键一并丢弃，防跨项目污染。
  useEffect(() => {
    sessionRef.current = null;
    busyRef.current = false;
    setSubmitting(false);
    setText("");
    setResults([]);
    setPhase("input");
    setOpen(false);
  }, [projectId]);

  /** 关闭只隐藏：保留 results（含 unknown/failed 行的 key 与冻结 payload）与草稿，重开可继续同批重试 */
  const close = () => {
    if (busyRef.current) return; // 提交期间拒绝关闭（防未知成功后重开盲重复）
    sessionRef.current = null;
    setOpen(false);
    const hadSuccess = results.some((r) => r.status === "ok" || r.status === "replayed");
    if (hadSuccess) void onDone();
  };

  /** 显式开新批次：清掉本批结果与重试身份（按钮旁的提示说明后果） */
  const startNewBatch = () => {
    if (busyRef.current) return;
    sessionRef.current = null;
    setResults([]);
    setText("");
    setPhase("input");
  };

  /**
   * 提交（首轮或重试）。首轮：行身份 = 批次 UUID + 行序号，一次生成永不重算；
   * 重试：直接复用先前 ResultRow 保存的 key 与冻结 payload（不重算、不从可编辑文本取）。
   * 过期会话（isCurrent=false）的循环路径只 return：不碰 busyRef、不碰任何 state/ref。
   */
  const submitAll = async (retry: boolean) => {
    if (busyRef.current) return;
    let pendingRows: ResultRow[];
    let ordered: ResultRow[];
    if (retry) {
      const failed = results.filter((r) => r.status === "failed" || r.status === "unknown");
      if (!failed.length) return;
      const failedKeys = new Set(failed.map((r) => r.idempotencyKey));
      pendingRows = failed.map((r) => ({ ...r, status: "pending", detail: "" }));
      // 成功行原位保留展示；重试行保持原行序，key/payload 原样复用
      ordered = results.map((r) =>
        failedKeys.has(r.idempotencyKey) ? { ...r, status: "pending" as const, detail: "" } : r);
    } else {
      const targets = lines.filter((line) => !lineError(line));
      if (!targets.length) return;
      const batchId = uuid();
      pendingRows = targets.map((line, i) => {
        const fingerprint = fingerprintOf(line);
        return {
          idempotencyKey: `br-${batchId}-${i}`,
          fingerprint,
          pn: line.pn,
          qty: fingerprint.qty,
          status: "pending" as const,
          detail: "",
        };
      });
      ordered = pendingRows;
    }

    const mySession: SubmitSession = { projectId };
    sessionRef.current = mySession;
    const isCurrent = () => sessionRef.current === mySession;
    busyRef.current = true;
    setResults([...ordered]);
    setPhase("results");
    setSubmitting(true);

    const rows = [...ordered];
    let okCount = 0;
    for (const row of pendingRows) {
      // 会话已失效（项目切换/unmount/关闭）：立即退出。锁与状态由失效方负责清理，
      // 这里绝不递减 busyRef、绝不 setResults/setSubmitting——不能污染新会话。
      if (!isCurrent()) return;
      try {
        const response = await createReturnReceipt(mySession.projectId, {
          pn: row.fingerprint.pn,
          qty: row.fingerprint.qty,
          serial_numbers: row.fingerprint.serials.length ? row.fingerprint.serials : null,
          idempotency_key: row.idempotencyKey,
        });
        if (!isCurrent()) return;
        const idx = rows.findIndex((r) => r.idempotencyKey === row.idempotencyKey);
        rows[idx] = {
          ...row,
          status: response.data.replayed ? "replayed" : "ok",
          detail: response.data.replayed
            ? "重复提交：返回已有登记"
            : raw(response.data.receipt_id).slice(0, 8),
        };
        okCount += 1;
      } catch (err) {
        if (!isCurrent()) return;
        const idx = rows.findIndex((r) => r.idempotencyKey === row.idempotencyKey);
        rows[idx] = hasResponse(err)
          ? { ...row, status: "failed", detail: readError(err, "登记失败") }
          // 响应丢失（网络断/超时）：服务器可能已登记 → 未知；重试复用同 key 幂等重放，不会重复登记
          : {
              ...row,
              status: "unknown",
              detail: `${readError(err, "网络错误，结果未知")}——可重试（复用原幂等键，不会重复登记）`,
            };
      }
      if (isCurrent()) setResults([...rows]);
    }

    busyRef.current = false; // 只有走完全程的拥有者才清锁（失效方已在 effect 里清过）
    if (!isCurrent()) return;
    setSubmitting(false);
    if (okCount === pendingRows.length) message.success(`批量登记完成：${okCount} 条`);
    else message.warning(`批量登记结束：成功 ${okCount} 条，失败/未知 ${pendingRows.length - okCount} 条`);
  };

  const retryable = results.filter((r) => r.status === "failed" || r.status === "unknown").length;
  const hasPending = results.some((r) => r.status === "pending");

  const resultColumns: ColumnsType<ResultRow> = [
    { title: "PN", dataIndex: "pn" },
    { title: "数量", dataIndex: "qty", width: 80 },
    {
      title: "结果", dataIndex: "status", width: 110,
      render: (status: ResultRow["status"]) => status === "ok" ? <Tag color="green">已登记</Tag>
        : status === "replayed" ? <Tag color="blue">幂等重放</Tag>
        : status === "unknown" ? <Tag color="orange">结果未知</Tag>
        : status === "pending" ? <Tag color="default">提交中…</Tag>
        : <Tag color="red">失败</Tag>,
    },
    { title: "说明", dataIndex: "detail" },
  ];

  const previewColumns: ColumnsType<BatchLine> = [
    { title: "PN", dataIndex: "pn" },
    {
      title: "SN", dataIndex: "serials", width: 300,
      render: (serials: string[]) => serials.length
        ? <Text style={{ fontSize: 12, wordBreak: "break-all" }}>{serials.join("、")}</Text>
        : "—",
    },
    {
      title: "数量", dataIndex: "qty", width: 80,
      render: (_v, line) => lineError(line) ? "—" : (line.serials.length || line.qty),
    },
    {
      title: "校验", dataIndex: "error", width: 260,
      render: (_v, line) => {
        const problem = lineError(line);
        return problem
          ? <Text type="danger" style={{ fontSize: 12 }}>{problem}</Text>
          : <Tag color="green">通过</Tag>;
      },
    },
  ];

  const validCount = lines.length - errorCount;
  const footer =
    phase === "input" ? [
      <Button key="cancel" onClick={close}>取消</Button>,
      <Button key="next" type="primary" disabled={lines.length === 0}
        onClick={() => setPhase("preview")}>解析预览</Button>,
    ]
    : phase === "preview" ? [
      <Button key="back" onClick={() => setPhase("input")}>返回修改</Button>,
      <Button key="submit" type="primary"
        disabled={validCount === 0}
        onClick={() => { void submitAll(false); }}>
        {`登记 ${validCount} 条`}
      </Button>,
    ]
    : retryable > 0 ? [
      <Button key="new" disabled={submitting} onClick={startNewBatch}>新批次</Button>,
      <Button key="close" disabled={submitting} onClick={close}>关闭</Button>,
      <Button key="retry" type="primary" loading={submitting}
        onClick={() => { void submitAll(true); }}>{`重试 ${retryable} 条`}</Button>,
    ]
    : [
      <Button key="new" disabled={submitting} onClick={startNewBatch}>新批次</Button>,
      <Button key="close" type="primary" loading={submitting} onClick={close}>关闭</Button>,
    ];

  return (
    <>
      <Button
        size="small"
        onClick={() => { setOpen(true); }}
      >
        批量录入
      </Button>
      <Modal
        open={open}
        title="批量录入返还（每行一个 PN）"
        width={760}
        destroyOnHidden
        maskClosable={!submitting}
        keyboard={!submitting}
        footer={footer}
        onCancel={() => { close(); }}
      >
        {phase === "input" ? (
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              每行一个 PN，支持从 Excel 直接粘贴。格式：<code>PN,SN1,SN2…</code>（数量自动=SN 数）
              或 <code>PN,{QTY_PREFIX}数量</code>（无 SN 的纯数量）。纯数字/前导零一律按 SN 处理。
              空行忽略；# 开头为注释。约束：PN ≤{PN_MAX} 字符、SN ≤{SN_MAX} 字符、单行 SN ≤{SERIALS_MAX} 条。
            </Text>
            <Input.TextArea
              rows={10}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder={`PN-1001,SN-A001,SN-A002\nPN-1002,${QTY_PREFIX}3\n# 注释行`}
              autoFocus
            />
          </Space>
        ) : phase === "preview" ? (
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            {errorCount > 0 ? (
              <Alert
                type="warning"
                showIcon
                message={`${errorCount} 行有误不会登记`
                  + (validCount > 0 ? `，其余 ${validCount} 行（合计 ${totalQty} 件）将继续` : "")}
              />
            ) : (
              <Alert type="success" showIcon message={`共 ${lines.length} 行，合计 ${totalQty} 件`} />
            )}
            <Table<BatchLine>
              size="small"
              rowKey="key"
              pagination={false}
              dataSource={lines}
              columns={previewColumns}
              scroll={{ x: 720, y: 360 }}
            />
          </Space>
        ) : (
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            {retryable > 0 && !hasPending ? (
              <Alert
                type="warning"
                showIcon
                message={`有 ${retryable} 条失败/结果未知：点「重试 ${retryable} 条」仅重试这些行`
                  + `（原样复用已保存的幂等键，服务器侧不会重复登记），成功行已锁定不可再提交。`
                  + `关闭窗口会保留本批结果与重试身份，重开可继续；「新批次」将清空以上身份。`}
              />
            ) : null}
            <Table<ResultRow>
              size="small"
              rowKey="idempotencyKey"
              pagination={false}
              dataSource={results}
              columns={resultColumns}
              scroll={{ x: 640, y: 360 }}
            />
          </Space>
        )}
      </Modal>
    </>
  );
}
