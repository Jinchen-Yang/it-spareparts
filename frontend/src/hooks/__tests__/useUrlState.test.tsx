import { act, cleanup, renderHook } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate, useNavigationType } from "react-router-dom";
import { afterEach, expect, it } from "vitest";
import type { ReactNode } from "react";
import { useUrlState } from "../useUrlState";

const fields = {
  q: { defaultValue: "" },
  tab: { defaultValue: "parts", isValid: (value: string) => ["parts", "issues"].includes(value) },
};
afterEach(cleanup);

function setup(entry: string) {
  return renderHook(() => {
    const [state, patch] = useUrlState(fields);
    return { state, patch, location: useLocation(), navigate: useNavigate(), action: useNavigationType() };
  }, { wrapper: ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={["/before", entry]} initialIndex={1}>{children}</MemoryRouter>
  ) });
}

it("读取声明键，更新保留无关键和重复参数，中文特殊字符可往返", () => {
  const { result } = setup("/governance?tab=issues&external=a&external=b");
  expect(result.current.state).toEqual({ q: "", tab: "issues" });
  act(() => result.current.patch({ q: "硬盘 A&B + /?" }));
  expect(result.current.state).toEqual({ q: "硬盘 A&B + /?", tab: "issues" });
  const query = new URLSearchParams(result.current.location.search);
  expect(query.getAll("external")).toEqual(["a", "b"]);
  expect(query.get("q")).toBe("硬盘 A&B + /?");
});

it.each(["", "?tab=", "?tab=unknown"])("缺省/空/非法值回退默认，读取不改写链接 %s", (search) => {
  const { result } = setup(`/governance${search}`);
  expect(result.current.state).toEqual({ q: "", tab: "parts" });
  expect(result.current.location.search).toBe(search);
  expect(result.current.action).toBe("POP");
});

it.each(["parts", "", null, undefined, "unknown"])("写默认/空/非法值删除键 %s", (tab) => {
  const { result } = setup("/governance?tab=issues&q=old&external=kept");
  act(() => result.current.patch({ tab, q: "" }));
  expect(result.current.location.search).toBe("?external=kept");
  expect(result.current.state).toEqual({ q: "", tab: "parts" });
});

it("筛选和标签变化都 replace；离页再后退恢复最终状态，前进同样恢复", () => {
  const { result } = setup("/governance");
  act(() => result.current.patch({ tab: "issues", q: "A" }));
  act(() => result.current.patch({ q: "B" }));
  expect(result.current.action).toBe("REPLACE");
  act(() => result.current.navigate("/after"));
  act(() => result.current.navigate(-1));
  expect(result.current.state).toEqual({ tab: "issues", q: "B" });
  act(() => result.current.navigate(-1));
  expect(result.current.location.pathname).toBe("/before");
  act(() => result.current.navigate(1));
  expect(result.current.state).toEqual({ tab: "issues", q: "B" });
});
