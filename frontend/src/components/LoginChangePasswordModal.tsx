import { useState } from "react";
import { Modal, Form, Input, Alert, message } from "antd";
import api from "../api";

/**
 * 登录页（未登录/登出后）修改密码：用 用户名+当前密码 自证身份，无需先登录。
 * 改成功后不自动登录——所有旧会话失效，提示用新密码登录（并把用户名回填到登录框）。
 */
export default function LoginChangePasswordModal({
  open, onClose, onDone,
}: {
  open: boolean;
  onClose: () => void;
  onDone: (username: string) => void;
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
      await api.post("/auth/change-password-unauth", {
        username: v.username,
        current_password: v.current_password,
        new_password: v.new_password,
      });
      message.success("密码已修改，请用新密码登录");
      onDone(v.username);
      close();
    } catch (e: any) {
      const status = e?.response?.status;
      setErr(
        !e?.response ? "无法连接服务器，请检查网络后重试"
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
        <Form.Item name="username" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}>
          <Input autoFocus autoComplete="username" />
        </Form.Item>
        <Form.Item
          name="current_password"
          label="当前密码"
          rules={[{ required: true, message: "请输入当前密码" }]}
        >
          <Input.Password autoComplete="current-password" />
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
