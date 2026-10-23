import { afterEach, beforeEach, expect, it, vi } from "vitest";
import api from "../api";
import { catalogClient, CommandIntent, sendUpload } from "./pnCatalog";
const original = api.defaults.adapter;
beforeEach(() => { localStorage.clear(); localStorage.setItem("token", "first"); });
afterEach(() => { api.defaults.adapter = original; vi.unstubAllGlobals(); });
it("同一操作超时重试沿用请求号，改动意图更换请求号", () => { const intent = new CommandIntent(); const id = intent.key("/parts/1/profile", { brand: "new" }); expect(intent.key("/parts/1/profile", { brand: "new" })).toBe(id); expect(intent.key("/parts/1/profile", { brand: "changed" })).not.toBe(id); });
it("账号切换前后的请求都不借用另一人的 bearer", async () => {
  const client = catalogClient("first"); const calls: string[] = [];
  api.defaults.adapter = async config => { calls.push(String(config.headers.Authorization)); localStorage.setItem("token", "second"); return { data: { data: [], meta: {} }, config, headers: {}, status: 200, statusText: "OK" }; };
  await expect(client.get("/parts")).rejects.toThrow("登录已变化"); await expect(client.get("/parts")).rejects.toThrow("登录已变化"); expect(calls).toEqual(["Bearer first"]);
});
it("OSS 直传不使用 api 拦截器或应用 cookies，并禁止重定向", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true }); vi.stubGlobal("fetch", fetchMock);
  await sendUpload({ method: "POST", url: "https://images.example.test/", fields: { policy: "signed" }, headers: {}, expires_at: "" }, new File(["image"], "test.png", { type: "image/png" }));
  expect(fetchMock).toHaveBeenCalledWith("https://images.example.test/", expect.objectContaining({ credentials: "omit", redirect: "error", headers: {} }));
  await expect(sendUpload({ method: "POST", url: "http://images.example.test", fields: {}, headers: {}, expires_at: "" }, new File(["x"], "x"))).rejects.toThrow("HTTPS");
});
