/**
 * 循环拍照建档页（板块 D，D-6）。
 *
 * 甲方确认的交互（2026-10-07 语音纠正，D-24）：
 * ① 先查 PN（扫码枪回车自动查找）；② 该 PN 已有照片 → 直接提示，不走下一步；
 * ③ 没有照片才进入引导拍照（首次至少一张正面图）；④ 确认上传。
 * 照片挂 PN 档案，不按 SN 逐件拍（D-22）。
 */
import { CameraOutlined, CheckCircleOutlined, InboxOutlined } from "@ant-design/icons";
import { Alert, Button, Card, List, Space, Steps, Typography, Upload } from "antd";
import type { UploadFile } from "antd";
import { useCallback, useState } from "react";
import {
  getArchive, uploadArchiveAttachment, type CirculationArchive,
} from "../../api/circulation";

type Stage = "idle" | "locating" | "has_photos" | "need_photo" | "uploading" | "done";

const PN_PLACEHOLDER = "输入或扫码枪扫入 PN / 序列号";

export default function PhotoArchivePage() {
  const [pn, setPn] = useState("");
  const [stage, setStage] = useState<Stage>("idle");
  const [message, setMessage] = useState<string>("");
  const [archive, setArchive] = useState<CirculationArchive | null>(null);
  const [files, setFiles] = useState<UploadFile[]>([]);

  const locate = useCallback(async () => {
    const target = pn.trim().toUpperCase();
    if (!target) {
      setMessage("请先输入 PN 或扫入序列号");
      return;
    }
    setStage("locating");
    setMessage("");
    setArchive(null);
    setFiles([]);
    try {
      const found = await getArchive(target);
      if (found && found.photo_count > 0) {
        // D-24：已有照片 → 不走下一步
        setArchive(found);
        setStage("has_photos");
        setMessage(`该 PN 已有 ${found.photo_count} 张照片，无需重复拍摄；如需可只补拍细节。`);
      } else {
        setStage("need_photo");
        setMessage(found
          ? "该 PN 档案还没有照片，请拍摄正面图并上传。"
          : "该 PN 尚无循环档案，上传照片后将自动建档。");
      }
    } catch {
      setStage("idle");
      setMessage("查找失败：网络或服务异常，请重试");
    }
  }, [pn]);

  const confirmUpload = useCallback(async () => {
    const target = pn.trim().toUpperCase();
    const payloads = files.filter((f) => f.originFileObj).map((f) => f.originFileObj as File);
    if (payloads.length === 0) {
      setMessage("请先拍摄或选择至少一张正面图");
      return;
    }
    setStage("uploading");
    try {
      let last: CirculationArchive | null = null;
      for (const file of payloads) {
        last = await uploadArchiveAttachment(target, "photo", file);
      }
      setArchive(last);
      setStage("done");
      setMessage(`已上传 ${payloads.length} 张照片。可上架还需补齐检测报告（由检测环节提供）。`);
      setFiles([]);
    } catch {
      setStage("need_photo");
      setMessage("上传失败：网络或服务异常，请重试");
    }
  }, [files, pn]);

  return (
    <Card title="循环拍照建档" style={{ maxWidth: 720, margin: "0 auto" }}>
      <Steps
        size="small"
        current={stage === "idle" || stage === "locating" ? 0
          : stage === "need_photo" ? 1
          : stage === "uploading" ? 2 : 3}
        items={[{ title: "定位档案" }, { title: "引导拍照" }, { title: "确认上传" }]}
      />
      <div style={{ marginTop: 24 }}>
        <Typography.Title level={5}>① 定位档案</Typography.Title>
        <Space.Compact style={{ width: "100%" }}>
          <input
            data-testid="pn-input"
            aria-label={PN_PLACEHOLDER}
            value={pn}
            disabled={stage === "locating" || stage === "uploading"}
            onChange={(e) => setPn(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void locate(); }}
            placeholder={PN_PLACEHOLDER + "，回车自动查找"}
            style={{ flex: 1, height: 32, padding: "0 11px", border: "1px solid #d9d9d9", borderRadius: "6px 0 0 6px" }}
          />
          <Button type="primary" onClick={() => void locate()} loading={stage === "locating"}>查找</Button>
        </Space.Compact>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          扫码枪回车自动查找。首次拍照至少传一张正面图；已有主图可只补拍细节。
        </Typography.Text>
      </div>

      {message && (
        <div style={{ marginTop: 16 }}>
          <Alert
            type={stage === "has_photos" ? "info" : stage === "done" ? "success"
              : stage === "idle" ? "warning" : "info"}
            showIcon={stage === "done" || stage === "has_photos"}
            icon={stage === "done" ? <CheckCircleOutlined /> : stage === "has_photos" ? <CameraOutlined /> : undefined}
            message={message}
          />
        </div>
      )}

      {stage === "has_photos" && archive && (
        <div style={{ marginTop: 16 }}>
          <List
            size="small"
            bordered
            dataSource={archive.attachments.filter((a) => a.kind === "photo")}
            renderItem={(a) => (
              <List.Item>
                <List.Item.Meta title={a.original_filename}
                  description={`上传人 ${a.uploaded_by} · ${a.uploaded_at.slice(0, 16).replace("T", " ")}`} />
              </List.Item>
            )}
          />
          <Button style={{ marginTop: 12 }} onClick={() => { setStage("need_photo"); setMessage("补拍细节：拍摄后确认上传，不会替换已有照片。"); }}>
            仍要补拍细节
          </Button>
        </div>
      )}

      {(stage === "need_photo" || stage === "uploading") && (
        <div style={{ marginTop: 16 }}>
          <Typography.Title level={5}>② 引导拍照</Typography.Title>
          <Upload.Dragger
            data-testid="photo-picker"
            accept="image/*"
            multiple
            beforeUpload={() => false}              // 不自动上传：收集到"确认上传"
            fileList={files}
            onChange={({ fileList }) => setFiles(fileList)}
            disabled={stage === "uploading"}
          >
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">拍摄或选择照片（可多张）</p>
            <p className="ant-upload-hint">正面图必传；细节图可选。照片挂在 PN 档案下，不按 SN 逐件拍。</p>
          </Upload.Dragger>
          <Space style={{ marginTop: 16 }}>
            <Button type="primary" loading={stage === "uploading"} onClick={() => void confirmUpload()}
                    disabled={files.length === 0}>
              ③ 确认上传（{files.length} 张）
            </Button>
          </Space>
        </div>
      )}

      {stage === "done" && (
        <div style={{ marginTop: 16 }}>
          <Alert type="success" showIcon message="建档照片已上传" />
          <Button style={{ marginTop: 12 }} onClick={() => { setStage("idle"); setPn(""); setMessage(""); setArchive(null); }}>
            继续下一个 PN
          </Button>
        </div>
      )}
    </Card>
  );
}
