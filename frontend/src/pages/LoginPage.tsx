import { useState } from "react";
import { Form, Input, Button, message } from "antd";
import { UserOutlined, LockOutlined, RobotOutlined } from "@ant-design/icons";
import api from "../api";

export default function LoginPage({ onLogin }: { onLogin: (token: string) => void }) {
  const [loading, setLoading] = useState(false);

  const submit = async (v: { username: string; password: string }) => {
    setLoading(true);
    try {
      const { data } = await api.post("/auth/login", v);
      localStorage.setItem("token", data.token);
      localStorage.setItem("role", data.role);
      localStorage.setItem("name", data.name || data.role);
      onLogin(data.token);
    } catch {
      message.error("用户名或密码错误");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{
      minHeight: "100vh",
      background: "linear-gradient(135deg, #0f0f23 0%, #1a1035 50%, #0f172a 100%)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      position: "relative",
      overflow: "hidden",
    }}>
      {/* 背景光晕 */}
      <div style={{
        position: "absolute", width: 500, height: 500, borderRadius: "50%",
        background: "radial-gradient(circle, rgba(79,70,229,0.18) 0%, transparent 70%)",
        top: "10%", left: "15%",
        animation: "blob-move 8s ease-in-out infinite",
      }} />
      <div style={{
        position: "absolute", width: 400, height: 400, borderRadius: "50%",
        background: "radial-gradient(circle, rgba(147,51,234,0.14) 0%, transparent 70%)",
        bottom: "10%", right: "10%",
        animation: "blob-move 10s ease-in-out infinite reverse",
      }} />

      {/* 登录卡片 */}
      <div style={{
        width: 380,
        background: "rgba(255,255,255,0.05)",
        backdropFilter: "blur(20px)",
        WebkitBackdropFilter: "blur(20px)",
        border: "1px solid rgba(255,255,255,0.1)",
        borderRadius: 20,
        padding: "40px 36px",
        boxShadow: "0 24px 64px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.08)",
        animation: "fade-up 0.5s ease both",
        position: "relative",
        zIndex: 1,
      }}>
        {/* Logo + 标题 */}
        <div style={{ textAlign: "center", marginBottom: 32 }}>
          <div style={{
            width: 56, height: 56, borderRadius: 16, margin: "0 auto 16px",
            background: "linear-gradient(135deg, #4f46e5, #9333ea)",
            display: "flex", alignItems: "center", justifyContent: "center",
            boxShadow: "0 0 24px rgba(99,102,241,0.5), 0 8px 20px rgba(0,0,0,0.3)",
          }}>
            <RobotOutlined style={{ fontSize: 26, color: "#fff" }} />
          </div>
          <h1 style={{
            margin: 0, fontSize: 20, fontWeight: 700,
            background: "linear-gradient(90deg, #e0e7ff, #c7d2fe)",
            WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
          }}>
            IT 备件智能管理
          </h1>
          <p style={{ margin: "6px 0 0", color: "rgba(255,255,255,0.4)", fontSize: 13 }}>
            AI 定价助手 · 采购销售分析
          </p>
        </div>

        <Form onFinish={submit} initialValues={{ username: "admin" }} layout="vertical">
          <Form.Item name="username" style={{ marginBottom: 14 }}>
            <Input
              prefix={<UserOutlined style={{ color: "rgba(255,255,255,0.3)" }} />}
              placeholder="用户名"
              autoFocus
              size="large"
              style={{
                background: "rgba(255,255,255,0.07)",
                border: "1px solid rgba(255,255,255,0.12)",
                borderRadius: 10,
                color: "#fff",
                height: 46,
              }}
            />
          </Form.Item>
          <Form.Item name="password" style={{ marginBottom: 24 }}>
            <Input.Password
              prefix={<LockOutlined style={{ color: "rgba(255,255,255,0.3)" }} />}
              placeholder="密码"
              size="large"
              style={{
                background: "rgba(255,255,255,0.07)",
                border: "1px solid rgba(255,255,255,0.12)",
                borderRadius: 10,
                color: "#fff",
                height: 46,
              }}
            />
          </Form.Item>
          <Button
            type="primary"
            htmlType="submit"
            loading={loading}
            block
            size="large"
            style={{
              height: 46,
              borderRadius: 10,
              fontWeight: 600,
              fontSize: 15,
              background: "linear-gradient(135deg, #4f46e5, #7c3aed)",
              border: "none",
              boxShadow: "0 4px 16px rgba(79,70,229,0.4)",
            }}
          >
            {loading ? "登录中…" : "登 录"}
          </Button>
        </Form>

        <div style={{ textAlign: "center", marginTop: 20, color: "rgba(255,255,255,0.2)", fontSize: 12 }}>
          仅限内部使用 · 数据保密
        </div>
      </div>
    </div>
  );
}
