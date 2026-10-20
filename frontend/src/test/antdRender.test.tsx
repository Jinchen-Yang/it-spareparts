import { fireEvent, screen } from "@testing-library/react";
import { App, Button } from "antd";
import { afterEach, describe, expect, it } from "vitest";
import { cleanupAntd, renderWithoutMotion } from "./antdRender";

afterEach(cleanupAntd);

describe("AntD 测试环境清理", () => {
  it("通过 App 上下文显示通知，卸载页面时一并清理", async () => {
    function Page() {
      const { message } = App.useApp();
      return <Button onClick={() => message.success("已保存回款")}>保存回款</Button>;
    }
    renderWithoutMotion(<Page />);
    fireEvent.click(screen.getByRole("button", { name: "保存回款" }));
    expect(await screen.findByText("已保存回款")).toBeInTheDocument();
    await cleanupAntd();
    expect(screen.queryByText("保存回款")).not.toBeInTheDocument();
    expect(screen.queryByText("已保存回款")).not.toBeInTheDocument();
  });
});
