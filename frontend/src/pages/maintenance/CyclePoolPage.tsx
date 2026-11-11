import { useEffect, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Input,
  InputNumber,
  Modal,
  Space,
  Spin,
  Table,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import PageHeader from "../../components/PageHeader";
import { readPermissionMap } from "../../nav";
import {
  attestShopAdjustment,
  getRepairRecord,
  listRepairPool,
  listRepairRecords,
  listRepairSummary,
  submitRepairRecord,
  voidRepairRecord,
  type RepairPoolItem,
  type RepairRecord,
  type RepairRecordDetail,
  type RepairSellIntent,
  type RepairSummaryItem,
} from "../../api/cyclePool";
import { formatBusinessDateTime } from "../../utils/date";
import {
  SHOP_STOCK_FORBIDDEN_DETAIL,
  SHOP_STOCK_REQUIRED_DETAIL,
  UNKNOWN_ATTEMPT_MESSAGE,
  commandOutcome,
  createdOnBusinessToday,
  detailOf,
  newIdempotencyKey,
  nextCommandAttempt,
  repairQtyError,
  settleCommandAttempt,
  visibleShopStockCheck,
  voidReasonError,
  type CommandAttempt,
  type ShopStockCheck,
} from "./repairPoolRules";

const PAGE_SIZE = 20;

interface Access {
  pool: boolean;
  summary: boolean;
  records: boolean;
  detail: boolean;
  voidAny: boolean;
}

function accessFrom(perms: Record<string, boolean>): Access {
  const manage = perms.page_cycle_manage === true;
  const repair = perms.page_cycle_repair === true;
  const wip = perms.page_cycle_wip === true;
  return {
    pool: manage || repair,
    summary: manage || wip,
    records: manage || repair,
    detail: manage,
    voidAny: manage,
  };
}

function textOrDash(value: string | null | undefined): string {
  const trimmed = value?.trim();
  return trimmed ? trimmed : "—";
}

function intentStatusText(status: string): string {
  const labels: Record<string, string> = {
    pending: "待发送",
    delivering: "发送中",
    delivered: "已送达",
    failed: "已停止",
    blocked: "未入账",
  };
  const label = labels[status];
  return label ? `${label}（${status}）` : status;
}

function recordStatusText(status: string): string {
  if (status === "active") return "有效";
  if (status === "voided") return "已作废";
  return status;
}

function asQty(value: number | string | null): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function useCommandRunner(prefix: string) {
  const attemptRef = useRef<CommandAttempt | null>(null);
  const lock = useRef(false);
  const [attempt, setAttempt] = useState<CommandAttempt | null>(null);
  const [running, setRunning] = useState(false);
  const replace = (next: CommandAttempt | null) => {
    attemptRef.current = next;
    setAttempt(next);
  };
  const run = async (
    fingerprint: string,
    work: (key: string) => Promise<unknown>,
  ): Promise<"busy" | "blocked" | "ok"> => {
    if (lock.current) return "busy";
    lock.current = true;
    setRunning(true);
    const next = nextCommandAttempt(attemptRef.current, fingerprint, () => newIdempotencyKey(prefix));
    if (next.blocked) {
      lock.current = false;
      setRunning(false);
      return "blocked";
    }
    replace(next.attempt);
    try {
      await work(next.attempt.key);
      replace(null);
      return "ok";
    } catch (error) {
      replace(settleCommandAttempt(next.attempt, commandOutcome(error)));
      throw error;
    } finally {
      lock.current = false;
      setRunning(false);
    }
  };
  return { attempt, running, run };
}

/** 待修池（D-0058）。同一路由按型号填写修好件数和废品件数。 */
export default function CyclePoolPage() {
  const { message } = App.useApp();
  const access = accessFrom(readPermissionMap());
  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [poolPage, setPoolPage] = useState(1);
  const [recordPage, setRecordPage] = useState(1);
  const [summaryPage, setSummaryPage] = useState(1);
  const [poolItems, setPoolItems] = useState<RepairPoolItem[]>([]);
  const [poolTotal, setPoolTotal] = useState(0);
  const [poolLoading, setPoolLoading] = useState(false);
  const [poolSettled, setPoolSettled] = useState(false);
  const [records, setRecords] = useState<RepairRecord[]>([]);
  const [recordTotal, setRecordTotal] = useState(0);
  const [recordLoading, setRecordLoading] = useState(false);
  const [summary, setSummary] = useState<RepairSummaryItem[]>([]);
  const [summaryTotal, setSummaryTotal] = useState(0);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [selected, setSelected] = useState<RepairPoolItem | null>(null);
  const [voidTarget, setVoidTarget] = useState<RepairRecord | null>(null);
  const reload = () => setRefresh((value) => value + 1);

  const applySearch = (value: string) => {
    const next = value.trim();
    if ([...next].length > 128) {
      message.warning("搜索词最长 128 字");
      return;
    }
    setQ(next);
    setPoolPage(1);
    setRecordPage(1);
    setSummaryPage(1);
  };

  useEffect(() => {
    if (!access.pool) return;
    let cancel = false;
    setPoolLoading(true);
    setPoolSettled(false);
    setPoolItems([]);
    setPoolTotal(0);
    void listRepairPool({ page: poolPage, page_size: PAGE_SIZE, ...(q ? { q } : {}) })
      .then((data) => {
        if (cancel) return;
        setPoolItems(data.items);
        setPoolTotal(data.total);
      })
      .catch((error: unknown) => {
        if (cancel) return;
        setPoolItems([]);
        setPoolTotal(0);
        message.error(detailOf(error));
      })
      .finally(() => {
        if (!cancel) {
          setPoolLoading(false);
          setPoolSettled(true);
        }
      });
    return () => { cancel = true; };
  }, [access.pool, message, poolPage, q, refresh]);

  useEffect(() => {
    if (!poolSettled || !selected) return;
    const next = poolItems.find((item) => item.part_id === selected.part_id) ?? null;
    if (!next) setSelected(null);
    else if (
      next.pending_qty !== selected.pending_qty
      || next.pn !== selected.pn
      || next.description !== selected.description
    ) setSelected(next);
  }, [poolItems, poolSettled, selected]);

  useEffect(() => {
    if (!access.records) return;
    let cancel = false;
    setRecordLoading(true);
    setRecords([]);
    setRecordTotal(0);
    void listRepairRecords({ page: recordPage, page_size: PAGE_SIZE, ...(q ? { q } : {}) })
      .then((data) => {
        if (cancel) return;
        setRecords(data.items);
        setRecordTotal(data.total);
      })
      .catch((error: unknown) => {
        if (cancel) return;
        setRecords([]);
        setRecordTotal(0);
        message.error(detailOf(error));
      })
      .finally(() => { if (!cancel) setRecordLoading(false); });
    return () => { cancel = true; };
  }, [access.records, message, q, recordPage, refresh]);

  useEffect(() => {
    if (!access.summary) return;
    let cancel = false;
    setSummaryLoading(true);
    setSummary([]);
    setSummaryTotal(0);
    void listRepairSummary({ page: summaryPage, page_size: PAGE_SIZE, ...(q ? { q } : {}) })
      .then((data) => {
        if (cancel) return;
        setSummary(data.items);
        setSummaryTotal(data.total);
      })
      .catch((error: unknown) => {
        if (cancel) return;
        setSummary([]);
        setSummaryTotal(0);
        message.error(detailOf(error));
      })
      .finally(() => { if (!cancel) setSummaryLoading(false); });
    return () => { cancel = true; };
  }, [access.summary, message, q, refresh, summaryPage]);

  const subtitle = access.pool
    ? "按型号填写修好件数和废品件数。修好的交给商城，这里不填价格。"
    : "每个型号的待修、已上电商和废品。这里不能填写结果。";

  const openVoid = (row: RepairRecord) => setVoidTarget(row);
  const voidRecord = voidTarget
    ? records.find((item) => item.record_id === voidTarget.record_id) ?? voidTarget
    : null;

  return (
    <div>
      <PageHeader title="待修池" subtitle={access.pool || access.summary ? subtitle : "当前账号不能使用坏件循环。"} />
      {!access.pool && !access.summary ? (
        <Alert type="error" showIcon message="无权访问坏件循环页面" />
      ) : (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Space>
            <Input
              allowClear
              aria-label="搜索 PN"
              placeholder="搜索 PN"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onPressEnter={() => applySearch(draft)}
              style={{ width: 280 }}
            />
            <Button autoInsertSpace={false} onClick={() => applySearch(draft)}>搜索</Button>
          </Space>
          {access.pool && (
            <PoolCard
              items={poolItems}
              total={poolTotal}
              page={poolPage}
              loading={poolLoading}
              selected={selected}
              onPage={setPoolPage}
              onSelect={setSelected}
              onSaved={reload}
            />
          )}
          {access.summary && (
            <SummaryCard
              items={summary}
              total={summaryTotal}
              page={summaryPage}
              loading={summaryLoading}
              onPage={setSummaryPage}
            />
          )}
          {access.records && (
            <RecordsCard
              items={records}
              total={recordTotal}
              page={recordPage}
              loading={recordLoading}
              detail={access.detail}
              voidAny={access.voidAny}
              refreshToken={refresh}
              onPage={setRecordPage}
              onVoid={openVoid}
              onChanged={reload}
            />
          )}
        </Space>
      )}
      {voidRecord && (
        <Modal
          title={`作废 ${voidRecord.record_no}`}
          open
          onCancel={() => setVoidTarget(null)}
          footer={null}
          destroyOnHidden
        >
          <VoidEditor
            recordId={voidRecord.record_id}
            version={voidRecord.version}
            intent={null}
            onDone={() => {
              setVoidTarget(null);
              reload();
            }}
          />
        </Modal>
      )}
    </div>
  );
}

function PoolCard({
  items,
  total,
  page,
  loading,
  selected,
  onPage,
  onSelect,
  onSaved,
}: {
  items: RepairPoolItem[];
  total: number;
  page: number;
  loading: boolean;
  selected: RepairPoolItem | null;
  onPage: (page: number) => void;
  onSelect: (row: RepairPoolItem | null) => void;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  // 结果未知的提交键放在这张卡片上。搜索或翻页会卸掉下面的表单，不能把键一起卸掉。
  const { attempt, running, run } = useCommandRunner("repair-submit");
  const columns: ColumnsType<RepairPoolItem> = [
    { title: "PN", dataIndex: "pn" },
    { title: "描述", dataIndex: "description", render: (value: string | null) => textOrDash(value) },
    { title: "待修件数", dataIndex: "pending_qty" },
  ];
  const submit = async (row: RepairPoolItem, fixedQty: number, scrapQty: number) => {
    const fingerprint = JSON.stringify({ part_id: row.part_id, fixed_qty: fixedQty, scrap_qty: scrapQty });
    try {
      const result = await run(fingerprint, (key) => submitRepairRecord({
        part_id: row.part_id,
        fixed_qty: fixedQty,
        scrap_qty: scrapQty,
      }, key));
      if (result === "busy") return false;
      if (result === "blocked") {
        message.warning(UNKNOWN_ATTEMPT_MESSAGE);
        return false;
      }
      message.success(`已记下修好 ${fixedQty} 件、废品 ${scrapQty} 件`);
      onSaved();
      return true;
    } catch (error) {
      message.error(detailOf(error));
      return false;
    }
  };
  return (
    <Card size="small" title="待修">
      <Table
        rowKey="part_id"
        loading={loading}
        columns={columns}
        dataSource={items}
        locale={{ emptyText: "没有待修件数" }}
        pagination={{ current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false, onChange: onPage }}
        rowSelection={{
          type: "radio",
          selectedRowKeys: selected ? [selected.part_id] : [],
          onChange: (_keys, rows) => onSelect(rows[0] ?? null),
        }}
      />
      {attempt?.unknown && <Alert type="warning" showIcon message={UNKNOWN_ATTEMPT_MESSAGE} style={{ marginTop: 16 }} />}
      {selected && (
        <ResultForm
          row={selected}
          running={running}
          onSubmit={(fixedQty, scrapQty) => submit(selected, fixedQty, scrapQty)}
        />
      )}
    </Card>
  );
}

function ResultForm({
  row,
  running,
  onSubmit,
}: {
  row: RepairPoolItem;
  running: boolean;
  onSubmit: (fixedQty: number, scrapQty: number) => Promise<boolean>;
}) {
  const { message } = App.useApp();
  const [partId, setPartId] = useState(row.part_id);
  const [fixed, setFixed] = useState<number | null>(null);
  const [scrap, setScrap] = useState<number | null>(null);
  if (partId !== row.part_id) {
    setPartId(row.part_id);
    setFixed(null);
    setScrap(null);
  }
  const touched = fixed !== null || scrap !== null;
  const qtyError = touched ? repairQtyError(fixed ?? 0, scrap ?? 0, row.pending_qty) : null;

  const submit = async () => {
    const fixedQty = fixed ?? 0;
    const scrapQty = scrap ?? 0;
    const problem = repairQtyError(fixedQty, scrapQty, row.pending_qty);
    if (!touched || problem) {
      message.warning(problem ?? "请填写修好件数或废品件数");
      return;
    }
    if (await onSubmit(fixedQty, scrapQty)) {
      setFixed(null);
      setScrap(null);
    }
  };

  return (
    <Space direction="vertical" style={{ width: "100%", marginTop: 16 }}>
      <Typography.Text>
        {`正在填写 ${row.pn}，待修 ${row.pending_qty} 件`}
      </Typography.Text>
      <Space wrap>
        <label>
          修好件数
          <InputNumber
            aria-label="修好件数"
            min={0}
            max={1000}
            precision={0}
            value={fixed}
            onChange={(value) => setFixed(asQty(value))}
            style={{ marginLeft: 8 }}
          />
        </label>
        <label>
          废品件数
          <InputNumber
            aria-label="废品件数"
            min={0}
            max={1000}
            precision={0}
            value={scrap}
            onChange={(value) => setScrap(asQty(value))}
            style={{ marginLeft: 8 }}
          />
        </label>
        <Button
          type="primary"
          disabled={running || !touched || qtyError !== null}
          onClick={() => { void submit(); }}
        >
          提交结果
        </Button>
      </Space>
      {qtyError && <Alert type="warning" showIcon message={qtyError} />}
    </Space>
  );
}

function SummaryCard({
  items,
  total,
  page,
  loading,
  onPage,
}: {
  items: RepairSummaryItem[];
  total: number;
  page: number;
  loading: boolean;
  onPage: (page: number) => void;
}) {
  const columns: ColumnsType<RepairSummaryItem> = [
    { title: "PN", dataIndex: "pn" },
    { title: "描述", dataIndex: "description", render: (value: string | null) => textOrDash(value) },
    { title: "待修", dataIndex: "pending_qty" },
    { title: "已上电商", dataIndex: "shop_qty" },
    { title: "废品", dataIndex: "scrap_qty" },
  ];
  return (
    <Card size="small" title="采购数量">
      <Table
        rowKey="part_id"
        loading={loading}
        columns={columns}
        dataSource={items}
        locale={{ emptyText: "没有循环数量" }}
        pagination={{ current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false, onChange: onPage }}
      />
    </Card>
  );
}

function RecordsCard({
  items,
  total,
  page,
  loading,
  detail,
  voidAny,
  refreshToken,
  onPage,
  onVoid,
  onChanged,
}: {
  items: RepairRecord[];
  total: number;
  page: number;
  loading: boolean;
  detail: boolean;
  voidAny: boolean;
  refreshToken: number;
  onPage: (page: number) => void;
  onVoid: (row: RepairRecord) => void;
  onChanged: () => void;
}) {
  const columns: ColumnsType<RepairRecord> = [
    { title: "单号", dataIndex: "record_no" },
    { title: "PN", dataIndex: "pn" },
    { title: "描述", dataIndex: "description", render: (value: string | null) => textOrDash(value) },
    { title: "修好", dataIndex: "fixed_qty" },
    { title: "废品", dataIndex: "scrap_qty" },
    { title: "状态", dataIndex: "status", render: (value: string) => recordStatusText(value) },
    {
      title: "时间",
      dataIndex: "created_at",
      render: (value: string) => formatBusinessDateTime(value),
    },
    {
      title: "操作",
      render: (_value, row) => {
        if (voidAny) return row.status === "voided" ? textOrDash(row.void_reason) : "见明细";
        if (row.status !== "active") return textOrDash(row.void_reason);
        if (!createdOnBusinessToday(row.created_at)) {
          return <Typography.Text type="secondary">已超过当天，只有主管可以作废</Typography.Text>;
        }
        return <Button type="link" onClick={() => onVoid(row)}>作废</Button>;
      },
    },
  ];
  return (
    <Card size="small" title={detail ? "全部记录" : "我的记录"}>
      <Typography.Paragraph type="secondary">
        {detail
          ? "展开后可以看到扣自哪些返还行，以及推送状态。"
          : "这里只有自己填写的记录。当天的有效记录可以作废。"}
      </Typography.Paragraph>
      <Table
        rowKey="record_id"
        loading={loading}
        columns={columns}
        dataSource={items}
        locale={{ emptyText: "没有记录" }}
        pagination={{ current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false, onChange: onPage }}
        expandable={detail ? {
          expandedRowRender: (row) => (
            <RecordDetail record={row} refreshToken={refreshToken} onChanged={onChanged} />
          ),
          expandIcon: ({ expanded, onExpand, record }) => (
            <Button type="link" onClick={(event) => onExpand(record, event)}>
              {expanded ? "收起明细" : "明细"}
            </Button>
          ),
        } : undefined}
      />
    </Card>
  );
}

function RecordDetail({
  record,
  refreshToken,
  onChanged,
}: {
  record: RepairRecord;
  refreshToken: number;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<RepairRecordDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancel = false;
    void getRepairRecord(record.record_id)
      .then((data) => {
        if (cancel) return;
        setDetail(data);
        setLoadError(null);
      })
      .catch((error: unknown) => {
        if (!cancel) setLoadError(detailOf(error));
      });
    return () => { cancel = true; };
  }, [record.record_id, record.version, refreshToken, tick]);

  if (!detail && !loadError) return <Spin />;
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {loadError && <Alert type="error" showIcon message={loadError} />}
      {detail && <IntentBlock intent={detail.sell_intent} />}
      {detail && (
        <Table
          size="small"
          pagination={false}
          rowKey="receipt_id"
          dataSource={detail.sources}
          locale={{ emptyText: "没有扣数来源" }}
          columns={[
            { title: "返还行", dataIndex: "receipt_id" },
            { title: "扣数", dataIndex: "qty" },
            {
              title: "发生时间",
              dataIndex: "occurred_at",
              render: (value: string | null) => formatBusinessDateTime(value),
            },
            { title: "行数量", dataIndex: "line_qty" },
            { title: "件况", dataIndex: "test_result", render: (value: string | null) => textOrDash(value) },
            { title: "项目", dataIndex: "project_id", render: (value: string | null) => textOrDash(value) },
          ]}
        />
      )}
      {detail?.status === "active" && (
        <VoidEditor
          recordId={detail.record_id}
          version={detail.version}
          intent={detail.sell_intent}
          onDone={() => {
            setTick((value) => value + 1);
            onChanged();
          }}
        />
      )}
      {detail?.status === "voided" && (
        <Typography.Text>{`作废原因：${textOrDash(detail.void_reason)}`}</Typography.Text>
      )}
      {detail?.sell_intent?.shop_closeout === "awaiting_shop_reversal" && (
        <AttestControl
          recordId={detail.record_id}
          version={detail.version}
          intent={detail.sell_intent}
          onDone={() => {
            setTick((value) => value + 1);
            onChanged();
          }}
        />
      )}
    </Space>
  );
}

function IntentBlock({ intent }: { intent: RepairSellIntent | null }) {
  if (!intent) return <Typography.Text type="secondary">没有推送</Typography.Text>;
  return (
    <Descriptions size="small" column={2} title="推送状态">
      <Descriptions.Item label="状态">{intentStatusText(intent.status)}</Descriptions.Item>
      <Descriptions.Item label="deliveryId">{intent.delivery_id}</Descriptions.Item>
      <Descriptions.Item label="unitId">{intent.unit_id}</Descriptions.Item>
      <Descriptions.Item label="件数">{intent.qty}</Descriptions.Item>
      <Descriptions.Item label="尝试次数">{intent.attempts}</Descriptions.Item>
      <Descriptions.Item label="block_code">{textOrDash(intent.block_code)}</Descriptions.Item>
      <Descriptions.Item label="last_error_code">{textOrDash(intent.last_error_code)}</Descriptions.Item>
      <Descriptions.Item label="回执">{textOrDash(intent.receipt_status)}</Descriptions.Item>
      <Descriptions.Item label="下次尝试">{formatBusinessDateTime(intent.next_attempt_at)}</Descriptions.Item>
    </Descriptions>
  );
}

function VoidEditor({
  recordId,
  version,
  intent,
  onDone,
}: {
  recordId: string;
  version: number;
  intent: RepairSellIntent | null;
  onDone: () => void;
}) {
  const { message } = App.useApp();
  const [reason, setReason] = useState("");
  const [checked, setChecked] = useState(false);
  const [serverDetail, setServerDetail] = useState<string | null>(null);
  const { attempt, running, run } = useCommandRunner("repair-void");
  const rule: ShopStockCheck = visibleShopStockCheck(intent, serverDetail);
  const reasonError = reason.trim() ? voidReasonError(reason) : null;

  const submit = async () => {
    const problem = voidReasonError(reason);
    if (problem) {
      message.warning(problem);
      return;
    }
    if (rule === "required" && !checked) {
      message.warning(SHOP_STOCK_REQUIRED_DETAIL);
      return;
    }
    const manual = rule === "required" && checked;
    const body = {
      expected_version: version,
      reason: reason.trim(),
      shop_stock_manually_reduced: manual,
    };
    const fingerprint = JSON.stringify({ action: "void", record_id: recordId, body });
    try {
      const result = await run(fingerprint, (key) => voidRepairRecord(recordId, body, key));
      if (result === "busy") return;
      if (result === "blocked") {
        message.warning(UNKNOWN_ATTEMPT_MESSAGE);
        return;
      }
      message.success("已作废");
      onDone();
    } catch (error) {
      const text = detailOf(error);
      if (text === SHOP_STOCK_REQUIRED_DETAIL || text === SHOP_STOCK_FORBIDDEN_DETAIL) {
        setServerDetail(text);
        if (text === SHOP_STOCK_FORBIDDEN_DETAIL) setChecked(false);
      }
      message.error(text);
    }
  };

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      {intent?.status === "delivering" && (
        <Alert type="info" showIcon message="推送进行中，请稍后再作废" />
      )}
      {rule === "forbid" && <Alert type="warning" showIcon message={SHOP_STOCK_FORBIDDEN_DETAIL} />}
      {attempt?.unknown && <Alert type="warning" showIcon message={UNKNOWN_ATTEMPT_MESSAGE} />}
      <label style={{ display: "block" }}>
        作废原因
        <Input.TextArea
          aria-label="作废原因"
          value={reason}
          maxLength={256}
          rows={2}
          onChange={(event) => setReason(event.target.value)}
          style={{ marginTop: 8 }}
        />
      </label>
      {reasonError && <Alert type="warning" showIcon message={reasonError} />}
      {rule === "required" && (
        <Checkbox checked={checked} onChange={(event) => setChecked(event.target.checked)}>
          商城已手工减库存
        </Checkbox>
      )}
      <Button danger disabled={running} onClick={() => { void submit(); }}>确认作废</Button>
    </Space>
  );
}

function AttestControl({
  recordId,
  version,
  intent,
  onDone,
}: {
  recordId: string;
  version: number;
  intent: RepairSellIntent;
  onDone: () => void;
}) {
  const { message } = App.useApp();
  const { attempt, running, run } = useCommandRunner("repair-attest");
  const submit = async () => {
    const fingerprint = JSON.stringify({
      action: "attest_shop_adjustment",
      record_id: recordId,
      expected_version: version,
    });
    try {
      const result = await run(
        fingerprint,
        (key) => attestShopAdjustment(recordId, { expected_version: version }, key),
      );
      if (result === "busy") return;
      if (result === "blocked") {
        message.warning(UNKNOWN_ATTEMPT_MESSAGE);
        return;
      }
      message.success("已登记减库存");
      onDone();
    } catch (error) {
      message.error(detailOf(error));
    }
  };
  return (
    <Space direction="vertical">
      {attempt?.unknown && <Alert type="warning" showIcon message={UNKNOWN_ATTEMPT_MESSAGE} />}
      <Alert
        type="warning"
        showIcon
        message="请到商城按 deliveryId 和件数盘点减回、核对在手，再登记。"
        description={`deliveryId ${intent.delivery_id}，unitId ${intent.unit_id}，件数 ${intent.qty}`}
      />
      <Button disabled={running} onClick={() => { void submit(); }}>登记已减库存</Button>
    </Space>
  );
}
