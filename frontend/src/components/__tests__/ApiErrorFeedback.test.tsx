import { afterEach, expect, it, vi } from "vitest";
import { cleanupAntd, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import { ApiErrorFeedback, useApiError } from "../ApiErrorFeedback";
import { apiErrorDiagnostic, parseApiError } from "../../utils/apiError";

afterEach(async () => { await cleanupAntd(); vi.restoreAllMocks(); });

it("展开后可点击复制诊断信息，复制失败提供手动方式", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  const info = parseApiError({ response: { status: 403, headers: { "x-request-id": "request-1" } } });
  render(<ApiErrorFeedback info={info} />);
  expect(screen.getByText("当前账号没有此操作权限")).toBeVisible();
  expect(screen.getByRole("button", { name: "复制诊断信息", hidden: true })).not.toBeVisible();
  fireEvent.click(screen.getByText("查看诊断信息"));
  fireEvent.click(screen.getByRole("button", { name: "复制诊断信息" }));
  await screen.findByRole("button", { name: "已复制诊断信息" });
  expect(writeText).toHaveBeenCalledWith(apiErrorDiagnostic(info));
  writeText.mockRejectedValueOnce(new Error("clipboard denied"));
  fireEvent.click(screen.getByRole("button", { name: "已复制诊断信息" }));
  expect(await screen.findByRole("status")).toHaveTextContent("手动复制");
});

it("原版本冲突与刷新失败均可诊断，处理函数不会抛出第二次异常", async () => {
  const refresh = vi.fn().mockRejectedValue({ response: { status: 503, headers: { "x-request-id": "refresh-2" } } });
  const done = vi.fn();
  function Feedback() {
    const handle = useApiError();
    return <button onClick={() => void handle({ config: { url: "/presales/inquiries/1", method: "patch" }, response: {
      status: 409, headers: { "x-request-id": "write-1" }, data: { detail: { code: "version_conflict", message: "原记录版本已更新" } },
    } }, refresh).then(done)}>保存</button>;
  }
  render(<Feedback />);
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(done).toHaveBeenCalledOnce());
  expect(refresh).toHaveBeenCalledOnce();
  expect(screen.getByText("原记录版本已更新")).toBeVisible();
  expect(await screen.findByText(/自动刷新也未成功/)).toBeVisible();
  fireEvent.click(screen.getByText("查看诊断信息"));
  expect(screen.getByText(/请求标识: write-1/)).toHaveTextContent("请求标识: refresh-2");
});

it.each(["duplicate_pn", "existing_offer", "conflict"])("%s 不刷新编辑内容", async (code) => {
  const refresh = vi.fn();
  const done = vi.fn();
  function Feedback() {
    const handle = useApiError();
    return <button onClick={() => void handle({ response: { status: 409, data: { detail: { code } } } }, refresh).then(done)}>保存</button>;
  }
  render(<Feedback />);
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(done).toHaveBeenCalledOnce());
  expect(refresh).not.toHaveBeenCalled();
});

it("写入成功后的刷新失败明确阻止用户误判为写入失败", async () => {
  function Feedback() {
    const handle = useApiError();
    return <button onClick={() => handle.afterSuccess({ response: { status: 503 } }, "建档")}>刷新</button>;
  }
  render(<Feedback />);
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  expect(await screen.findByText("建档已成功，但页面刷新失败，请勿重复提交。")).toBeVisible();
});
