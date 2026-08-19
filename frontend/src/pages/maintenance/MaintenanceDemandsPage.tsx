import { useCallback, useEffect, useState } from "react";
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
} from "../../api/maintenanceDemands";
import {
  restoreMaintenanceDemand,
  searchMaintenanceDemands,
  voidFastMaintenanceDemands,
} from "../../api/maintenanceDemands";
import type { WbddMissing, WbddMissingOrder } from "../../api/maintenanceWbddImport";
import { getWbddMissing, uploadWbdd } from "../../api/maintenanceWbddImport";
import { readPermissionMap } from "../../nav";

const { Title, Text } = Typography;

const DEMAND_PAGE_SIZE = 20;

/**
 * 需求单与数据同步（#267 前端任务 1+2）。
 *
 * 布局按用户操作顺序讲一个故事：传氚云快照 → 看差异清单 → 按氚云现状批量作废
 * → 日常查单 / 单张作废 / 恢复。进入页面即拉一次差异清单（后端实时重算口径，
 * 已作废的单自动从清单消失，重复作废幂等安全）。
 *
 * 写按钮按动作键显隐：作废/批量作废＝action_maintenance_demand_delete，
 * 快照上传＝action_maintenance_wbdd_import；页面可见性本身由路由 page_maintenance 控制。
 */
export function MaintenanceDemandsPage() {
  const permissions = readPermissionMap();
  const canVoid = !!permissions.action_maintenance_demand_delete;
  const canImport = !!permissions.action_maintenance_wbdd_import;

  // ---- 区块一：氚云快照同步 + 差异清单 ----
  const [missing, setMissing] = useState<WbddMissing | null>(null);
  const [missingLoading, setMissingLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [selectedRowKeys, setSelectedRowKeys] = useState<string[]>([]);

  // ---- 区块二：需求单查询 ----
  const [keyword, setKeyword] = useState("");
  const [includeVoided, setIncludeVoided] = useState(false);
  const [demands, setDemands] = useState<MaintenanceDemandSummary[]>([]);
  const [demandsTotal, setDemandsTotal] = useState(0);
  const [demandPage, setDemandPage] = useState(1);
  const [demandsLoading, setDemandsLoading] = useState(false);

  // ---- 作废原因弹窗（批量与单张共用） ----
  const [voidTarget, setVoidTarget] = useState<string[] | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voiding, setVoiding] = useState(false);

  const loadMissing = useCallback(async () => {
    setMissingLoading(true);
    try {
      const resp = await getWbddMissing();
      setMissing(resp.data);
      // 清单已重算，旧勾选可能已不在清单里，直接清空
      setSelectedRowKeys([]);
    } catch {
      // 还没传过快照（404）或拉取失败：按空清单展示，空态文案引导用户先传快照
      setMissing(null);
    } finally {
      setMissingLoading(false);
    }
  }, []);

  const loadDemands = useCallback(
    async (page: number, q: string, withVoided: boolean) => {
      setDemandsLoading(true);
      try {
        const resp = await searchMaintenanceDemands({
          page,
          page_size: DEMAND_PAGE_SIZE,
          ...(q.trim() ? { q: q.trim() } : {}),
          ...(withVoided ? { include_voided: true } : {}),
        });
        setDemands(resp.data.items);
        setDemandsTotal(resp.data.total);
        setDemandPage(resp.data.page ?? page);
      } catch (error) {
        message.error(readError(error, "需求单列表加载失败"));
      } finally {
        setDemandsLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void loadMissing();
    void loadDemands(1, "", false);
  }, [loadMissing, loadDemands]);

  const handleWbddUpload = async (file: File) => {
    setUploading(true);
    try {
      const resp = await uploadWbdd(file, crypto.randomUUID());
      const diffCount = resp.data.snapshot_diff?.missing_orders ?? 0;
      message.success(
        `快照已同步（批次 #${resp.data.batch_id}）` +
          (diffCount ? `，发现 ${diffCount} 张消失的单，请核对下方差异清单` : ""),
      );
      await loadMissing();
    } catch (error) {
      message.error(readError(error, "快照上传失败"));
    } finally {
      setUploading(false);
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
        idempotency_key: crypto.randomUUID(),
      });
      const already = resp.data.results.filter(
        (r) => r.status === "already_voided",
      ).length;
      message.success(
        `已作废 ${resp.data.voided} 张需求单` +
          (already ? `（其中 ${already} 张此前已是作废状态）` : ""),
      );
      setVoidTarget(null);
      await Promise.all([
        loadMissing(),
        loadDemands(demandPage, keyword, includeVoided),
      ]);
    } catch (error) {
      message.error(readVoidError(error));
    } finally {
      setVoiding(false);
    }
  };

  const handleRestore = async (row: MaintenanceDemandSummary) => {
    try {
      await restoreMaintenanceDemand(row.source_order_id);
      message.success(`已恢复需求单 ${row.order_no}`);
      await loadDemands(demandPage, keyword, includeVoided);
    } catch (error) {
      message.error(readError(error, "恢复失败"));
    }
  };

  const missingColumns: ColumnsType<WbddMissingOrder> = [
    { title: "需求单号", dataIndex: "order_no" },
    { title: "制单日期", dataIndex: "order_date", render: (v) => v ?? "—" },
    { title: "行数", dataIndex: "line_count", width: 80, align: "right" },
    {
      title: "挂靠项目ID",
      dataIndex: "assigned_project_id",
      render: (v) => v ?? "—",
    },
  ];

  const demandColumns: ColumnsType<MaintenanceDemandSummary> = [
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
    ...(canVoid
      ? ([
          {
            title: "操作",
            key: "actions",
            width: 120,
            render: (_: unknown, row: MaintenanceDemandSummary) =>
              isVoided(row) ? (
                <Button size="small" onClick={() => void handleRestore(row)}>
                  恢复
                </Button>
              ) : (
                <Button
                  size="small"
                  danger
                  onClick={() => openVoidModal([row.source_order_id])}
                >
                  作废
                </Button>
              ),
          },
        ] as ColumnsType<MaintenanceDemandSummary>)
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
          每次从氚云导出需求单快照后，来这里同步并处理消失的单。
        </Text>
      </div>

      <Card title="氚云快照同步">
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Space wrap size={12} align="center">
            {canImport ? (
              <Upload
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

          {missing?.truncated ? (
            <Alert
              type="warning"
              showIcon
              message="消失的单太多，这里只显示前 1000 条；处理完这批后请重新拉取差异清单。"
            />
          ) : null}

          <Table<WbddMissingOrder>
            rowKey="source_order_id"
            size="small"
            loading={missingLoading}
            dataSource={missing?.missing_orders ?? []}
            columns={missingColumns}
            pagination={false}
            locale={{ emptyText: "最近一份快照没有消失的单" }}
            rowSelection={
              canVoid
                ? {
                    selectedRowKeys,
                    onChange: (keys) => setSelectedRowKeys(keys as string[]),
                  }
                : undefined
            }
          />

          {canVoid ? (
            <div>
              <Button
                type="primary"
                danger
                disabled={!selectedRowKeys.length}
                onClick={() => openVoidModal(selectedRowKeys)}
              >
                按氚云现状批量作废
                {selectedRowKeys.length ? `（已选 ${selectedRowKeys.length} 张）` : ""}
              </Button>
            </div>
          ) : null}
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

          <Table<MaintenanceDemandSummary>
            rowKey="source_order_id"
            size="small"
            loading={demandsLoading}
            dataSource={demands}
            columns={demandColumns}
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
