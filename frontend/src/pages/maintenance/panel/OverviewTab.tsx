import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Card,
  Descriptions,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { BoardProjectRow } from "../../../api/maintenanceBossBoard";
import type { MaintenanceProject } from "../../../api/maintenanceProjects";
import {
  assignMaintenanceSourceOrders,
  autoAssignMaintenanceSourceOrders,
  listMaintenanceSourceOrders,
} from "../../../api/maintenanceSourceAssignments";
import type { MaintenanceSourceOrderRow } from "../../../api/maintenanceSourceAssignments";
import { LIFECYCLE_LABEL, raw, readError, statText } from "./panelUtils";

const { Text } = Typography;

/**
 * 概览 tab（2026-08-19 重设计）：基本信息 + 归属挂靠（#39/#45）。
 * 01 表只读——这里不再挂下载/上传（总表在页头，含 01 sheet）；成本率已上健康带。
 */
export function OverviewTab({
  projectId,
  row,
  project,
  canAssign,
  onAssigned,
}: {
  projectId: string;
  row: BoardProjectRow | null;
  /** stable 目录的基础信息——boss-board 聚合行缺位时的回退源（数据源不同，字段较少）。 */
  project: MaintenanceProject | null;
  canAssign: boolean;
  onAssigned: () => void;
}) {
  const [candidates, setCandidates] = useState<MaintenanceSourceOrderRow[]>([]);
  const [busy, setBusy] = useState(false);

  const loadCandidates = useCallback(async () => {
    try {
      // #48：让后端按本项目 XSDD 集合排序——前端只拿一页，若在前端筛会漏掉
      // 命中但排在 20 条之外的单。多合同项目的全部 XSDD 都由后端从台账取。
      const resp = await listMaintenanceSourceOrders({
        page: 1, page_size: 20, assignment_status: "unassigned",
        xsdd_project_id: projectId,
      });
      setCandidates(resp.data.rows ?? []);
    } catch {
      setCandidates([]);
    }
  }, [projectId]);

  useEffect(() => {
    if (canAssign) void loadCandidates();
  }, [canAssign, loadCandidates]);

  const confirm = async (sourceOrderId: string) => {
    setBusy(true);
    try {
      await assignMaintenanceSourceOrders({
        project_id: projectId,
        items: [{ source_order_id: sourceOrderId }],
        reason: "项目面板确认挂靠",
      });
      message.success("已确认挂靠");
      await loadCandidates();
      onAssigned();
    } catch (err) {
      message.error(readError(err, "挂靠失败"));
    } finally {
      setBusy(false);
    }
  };

  const autoAssign = async () => {
    setBusy(true);
    try {
      const { data } = await autoAssignMaintenanceSourceOrders();
      const r = data.result;
      message.success(
        `自动挂靠完成：${r.assigned_orders} 张单` +
          (r.matched_projects ? `，挂到 ${r.matched_projects} 个已有项目` : "") +
          (r.created_projects ? `，自动新建 ${r.created_projects} 个项目` : "") +
          (r.skipped_groups ? `；${r.skipped_groups} 个无项目名已跳过` : ""),
      );
      await loadCandidates();
      onAssigned();
    } catch (err) {
      message.error(readError(err, "自动挂靠失败"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Descriptions bordered size="small" column={2}>
        <Descriptions.Item label="项目名称">
          {row?.display_name ?? project?.display_name ?? "—"}
        </Descriptions.Item>
        <Descriptions.Item label="项目编号">
          {row?.project_code ?? project?.project_code ?? "—"}
        </Descriptions.Item>
        <Descriptions.Item label="合同号(XSDD)">
          {row?.contract_nos.length ? row.contract_nos.join("、") : "—"}
        </Descriptions.Item>
        <Descriptions.Item label="项目经理（负责人）">
          {/* boss 聚合行给显示人名；缺失回退账号；编辑保存后 loadProject 刷新 */}
          {row?.project_manager ?? project?.project_manager_id ?? "—"}
        </Descriptions.Item>
        <Descriptions.Item label="合同总额">
          <Space size={6}>
            <span>{statText(row?.contract_amount_inc_tax)}</span>
            {row?.contract_shared ? (
              <Tooltip title="有销售订单同时挂在多个项目上，合同额会在项目间重复计入，仅作参考">
                <Tag color="orange">共用单</Tag>
              </Tooltip>
            ) : null}
            {row?.contract_incomplete ? (
              <Tooltip title="部分关联销售订单未在销售表中找到金额，合同额被低估">
                <Tag color="orange">不完整</Tag>
              </Tooltip>
            ) : null}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="维保期限">
          {(() => {
            const pf = row?.period_from ?? project?.period_from;
            const pt = row?.period_to ?? project?.period_to;
            if (!pf && !pt) return "—";
            return `${pf ?? "—"} ~ ${pt ?? "—"}`;
          })()}
        </Descriptions.Item>
        <Descriptions.Item label="生命周期">
          {(() => {
            const lc = row?.lifecycle ?? project?.lifecycle_status;
            return lc ? LIFECYCLE_LABEL[lc] ?? lc : "—";
          })()}
        </Descriptions.Item>
      </Descriptions>

      {canAssign ? (
        <Card
          size="small"
          title="归属挂靠（判定依据＝XSDD 销售订单）"
          extra={(
            <Button size="small" type="primary" ghost loading={busy} onClick={() => void autoAssign()}>
              自动匹配挂靠
            </Button>
          )}
        >
          <Text type="secondary" style={{ fontSize: 11.5 }}>
            同一销售订单＝同一项目。点「自动匹配挂靠」会按单据自带的项目名自动挂到已有项目，
            对不上的才留在下面人工确认；标「同 XSDD」的是命中本项目销售单的候选，已排最前。
          </Text>
          <Table<MaintenanceSourceOrderRow>
            rowKey="raw_order_id"
            size="small"
            dataSource={candidates}
            pagination={false}
            locale={{ emptyText: "没有待确认的未归属单据" }}
            columns={[
              {
                title: "需求单号",
                dataIndex: "order_no",
                render: (value: string, order) => (
                  <Space size={4}>
                    <span>{value}</span>
                    {order.matches_project_xsdd ? (
                      <Tag color="blue">同 XSDD</Tag>
                    ) : null}
                  </Space>
                ),
              },
              { title: "制单日期", dataIndex: "order_date", render: raw },
              { title: "项目原文", dataIndex: "project_raw", render: raw },
              {
                title: "操作",
                render: (_: unknown, order) => (
                  <Button
                    size="small"
                    loading={busy}
                    onClick={() => confirm(order.raw_order_id)}
                  >
                    确认挂靠到本项目
                  </Button>
                ),
              },
            ]}
          />
        </Card>
      ) : null}
    </Space>
  );
}

export default OverviewTab;
