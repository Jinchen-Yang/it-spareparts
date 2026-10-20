import type { ReactNode } from "react";
import { App, ConfigProvider, Modal } from "antd";
import { act, cleanup, render as rtlRender, type RenderOptions } from "@testing-library/react";

const theme = { token: { motion: false } };

function WithoutMotion({ children }: { children: ReactNode }) {
  return <ConfigProvider theme={theme}>{children}</ConfigProvider>;
}

// 仍在使用的静态 Modal 也关闭动画；通知由 App 上下文随 RTL 树一起卸载。
ConfigProvider.config({ holderRender: (children) => <WithoutMotion>{children}</WithoutMotion> });

export function renderWithoutMotion(ui: ReactNode, options: RenderOptions = {}) {
  const Wrapper = options.wrapper;
  function WithApp({ children }: { children: ReactNode }) {
    return (
      <WithoutMotion>
        {Wrapper ? <Wrapper><App>{children}</App></Wrapper> : <App>{children}</App>}
      </WithoutMotion>
    );
  }
  return rtlRender(ui, { ...options, wrapper: WithApp });
}

export * from "@testing-library/react";
export { renderWithoutMotion as render };

export async function cleanupAntd() {
  cleanup();
  // 静态 Modal 不归 RTL cleanup 管理，等待它的销毁任务和 React 提交完成。
  await act(async () => {
    Modal.destroyAll();
  });
}
