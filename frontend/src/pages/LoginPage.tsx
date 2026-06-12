import { useState } from "react";
import { Card, Form, Input, Button, message } from "antd";
import { COLORS } from "../theme";
import api from "../api";

export default function LoginPage({ onLogin }: { onLogin: (token: string) => void }) {
  const [loading, setLoading] = useState(false);

  const submit = async (v: { username: string; password: string }) => {
    setLoading(true);
    try {
      const { data } = await api.post("/auth/login", v);
      localStorage.setItem("token", data.token);
      localStorage.setItem("role", data.role);
      onLogin(data.token);
    } catch {
      message.error("用户名或密码错误");
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
        <Form onFinish={submit} initialValues={{ username: "admin" }} layout="vertical">
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}>
            <Input autoFocus />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={loading} block>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
