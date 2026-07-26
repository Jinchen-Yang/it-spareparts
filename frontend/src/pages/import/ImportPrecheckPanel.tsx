import { Alert, Card, Descriptions, List, Space, Tag, Typography } from "antd";
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

export default function ImportPrecheckPanel({ result }: { result: ImportPrecheckResult }) {
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
