import { beforeEach, expect, it, vi } from "vitest";
import { api } from "../../../api";
import {
  claimWorkItem, completeWorkItem, declineWorkItem, getWorkBadge, listSubjectEvents,
  listWorkCandidates, listWorkItems, reassignWorkItem, releaseWorkItem, startWorkItem,
  transferWorkItem,
} from "../../../api/workItems";

vi.mock("../../../api", () => ({ api: { get: vi.fn(), post: vi.fn() } }));

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.get).mockResolvedValue({ data: { items: [], total: 0 } });
  vi.mocked(api.post).mockResolvedValue({ data: { id: 8, version: 4 } });
});

it("列表、角标和候选人复用登录客户端并传递取消信号", async () => {
  const controller = new AbortController();
  const params = { tab: "pool" as const, subject_type: "demo", page: 2, page_size: 20 };
  await listWorkItems(params, controller.signal);
  expect(api.get).toHaveBeenLastCalledWith("/work-items", { params, signal: controller.signal });
  await getWorkBadge(controller.signal);
  expect(api.get).toHaveBeenLastCalledWith("/work-items/badge", { signal: controller.signal });
  await listWorkCandidates("demo_pool", controller.signal);
  expect(api.get).toHaveBeenLastCalledWith("/work-items/candidates", {
    params: { pool_key: "demo_pool" }, signal: controller.signal,
  });
});

it.each([
  ["claim", claimWorkItem], ["start", startWorkItem], ["release", releaseWorkItem],
  ["transfer", transferWorkItem], ["reassign", reassignWorkItem],
  ["complete", completeWorkItem], ["decline", declineWorkItem],
] as const)("%s 保留版本和操作参数，并返回最新待办", async (action, perform) => {
  const body = { expected_version: 3, reason: "交接", to_user_id: 9 };
  expect(await perform(8, body)).toEqual({ id: 8, version: 4 });
  expect(api.post).toHaveBeenCalledWith(`/work-items/8/${action}`, body);
});

it("时间线按业务身份编码地址并传递分页", async () => {
  const controller = new AbortController();
  await listSubjectEvents("demo/type", 10, { page: 2, page_size: 20 }, controller.signal);
  expect(api.get).toHaveBeenCalledWith("/subjects/demo%2Ftype/10/events", {
    params: { page: 2, page_size: 20 }, signal: controller.signal,
  });
});
