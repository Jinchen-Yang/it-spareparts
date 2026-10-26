import { beforeEach, expect, it, vi } from "vitest";
import {
  archiveAudienceGroup, createAudienceGroup, listAudienceGroups, listAudiencePeople, updateAudienceGroup,
} from "../audience";
import {
  defineField, getFieldTemplate, listFieldDefinitions, suggestFieldNames, updateFieldDefinition,
} from "../fields";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("../../api", () => ({ api }));

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue({ data: [] });
  api.post.mockResolvedValue({ data: { id: 3 } });
  api.patch.mockResolvedValue({ data: { id: 3 } });
  api.delete.mockResolvedValue({ data: {} });
});

it("受众接口复用统一客户端，用户名作为账号id，分组修改和归档使用id路径", async () => {
  const signal = new AbortController().signal;
  await expect(listAudiencePeople("杨", signal)).resolves.toEqual([]);
  expect(api.get).toHaveBeenCalledWith("/audience/people", { params: { q: "杨" }, signal });
  await listAudienceGroups(signal);
  expect(api.get).toHaveBeenCalledWith("/audience/groups", { signal });
  const group = { name: "采购", scope: "personal" as const, user_ids: ["alice"] };
  await expect(createAudienceGroup(group)).resolves.toEqual({ id: 3 });
  expect(api.post).toHaveBeenCalledWith("/audience/groups", group);
  await updateAudienceGroup(3, { user_ids: ["bob"] });
  expect(api.patch).toHaveBeenCalledWith("/audience/groups/3", { user_ids: ["bob"] });
  await archiveAudienceGroup(3);
  expect(api.delete).toHaveBeenCalledWith("/audience/groups/3");
});

it("字段模板透传通用anchor，定义与联想取直接数组，不绑定任何业务路由", async () => {
  const signal = new AbortController().signal;
  await expect(getFieldTemplate("demo", "line", "part:123", signal)).resolves.toEqual([]);
  expect(api.get).toHaveBeenCalledWith("/fields/template", {
    params: { subject_type: "demo", level: "line", anchor: "part:123" }, signal,
  });
  await listFieldDefinitions("demo", "header", signal);
  expect(api.get).toHaveBeenCalledWith("/fields/defs", { params: { subject_type: "demo", level: "header" }, signal });
  await suggestFieldNames("demo", "备注", signal);
  expect(api.get).toHaveBeenCalledWith("/fields/name-suggestions", { params: { subject_type: "demo", q: "备注" }, signal });
  const body = { subject_type: "demo", level: "line" as const, name: "备注1" };
  await defineField(body);
  expect(api.post).toHaveBeenCalledWith("/fields/defs", body);
  await updateFieldDefinition(3, { auto_include: false, archived: true });
  expect(api.patch).toHaveBeenCalledWith("/fields/defs/3", { auto_include: false, archived: true });
});
