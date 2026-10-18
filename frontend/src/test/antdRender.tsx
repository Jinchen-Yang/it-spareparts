import type { ReactNode } from "react";
import { ConfigProvider, Modal, message } from "antd";
import { act, cleanup, render } from "@testing-library/react";

const theme = { token: { motion: false } };

function WithoutMotion({ children }: { children: ReactNode }) {
  return <ConfigProvider theme={theme}>{children}</ConfigProvider>;
}

// 静态 message/Modal 在 RTL 树外创建 React root，也要关闭动画。业务断言不依赖
// CSS 动画；避免 leave deadline / 通知定时器随负载跨过 jsdom 环境的销毁边界。
ConfigProvider.config({ holderRender: (children) => <WithoutMotion>{children}</WithoutMotion> });

export function renderWithoutMotion(ui: ReactNode) {
  return render(ui, { wrapper: WithoutMotion });
}

export async function cleanupAntd() {
  cleanup();
  // 通知 root 不归 RTL cleanup 管理。act 要等销毁任务和 React 提交真正完成，
  // 而非销毁后直接结束测试、让后续回调去读已移除的 window。
  await act(async () => {
    Modal.destroyAll();
    message.destroy();
  });
}
