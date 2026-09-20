import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, Button, DatePicker, Form, Input, InputNumber, Modal, Select, Space, Typography, message,
} from "antd";
import type { Dayjs } from "dayjs";
import dayjs from "dayjs";
import type { MaintenanceProject } from "../../api/maintenanceProjects";
import {
  listMaintenanceProjects,
  searchMaintenanceProjects,
} from "../../api/maintenanceProjects";
import { createDemandLine } from "../../api/maintenanceDemands";
import PartPicker from "../../components/PartPicker";

const { Text } = Typography;

/** 与后端 serial_numbers / description 列宽一致（v1.36 全链路扩容口径）。 */
const TEXT_FIELD_MAX = 32767;

/** 幂等键：demand-line- + crypto UUID（满足后端 8–128 字符 [A-Za-z0-9._:-]+）。 */
function idempotencyKey(): string {
  const suffix = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `demand-line-${suffix}`;
}

function readError(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null) {
    const detail = (error as { response?: { data?: { detail?: unknown } } })
      .response?.data?.detail;
    if (typeof detail === "string" && detail) return detail;
  }
  return fallback;
}

function isUnknownOutcome(error: unknown): boolean {
  /** 结果未知＝没收到响应（网络层失败）或 5xx（服务器可能已写入）。 */
  const status = (error as { response?: { status?: number } })?.response?.status;
  return status === undefined || status >= 500;
}

function isFormValidationError(error: unknown): boolean {
  return typeof error === "object" && error !== null && "errorFields" in error;
}

interface CreateFormValues {
  project_id?: string;
  order_date: Dayjs;
  qty: number;
  return_qty?: number;
  serial_numbers?: string;
  description?: string;
  reason: string;
}

/**
 * 页面直建手工需求行（v1.36 Phase E）：创建独立新单（page_manual 来源，
 * 不参与氚云删单比对）。两种入口共用：
 * - 项目面板（PartsOrdersTab）：fixedProjectId 固定当前项目，用户不接触内部 UUID；
 * - 全局需求单页（MaintenanceDemandsPage）：项目走 list/search 接口可选可搜
 *   （active only，覆盖分页）。
 *
 * 幂等契约：一份归一化 payload（项目/PN/日期/数量/SN/描述/原因）绑定一个 key；
 * 结果未知的失败（网络断/5xx）重试同内容复用 key（后端按完整请求指纹识别重放，
 * 不会重复建行）；改任何内容＝新 key；业务成功后清空，下一次新增用新 key；
 * 409（服务端明确拒绝：key 对应行已被作废/迁移、同 key 内容已变化）不清 key
 * ——同 key 重试只会再被拒，用户必须显式改 payload（自然换新 key）或新开对话框。
 *
 * 并发契约（epoch 隔离）：
 * - 同步 ref 锁在任何 await 之前挡住快速双击，一次只有一个在途请求；
 * - fixedProjectId 变化（父级路由切项目）/全局模式换项目/卸载都推进 epoch，
 *   旧请求的成功/错误/finally 一律不碰新对话框的状态、不解锁、不清 attempt、
 *   不回调（成功既成事实也只在同代内提示/关闭/回读）；
 * - 提交期间整个 Form disabled，旧请求的返回不能关闭新输入。
 */
export default function DemandLineCreate({
  fixedProjectId,
  fixedProjectLabel,
  onClose,
  onCreated,
}: {
  /** 面板模式：固定当前项目（全局模式不传，走项目选择器）。 */
  fixedProjectId?: string;
  /** 固定项目的展示名（如项目面板的 exportBase）；缺省只显示「当前项目」。 */
  fixedProjectLabel?: string;
  onClose: () => void;
  /** 创建成功后的回读钩子（父级刷新列表/指标）。只在同代（未过期）时调用。 */
  onCreated: () => void;
}) {
  const [form] = Form.useForm<CreateFormValues>();
  const [submitting, setSubmitting] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  /** 已选型号主数据（PartPicker 只回传 part_id + item；提交用 pn_std 原文）。 */
  const [pickedPart, setPickedPart] = useState<{ part_id: number; pn_std: string } | null>(null);
  /** 一次创建尝试：归一化 payload 指纹 + 绑定的幂等键。 */
  const attemptRef = useRef<{ payload: string; key: string } | null>(null);
  const submitLockRef = useRef(false);
  /** 对话框代次：fixedProjectId 变化/换项目/卸载推进；过期请求不得碰任何状态。 */
  const epochRef = useRef(0);

  // ---- 全局模式：项目远程选择（active only，空搜索拉全分页，关键字走 search） ----
  const [projectOptions, setProjectOptions] = useState<MaintenanceProject[]>([]);
  const [projectLoading, setProjectLoading] = useState(false);
  const [projectError, setProjectError] = useState<string | null>(null);
  const projectGen = useRef(0);
  const projectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadProjects = useCallback(async (q?: string) => {
    const g = ++projectGen.current;
    setProjectLoading(true);
    setProjectError(null);
    try {
      if (q && q.trim()) {
        const resp = await searchMaintenanceProjects({
          q: q.trim(), page: 1, page_size: 50, include_inactive: false,
        });
        if (g !== projectGen.current) return;
        setProjectOptions(resp.data.rows);
      } else {
        // 空搜索：拉全 active 项目（覆盖分页，后端单页上限 200）
        const pageSize = 200;
        const all: MaintenanceProject[] = [];
        let page = 1;
        let total = 0;
        do {
          const resp = await listMaintenanceProjects({
            page, page_size: pageSize, include_inactive: false,
          });
          if (g !== projectGen.current) return;
          total = resp.data.total;
          all.push(...resp.data.rows);
          if (!resp.data.rows.length) break;
          page += 1;
        } while (all.length < total);
        if (g !== projectGen.current) return;
        setProjectOptions(all);
      }
    } catch (err) {
      if (g === projectGen.current) {
        // 真实错误 + 重试：不能和「没搜到」混成一句空态提示
        setProjectOptions([]);
        setProjectError(readError(err, "项目列表加载失败"));
      }
    } finally {
      if (g === projectGen.current) setProjectLoading(false);
    }
  }, []);

  useEffect(() => {
    if (fixedProjectId) return; // 面板模式不需要项目列表
    void loadProjects();
    return () => { projectGen.current += 1; };
  }, [fixedProjectId, loadProjects]);

  const onProjectSearch = (value: string) => {
    if (projectTimer.current) clearTimeout(projectTimer.current);
    projectTimer.current = setTimeout(() => { void loadProjects(value); }, 300);
  };

  /**
   * 全局模式换项目＝新对话框代次：清幂等尝试（旧项目 payload 不能带到新项目
   * 重试）、清已选 PN/错误，推进 epoch 让旧在途请求（被 Form disabled 挡住，
   * 双保险）全部过期。
   */
  const onProjectChange = () => {
    epochRef.current += 1;
    submitLockRef.current = false;
    attemptRef.current = null;
    setPickedPart(null);
    setSaveError(null);
    setSubmitting(false);
  };

  // 面板模式切项目（父级路由变化）：整代作废——epoch 推进、锁复位、表单/PN/尝试/
  // 错误全部清空。父级同时以 key={projectId} 挂载本组件，这里兜底防复用实例。
  useEffect(() => {
    epochRef.current += 1;
    submitLockRef.current = false;
    attemptRef.current = null;
    setPickedPart(null);
    setSaveError(null);
    setSubmitting(false);
    form.resetFields();
  }, [fixedProjectId, form]);

  useEffect(() => () => {
    // 卸载＝close：推进 epoch，让在途请求全部过期
    epochRef.current += 1;
    if (projectTimer.current) clearTimeout(projectTimer.current);
  }, []);

  const doSubmit = async () => {
    if (submitLockRef.current) return; // 同步锁：在任何 await 之前挡住快速双击
    submitLockRef.current = true;
    const epoch = epochRef.current;
    setSubmitting(true);
    try {
      const values = await form.validateFields();
      if (epochRef.current !== epoch) return;
      if (!pickedPart) {
        setSaveError("请先选择型号（PN），从主数据搜索选中，不能手填");
        return;
      }
      // 归一化 payload：指纹与实发请求逐字段一致，改内容必换 key
      const request = {
        order_date: values.order_date.format("YYYY-MM-DD"),
        project_id: fixedProjectId ?? values.project_id!,
        pn_std: pickedPart.pn_std,
        qty: values.qty,
        return_qty: values.return_qty ?? 0,
        serial_numbers: values.serial_numbers?.trim() || null,
        description: values.description?.trim() || null,
        reason: values.reason.trim(),
      };
      const fingerprint = JSON.stringify(request);
      if (!attemptRef.current || attemptRef.current.payload !== fingerprint) {
        attemptRef.current = { payload: fingerprint, key: idempotencyKey() };
      }
      const resp = await createDemandLine({
        ...request, idempotency_key: attemptRef.current.key,
      });
      // API 返回后第一件事：过期代不碰任何东西——不清 attempt（新代的尝试
      // 不能被旧代成功清掉）、不回调、不提示、不关闭。
      if (epochRef.current !== epoch) return;
      // 业务成功：清空尝试，下一次新增用新 key
      attemptRef.current = null;
      message.success(`已创建手工需求单 ${resp.data.order_no ?? ""}（独立新单，不参与氚云删单比对）`);
      onCreated();
      onClose();
    } catch (error) {
      if (epochRef.current !== epoch) return;
      if (isFormValidationError(error)) return; // 字段错误由 antd 行内展示
      if (isUnknownOutcome(error)) {
        // 结果未知：保留 key，同内容重试复用（后端指纹比对不会重复建行）
        setSaveError(`${readError(error, "网络异常，创建结果未知")}；可直接重试——相同内容会复用幂等键，不会重复建行`);
      } else {
        // 已知被拒（400/403/404/409/422）：保留 key 与错误展示——尤其 409（key
        // 对应行已被作废/迁移、同 key 内容已变化）重试同 key 只会再被拒，
        // 必须由用户显式改 payload（自然换新 key）或新开对话框。
        setSaveError(readError(error, "创建失败；请核对项目、PN 与字段范围"));
      }
    } finally {
      if (epochRef.current === epoch) {
        setSubmitting(false);
        submitLockRef.current = false;
      }
    }
  };

  return (
    <Modal
      open
      width={680}
      title="新增手工需求行"
      okText="创建需求行"
      cancelText="取消"
      confirmLoading={submitting}
      onCancel={() => { if (!submitting) onClose(); }}
      onOk={() => { void doSubmit(); }}
      maskClosable={false}
    >
      <Space direction="vertical" size={10} style={{ width: "100%" }}>
        <Alert
          type="info"
          showIcon
          message="手工需求是独立新单（页面创建，不参与氚云删单比对）；创建后可再逐行编辑/撤销。"
        />
        {saveError ? (
          <Alert type="error" showIcon message={saveError} />
        ) : null}
        <Form
          form={form}
          layout="vertical"
          disabled={submitting}
          initialValues={{ order_date: dayjs(), return_qty: 0 }}
        >
          {fixedProjectId ? (
            <Form.Item label="项目（当前项目，固定）">
              <Text>{fixedProjectLabel ?? "当前项目"}</Text>
            </Form.Item>
          ) : (
            <Form.Item
              name="project_id"
              label="项目"
              rules={[{ required: true, message: "请选择项目" }]}
            >
              <Select
                showSearch
                filterOption={false}
                loading={projectLoading}
                placeholder="选择项目（可按名称/编码搜索）"
                onSearch={onProjectSearch}
                onChange={onProjectChange}
                notFoundContent={projectError
                  ? "项目列表加载失败"
                  : projectLoading ? "加载中…" : "输入项目名/编码搜索"}
                options={projectOptions.map((p) => ({
                  value: p.project_id,
                  label: `${p.display_name}（${p.project_code}）`,
                }))}
              />
            </Form.Item>
          )}
          {projectError && !fixedProjectId ? (
            <Alert
              type="error"
              showIcon
              message={projectError}
              action={<Button size="small" onClick={() => { void loadProjects(); }}>重试</Button>}
              style={{ marginBottom: 12 }}
            />
          ) : null}
          <Form.Item
            name="order_date"
            label="制单日期"
            rules={[{ required: true, message: "请选择制单日期" }]}
          >
            <DatePicker style={{ width: 180 }} />
          </Form.Item>
          <Form.Item
            label="型号（PN，从主数据选择）"
            required
          >
            <PartPicker
              value={pickedPart?.part_id ?? null}
              onChange={(partId, item) => {
                setPickedPart(item && partId != null
                  ? { part_id: partId, pn_std: item.pn_std }
                  : null);
              }}
              disabled={submitting}
            />
          </Form.Item>
          <Space size={12}>
            <Form.Item
              name="qty"
              label="需求数量"
              rules={[
                { required: true, message: "数量必填" },
                { validator: (_r, v: number) =>
                    typeof v === "number" && Number.isFinite(v) && v > 0
                      ? Promise.resolve()
                      : Promise.reject(new Error("数量必须是大于 0 的数值")) },
              ]}
            >
              <InputNumber min={0} precision={3} style={{ width: 140 }} />
            </Form.Item>
            <Form.Item
              name="return_qty"
              label="退货数量"
              rules={[{ validator: (_r, v: number | undefined) =>
                    v == null || (Number.isFinite(v) && v >= 0)
                      ? Promise.resolve()
                      : Promise.reject(new Error("退货数量必须 ≥ 0")) },
              ]}
            >
              <InputNumber min={0} precision={3} style={{ width: 140 }} />
            </Form.Item>
          </Space>
          <Form.Item
            name="serial_numbers"
            label="SN（可选，每行一个）"
            extra="逐行扫描或整段粘贴（换行分隔）；超长不截断，超 32767 字符会被拒绝。"
          >
            <Input.TextArea
              rows={4}
              placeholder={"扫码枪逐个扫描（每扫一个自动换行），或从 Excel 整列粘贴：\nSN-A001\nSN-A002"}
              style={{ fontFamily: "monospace" }}
            />
          </Form.Item>
          <Form.Item
            name="description"
            label="描述（可选）"
            extra="超长不截断，超 32767 字符会被拒绝。"
          >
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item
            name="reason"
            label="创建原因（必填，审计留痕）"
            rules={[
              { required: true, message: "创建原因必填" },
              { validator: (_r, v: string) =>
                  v && v.trim() ? Promise.resolve()
                    : Promise.reject(new Error("创建原因不能是空白")) },
            ]}
          >
            <Input.TextArea
              rows={2}
              placeholder="如：氚云漏单，按现场需求补录"
            />
          </Form.Item>
        </Form>
      </Space>
    </Modal>
  );
}
