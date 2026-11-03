import { Alert, Modal, Space } from "antd";
import { WorkbookExportButton, WorkbookImport } from "../../components/workbook";
import { presalesWorkbooks } from "../../api/presalesWorkbooks";

export default function WorkbookDialog({ mode, inquiryId, onClose, onApplied }: {
  mode: "inquiry" | "quote"; inquiryId?: number; onClose: () => void;
  onApplied: (response: unknown) => void | Promise<void>;
}) {
  const inquiry = mode === "inquiry";
  return <Modal open title={inquiry ? inquiryId ? "上传本单 Excel" : "从 Excel 新建询价" : "上传报价表"}
    width={1000} footer={null} onCancel={onClose} destroyOnHidden>
    <Space direction="vertical" style={{ width: "100%" }}>
      <Alert type="info" message={inquiry
        ? "预览核对后再应用；应用后刷新为 Excel 保存的草稿，页面上尚未保存的修改会被替换。"
        : "支持单张或合并报价表，按文件中的来源分发；应用后价格立即生效，所有人可见。"} />
      {inquiry && !inquiryId && <WorkbookExportButton schemaName="presales_inquiry_lines" label="下载空模板"
        fetchSchema={() => presalesWorkbooks.getInquirySchema()} onExport={presalesWorkbooks.exportInquiryTemplate} filename="询价空模板.xlsx" />}
      <WorkbookImport schemaName={inquiry ? "presales_inquiry_lines" : "presales_quote_sheet"}
        buttonLabel={inquiry ? "上传 Excel" : "上传报价表"}
        previewFile={(file) => inquiry ? presalesWorkbooks.previewInquiry(file, inquiryId) : presalesWorkbooks.previewQuoteSheet(file)}
        successMessage={inquiry ? "询价草稿已保存" : "报价已生效"} onApplied={onApplied} />
    </Space>
  </Modal>;
}
