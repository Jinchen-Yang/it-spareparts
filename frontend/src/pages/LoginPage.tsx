import { useState } from "react";
import { Card, Form, Input, Button, message } from "antd";
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
    <div style={{ display: "flex", justifyContent: "center", paddingTop: 120, background: "#f0f2f5", minHeight: "100vh" }}>
      <Card title="登录 · IT 备件智能管理系统" style={{ width: 360 }}>
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
