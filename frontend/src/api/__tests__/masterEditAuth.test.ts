import { afterEach, beforeEach, expect, expectTypeOf, it, vi } from "vitest";
import { api, masterEdit } from "../../api";
import type { MasterEditPayload, MasterEditSnapshot } from "../search";

const originalAdapter = api.defaults.adapter;
const baseline: MasterEditSnapshot = {
  part_id: 1, pn_std: "PN-1",
  identity: { part_id: 1, pn_std: "PN-1", status: "active", merged_into_id: null },
  fields: { description: null, brand: null, category_major: null, category_minor: null,
    machine_or_part: null, unit: null },
  specs: {}, version_mode: "field_snapshot",
  identity_revision: 2, profile_revision: 5, field_versions: { description: 3 },
};
const payload: MasterEditPayload = {
  part_id: 1, pn_std: "PN-1", base_snapshot: baseline, description: "新描述",
};

it("主数据编辑类型要求稳定 ID 和原始字段基线", () => {
  expectTypeOf<Parameters<typeof masterEdit>[0]>().toEqualTypeOf<MasterEditPayload>();
});

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

afterEach(() => {
  api.defaults.adapter = originalAdapter;
});

it("主数据保存使用编辑会话绑定的 token，不被当前 localStorage token 覆盖", async () => {
  localStorage.setItem("token", "new-session-token");
  const adapter = vi.fn(async (config: any) => ({
    data: { id: 1, pn_std: "PN-1", updated: ["description"], locked_fields: ["description"] },
    status: 200,
    statusText: "OK",
    headers: {},
    config,
  }));
  api.defaults.adapter = adapter;

  await masterEdit(payload, "bound-edit-token");

  expect(adapter).toHaveBeenCalledOnce();
  expect(adapter.mock.calls[0][0].headers.Authorization).toBe("Bearer bound-edit-token");
  expect(JSON.parse(adapter.mock.calls[0][0].data)).toEqual(payload);
});

it("旧编辑会话的迟到 401 不会清除新账号 token", async () => {
  localStorage.setItem("token", "old-session-token");
  api.defaults.adapter = vi.fn(async (config: any) => {
    localStorage.setItem("token", "new-session-token");
    throw Object.assign(new Error("unauthorized"), {
      config,
      response: { status: 401 },
    });
  });

  await expect(masterEdit(
    payload,
    "old-session-token",
  )).rejects.toThrow("unauthorized");

  expect(localStorage.getItem("token")).toBe("new-session-token");
});
