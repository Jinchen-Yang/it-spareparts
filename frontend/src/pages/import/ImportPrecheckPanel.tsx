import { Alert, Button, Card, Descriptions, List, Space, Tag, Typography } from "antd";
import type {
  ImportPrecheckIssue, ImportPrecheckResult, ImportSeverity, ImportSheetAction,
} from "../../api/imports";

const SEVERITY: Record<ImportSeverity | "unknown", { label: string; color: string }> = {
  info: { label: "提示", color: "blue" },
  warning: { label: "警告", color: "orange" },
  error: { label: "错误", color: "red" },
  unknown: { label: "未知", color: "default" },
};

const ACTION: Record<ImportSheetAction, { label: string; color: string }> = {
  selected: { label: "将导入", color: "green" },
  ignored_recognized: { label: "已识别但不导入", color: "orange" },
  ignored_unrecognized: { label: "无法识别且不导入", color: "default" },
};

function recommendation(code: string) {
  if (code === "missing_price_columns") {
    return "请使用包含单价、金额或税字段的视图重新导出；仅确认确实无需金额时才继续。";
  }
  if (code === "duplicate_headers") return "请删除或重命名重复表头后重新预检。";
  if (code === "no_recognized_sheet") return "请重新导出支持的文件或按模板修正表头后重新预检。";
  if (code === "sheet_ignored_recognized") {
    return "当前规则只导入选中页；如需该页请单独导出并上传。";
  }
  // 修复模式删除侧（D-09）。这些提示描述的是系统**将怎样执行**，不是文件有错；
  // 千万不要建议用户「修正后重新预检」——把无销售订单的行删掉再传正是 2026-09-04 丢账的路径。
  if (code === "upsert_blocking_errors") {
    return "修正这些错误行后重新预检；或改用「跳过」模式，只补新行、不作废旧行。";
  }
  if (code === "upsert_void_suppressed_dropped") {
    return "无需修改文件。请勿为了「让删除生效」而删掉无销售订单的行：一单多行的明细行靠单头继承合同号，按单元格是否为空过滤会连带删掉它们。要删旧行请用对应项目的工作簿报销页。";
  }
  if (code === "upsert_void_suppressed_multi_contract" || code === "upsert_void_suppressed_unanchored") {
    return "无需修改文件。只有单合同、带页级锚的项目工作簿报销页才会执行删除；如需删除旧行，从对应项目下载工作簿、在报销页上修改后回传。";
  }
  if (code === "upsert_void_armed") {
    return "确认本表完整覆盖了该合同的全部报销后再继续；不在本表里的旧行会被作废。若只想补新行/改金额，改用「跳过」模式。";
  }
  if (code === "upsert_precheck_skipped") {
    return "本次未预演；导入时仍按同一规则执行。文件较多或较大时请分批预检。";
  }
  if (code === "upsert_precheck_failed") {
    return "该文件预演解析失败；导入时也会失败，请先按提示修正。";
  }
  return "请根据问题说明检查文件，修正后重新预检。";
}

function Issues({ issues }: { issues: ImportPrecheckIssue[] }) {
  if (!issues.length) return null;
  return (
    <List
      size="small"
      dataSource={issues}
      renderItem={(issue) => {
        const advice = recommendation(issue.code);
        return (
          <List.Item>
            <Space align="start">
              <Tag color={SEVERITY[issue.severity].color}>{SEVERITY[issue.severity].label}</Tag>
              <Space direction="vertical" size={2}>
                <span>{issue.message}</span>
                <Typography.Text>处理建议：{advice}</Typography.Text>
              </Space>
              <Typography.Text type="secondary">{issue.code}</Typography.Text>
            </Space>
          </List.Item>
        );
      }}
    />
  );
}

export default function ImportPrecheckPanel({
  result, onOpenBatch,
}: {
  result: ImportPrecheckResult;
  onOpenBatch: (batchId: number) => void;
}) {
  return (
    <Card title="预检结果" size="small" style={{ marginTop: 16 }}>
      {result.contract !== "v2" && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="预检结果无法安全确认"
          description="服务器返回旧版或无效结果，仅供定位文件问题；请联系管理员升级后端后重新预检。"
        />
      )}
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {result.files.map((file, fileIndex) => (
          <Card key={`${file.filename}-${fileIndex}`} type="inner" title={file.filename}>
            <Descriptions size="small" column={{ xs: 1, sm: 3 }}>
              <Descriptions.Item label="识别类型">{file.file_type || "未识别"}</Descriptions.Item>
              <Descriptions.Item label="严重程度">
                <Tag color={SEVERITY[file.severity].color}>{SEVERITY[file.severity].label}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="可导入">
                {file.can_import === null ? "未知" : file.can_import ? "是" : "否"}
              </Descriptions.Item>
            </Descriptions>
            {file.exact_success_match && (
              <Alert
                type={file.blocked_reason === "exact_success_duplicate" ? "success" : "info"}
                showIcon
                style={{ marginTop: 8 }}
                message={file.blocked_reason === "exact_success_duplicate"
                  ? "已成功导入"
                  : "文件与成功批次字节完全相同"}
                description={(
                  <Space direction="vertical" size={2}>
                    <span>{file.blocked_reason === "exact_success_duplicate"
                      ? "文件字节完全相同，系统不会再次导入"
                      : "当前为修复模式，继续后会重新处理；仅新批次完整成功后原批次才标记为已替代"}</span>
                    <Button type="link" size="small" style={{ padding: 0 }}
                      onClick={() => onOpenBatch(file.exact_success_match!.batch_id)}>
                      查看原批次 #{file.exact_success_match.batch_id}
                    </Button>
                  </Space>
                )}
              />
            )}
            {file.warning && <Alert type="warning" showIcon message={file.warning} style={{ marginTop: 8 }} />}
            <Issues issues={file.issues} />
            <Space direction="vertical" size="small" style={{ width: "100%", marginTop: 8 }}>
              {file.sheets.map((sheet, sheetIndex) => (
                <Card
                  key={`${sheet.sheet_name}-${sheetIndex}`}
                  size="small"
                  title={sheet.sheet_name}
                  extra={<Tag color={ACTION[sheet.action].color}>{ACTION[sheet.action].label}</Tag>}
                >
                  <Space wrap size="large">
                    <span>识别类型：{sheet.detected_type || "未识别"}</span>
                    <span>表头行：{sheet.header_row ?? "未识别"}</span>
                    <span>数据行：{sheet.data_rows}</span>
                    <span>重复表头：{sheet.duplicate_headers.length
                      ? sheet.duplicate_headers.join("、") : "无"}</span>
                  </Space>
                  <Issues issues={sheet.issues} />
                </Card>
              ))}
            </Space>
          </Card>
        ))}
      </Space>
    </Card>
  );
}
