import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Alert,
  Badge,
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
  Tag,
  Typography,
} from "antd";
import { DownOutlined, FilterOutlined, UpOutlined } from "@ant-design/icons";
import type { Dayjs } from "dayjs";
import type { BoardProjectRow, CardStatus } from "../../api/maintenanceBossBoard";
import { getBoardProjects, searchBoardProjects } from "../../api/maintenanceBossBoard";
import type { RangePreset } from "../../api/maintenanceWorkbooks";
import {
  RANGE_LABELS,
  applySparePartLines,
  downloadSparePartLines,
  validateSparePartLines,
} from "../../api/maintenanceWorkbooks";
import ProjectCard from "../../components/maintenance/ProjectCard";
import MaintenanceBatchTransferButton from "../../components/maintenance/MaintenanceBatchTransferButton";
import MaintenanceProjectExportButton from "../../components/maintenance/MaintenanceProjectExportButton";
import {
  BOARD_BUSINESS_TYPE_CODES,
  BOARD_BUSINESS_TYPE_LABELS,
  boardBusinessTypeParam,
  type BoardBusinessTypeCode,
} from "../../api/maintenanceBossBoard";
import WorkbookRoundTrip from "../../components/maintenance/WorkbookRoundTrip";
import { readPermissionMap } from "../../nav";

const { Title, Text } = Typography;
const { RangePicker } = DatePicker;

const PAGE_SIZE = 20;   // 一行 5 张 → 一屏 4 行；下滑续拉（#37）
const FILTERS_EXPANDED_KEY = "maintenance.home.filters-expanded";
const STATUS_LABELS: Record<CardStatus, string> = { normal: "正常", warning: "提醒", alert: "报警" };

// missing＝台账未提供项目周期（plan v1.3 R5：期限缺失要以明确状态可见，而非空白）。
// 台账导入生产之前 415 个项目全部 missing——若筛选器没有这一档，整面卡墙会
// 无声全空，用户无从区分「没项目」和「周期未维护」。默认仍是进行中（#37）。
// payment_complete＝回款已完成（2026-09-04 客户反馈：收满回款的项目不用再盯），
// 由合同额+回款推得且优先于三个期限桶；只对持有合同财务权限的账号展示与放行。
type LifecycleFilter = "ongoing" | "ended" | "missing" | "payment_complete";
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
  // 回款已完成桶由合同额+回款推得，属于合同财务数据：无 data_profit 的账号
  // 不展示该页签（后端同样 422 拒绝），也不做期限桶排除。
  const canViewContract = localStorage.getItem("role") === "admin"
    || permissions.data_profit === true;
  const [lifecycle, setLifecycle] = useState<LifecycleFilter>("ongoing");
  // 业务类型（2026-09-08 客户需求；2026-09-16 新增拆改配服务）：与期限状态**叠加**的独立一维。
  // 默认六档全选＝不排除任何项目：生产 648 个项目 647 个未标注，默认排除等于把
  // 卡墙筛空（R5）。用户主动取消勾选才开始收窄。
  const [businessTypes, setBusinessTypes] = useState<BoardBusinessTypeCode[]>(
    () => [...BOARD_BUSINESS_TYPE_CODES],
  );
  const [hiddenByBusinessType, setHiddenByBusinessType] = useState(0);
  const [status, setStatus] = useState<CardStatus | undefined>();
  const [sort, setSort] = useState<ProjectSort>(() => canViewCost ? "cost_ratio" : "name");
  const [keyword, setKeyword] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [filtersExpanded, setFiltersExpanded] = useState(() => {
    try {
      return localStorage.getItem(FILTERS_EXPANDED_KEY) !== "false";
    } catch {
      return true;
    }
  });
  // 全选和空选都不收窄，因此没有业务类型条件胶囊；期限和排序不计入条件数。
  const activeBusinessTypes = boardBusinessTypeParam(businessTypes) === "all" ? [] : businessTypes;
  const activeFilterCount = activeBusinessTypes.length + Number(!!status) + Number(!!keyword.trim());
  const toggleFilters = () => {
    const expanded = !filtersExpanded;
    setFiltersExpanded(expanded);
    try {
      localStorage.setItem(FILTERS_EXPANDED_KEY, String(expanded));
    } catch {
      // 浏览器禁用存储时仍允许折叠，只是不跨访问记忆。
    }
  };
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
          business_type: boardBusinessTypeParam(businessTypes),
        };
        const resp = keyword.trim()
          ? await searchBoardProjects({ q: keyword.trim(), ...params })
          : await getBoardProjects(params);
        const body = resp.data;
        if (seq !== requestSeq.current) return false;      // 已被更新的筛选取代
        setRows((prev) => (replace ? body.rows : [...prev, ...body.rows]));
        setHiddenByBusinessType(body.business_type_hidden ?? 0);
        setPage(nextPage);
        // card_status 在后端按候选页计算后过滤：某页可以 0 命中、下一页仍有命中。
        // 因此必须按候选 total 继续拉，不能用过滤后的 rows.length 提前截断。
        setDone(nextPage * body.page_size >= body.total);
        return true;
      } catch (err) {
        if (seq !== requestSeq.current) return false;
        if (replace) setRows([]);
        setError(readError(err));
        setDone(true);
        return false;
      } finally {
        if (seq === requestSeq.current) setLoading(false);
      }
    },
    [lifecycle, status, keyword, sort, businessTypes],
  );
  // 上传流程跨越“预检 → 人工确认”，期间筛选可能已变化。旧 onApply 闭包只
  // 通过这个 ref 调用当前 render 的 load，避免旧筛选主动成为最新请求。
  const latestLoad = useRef(load);
  latestLoad.current = load;

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
            <MaintenanceBatchTransferButton
              filters={{
                lifecycle,
                sort,
                business_type: boardBusinessTypeParam(businessTypes),
                ...(status ? { card_status: status } : {}),
                ...(keyword.trim() ? { q: keyword.trim() } : {}),
              }}
              onApplied={() => latestLoad.current(1, true)}
            />
            <MaintenanceProjectExportButton
              filters={{
                lifecycle,
                sort,
                business_type: boardBusinessTypeParam(businessTypes),
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
              onValidate={validateSparePartLines}
              onApply={applySparePartLines}
              onAfterApply={() => latestLoad.current(1, true)}
            />
          </Space>
        </Col>
      </Row>

      {/* 期限常驻；折叠只改变控件可见性，不改变筛选与导出参数。 */}
      <Card size="small">
        <Row justify="space-between" align="middle" gutter={[12, 12]}>
          <Col style={{ minWidth: 0, maxWidth: "100%", overflowX: "auto" }}>
          <Segmented
            value={lifecycle}
            onChange={(value) => setLifecycle(value as LifecycleFilter)}
            options={[
              { label: "进行中", value: "ongoing" },
              { label: "已结束", value: "ended" },
              { label: "期限缺失", value: "missing" },
              ...(canViewContract
                ? [{ label: "回款已完成", value: "payment_complete" as const }]
                : []),
            ]}
          />
          </Col>
          <Col>
            <Badge count={activeFilterCount} size="small" color="#1677ff" data-testid="active-filter-count">
              <Button
                icon={<FilterOutlined />}
                aria-label="筛选"
                aria-expanded={filtersExpanded}
                aria-controls="maintenance-home-filters"
                onClick={toggleFilters}
              >
                筛选 {filtersExpanded ? <UpOutlined /> : <DownOutlined />}
              </Button>
            </Badge>
          </Col>
        </Row>
        {filtersExpanded ? <div id="maintenance-home-filters" role="region" aria-label="项目筛选条件">
        <Row gutter={[16, 12]} style={{ marginTop: 16 }}>
          <Col xs={24} sm={12} lg={6}>
          <Text type="secondary">业务类型</Text>
          {/* 业务类型（2026-09-08）：独立一维，与期限状态叠加。多选，默认全选＝不排除。 */}
          <Select
            mode="multiple"
            allowClear
            data-testid="business-type-filter"
            maxTagCount="responsive"
            placeholder="全部业务类型"
            aria-label="业务类型筛选"
            style={{ width: "100%", marginTop: 4 }}
            value={businessTypes}
            onChange={(value) => setBusinessTypes(value as BoardBusinessTypeCode[])}
            options={BOARD_BUSINESS_TYPE_CODES.map((code) => ({
              label: BOARD_BUSINESS_TYPE_LABELS[code],
              value: code,
            }))}
          />
          </Col>
          <Col xs={24} sm={12} lg={5}>
          <Text type="secondary">项目状态</Text>
          <Select
            allowClear
            placeholder="全部状态"
            aria-label="项目状态筛选"
            style={{ width: "100%", marginTop: 4 }}
            value={status}
            onChange={(value) => setStatus(value as CardStatus | undefined)}
            options={[
              { label: "正常", value: "normal" },
              { label: "提醒", value: "warning" },
              { label: "报警", value: "alert" },
            ]}
          />
          </Col>
          <Col xs={24} sm={12} lg={5}>
          <Text type="secondary">排序方式</Text>
          <Select
            aria-label="项目排序"
            style={{ width: "100%", marginTop: 4 }}
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
          </Col>
          <Col xs={24} sm={12} lg={8}>
          <Text type="secondary">关键词</Text>
          <Input.Search
            allowClear
            placeholder="搜项目名 / XSDD 单号 / 销售姓名"
            aria-label="项目关键词"
            style={{ width: "100%", marginTop: 4 }}
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            onSearch={(value) => setKeyword(value.trim())}
          />
          </Col>
        </Row>
        </div> : null}
        {activeFilterCount > 0 ? (
          <Space wrap size={[0, 8]} role="group" aria-label="已应用筛选条件" style={{ marginTop: 16 }}>
            {activeBusinessTypes.map((code) => (
              <Tag
                key={code}
                color="blue"
                closable
                closeIcon={<button type="button" aria-label={`移除业务类型：${BOARD_BUSINESS_TYPE_LABELS[code]}`}
                  style={{ border: 0, background: "none", color: "inherit", padding: 0 }}>×</button>}
                onClose={() => setBusinessTypes((current) => current.filter((value) => value !== code))}
                style={{ borderRadius: 16 }}
              >业务类型：{BOARD_BUSINESS_TYPE_LABELS[code]}</Tag>
            ))}
            {status ? <Tag color="blue" closable
              closeIcon={<button type="button" aria-label="移除状态筛选" style={{ border: 0, background: "none", color: "inherit", padding: 0 }}>×</button>}
              onClose={() => setStatus(undefined)} style={{ borderRadius: 16 }}>状态：{STATUS_LABELS[status]}</Tag> : null}
            {keyword.trim() ? <Tag color="blue" closable
              closeIcon={<button type="button" aria-label="移除关键词筛选" style={{ border: 0, background: "none", color: "inherit", padding: 0 }}>×</button>}
              onClose={() => { setKeyword(""); setSearchInput(""); }}
              style={{ borderRadius: 16, maxWidth: "100%", whiteSpace: "normal", overflowWrap: "anywhere" }}
            >关键词：{keyword.trim()}</Tag> : null}
          </Space>
        ) : null}
      </Card>

      {hiddenByBusinessType > 0 ? (
        <Alert
          type="info"
          showIcon
          message={`已按业务类型隐藏 ${hiddenByBusinessType} 个项目`}
          action={
            <Button
              size="small"
              type="link"
              onClick={() => setBusinessTypes([...BOARD_BUSINESS_TYPE_CODES])}
            >
              查看全部
            </Button>
          }
        />
      ) : null}

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
            businessTypes.length > 0
            && businessTypes.length < BOARD_BUSINESS_TYPE_CODES.length
              ? "没有符合条件的项目；多数存量项目尚未标注业务类型，请勾上「未标注」或清空业务类型筛选再看"
              : lifecycle === "missing"
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
