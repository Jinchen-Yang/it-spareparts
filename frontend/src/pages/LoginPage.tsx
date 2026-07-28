import { useState } from "react";
import { Card, Form, Input, Button, Alert } from "antd";
import { COLORS } from "../theme";
import api from "../api";
import LoginChangePasswordModal from "../components/LoginChangePasswordModal";
import { clearSessionScopedPreferences } from "../sessionPreferences";

interface LoginSessionData {
  token: string;
  role: string;
  name?: string | null;
  permissions?: Record<string, boolean> | null;
}

export function persistLoginSession(data: LoginSessionData) {
  // 个人口径是会话级临时偏好，绝不能由上一个账号继承。
  clearSessionScopedPreferences();
  localStorage.setItem("role", data.role);
  localStorage.setItem("name", data.name || data.role);
  localStorage.setItem("permissions", JSON.stringify(data.permissions || {}));
  // token 是跨标签页的会话提交标志；最后写，确保接收方读取到完整元数据。
  localStorage.setItem("token", data.token);
}

export default function LoginPage({ onLogin }: { onLogin: (token: string) => void }) {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [changePwOpen, setChangePwOpen] = useState(false);

  const submit = async (v: { username: string; password: string }) => {
    setLoading(true);
    setErr(null);
    try {
      const { data } = await api.post("/auth/login", v);
      persistLoginSession(data);
      onLogin(data.token);
    } catch (e: any) {
      const status = e?.response?.status;
      const msg =
        status === 401 ? "用户名或密码错误，请核对后重试"
        : !e?.response ? "无法连接服务器，请检查网络后重试"
        : e?.response?.data?.detail || `登录失败（${status}），请稍后重试`;
      setErr(msg);   // 仅行内 Alert，不再叠加全局 toast（去重复提示）
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", paddingTop: 120, background: COLORS.page, minHeight: "100vh" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 20 }}>
        <span style={{ width: 30, height: 30, borderRadius: 9, background: COLORS.accent, display: "inline-flex", alignItems: "center", justifyContent: "center", color: "#F3F8FA", fontWeight: 500 }}>IT</span>
        <span style={{ fontSize: 18, fontWeight: 500, color: COLORS.text }}>备件智能管理系统</span>
      </div>
      <Card style={{ width: 360 }}>
        <Form form={form} onFinish={submit} layout="vertical">
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}>
            <Input autoFocus onChange={() => setErr(null)} />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}>
            <Input.Password onChange={() => setErr(null)} />
          </Form.Item>
          {err && <Alert type="error" message={err} showIcon style={{ marginBottom: 14 }} />}
          <Button type="primary" htmlType="submit" loading={loading} block>
            登录
          </Button>
          <div style={{ textAlign: "center", marginTop: 12 }}>
            <Button type="link" size="small" onClick={() => setChangePwOpen(true)}
                    style={{ color: COLORS.text2 }}>
              修改密码
            </Button>
          </div>
        </Form>
      </Card>
      <LoginChangePasswordModal
        open={changePwOpen}
        onClose={() => setChangePwOpen(false)}
        onDone={(username) => { form.setFieldsValue({ username }); setErr(null); }}
      />
    </div>
  );
}
