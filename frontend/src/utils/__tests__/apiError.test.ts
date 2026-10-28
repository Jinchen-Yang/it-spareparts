import { describe, expect, it } from "vitest";
import { apiErrorDiagnostic, parseApiError } from "../apiError";

describe("操作错误分类与可复制诊断", () => {
  it("只把明确版本冲突分类为需刷新的冲突", () => {
    expect(parseApiError({ response: { status: 409, data: { detail: { code: "version_conflict", message: "记录已更新" } } } }).kind).toBe("version_conflict");
    for (const code of ["duplicate_pn", "near_duplicates", "existing_offer", "existing_draft", "already_bound", "integrity_conflict"]) {
      expect(parseApiError({ response: { status: 409, data: { detail: { code } } } }).kind).not.toBe("version_conflict");
    }
    expect(parseApiError({ response: { status: 409 } }).kind).toBe("conflict");
    expect(parseApiError({ response: { status: 409, data: { detail: "版本冲突" } } }).kind).toBe("version_conflict");
  });

  it("FastAPI 校验列表显示字段位置和中文原因，不泄露 input 与 ctx", () => {
    const info = parseApiError({ response: { status: 422, data: { detail: [
      { loc: ["body", "lines", 0, "qty"], type: "greater_than", msg: "Quantity SECRET", input: "SECRET", ctx: { gt: "SECRET" } },
      { loc: ["body", "pn_std"], type: "missing", input: "SECRET" },
    ] } } });
    expect(info).toMatchObject({ code: "validation_error", kind: "validation" });
    expect(info.message).toContain("明细 / 第 1 项 / 数量：超出允许范围");
    expect(info.message).toContain("PN：必填");
    expect(JSON.stringify(info)).not.toContain("SECRET");
  });

  it("诊断仅复制状态、方法、无查询路径和服务端请求 ID", () => {
    const info = parseApiError({
      config: { baseURL: "https://user:SECRET@example.com/api", url: "/presales/inquiries?token=SECRET#SECRET", method: "post",
        headers: { Authorization: "Bearer SECRET" }, data: { client_name: "SECRET" } },
      response: { status: 409, headers: { "X-Request-ID": "req-123" }, data: { request_id: "fallback", detail: {
        code: "near_duplicates", message: "业务内容SECRET", near_duplicates: [{ description: "SECRET" }],
      } } },
    });
    expect(info).toMatchObject({ requestId: "req-123", path: "/api/presales/inquiries", method: "POST" });
    expect(apiErrorDiagnostic(info)).toBe("错误码: near_duplicates\nHTTP 状态: 409\n请求方法: POST\n请求路径: /api/presales/inquiries\n请求标识: req-123");
    expect(apiErrorDiagnostic(info)).not.toContain("SECRET");
  });

  it("500 原始内部异常不能显示或复制，缺失服务端请求 ID 不伪造", () => {
    const info = parseApiError({ response: { status: 500, data: { detail: "SELECT password FROM internal_db SECRET" } } });
    expect(info.message).toBe("服务暂时不可用");
    expect(JSON.stringify(info)).not.toContain("SECRET");
    expect(apiErrorDiagnostic(info)).toContain("请求标识: 服务端未返回");
  });

  it.each(["ERR_NETWORK", "ETIMEDOUT", "ECONNABORTED"])("%s 提示先核对写入结果", (code) => {
    const info = parseApiError({ code, config: { url: "/presales/inquiries", method: "post" } });
    expect(info.kind).toBe("network");
    expect(info.suggestion).toContain("确认操作结果");
    expect(info.suggestion).toContain("避免重复提交");
  });

  it("权限与重复 PN 提供相应恢复建议", () => {
    expect(parseApiError({ response: { status: 403 } })).toMatchObject({ kind: "permission", code: "permission_denied" });
    expect(parseApiError({ response: { status: 422, data: { detail: { code: "duplicate_pn" } } } })).toMatchObject({ kind: "duplicate" });
  });
});
