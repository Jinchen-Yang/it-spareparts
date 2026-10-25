import { expect, it, vi } from "vitest";
const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("../../api", () => ({ api: { get }, masterEdit: vi.fn() }));
import { suggestParts } from "../search";

it("实时联想客户端传递 q、limit 和取消信号，返回后端完整条目", async () => {
  const signal = new AbortController().signal;
  const items = [{ part_id: 42, pn_std: "PN-42" }];
  get.mockResolvedValue({ data: items });
  expect(await suggestParts(" PN ", 30, signal)).toEqual(items);
  expect(get).toHaveBeenCalledWith("/parts/suggest", { params: { q: " PN ", limit: 30 }, signal });
});
