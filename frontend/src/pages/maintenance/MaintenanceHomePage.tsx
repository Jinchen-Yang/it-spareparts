import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
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
import WorkbookRoundTrip from "../../components/maintenance/WorkbookRoundTrip";
import { readPermissionMap } from "../../nav";

const { Title, Text } = Typography;
const { RangePicker } = DatePicker;

const PAGE_SIZE = 20;   // 一行 5 张 → 一屏 4 行；下滑续拉（#37）

type LifecycleFilter = "ongoing" | "ended";

/**
 * 维保主页（项目卡墙）——页面定稿两页之一（REQUIREMENTS #33/#34/#35/#37/#38）。
 *
 * 顶部筛选（进行中默认 / 已结束、正常/提醒/报警、模糊搜索）＋ 一行 5 卡下滑无限加载
 * ＋ 全局下载全项目备件行级表（改价补价上传覆盖＝真实源）。
 *
 * 「需关注」不再是独立栏目：超预算即黄/红，直接体现在卡片状态上（#43）。
 */
export function MaintenanceHomePage() {
  const [lifecycle, setLifecycle] = useState<LifecycleFilter>("ongoing");
  const [status, setStatus] = useState<CardStatus | undefined>();
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

  const canUpload = !!readPermissionMap().action_maintenance_expense_collection_upload;

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
    [lifecycle, status, keyword],
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
      <Card size="small">
        <Space wrap size={12} align="center" style={{ width: "100%" }}>
          <Segmented
            value={lifecycle}
            onChange={(value) => setLifecycle(value as LifecycleFilter)}
            options={[
              { label: "进行中", value: "ongoing" },
              { label: "已结束", value: "ended" },
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
          <Input.Search
            allowClear
            placeholder="搜项目名 / XSDD 单号"
            style={{ width: 260 }}
            onSearch={setKeyword}
          />
          <div style={{ marginLeft: "auto" }}>
            <Space size={8} align="center" wrap>
              <Select
                size="small"
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
                  size="small"
                  onChange={(value) =>
                    setCustomRange(value as [Dayjs, Dayjs] | null)
                  }
                />
              ) : null}
              <WorkbookRoundTrip
                size="small"
                title="全项目备件行级表"
                filename={`spare-part-lines-${rangePreset}.xlsx`}
                canUpload={canUpload}
                hint="下载 → 改价/补价 → 上传覆盖＝真实源"
                onDownload={async () => downloadSparePartLines(rangeParams())}
                onApply={(file) => applySparePartLines(file)}
              />
            </Space>
          </div>
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
        <Empty description="没有符合条件的项目" />
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
