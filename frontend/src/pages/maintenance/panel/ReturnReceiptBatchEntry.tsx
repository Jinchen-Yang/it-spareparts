import { useMemo, useRef, useState } from "react";
import { Alert, Button, Input, Modal, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  type ReturnReceipt,
  createReturnReceipt,
} from "../../../api/maintenanceOperations";
import { raw, readError } from "./panelUtils";

const { Text } = Typography;

/** 批量行（解析后的中间形态） */
interface BatchLine {
  key: string;
  pn: string;
  serials: string[];
  /** 仅无 SN 行可显式数量；有 SN 行数量恒等于 SN 数 */
  qty: number | null;
  error: string | null;
}

/** 提交结果行 */
interface ResultRow {
  key: string;
  pn: string;
  qty: number;
  status: "ok" | "replayed" | "failed";
  detail: string;
}

/**
 * 解析批量粘贴文本。每行格式（分隔符支持逗号/制表符/多个空白混用）：
 *   PN
 *   PN,数量                —— 无 SN 的数量行
 *   PN,SN1,SN2,SN3         —— SN 行，数量 = SN 个数
 *   PN,数量,SN1,SN2        —— 不允许：有 SN 时数量由 SN 决定
 * 空行忽略；# 开头视为注释行；行内 SN 去重报错。
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
      .map((cell) => cell.trim())
      .filter((cell, i, arr) => !(cell === "" && (i === 0 || i === arr.length - 1)));
    if (!cells.length) return;
    const pn = cells[0];
    if (!pn) return;
    const key = `${index}-${pn}`;
    const rest = cells.slice(1);
    const restNumbers = rest.filter((cell) => /^\d+$/.test(cell));
    let error: string | null = null;
    let serials: string[] = [];
    let qty: number | null = null;

    const allNumbers = rest.length > 0 && rest.every((cell) => /^\d+$/.test(cell));
    if (rest.length === 0) {
      error = "缺少数量或 SN（每行至少：PN,数量 或 PN,SN）";
    } else if (allNumbers) {
      // 纯数字：单值=数量；多值=错误（SN 不会是纯数字串吗？可能是，按"第一个为数量"歧义太大，直接拒绝）
      if (rest.length === 1) {
        const value = Number(rest[0]);
        if (value <= 0 || !Number.isInteger(value)) error = "数量必须为正整数";
        else qty = value;
      } else {
        error = "多个纯数字无法区分数量与 SN：SN 行请用 PN,SN1,SN2…（数量自动=SN 数）";
      }
    } else {
      // 含非数字 → 全部视为 SN
      serials = rest.map((cell) => cell);
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
    lines.push({ key, pn, serials, qty, error });
  });
  return lines;
}

/**
 * 批量录入返还（v1.36 Phase B）：
 * 粘贴多行 `PN,SN…` → 预览校验（本地）→ 逐条登记（独立幂等键）→ 结果表。
 * 部分失败语义：每行独立成败，失败行带原因，成功的不会回滚（与 void-fast 一致）。
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
  const seq = useRef(0);

  const lines = useMemo(() => parseBatchText(text), [text]);
  const errorCount = lines.filter((line) => line.error).length;
  const totalQty = lines.reduce((sum, line) =>
    sum + (line.error ? 0 : (line.serials.length || line.qty || 0)), 0);

  const reset = () => {
    setText("");
    setPhase("input");
    setResults([]);
  };

  const close = () => {
    if (submitting) return;
    const hadSuccess = results.some((row) => row.status !== "failed");
    setOpen(false);
    if (hadSuccess) void onDone();
    reset();
  };

  const submitAll = async () => {
    const valid = lines.filter((line) => !line.error);
    if (!valid.length) return;
    const current = ++seq.current;
    setSubmitting(true);
    const rows: ResultRow[] = [];
    for (const line of valid) {
      const qty = line.serials.length || line.qty || 0;
      const payload = {
        pn: line.pn,
        qty,
        serial_numbers: line.serials.length ? line.serials : null,
        idempotency_key: `batch-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`,
      };
      try {
        const response = await createReturnReceipt(projectId, payload);
        if (seq.current !== current) return;
        rows.push({
          key: line.key,
          pn: line.pn,
          qty,
          status: response.data.replayed ? "replayed" : "ok",
          detail: response.data.replayed ? "重复提交：返回已有登记" : raw(response.data.receipt_id).slice(0, 8),
        });
      } catch (err) {
        if (seq.current !== current) return;
        rows.push({
          key: line.key,
          pn: line.pn,
          qty,
          status: "failed",
          detail: readError(err, "登记失败"),
        });
      }
      setResults([...rows]);
    }
    setSubmitting(false);
    setPhase("results");
    const okCount = rows.filter((row) => row.status !== "failed").length;
    if (okCount === rows.length) message.success(`批量登记完成：${okCount} 条`);
    else message.warning(`批量登记结束：成功 ${okCount} 条，失败 ${rows.length - okCount} 条`);
  };

  const resultColumns: ColumnsType<ResultRow> = [
    { title: "PN", dataIndex: "pn" },
    { title: "数量", dataIndex: "qty", width: 80 },
    {
      title: "结果", dataIndex: "status", width: 110,
      render: (status: ResultRow["status"]) => status === "ok" ? <Tag color="green">已登记</Tag>
        : status === "replayed" ? <Tag color="blue">幂等重放</Tag>
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
      render: (_v, line) => line.error ? "—" : (line.serials.length || line.qty),
    },
    {
      title: "校验", dataIndex: "error", width: 260,
      render: (error: string | null) => error
        ? <Text type="danger" style={{ fontSize: 12 }}>{error}</Text>
        : <Tag color="green">通过</Tag>,
    },
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
        confirmLoading={submitting}
        okText={phase === "results" ? "关闭" : phase === "preview" ? `登记 ${lines.length - errorCount} 条` : "解析预览"}
        okButtonProps={{
          disabled: phase === "preview" && (submitting || lines.length === 0 || errorCount === lines.length),
        }}
        cancelText={phase === "results" ? undefined : phase === "preview" ? "返回修改" : "取消"}
        onCancel={() => {
          if (submitting) return;
          if (phase === "preview") { setPhase("input"); return; }
          close();
        }}
        maskClosable={!submitting}
        onOk={() => {
          if (phase === "input") { setPhase("preview"); return; }
          if (phase === "preview") { void submitAll(); return; }
          close();
        }}
      >
        {phase === "input" ? (
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              每行一个 PN，支持从 Excel 直接粘贴。格式：<code>PN,SN1,SN2…</code>（数量自动=SN 数）
              或 <code>PN,数量</code>（无 SN）。空行忽略；# 开头为注释。
            </Text>
            <Input.TextArea
              rows={10}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder={"PN-1001,SN-A001,SN-A002\nPN-1002,3\n# 注释行"}
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
                  + (lines.length - errorCount > 0 ? `，其余 ${lines.length - errorCount} 行（合计 ${totalQty} 件）将继续` : "")}
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
          <Table<ResultRow>
            size="small"
            rowKey="key"
            pagination={false}
            dataSource={results}
            columns={resultColumns}
            scroll={{ x: 640, y: 360 }}
          />
        )}
      </Modal>
    </>
  );
}
