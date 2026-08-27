import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Empty,
  Input,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Typography,
} from "antd";
import type { Dayjs } from "dayjs";
import type { BoardProjectRow, CardStatus } from "../../api/maintenanceBossBoard";
import { getBoardProjects, searchBoardProjects } from "../../api/maintenanceBossBoard";
import type { RangePreset } from "../../api/maintenanceWorkbooks";
import {
  RANGE_LABELS,
  applySparePartLines,
  downloadSparePartLines,
} from "../../api/maintenanceWorkbooks";
import ProjectCard from "../../components/maintenance/ProjectCard";
import MaintenanceProjectExportButton from "../../components/maintenance/MaintenanceProjectExportButton";
import WorkbookRoundTrip from "../../components/maintenance/WorkbookRoundTrip";
import { readPermissionMap } from "../../nav";

const { Title, Text } = Typography;
const { RangePicker } = DatePicker;

const PAGE_SIZE = 20;   // 一行 5 张 → 一屏 4 行；下滑续拉（#37）

// missing＝台账未提供项目周期（plan v1.3 R5：期限缺失要以明确状态可见，而非空白）。
// 台账导入生产之前 415 个项目全部 missing——若筛选器没有这一档，整面卡墙会
// 无声全空，用户无从区分「没项目」和「周期未维护」。默认仍是进行中（#37）。
type LifecycleFilter = "ongoing" | "ended" | "missing";
type ProjectSort = "name" | "attention" | "orders" | "known_cost" | "cost_ratio";

/**
 * 维保主页（项目卡墙）——页面定稿两页之一（REQUIREMENTS #33/#34/#35/#37/#38）。
 *
 * 顶部筛选（进行中默认 / 已结束、正常/提醒/报警、模糊搜索）＋ 一行 5 卡下滑无限加载
 * ＋ 全局下载全项目备件行级表（改价补价上传覆盖＝真实源）。
 *
 * 布局（#267）：页头行＝标题副标（左）＋全局操作（右：需求单与同步入口 /
 * 日期区间 / 全项目备件行级表）；筛选行独立一行，只放筛项目的四个控件。
 * 全局操作从筛选行解放出来，轻重分开。
 *
 * 「需关注」不再是独立栏目：超预算即黄/红，直接体现在卡片状态上（#43）。
 */
export function MaintenanceHomePage() {
  const permissions = readPermissionMap();
  const canUpload = !!permissions.action_maintenance_expense_collection_upload;
  const canViewCost = localStorage.getItem("role") === "admin"
    || permissions.data_purchase_cost === true;
  const [lifecycle, setLifecycle] = useState<LifecycleFilter>("ongoing");
  const [status, setStatus] = useState<CardStatus | undefined>();
  const [sort, setSort] = useState<ProjectSort>(() => canViewCost ? "cost_ratio" : "name");
  const [keyword, setKeyword] = useState("");
  const [rows, setRows] = useState<BoardProjectRow[]>([]);
  const [page, setPage] = useState(1);
  const [done, setDone] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rangePreset, setRangePreset] = useState<RangePreset>("this_month");
  const [customRange, setCustomRange] = useState<[Dayjs, Dayjs] | null>(null);
  const sentinel = useRef<HTMLDivElement | null>(null);
  // 并发保护：筛选一变就作废在途请求的结果，避免旧响应盖掉新筛选
  const requestSeq = useRef(0);

  const load = useCallback(
    async (nextPage: number, replace: boolean) => {
      const seq = ++requestSeq.current;
      setLoading(true);
      setError(null);
      try {
        const params = {
          page: nextPage,
          page_size: PAGE_SIZE,
          lifecycle,
          card_status: status,
          sort,
        };
        const resp = keyword.trim()
          ? await searchBoardProjects({ q: keyword.trim(), ...params })
          : await getBoardProjects(params);
        const body = resp.data;
        if (seq !== requestSeq.current) return;      // 已被更新的筛选取代
        setRows((prev) => (replace ? body.rows : [...prev, ...body.rows]));
        setPage(nextPage);
        // 桶行是后端额外置顶、不计入 total 的一行，所以以「本页拿到多少真实行」
        // 判断是否到底，而不是拿 total 做算术（那会少算或多算一页）。
        setDone(body.rows.length < PAGE_SIZE);
      } catch (err) {
        if (seq !== requestSeq.current) return;
        setError(readError(err));
        setDone(true);
      } finally {
        if (seq === requestSeq.current) setLoading(false);
      }
    },
    [lifecycle, status, keyword, sort],
  );

  useEffect(() => {
    void load(1, true);
  }, [load]);

  useEffect(() => {
    const node = sentinel.current;
    if (!node || done) return undefined;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting && !loading) void load(page + 1, false);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [done, loading, page, load]);

  const rangeParams = () => {
    if (rangePreset !== "custom") return { range: rangePreset };
    if (!customRange) throw new Error("请选择自定义日期区间");
    return {
      range: "custom" as const,
      from: customRange[0].format("YYYY-MM-DD"),
      to: customRange[1].format("YYYY-MM-DD"),
    };
  };

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {/* 页头行：左标题副标，右全局操作；窄屏自动换行 */}
      <Row justify="space-between" align="middle" gutter={[16, 12]} wrap>
        <Col flex="auto">
          <Title level={4} style={{ marginBottom: 4 }}>
            维保项目
          </Title>
          <Text type="secondary">
            超预算的项目会变黄、变红；点卡片进项目看明细
          </Text>
        </Col>
        <Col flex="none">
          <Space size={8} align="start" wrap>
            <Link to="/maintenance/demands">
              <Button>需求单与同步</Button>
            </Link>
            <MaintenanceProjectExportButton
              filters={{
                lifecycle,
                sort,
                ...(status ? { card_status: status } : {}),
                ...(keyword.trim() ? { q: keyword.trim() } : {}),
              }}
            />
            <Select
              style={{ width: 110 }}
              value={rangePreset}
              onChange={(value) => setRangePreset(value as RangePreset)}
              options={(Object.keys(RANGE_LABELS) as RangePreset[]).map((key) => ({
                label: RANGE_LABELS[key],
                value: key,
              }))}
            />
            {rangePreset === "custom" ? (
              <RangePicker
                onChange={(value) =>
                  setCustomRange(value as [Dayjs, Dayjs] | null)
                }
              />
            ) : null}
            <WorkbookRoundTrip
              title="全项目备件行级表"
              filename={`spare-part-lines-${rangePreset}.xlsx`}
              canUpload={canUpload}
              hint="下载 → 改价/补价 → 上传覆盖＝真实源"
              onDownload={async () => downloadSparePartLines(rangeParams())}
              onApply={(file) => applySparePartLines(file)}
            />
          </Space>
        </Col>
      </Row>

      {/* 筛选行：只放「筛哪面墙」的控件 */}
      <Card size="small">
        <Space wrap size={12} align="center" style={{ width: "100%" }}>
          <Segmented
            value={lifecycle}
            onChange={(value) => setLifecycle(value as LifecycleFilter)}
            options={[
              { label: "进行中", value: "ongoing" },
              { label: "已结束", value: "ended" },
              { label: "期限缺失", value: "missing" },
            ]}
          />
          <Select
            allowClear
            placeholder="全部状态"
            style={{ width: 140 }}
            value={status}
            onChange={(value) => setStatus(value as CardStatus | undefined)}
            options={[
              { label: "正常", value: "normal" },
              { label: "提醒", value: "warning" },
              { label: "报警", value: "alert" },
            ]}
          />
          <Select
            style={{ width: 150 }}
            value={sort}
            onChange={(value) => setSort(value as ProjectSort)}
            options={[
              ...(canViewCost ? [{ label: "成本率降序", value: "cost_ratio" as const }] : []),
              { label: "项目名称", value: "name" as const },
              ...(canViewCost ? [{ label: "备件成本", value: "known_cost" as const }] : []),
              { label: "订单数", value: "orders" as const },
              ...(canViewCost ? [{ label: "需关注", value: "attention" as const }] : []),
            ]}
          />
          <Input.Search
            allowClear
            placeholder="搜项目名 / XSDD 单号"
            style={{ width: 260 }}
            onSearch={setKeyword}
          />
        </Space>
      </Card>

      {error ? <Alert type="error" showIcon message={error} /> : null}

      <Row gutter={[12, 12]}>
        {rows.map((row) => (
          // 一行 5 张（#37）：24 栅格取 xl=5 ≈ 每行 4.8 张，故用 flex 固定五等分
          <Col key={row.project_id} xs={24} sm={12} md={8} lg={6} xl={5}
               style={{ flex: "0 0 20%", maxWidth: "20%" }}>
            <ProjectCard row={row} />
          </Col>
        ))}
      </Row>

      {!rows.length && !loading ? (
        <Empty
          description={
            lifecycle === "missing"
              ? "没有符合条件的项目"
              : "没有符合条件的项目；若项目台账尚未导入，项目周期无从判定，请切换「期限缺失」查看"
          }
        />
      ) : null}

      <div ref={sentinel} style={{ textAlign: "center", padding: 12 }}>
        {loading ? <Spin /> : null}
        {done && rows.length ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            已到底
          </Text>
        ) : null}
      </div>
    </Space>
  );
}

function readError(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response
    ?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return "项目列表加载失败";
}

export default MaintenanceHomePage;
