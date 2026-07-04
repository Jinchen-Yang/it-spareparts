import { useState } from "react";
import { Modal, Form, Input, Alert, message } from "antd";
import api from "../api";

/**
 * 自助修改密码弹窗。成功后后端会签发新 token（当前会话不掉线，其他设备被踢），
 * 这里热替换 localStorage.token 并通过 onChanged 同步给 App 的 token 状态。
 */
export default function ChangePasswordModal({
  open, onClose, onChanged,
}: {
  open: boolean;
  onClose: () => void;
  onChanged: (token: string) => void;
}) {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const close = () => {
    form.resetFields();
    setErr(null);
    onClose();
  };

  const submit = async () => {
    const v = await form.validateFields();
    setLoading(true);
    setErr(null);
    try {
      const { data } = await api.post("/auth/change-password", {
        current_password: v.current_password,
        new_password: v.new_password,
      });
      localStorage.setItem("token", data.token);   // 换新 token，本会话继续可用
      onChanged(data.token);
      message.success("密码已修改，其他设备需重新登录");
      close();
    } catch (e: any) {
      const status = e?.response?.status;
      setErr(
        status === 401 ? "登录状态已失效，请退出后重新登录"
        : !e?.response ? "无法连接服务器，请检查网络后重试"
        : e?.response?.data?.detail || `修改失败（${status ?? "未知"}），请稍后重试`,
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      title="修改密码"
      open={open}
      onOk={submit}
      onCancel={close}
      okText="确认修改"
      cancelText="取消"
      confirmLoading={loading}
      destroyOnClose
      maskClosable={false}
    >
      <Form form={form} layout="vertical" requiredMark={false} onValuesChange={() => setErr(null)}>
        <Form.Item
          name="current_password"
          label="当前密码"
          rules={[{ required: true, message: "请输入当前密码" }]}
        >
          <Input.Password autoFocus autoComplete="current-password" />
        </Form.Item>
        <Form.Item
          name="new_password"
          label="新密码"
          rules={[
            { required: true, message: "请输入新密码" },
            { min: 6, message: "新密码至少 6 位" },
          ]}
        >
          <Input.Password autoComplete="new-password" placeholder="至少 6 位" />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="确认新密码"
          dependencies={["new_password"]}
          rules={[
            { required: true, message: "请再次输入新密码" },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue("new_password") === value) return Promise.resolve();
                return Promise.reject(new Error("两次输入的新密码不一致"));
              },
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" onPressEnter={submit} />
        </Form.Item>
        {err && <Alert type="error" message={err} showIcon />}
      </Form>
    </Modal>
  );
}
