import { act, screen } from "@testing-library/react";
import { message } from "antd";
import { afterEach, describe, expect, it } from "vitest";
import { cleanupAntd, renderWithoutMotion } from "./antdRender";

afterEach(cleanupAntd);

describe("AntD 测试环境清理", () => {
  it("清理 RTL 树外的静态通知，不把通知留到环境销毁后", async () => {
    renderWithoutMotion(<div>页面</div>);
    await act(async () => { message.success("已保存回款"); });
    expect(screen.getByText("已保存回款")).toBeInTheDocument();
    await cleanupAntd();
    expect(screen.queryByText("页面")).not.toBeInTheDocument();
    expect(screen.queryByText("已保存回款")).not.toBeInTheDocument();
  });
});
