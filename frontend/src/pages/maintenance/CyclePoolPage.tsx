import { useEffect, useMemo, useState } from "react";
import {
  App,
  Button,
  Card,
  Drawer,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Table,
  Tabs,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import PageHeader from "../../components/PageHeader";
import { readPermissionMap } from "../../nav";
import { listWorkItems } from "../../api/workItems";
import {
  claimUnit,
  closeUnit,
  completeRepair,
  completeTest,
  dispatchUnit,
  intakeExternal,
  listUnits,
  markSold,
  outsourceReturn,
  selfPass,
  sellIntent,
  wipCounts,
  withdrawUnit,
  type CycleUnit,
  type SellIntentStatus,
} from "../../api/cyclePool";

const STATIONS: { key: string; label: string; pages: string[] }[] = [
  { key: "pool", label: "回收", pages: ["page_cycle_recycle", "page_cycle_manage"] },
  { key: "outsource", label: "委外", pages: ["page_cycle_recycle", "page_cycle_manage"] },
  { key: "ready", label: "待处置", pages: ["page_cycle_recycle", "page_cycle_manage"] },
  { key: "testing", label: "检测", pages: ["page_cycle_test", "page_cycle_manage"] },
  { key: "repairing", label: "维修", pages: ["page_cycle_repair", "page_cycle_manage"] },
  { key: "closed", label: "已结案", pages: ["page_cycle_manage"] },
];

function detailOf(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : "操作没有完成";
}

export default function CyclePoolPage() {
  const { message } = App.useApp();
  const perms = readPermissionMap();
  const manage = !!perms.page_cycle_manage;
  const recycle = !!perms.page_cycle_recycle;
  const canTest = !!perms.page_cycle_test;
  const canRepair = !!perms.page_cycle_repair;
  const canWip = !!perms.page_cycle_wip;
  const stations = useMemo(() => STATIONS.filter((item) => item.pages.some((key) => (
    (key === "page_cycle_recycle" && (recycle || manage))
    || (key === "page_cycle_test" && (canTest || manage))
    || (key === "page_cycle_repair" && (canRepair || manage))
    || (key === "page_cycle_manage" && manage)
    || (key === "page_cycle_wip" && canWip)
  ))), [recycle, canTest, canRepair, manage, canWip]);
  const [station, setStation] = useState(stations[0]?.key ?? "pool");
  const [rows, setRows] = useState<CycleUnit[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [current, setCurrent] = useState<CycleUnit | null>(null);
  const [intent, setIntent] = useState<SellIntentStatus | null>(null);
  const [poolVersions, setPoolVersions] = useState<Record<number, number>>({});
  const [claimLoad, setClaimLoad] = useState<"off" | "loading" | "ready" | "failed">("off");
  const [claimNote, setClaimNote] = useState<string | null>(null);
  const [wipPart, setWipPart] = useState("");
  const [wip, setWip] = useState<string>("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (!stations.some((item) => item.key === station)) return;
    let cancel = false;
    setLoading(true);
    void (async () => {
      try {
        const data = await listUnits({ station, page, page_size: 20 });
        if (cancel) return;
        setRows(data.items);
        setTotal(data.total);
        const mayClaim = (station === "testing" && canTest) || (station === "repairing" && canRepair);
        if (!mayClaim) {
          setPoolVersions({});
          setClaimLoad("off");
          setClaimNote(null);
        } else {
          const subject = station === "testing" ? "cycle_test" : "cycle_repair";
          setClaimLoad("loading");
          try {
            const versions = await loadPoolVersions(subject, data.items.map((item) => item.id));
            if (cancel) return;
            setPoolVersions(versions);
            setClaimLoad("ready");
            setClaimNote(null);
          } catch (error) {
            if (cancel) return;
            setPoolVersions({});
            setClaimLoad("failed");
            setClaimNote(detailOf(error));
            message.warning("待办版本没有取到，领取暂时不可用");
          }
        }
      } catch (error) {
        if (!cancel) message.error(detailOf(error));
      } finally {
        if (!cancel) setLoading(false);
      }
    })();
    return () => { cancel = true; };
  }, [canRepair, canTest, manage, message, page, recycle, refresh, station, stations]);

  const run = async (work: () => Promise<unknown>) => {
    try {
      await work();
      message.success("已保存");
      setCurrent(null);
      setRefresh((value) => value + 1);
    } catch (error) {
      message.warning(detailOf(error));
    }
  };

  const columns: ColumnsType<CycleUnit> = useMemo(() => {
    const base: ColumnsType<CycleUnit> = [
      { title: "PN", dataIndex: "pn" },
      { title: "工位", dataIndex: "station" },
      { title: "去向", dataIndex: "outcome" },
      { title: "件况", dataIndex: "condition_bucket" },
      { title: "负责人", dataIndex: "assignee_username" },
      { title: "版本", dataIndex: "version" },
    ];
    if (manage) {
      base.push(
        { title: "来源单", dataIndex: "source_receipt_id" },
        { title: "供应商", dataIndex: "vendor_name" },
        { title: "序列号", dataIndex: "serial_number" },
      );
    }
    base.push({
      title: "操作",
      render: (_, row) => (
        <Button type="link" onClick={() => { setIntent(null); setCurrent(row); }}>
          处理
        </Button>
      ),
    });
    return base;
  }, [manage]);

  const showWip = manage || canWip;

  return (
    <div>
      <PageHeader title="维修循环" subtitle="一件一条。去电商后的价格和帖子在商城，不在这里填写。" />
      {showWip && (
        <Card size="small" style={{ marginBottom: 16 }} title="在制数量">
          <Space>
            <Input
              placeholder="part_id，逗号分隔"
              value={wipPart}
              onChange={(event) => setWipPart(event.target.value)}
              style={{ width: 280 }}
            />
            <Button onClick={async () => {
              const ids = wipPart.split(",").map((item) => Number(item.trim())).filter((item) => item > 0);
              try {
                setWip(JSON.stringify(await wipCounts(ids)));
              } catch (error) {
                message.warning(detailOf(error));
              }
            }}>查询</Button>
          </Space>
          {wip && <Typography.Paragraph><pre>{wip}</pre></Typography.Paragraph>}
        </Card>
      )}
      {(manage || recycle) && (
        <Card size="small" style={{ marginBottom: 16 }} title="入池">
          <Space wrap>
            <IntakeForm onSubmit={(body) => run(() => intakeExternal(body))} />
          </Space>
        </Card>
      )}
      <Tabs
        activeKey={station}
        onChange={(key) => { setStation(key); setPage(1); }}
        items={stations.map((item) => ({ key: item.key, label: item.label }))}
      />
      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={{ current: page, pageSize: 20, total, onChange: setPage }}
      />
      <Drawer
        title={current ? current.pn : ""}
        open={current !== null}
        onClose={() => setCurrent(null)}
        width={420}
      >
        {current && (
          <UnitActions
            unit={current}
            manage={manage}
            canClaim={(current.station === "testing" && canTest) || (current.station === "repairing" && canRepair)}
            claimLoad={claimLoad}
            claimNote={claimNote}
            itemVersion={poolVersions[current.id]}
            intent={intent}
            onIntent={async () => {
              try {
                setIntent(await sellIntent(current.id));
              } catch (error) {
                message.warning(detailOf(error));
              }
            }}
            onRun={run}
          />
        )}
      </Drawer>
    </div>
  );
}

async function loadPoolVersions(subject: string, unitIds: number[]) {
  const wanted = new Set(unitIds);
  const versions: Record<number, number> = {};
  let page = 1;
  let seen = 0;
  let total = 0;
  do {
    const pool = await listWorkItems({ tab: "pool", subject_type: subject, page, page_size: 100 });
    total = pool.total;
    seen += pool.items.length;
    for (const item of pool.items) {
      if (wanted.has(item.subject_id)) versions[item.subject_id] = item.version;
    }
    if (pool.items.length === 0) break;
    page += 1;
  } while (seen < total && Object.keys(versions).length < wanted.size && page <= 50);
  return versions;
}

function compact(body: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(body).filter(([, value]) => value !== undefined && value !== null && value !== ""),
  );
}

function IntakeForm({ onSubmit }: { onSubmit: (body: object) => void }) {
  return (
    <Form layout="inline" onFinish={(values) => onSubmit(compact(values))}>
      <Form.Item name="part_id" label="part_id"><InputNumber min={1} /></Form.Item>
      <Form.Item name="pn" label="PN"><Input /></Form.Item>
      <Form.Item name="qty" label="数量" rules={[{ required: true }]}><InputNumber min={1} max={1000} /></Form.Item>
      <Button htmlType="submit">外部入池</Button>
    </Form>
  );
}

function UnitActions({
  unit,
  manage,
  canClaim,
  claimLoad,
  claimNote,
  itemVersion,
  intent,
  onIntent,
  onRun,
}: {
  unit: CycleUnit;
  manage: boolean;
  canClaim: boolean;
  claimLoad: "off" | "loading" | "ready" | "failed";
  claimNote: string | null;
  itemVersion?: number;
  intent: SellIntentStatus | null;
  onIntent: () => void;
  onRun: (work: () => Promise<unknown>) => Promise<void>;
}) {
  const version = unit.version;
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Typography.Text>版本 {version}</Typography.Text>
      {unit.station === "pool" && (
        <Button onClick={() => onRun(() => selfPass(unit.id, version))}>自测通过</Button>
      )}
      {unit.station === "pool" && (
        <Button onClick={() => onRun(() => withdrawUnit(unit.id, { expected_version: version }))}>撤回</Button>
      )}
      {(unit.station === "pool" || unit.station === "ready") && (
        <DispatchForm onSubmit={(body) => onRun(() => dispatchUnit(unit.id, { expected_version: version, ...body }))} />
      )}
      {(unit.station === "pool" || unit.station === "ready") && (
        <CloseForm onSubmit={(body) => onRun(() => closeUnit(unit.id, { expected_version: version, ...body }))} />
      )}
      {unit.station === "outsource" && (
        <ReturnForm onSubmit={(body) => onRun(() => outsourceReturn(unit.id, { expected_version: version, ...body }))} />
      )}
      {canClaim && (unit.station === "testing" || unit.station === "repairing") && !unit.assignee_username && (
        itemVersion === undefined ? (
          <Typography.Text type="secondary">
            {claimLoad === "failed"
              ? `待办版本没有取到，暂时不能领取：${claimNote ?? "请稍后重试"}`
              : claimLoad === "loading"
                ? "正在读取待办版本"
                : "没有可领取的待办"}
          </Typography.Text>
        ) : (
          <Button
            onClick={() => onRun(() => claimUnit(unit.id, { expected_version: version, item_version: itemVersion }))}
          >领取</Button>
        )
      )}
      {unit.station === "testing" && (
        <ResultForm
          kind="test"
          onSubmit={(body) => onRun(() => completeTest(unit.id, { expected_version: version, ...body }))}
        />
      )}
      {unit.station === "repairing" && (
        <ResultForm
          kind="repair"
          onSubmit={(body) => onRun(() => completeRepair(unit.id, { expected_version: version, ...body }))}
        />
      )}
      {manage && unit.station === "closed" && unit.outcome === "to_shop" && (
        <Button onClick={() => onRun(() => markSold(unit.id, version))}>标记售出</Button>
      )}
      {manage && (
        <Button onClick={onIntent}>投递状态</Button>
      )}
      {intent && (
        <pre>{JSON.stringify({
          status: intent.status,
          deliveryId: intent.deliveryId,
          blockCode: intent.blockCode,
          nextAttemptAt: intent.nextAttemptAt,
        }, null, 2)}</pre>
      )}
    </Space>
  );
}

function DispatchForm({ onSubmit }: { onSubmit: (body: object) => void }) {
  return (
    <Form layout="vertical" onFinish={(values) => onSubmit(compact(values))}>
      <Form.Item name="to" label="派工" rules={[{ required: true }]}>
        <Select options={[
          { value: "testing", label: "检测" },
          { value: "repairing", label: "维修" },
          { value: "outsource", label: "委外" },
        ]} />
      </Form.Item>
      <Form.Item name="assignee_username" label="检测/维修账号"><Input /></Form.Item>
      <Form.Item name="vendor_name" label="委外供应商"><Input /></Form.Item>
      <Button htmlType="submit">派工</Button>
    </Form>
  );
}

function CloseForm({ onSubmit }: { onSubmit: (body: object) => void }) {
  return (
    <Form layout="vertical" onFinish={(values) => onSubmit(compact(values))}>
      <Form.Item name="outcome" label="结案" rules={[{ required: true }]}>
        <Select options={[
          { value: "to_shop", label: "去电商" },
          { value: "to_company", label: "回公司" },
          { value: "scrap", label: "报废" },
        ]} />
      </Form.Item>
      <Form.Item name="condition_bucket" label="去电商时的好坏">
        <Select allowClear options={[{ value: "good", label: "好" }, { value: "bad", label: "坏" }]} />
      </Form.Item>
      <Button htmlType="submit">结案</Button>
    </Form>
  );
}

function ReturnForm({ onSubmit }: { onSubmit: (body: object) => void }) {
  return (
    <Form layout="vertical" onFinish={(values) => onSubmit(compact(values))}>
      <Form.Item name="next" label="委外回来" rules={[{ required: true }]}>
        <Select options={[
          { value: "testing", label: "检测" },
          { value: "repairing", label: "维修" },
          { value: "to_shop", label: "去电商" },
          { value: "to_company", label: "回公司" },
          { value: "scrap", label: "报废" },
        ]} />
      </Form.Item>
      <Form.Item name="condition_bucket" label="去电商时的好坏">
        <Select allowClear options={[{ value: "good", label: "好" }, { value: "bad", label: "坏" }]} />
      </Form.Item>
      <Button htmlType="submit">登记回来</Button>
    </Form>
  );
}

export function resultChoices(kind: "test" | "repair", result?: string) {
  if (kind === "test" && result === "good") {
    return { options: [{ value: "ready", label: "待处置" }], required: false };
  }
  if (kind === "test" && result === "bad") {
    return {
      options: [{ value: "repairing", label: "送修" }, { value: "scrap", label: "报废" }],
      required: true,
    };
  }
  if (kind === "repair" && result === "fixed") {
    return {
      options: [{ value: "testing", label: "再测" }, { value: "ready", label: "不测" }],
      required: true,
    };
  }
  if (kind === "repair" && result === "unfixed") {
    return { options: [{ value: "scrap", label: "报废" }], required: true };
  }
  return { options: [] as { value: string; label: string }[], required: false };
}

function ResultForm({
  kind,
  onSubmit,
}: {
  kind: "test" | "repair";
  onSubmit: (body: object) => void;
}) {
  const [form] = Form.useForm();
  const result = Form.useWatch("result", form) as string | undefined;
  const choice = resultChoices(kind, result);
  return (
    <Form
      form={form}
      layout="vertical"
      onFinish={(values) => onSubmit(compact({ result: values.result, next: values.next }))}
    >
      <Form.Item name="result" label="结果" rules={[{ required: true, message: "请选择结果" }]}>
        <Select
          options={kind === "test"
            ? [{ value: "good", label: "好" }, { value: "bad", label: "坏" }]
            : [{ value: "fixed", label: "修好" }, { value: "unfixed", label: "没修好" }]}
          onChange={() => form.setFieldValue("next", undefined)}
        />
      </Form.Item>
      <Form.Item
        name="next"
        label="下一步"
        rules={choice.required ? [{ required: true, message: "请选择下一步" }] : []}
      >
        <Select allowClear={!choice.required} disabled={!result} options={choice.options} />
      </Form.Item>
      <Button htmlType="submit">完成</Button>
    </Form>
  );
}
