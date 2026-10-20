import { useEffect, useRef, useState } from "react";
import { App, Alert, Button, List, Modal, Space, Typography, Upload } from "antd";
import { DownloadOutlined, UploadOutlined } from "@ant-design/icons";
import type {
  WillReassignOrder,
  WillVoidRow,
  WorkbookApplyOptions,
  WorkbookApplyResult,
  WorkbookFieldChange,
  WorkbookProtectionDriftDetail,
  WorkbookProtectionDriftIssue,
  WorkbookSiteIssueMerge,
  WorkbookSiteReplayErrorDetail,
  WorkbookSiteReplayRow,
  WorkbookValidateResult,
} from "../../api/maintenanceWorkbooks";
import { saveBlob } from "../../api/maintenanceWorkbooks";

const { Text } = Typography;
const SITE_ISSUE_HEADER_LABELS: Record<string, string> = {
  issue_no: "领用单号",
  issue_date: "领用日期",
  receiver: "领用人",
  issued_by: "发放人",
  site_location: "领用地址",
  raw_status: "原始状态",
  normalized_status: "领用状态",
};

/**
 * 「下载 → 改 → 上传覆盖」这一组动作的唯一复用件（REQUIREMENTS #38/#40）。
 *
 * 之所以把下载与上传绑在同一个组件里：口径是**在哪下载就在哪上传**。分开摆放
 * 迟早出现「在 A 处下载、到 B 处上传」，而两处的 sheet 集合并不相同。
 *
 * 总表 2.1（#265 契约三）：传入 onValidate 即启用两阶段回传——先预检，
 * 把「将被作废的行」逐条摆给用户确认后才真正落库；预检被拒（如行数骤减
 * 防呆）直接展示后端行级文案，绝不偷偷 apply。
 */
export interface WorkbookRoundTripProps {
  title: string;
  hint?: string;
  filename: string;
  onDownload: () => Promise<Blob>;
  /** opts.forceTakeover：2.7.0 行级冲突强制接管（覆盖他人改动并留审计）。 */
  onApply: (file: File, opts?: WorkbookApplyOptions) => Promise<WorkbookApplyResult>;
  /**
   * 落库成功后的读回校验。返回 false/抛错都只表示“刷新失败”，不能把已经
   * commit 的上传误报成上传失败；调用方应同时失效页面里的旧快照。
   */
  onAfterApply?: () => Promise<boolean | void>;
  /** 传入即启用「预检 → 作废预览 → 确认 → 落库」两阶段（项目总表 2.1）。 */
  onValidate?: (file: File) => Promise<WorkbookValidateResult>;
  /** 无上传动作键时只给下载（只读对账的人也该能把表拉下来核对）。 */
  canUpload: boolean;
  size?: "small" | "middle";
}

function describe(result: Partial<WorkbookApplyResult>): string {
  const parts: string[] = [];
  if (result.changes?.length) parts.push(`字段改动 ${result.changes.length} 处`);
  if (result.overridden?.length) {
    parts.push(`强制覆盖他人改动 ${result.overridden.length} 处（已记审计）`);
  }
  if (result.voided_rows?.length) {
    parts.push(`已作废行改动未生效 ${result.voided_rows.length} 行（作废优先）`);
  }
  if (result.cost_refills || result.cost_overrides) {
    parts.push(`补价 ${result.cost_refills || result.cost_overrides} 行`);
  }
  if (result.line_creates) parts.push(`备件新增 ${result.line_creates} 行`);
  if (result.line_updates || result.qty_updates) {
    parts.push(`备件更新 ${result.line_updates || result.qty_updates} 行`);
  }
  if (result.line_voids) parts.push(`备件作废 ${result.line_voids} 行`);
  if (result.order_reassignments) {
    parts.push(`WBDD 归属更正 ${result.order_reassignments} 张`);
  }
  if (result.contract_updates) parts.push(`项目合同总额更新 ${result.contract_updates} 项`);
  if (result.project_note_updates) parts.push(`项目备注更新 ${result.project_note_updates} 项`);
  if (result.expense_creates) parts.push(`报销新增 ${result.expense_creates} 行`);
  if (result.expense_updates) parts.push(`报销更新 ${result.expense_updates} 行`);
  if (result.expense_voids) parts.push(`报销作废 ${result.expense_voids} 行`);
  if (result.collection_creates || result.collection_updates) {
    parts.push(`回款新增/覆盖 ${result.collection_creates || result.collection_updates} 条`);
  }
  if (result.collection_voids) parts.push(`回款作废 ${result.collection_voids} 条`);
  if (result.plan_creates) parts.push(`回款计划新增 ${result.plan_creates} 条`);
  if (result.plan_updates) parts.push(`回款计划更新 ${result.plan_updates} 条`);
  if (result.plan_voids) parts.push(`回款计划作废 ${result.plan_voids} 条`);
  if (result.site_return_flags) parts.push(`返还标记 ${result.site_return_flags} 行`);
  if (result.site_creates) parts.push(`领用新增 ${result.site_creates} 行`);
  if (result.site_voids) parts.push(`领用作废 ${result.site_voids} 行`);
  if (result.site_updates) parts.push(`领用更新 ${result.site_updates} 行`);
  const mergeCount = result.site_issue_merge_count ?? result.site_issue_merges?.length ?? 0;
  if (mergeCount) parts.push(`领用单合并 ${mergeCount} 组`);
  return parts.length ? parts.join("、") : "文件没有改动";
}

/** 作废预览的一行说人话：能定位到 sheet + 单号 + 原因即可，字段缺啥降级显示啥。 */
function describeVoidRow(row: WillVoidRow, index: number): string {
  const parts = [
    row.sheet ? String(row.sheet) : null,
    row.order_no ? `单号 ${String(row.order_no)}` : null,
    row.reason ? String(row.reason) : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : `第 ${index + 1} 行`;
}

function describeReassignment(row: WillReassignOrder, index: number): string {
  const order = row.order_no || row.source_order_id || `第 ${index + 1} 张`;
  return `${order} · ${row.from_project_name || "未归属"} → 当前项目`;
}

export function WorkbookRoundTrip({
  title,
  hint,
  filename,
  onDownload,
  onApply,
  onAfterApply,
  onValidate,
  canUpload,
  size = "middle",
}: WorkbookRoundTripProps) {
  const { message } = App.useApp();
  const [downloading, setDownloading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [feedback, setFeedback] = useState<{ type: "error" | "warning"; text: string } | null>(null);
  const [uploadInputVersion, setUploadInputVersion] = useState(0);
  const [retainedDraft, setRetainedDraft] = useState<{ file: File; session: string | null } | null>(null);
  const uploadSession = localStorage.getItem("token");
  const uploadAllowed = useRef(canUpload);
  uploadAllowed.current = canUpload;
  const mounted = useRef(false);
  const uploadEpoch = useRef(0);
  const closeConfirmation = useRef<(() => void) | null>(null);
  const retainedFile = retainedDraft?.session === uploadSession ? retainedDraft.file : null;

  useEffect(() => {
    // 文件仅留在当前页面内存；账号或权限变化后不能复用上个操作上下文的草稿。
    uploadEpoch.current += 1;
    closeConfirmation.current?.();
    setRetainedDraft(null);
    setApplying(false);
    setFeedback(null);
  }, [canUpload, uploadSession]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      uploadEpoch.current += 1;
      // Modal.confirm 渲染在门户中，需要随页面显式销毁。
      closeConfirmation.current?.();
    };
  }, []);

  const handleDownload = async () => {
    setDownloading(true);
    setFeedback(null);
    try {
      saveBlob(await onDownload(), filename);
    } catch (error) {
      setFeedback({ type: "error", text: readError(error, "下载失败") });
    } finally {
      setDownloading(false);
    }
  };

  const applyDirectly = async (
    file: File,
    isCurrent: () => boolean,
    opts?: WorkbookApplyOptions,
  ) => {
    if (!isCurrent()) return;
    const result = await onApply(file, opts);
    if (!isCurrent()) return;
    setRetainedDraft(null);
    if (onAfterApply) {
      try {
        const refreshed = await onAfterApply();
        if (!isCurrent()) return;
        if (refreshed === false) {
          setFeedback({ type: "warning", text: "数据已写入，但页面刷新失败；旧数据已失效，请点击重试。" });
          return;
        }
      } catch {
        if (!isCurrent()) return;
        setFeedback({ type: "warning", text: "数据已写入，但页面刷新失败；旧数据已失效，请点击重试。" });
        return;
      }
    }
    message.success(describe(result) === "文件没有改动"
      ? "文件没有改动，未写入任何数据"
      : `${onAfterApply ? "已覆盖并刷新" : "已覆盖"}：${describe(result)}`);
  };

  /** 两阶段：预检 → 摆变更摘要和作废预览 → 用户确认才落库。 */
  const validateThenApply = async (
    file: File,
    isCurrent: () => boolean,
    rememberMergeToken: (token: string | undefined) => void,
  ) => {
    const preview = await onValidate!(file);
    if (!isCurrent()) return;
    const mergeToken = preview.site_issue_merge_token || undefined;
    rememberMergeToken(mergeToken);
    const mergeRows = preview.site_issue_merges ?? [];
    const voidRows = preview.will_void_rows ?? [];
    const reassignRows = preview.will_reassign_orders ?? [];
    const voidedRows = preview.voided_rows ?? [];
    await new Promise<void>((resolve, reject) => {
      const finish = () => {
        modal.destroy();
        if (closeConfirmation.current === finish) closeConfirmation.current = null;
        resolve();
      };
      const modal = Modal.confirm({
        title: `确认回传${title}？`,
        width: 560,
        content: (
        <Space direction="vertical" size={12} style={{ width: "100%", maxHeight: "65vh", overflowY: "auto", overflowWrap: "anywhere" }}>
          <Text>{describe(preview)}</Text>
          {mergeRows.length ? <SiteIssueMerges merges={mergeRows} /> : null}
          {voidedRows.length ? (
            // D-02 作废优先：这些行的改动不会写入，其余行照常回传
            <Alert
              type="info"
              showIcon
              message={`以下 ${voidedRows.length} 行落在已作废的领用行/单上，改动不会生效（作废优先）`}
              description={
                <List
                  size="small"
                  dataSource={voidedRows}
                  renderItem={(row) => (
                    <List.Item style={{ padding: "4px 0" }}>
                      <Text style={{ fontSize: 12 }}>{row.message}</Text>
                    </List.Item>
                  )}
                />
              }
            />
          ) : null}
          {voidRows.length ? (
            <Alert
              type="warning"
              showIcon
              message={`以下 ${voidRows.length} 行将被作废（不可撤销，需恢复请走需求单恢复）`}
              description={
                <List
                  size="small"
                  dataSource={voidRows}
                  renderItem={(row, index) => (
                    <List.Item style={{ padding: "4px 0" }}>
                      <Text style={{ fontSize: 12 }}>{describeVoidRow(row, index)}</Text>
                    </List.Item>
                  )}
                />
              }
            />
          ) : null}
          {reassignRows.length ? (
            <Alert
              type="warning"
              showIcon
              message={`以下 ${reassignRows.length} 张 WBDD 将按本次项目内人工认证更正归属`}
              description={
                <Space direction="vertical" size={2}>
                  {reassignRows.map((row, index) => (
                    <Text key={row.source_order_id || `${row.order_no}-${index}`} style={{ fontSize: 12 }}>
                      {describeReassignment(row, index)}
                    </Text>
                  ))}
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    旧归属会保留为历史记录；已有明细将被复用，不会重复新增。
                  </Text>
                </Space>
              }
            />
          ) : null}
        </Space>
        ),
        okText: "确认回传",
        cancelText: "取消",
        onCancel: finish,
        onOk: async () => {
          try {
            await applyDirectly(file, isCurrent, mergeToken ? { siteIssueMergeToken: mergeToken } : undefined);
            finish();
          } catch (error) {
            modal.destroy();
            if (closeConfirmation.current === finish) closeConfirmation.current = null;
            reject(error);
          }
        },
      });
      closeConfirmation.current = finish;
    });
  };

  const showSiteReplayError = (detail: WorkbookSiteReplayErrorDetail) => {
    setRetainedDraft(null);
    return new Promise<void>((resolve) => {
      const finish = () => {
        modal.destroy();
        if (closeConfirmation.current === finish) closeConfirmation.current = null;
        resolve();
      };
      const modal = Modal.error({
        title: "领用记录冲突，请修改文件后重新上传",
        width: 720,
        content: <SiteReplayConflicts detail={detail} />,
        okText: "返回修改文件",
        onOk: finish,
        onCancel: finish,
      });
      closeConfirmation.current = finish;
    });
  };

  const showProtectionDriftError = (detail: WorkbookProtectionDriftDetail) => {
    setRetainedDraft(null);
    return new Promise<void>((resolve) => {
      const finish = () => {
        modal.destroy();
        if (closeConfirmation.current === finish) closeConfirmation.current = null;
        resolve();
      };
      const modal = Modal.error({
        title: "保护基准已变化，请重新下载工作簿",
        width: 640,
        content: (
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Text type="danger" style={{ fontSize: 12 }}>
              以下你编辑的字段，其保护比较基准或保护周期在你下载后已变化；
              本次上传未写入任何数据，也不能用强制接管跳过。请重新下载最新
              工作簿，逐项核对这些字段后再上传。
            </Text>
            <List
              size="small"
              dataSource={detail.issues.slice(0, 30)}
              renderItem={(issue) => (
                <List.Item style={{ padding: "4px 0" }}>
                  <Text style={{ fontSize: 12 }}>
                    {describeProtectionDrift(issue)}
                  </Text>
                </List.Item>
              )}
            />
            {detail.issues.length > 30 ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                ……另有 {detail.issues.length - 30} 处，请重新下载后逐项核对
              </Text>
            ) : null}
          </Space>
        ),
        okText: "知道了，重新下载",
        onOk: finish,
        onCancel: finish,
      });
      closeConfirmation.current = finish;
    });
  };

  const handleUpload = async (file: File) => {
    if (!uploadAllowed.current) return false;
    const requestSession = localStorage.getItem("token");
    const requestEpoch = uploadEpoch.current;
    const isCurrent = () => mounted.current && uploadAllowed.current
      && uploadEpoch.current === requestEpoch && localStorage.getItem("token") === requestSession;
    setRetainedDraft(null);
    setApplying(true);
    setFeedback(null);
    let siteIssueMergeToken: string | undefined;
    try {
      if (onValidate) {
        await validateThenApply(file, isCurrent, (token) => { siteIssueMergeToken = token; });
      } else {
        await applyDirectly(file, isCurrent);
      }
    } catch (error) {
      if (!isCurrent()) return false;
      if ((error as { response?: { status?: number } })?.response?.status === 409) {
        setRetainedDraft({ file, session: requestSession });
      }
      // 2.7.0 行级冲突：展示三值对照，用户确认后可强制接管重传。
      const conflicts = extractConflicts(error);
      const siteReplay = extractSiteReplayError(error);
      // Q3=A：保护基准/周期漂移——字段级提示重下核对，没有接管按钮。
      const protectionDrift = extractProtectionDrift(error);
      if (siteReplay) {
        await showSiteReplayError(siteReplay);
      } else if (protectionDrift) {
        await showProtectionDriftError(protectionDrift);
      } else if (conflicts?.length) {
        await new Promise<void>((resolve) => {
          const finish = () => {
            modal.destroy();
            if (closeConfirmation.current === finish) closeConfirmation.current = null;
            resolve();
          };
          const modal = Modal.confirm({
            title: `有 ${conflicts.length} 处改动与他人冲突`,
            width: 640,
            content: (
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <Text type="danger" style={{ fontSize: 12 }}>
                  以下字段在你下载后被他人更新；本次上传未写入任何数据。
                  可选择「强制接管」用你的值覆盖（每次覆盖均记入审计），
                  或取消后重新下载把改动搬进最新表。
                </Text>
                <List
                  size="small"
                  dataSource={conflicts.slice(0, 30)}
                  renderItem={(row, index) => (
                    <List.Item style={{ padding: "4px 0" }}>
                      <Text style={{ fontSize: 12 }}>
                        {describeConflict(row, index)}
                      </Text>
                    </List.Item>
                  )}
                />
                {conflicts.length > 30 ? (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    ……另有 {conflicts.length - 30} 处，明细见审计日志
                  </Text>
                ) : null}
              </Space>
            ),
            okText: "强制接管并上传",
            okButtonProps: { danger: true },
            cancelText: "取消（重新下载处理）",
            onOk: async () => {
              try {
                await applyDirectly(file, isCurrent, {
                  forceTakeover: true,
                  ...(siteIssueMergeToken ? { siteIssueMergeToken } : {}),
                });
              } catch (takeoverError) {
                if (isCurrent()) {
                  const replayError = extractSiteReplayError(takeoverError);
                  const driftError = extractProtectionDrift(takeoverError);
                  if (replayError) {
                    // 接管后重校验也可能发现身份碰撞；销毁旧弹窗，让只读
                    // 明细接续同一次上传的生命周期，不能继续提供强制接管。
                    modal.destroy();
                    await showSiteReplayError(replayError);
                  } else if (driftError) {
                    // 漂移同样不可接管：销毁接管弹窗，切到重下核对明细。
                    modal.destroy();
                    await showProtectionDriftError(driftError);
                  } else {
                    setFeedback({ type: "error", text: readError(takeoverError, "上传失败") });
                  }
                }
              } finally {
                finish();
              }
            },
            onCancel: finish,
          });
          closeConfirmation.current = finish;
        });
      } else {
        // 后端整份拒绝时把原文 message 显示出来：告诉用户**哪一行**不合法，
        // 而不是一句「上传失败」——这份表是人工编辑的，定位靠这句话。
        setFeedback({ type: "error", text: readError(error, "上传失败") });
      }
    } finally {
      if (mounted.current && uploadEpoch.current === requestEpoch) {
        setApplying(false);
        // 浏览器不会为同一个原生 file input 的同文件重选再次触发 change。
        // 无论成功、失败还是预检弹窗取消，都重建 input，让用户能直接重试原文件。
        setUploadInputVersion((current) => current + 1);
      }
    }
    return false;
  };

  return (
    <Space direction="vertical" size={2}>
      <Space size={8}>
        <Button
          size={size}
          icon={<DownloadOutlined />}
          loading={downloading}
          onClick={handleDownload}
        >
          下载{title}
        </Button>
        {canUpload ? (
          <Upload
            key={`workbook-upload-${uploadInputVersion}`}
            accept=".xlsx"
            disabled={applying}
            maxCount={1}
            showUploadList={false}
            beforeUpload={(file) => handleUpload(file)}
          >
            <Button size={size} icon={<UploadOutlined />} loading={applying}>
              上传覆盖
            </Button>
          </Upload>
        ) : null}
      </Space>
      {feedback ? <Alert type={feedback.type} showIcon message={feedback.text} /> : null}
      {canUpload && retainedFile ? (
        <Alert
          type="warning"
          showIcon
          message={`待处理文件：${retainedFile.name}`}
          description={(
            <Space direction="vertical" size={6}>
              <Text>原文件暂留在本页，可重试或取消。版本冲突时请重新下载，并将草稿修改搬入最新文件。</Text>
              <Space wrap>
                <Button size="small" disabled={applying} onClick={() => void handleUpload(retainedFile)}>重试原文件</Button>
                <Button size="small" disabled={applying} onClick={() => setRetainedDraft(null)}>取消文件草稿</Button>
              </Space>
            </Space>
          )}
        />
      ) : null}
      {hint ? (
        <Text type="secondary" style={{ fontSize: 11.5 }}>
          {hint}
        </Text>
      ) : null}
    </Space>
  );
}

function SiteIssueMerges({ merges }: { merges: WorkbookSiteIssueMerge[] }) {
  return (
    <Alert
      type="warning"
      style={{ padding: 12 }}
      message={`以下 ${merges.length} 组合并为同号领用单，请核对明细归属及单头取值`}
      description={(
        <List
          size="small"
          dataSource={merges}
          rowKey={(merge) => `${merge.target_issue_no}-${merge.survivor_issue_id}`}
          renderItem={(merge) => {
            const survivor = merge.sources.find((source) => source.issue_id === merge.survivor_issue_id);
            return (
              <List.Item style={{ padding: "8px 0" }}>
                <Space direction="vertical" size={4} style={{ minWidth: 0, width: "100%" }}>
                  <Text strong>合并目标：{merge.target_issue_no}</Text>
                  <Text>保留单：{survivor?.issue_no || merge.target_issue_no}（{merge.survivor_issue_id}）</Text>
                  {merge.sources.map((source) => (
                    <Text key={source.issue_id}>
                      来源单：{source.issue_no}（{source.issue_id}）· 明细 {source.line_count} 行
                      {source.rows.length ? ` · Excel 第 ${source.rows.join("、")} 行` : " · 当前项目内已有单据"}
                    </Text>
                  ))}
                  <Text>
                    合并后现有明细共 {merge.total_line_count} 行
                    {merge.rows.length ? ` · Excel 第 ${merge.rows.join("、")} 行` : ""}
                  </Text>
                  {merge.header_conflicts.map((conflict) => (
                    <Space key={conflict.field} direction="vertical" size={2} style={{ minWidth: 0, width: "100%" }}>
                      <Text>
                        {conflict.field_label || SITE_ISSUE_HEADER_LABELS[conflict.field] || conflict.field}：保留单原值 {conflict.survivor_value || "（空）"}；最终值 {conflict.final_value || "（空）"}
                      </Text>
                      {conflict.discarded.map((discarded) => (
                        <Text key={discarded.issue_id} type="secondary">
                          舍弃来源值：{discarded.issue_no}（{discarded.issue_id}）· {discarded.value || "（空）"}
                        </Text>
                      ))}
                    </Space>
                  ))}
                </Space>
              </List.Item>
            );
          }}
        />
      )}
    />
  );
}

function SiteReplayConflicts({ detail }: { detail: WorkbookSiteReplayErrorDetail }) {
  return (
    <Space direction="vertical" size={12} style={{ width: "100%", maxHeight: "65vh", overflowY: "auto", overflowWrap: "anywhere" }}>
      <Alert type="error" showIcon message={detail.message} description={detail.fix_hint} />
      <Text type="secondary">字段对照：原值 → 上传值；本次上传未写入任何数据。</Text>
      {detail.total_count > detail.rows.length ? (
        <Text type="warning">
          共 {detail.total_count} 行冲突，当前展示前 {detail.rows.length} 行；其余 {detail.total_count - detail.rows.length} 行请在处理后重新上传查看。
        </Text>
      ) : null}
      <List
        size="small"
        dataSource={detail.rows}
        rowKey={(row) => `${row.row_no}-${row.entity_id}`}
        renderItem={(row) => (
          <List.Item>
            <Space direction="vertical" size={4} style={{ minWidth: 0, width: "100%" }}>
              <Text strong>行号：{row.row_no}</Text>
              <Text>实体ID：{row.entity_id}</Text>
              <Text>领用单号：{row.issue_no} · PN：{row.pn} · SN：{row.sn || "（空）"}</Text>
              {row.differences.map((difference) => (
                <Text key={difference.field}>
                  {difference.field}：{difference.old || "（空）"} → {difference.new || "（空）"}
                </Text>
              ))}
            </Space>
          </List.Item>
        )}
      />
    </Space>
  );
}

function isSiteReplayRow(value: unknown): value is WorkbookSiteReplayRow {
  if (!value || typeof value !== "object") return false;
  const row = value as Partial<WorkbookSiteReplayRow>;
  return typeof row.row_no === "number" && Number.isInteger(row.row_no)
    && typeof row.entity_id === "string" && typeof row.issue_no === "string"
    && typeof row.pn === "string" && typeof row.sn === "string"
    && Array.isArray(row.differences) && row.differences.every((difference) => (
      difference && typeof difference.field === "string"
      && typeof difference.old === "string" && typeof difference.new === "string"
    ));
}

function extractSiteReplayError(error: unknown): WorkbookSiteReplayErrorDetail | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (!detail || typeof detail !== "object") return null;
  const candidate = detail as Partial<WorkbookSiteReplayErrorDetail>;
  if (candidate.code !== "stale_site_replay" || typeof candidate.message !== "string"
    || typeof candidate.fix_hint !== "string" || !Number.isInteger(candidate.total_count)
    || !Array.isArray(candidate.rows) || !candidate.rows.length
    || !candidate.rows.every(isSiteReplayRow)) return null;
  return candidate as WorkbookSiteReplayErrorDetail;
}

/** 从 409 响应里提取行级冲突三值明细（2.7.0）。 */
function extractConflicts(error: unknown): WorkbookFieldChange[] | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response
    ?.data?.detail;
  if (detail && typeof detail === "object" && Array.isArray(
    (detail as { conflicts?: unknown }).conflicts)) {
    return (detail as { conflicts: WorkbookFieldChange[] }).conflicts;
  }
  return null;
}

const PROTECTION_DRIFT_REASONS: Record<string, string> = {
  protection_released_since_export: "该字段的人工保护在你下载后已被解除",
  protection_cycle_changed: "该字段的保护周期在你下载后已变化（期间被解除又重新保护）",
  protection_started_since_export: "该字段在你下载后新建立了人工保护",
  protection_evidence_missing: "工作簿缺少该字段的保护基准证据（旧版导出）",
};

function describeProtectionDrift(issue: WorkbookProtectionDriftIssue): string {
  const reason = PROTECTION_DRIFT_REASONS[issue.reason] || issue.reason;
  return [
    issue.sheet ? String(issue.sheet) : null,
    issue.row ? `行 ${issue.row}` : null,
    issue.field ? `字段「${issue.field}」` : null,
    reason,
  ].filter(Boolean).join(" · ");
}

function isProtectionDriftIssue(value: unknown): value is WorkbookProtectionDriftIssue {
  if (!value || typeof value !== "object") return false;
  const issue = value as Partial<WorkbookProtectionDriftIssue>;
  return typeof issue.sheet === "string" && typeof issue.row === "string"
    && typeof issue.field === "string" && typeof issue.reason === "string";
}

/** 从 409 响应里提取保护基准/周期漂移明细（Q3=A，不可强制接管）。 */
function extractProtectionDrift(error: unknown): WorkbookProtectionDriftDetail | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response
    ?.data?.detail;
  if (!detail || typeof detail !== "object") return null;
  const candidate = detail as Partial<WorkbookProtectionDriftDetail>;
  if (candidate.code !== "protection_baseline_drift"
    || typeof candidate.message !== "string"
    || !Array.isArray(candidate.issues) || !candidate.issues.length
    || !candidate.issues.every(isProtectionDriftIssue)) return null;
  return candidate as WorkbookProtectionDriftDetail;
}

function describeConflict(row: WorkbookFieldChange, index: number): string {
  return [
    row.sheet ? String(row.sheet) : null,
    row.row ? String(row.row) : null,
    row.field ? String(row.field) : null,
    `你下载时的值：${row.base || "（空）"}`,
    `当前最新值：${row.old || "（空）"}`,
    `你上传的值：${row.new || "（空）"}`,
  ].filter(Boolean).join(" · ") || `第 ${index + 1} 处`;
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

export default WorkbookRoundTrip;
