import { describe, expect, it } from "vitest";
import { profileDraft, profilePatch, rebaseProfile } from "./drafts";

import { part } from "./fixtures";

describe("共享 PN 编辑草稿", () => {
  it("只提交改动字段及其原始版本，并保留主动清空", () => {
    const draft = profileDraft(part); draft.fields.brand = "";
    expect(profilePatch(part, draft)).toEqual({ expected_identity_revision: 2, changes: { brand: null }, spec_updates: {}, field_versions: { brand: 3 } });
  });
  it("品类两列使用同一编辑基线", () => {
    const draft = profileDraft(part); draft.fields.category_minor = "SATA";
    expect(profilePatch(part, draft).field_versions).toEqual({ category_major: 4, category_minor: 4 });
    expect(profilePatch(part, draft).changes).toEqual({ category_major: "硬盘", category_minor: "SATA" });
  });
  it("重基线保留本人品牌草稿和他人未编辑的描述", () => {
    const draft = profileDraft(part); draft.fields.brand = "";
    const latest = { ...part, profile: { ...part.profile, description: "他人新描述" } };
    expect(rebaseProfile(part, draft, latest).fields).toMatchObject({ description: "他人新描述", brand: null });
    expect(() => rebaseProfile(part, draft, { ...latest, identity_revision: 3 })).toThrow("PN 身份已变化");
  });
  it("删除规格使用持久版本，权限消失时不能复写旧字段", () => {
    const base = { ...part, specs: { capacity: { value: "1TB", unit: "TB", numeric_value: "1", source: "manual", deleted: false, version: 5 } } };
    const draft = profileDraft(base); draft.specs.capacity = null; draft.fields.brand = "new";
    expect(profilePatch(base, draft)).toMatchObject({ field_versions: { "specs.capacity": 5 }, spec_updates: { capacity: null } });
    expect(() => rebaseProfile(base, draft, { ...base, profile: { description: "visible" } })).toThrow("字段权限");
  });
});
