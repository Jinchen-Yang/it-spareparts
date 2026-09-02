import { useCallback, useEffect, useRef, useState } from "react";
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
import type { MaintenanceProjectOperationsSummary } from "../../../api/maintenanceOperations";
import {
  assignMaintenanceSourceOrders,
  autoAssignMaintenanceSourceOrders,
  listMaintenanceSourceOrders,
} from "../../../api/maintenanceSourceAssignments";
import type { MaintenanceSourceOrderRow } from "../../../api/maintenanceSourceAssignments";
import {
  LIFECYCLE_LABEL,
  type RegisterPanelRefresh,
  raw,
  readError,
  statText,
} from "./panelUtils";

const { Text } = Typography;

/**
 * 概览 tab（2026-08-19 重设计）：基本信息 + 归属挂靠（#39/#45）。
 * 01 表只读——这里不再挂下载/上传（总表在页头，含 01 sheet）；成本率已上健康带。
 */
export function OverviewTab({
  projectId,
  row,
  project,
  operationsProject,
  canAssign,
  onAssigned,
  registerRefresh,
}: {
  projectId: string;
  row: BoardProjectRow | null;
  /** stable 目录的基础信息——boss-board 聚合行缺位时的回退源（数据源不同，字段较少）。 */
  project: MaintenanceProject | null;
  /** workspace 中的权威负责人账号映射；project_manager_id 仅是来源原文。 */
  operationsProject?: MaintenanceProjectOperationsSummary | null;
  canAssign: boolean;
  onAssigned: () => Promise<boolean>;
  registerRefresh: RegisterPanelRefresh;
}) {
  const [candidates, setCandidates] = useState<MaintenanceSourceOrderRow[]>([]);
  const [busy, setBusy] = useState(false);
  const requestSeq = useRef(0);

  const loadCandidates = useCallback(async () => {
    const seq = ++requestSeq.current;
    if (!canAssign) {
      setCandidates([]);
      return true;
    }
    try {
      // #48：让后端按本项目 XSDD 集合排序——前端只拿一页，若在前端筛会漏掉
      // 命中但排在 20 条之外的单。多合同项目的全部 XSDD 都由后端从台账取。
      const resp = await listMaintenanceSourceOrders({
        page: 1, page_size: 20, assignment_status: "unassigned",
        xsdd_project_id: projectId,
      });
      if (seq !== requestSeq.current) return false;
      setCandidates(resp.data.rows ?? []);
      return true;
    } catch {
      if (seq === requestSeq.current) setCandidates([]);
      return false;
    }
  }, [canAssign, projectId]);

  useEffect(() => {
    registerRefresh("overview", loadCandidates);
    void loadCandidates();
    return () => {
      requestSeq.current += 1;
      registerRefresh("overview", null);
    };
  }, [loadCandidates, registerRefresh]);

  const confirm = async (sourceOrderId: string) => {
    setBusy(true);
    try {
      await assignMaintenanceSourceOrders({
        project_id: projectId,
        items: [{ source_order_id: sourceOrderId }],
        reason: "项目面板确认挂靠",
      });
      if (await onAssigned()) {
        message.success("已确认挂靠并刷新");
      } else {
        message.warning("挂靠已写入，但页面刷新失败；旧数据已失效，请重试。");
      }
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
      const summary = `自动挂靠完成：${r.assigned_orders} 张单` +
          (r.matched_projects ? `，挂到 ${r.matched_projects} 个合同 owner 项目` : "") +
          (r.skipped_groups ? `；${r.skipped_groups} 组无有效 XSDD 归属已跳过，待人工处理` : "");
      if (await onAssigned()) {
        message.success(`${summary}，页面已刷新`);
      } else {
        message.warning(`${summary}，但页面刷新失败；旧数据已失效，请重试。`);
      }
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
        <Descriptions.Item label="维保负责人账号">
          {operationsProject?.manager_assignment?.display_name
            ?? operationsProject?.manager_assignment?.username
            ?? "未映射系统账号"}
        </Descriptions.Item>
        <Descriptions.Item label="来源负责人原文">
          {operationsProject?.project_manager_id ?? project?.project_manager_id ?? "—"}
        </Descriptions.Item>
        <Descriptions.Item label="合同总额（含税）">
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
            同一销售订单＝同一项目。点「自动匹配挂靠」只把带有效 XSDD 的单据挂到对应销售合同
             owner 项目；无 XSDD 或 XSDD 非法的单据绝不按名称匹配或建项，留在下面人工确认；
            标「同 XSDD」的是命中本项目销售单的候选，已排最前。
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
