import { Component, type ReactNode } from "react";
import { Button, Result } from "antd";

type Props = { children: ReactNode };
type State = { hasError: boolean };

/**
 * 全局错误边界：任一子组件渲染期抛未捕获异常时，展示友好兜底页而非整页白屏，
 * 并提供「刷新 / 退出登录」自救路径（审计 2026-06-28 U-1）。面向非技术业务用户。
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: unknown, info: unknown) {
    // 仅落控制台，避免在错误处理里再抛错；生产可在此接入前端错误上报。
    console.error("页面渲染异常：", error, info);
  }

  private reload = () => window.location.reload();

  private relogin = () => {
    try {
      ["token", "role", "name", "permissions"].forEach((k) => localStorage.removeItem(k));
    } catch {
      /* localStorage 不可用时忽略，仍跳登录 */
    }
    window.location.href = "/";
  };

  render() {
    if (!this.state.hasError) return this.props.children;
    return (
      <Result
        status="warning"
        title="页面出错了"
        subTitle="抱歉，页面遇到异常。可先尝试刷新；若仍无法恢复，请退出后重新登录。"
        extra={[
          <Button type="primary" key="reload" onClick={this.reload}>刷新页面</Button>,
          <Button key="relogin" onClick={this.relogin}>退出登录</Button>,
        ]}
      />
    );
  }
}
