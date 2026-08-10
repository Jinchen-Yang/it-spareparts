import { describe, expect, it } from "vitest";

import {
  artifactIdFromFileUrl,
  parseArtifactId,
  parseUploadedAttachmentMessage,
} from "../artifactIds";

const UUID = "3f1c5cd7-5d64-4e86-a364-42b6b663f4f2";

describe("Artifact identifiers", () => {
  it("accepts a complete UUID and the exact legacy 12-hex form", () => {
    expect(parseArtifactId(UUID)).toBe(UUID);
    expect(parseArtifactId(UUID.toUpperCase())).toBe(UUID);
    expect(parseArtifactId("abcdef123456")).toBe("abcdef123456");
  });

  it("rejects malformed, overlong and truncated identifiers", () => {
    for (const value of [
      "abcdef12345",
      "abcdef1234567",
      "3f1c5cd7-5d64-4e86-a364-42b6b663",
      `${UUID}suffix`,
      "../../abcdef123456",
      "",
    ]) {
      expect(parseArtifactId(value)).toBeNull();
    }
  });

  it("extracts only a complete file URL and never truncates the id", () => {
    expect(artifactIdFromFileUrl(`/api/agent/files/${UUID}`)).toBe(UUID);
    expect(artifactIdFromFileUrl("/api/agent/files/abcdef123456")).toBe("abcdef123456");
    expect(artifactIdFromFileUrl(`/api/agent/files/${UUID.slice(0, -1)}`)).toBeNull();
    expect(artifactIdFromFileUrl(`/api/agent/files/${UUID}/preview`)).toBeNull();
  });

  it("collapses attachment prefixes only when file_id is complete and valid", () => {
    expect(parseUploadedAttachmentMessage(
      `[已上传文件「报价单.xlsx」 file_id=${UUID}，表格]\n\n请处理`,
    )).toEqual({ filename: "报价单.xlsx", fileId: UUID, body: "请处理" });
    expect(parseUploadedAttachmentMessage(
      "[已上传文件「旧表.xlsx」 file_id=abcdef123456，表格]\n请处理",
    )).toEqual({ filename: "旧表.xlsx", fileId: "abcdef123456", body: "请处理" });
    expect(parseUploadedAttachmentMessage(
      `[已上传文件「坏表.xlsx」 file_id=${UUID.slice(0, -1)}，表格]\n\n请处理`,
    )).toBeNull();
  });
});
