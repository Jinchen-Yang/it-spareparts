import { FileExcelOutlined } from "@ant-design/icons";
import { Card, Tag } from "antd";

import type { MaintenanceWorkbookPreview } from "../../api/maintenanceOperations";

export default function WorkbookFourSheetPreview({ preview }: {
  preview: MaintenanceWorkbookPreview;
}) {
  return (
    <div
      data-testid="workbook-four-sheet-preview"
      className="maintenance-project-grid"
      aria-label="完整项目工作簿四表预览"
    >
      {preview.sheets.map((sheet) => (
        <Card key={sheet.code} size="small">
          <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
            <FileExcelOutlined style={{ color: "var(--mb-success)", marginTop: 3 }} />
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 600 }}>{sheet.name}</div>
              <div style={{ marginTop: 5, color: "var(--mb-text-3)", fontSize: 12 }}>
                {sheet.row_count.toLocaleString("zh-CN")} 行
                <Tag style={{ marginInlineStart: 7 }} color={
                  sheet.ownership === "append_only" ? "gold" : "blue"
                }>
                  {sheet.ownership === "append_only" ? "仅回款表尾可追加" : "系统生成"}
                </Tag>
              </div>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}
