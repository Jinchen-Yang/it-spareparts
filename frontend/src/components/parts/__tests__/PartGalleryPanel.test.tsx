import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  act, cleanupAntd, fireEvent, render, screen, waitFor, within,
} from "../../../test/antdRender";
import { api } from "../../../api";
import PartGalleryPanel from "../PartGalleryPanel";

const originalAdapter = api.defaults.adapter;
const originalXHR = globalThis.XMLHttpRequest;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const image = {
  file_no: 1,
  filename: "front.jpg",
  id: "11111111-1111-4111-8111-111111111111",
  pn_id: "33333333-3333-4333-8333-333333333333",
  url: "https://oss.example/front.jpg",
  thumb_url: "https://oss.example/front-thumb.jpg",
  width: 800,
  height: 600,
  bytes: 3,
  content_type: "image/jpeg",
  source: "upload" as const,
  status: "gallery" as const,
  sort_order: 0,
  is_defect: false,
  has_foreign_watermark: false,
  thumbnail_status: "ready" as const,
  created_at: "2026-10-24T00:00:00Z",
};

const gallery = {
  part_id: 42,
  pn_id: "33333333-3333-4333-8333-333333333333",
  pn_display: "PN-42",
  gallery_revision: 3,
  has_image: true,
  main_image: image,
  images: [image],
  candidates: [],
};

const ticket = {
  upload_id: "22222222-2222-4222-8222-222222222222",
  method: "POST" as const,
  url: "https://oss.example/upload",
  fields: { key: "gallery/front.jpg", policy: "signed" },
  headers: {},
  expires_at: "2026-10-24T01:00:00Z",
  max_bytes: 20 * 1024 * 1024,
};

function response(config: any, data: unknown, status = 200, headers: Record<string, string> = {}) {
  return { data, status, statusText: "OK", headers, config };
}

function header(config: any, name: string): string | undefined {
  const headers = config.headers;
  if (!headers) return undefined;
  if (typeof headers.get === "function") return headers.get(name) ?? undefined;
  return headers[name] ?? headers[name.toLowerCase()];
}

class FakeXHR {
  static instances: FakeXHR[] = [];
  upload = { onprogress: undefined as ((event: any) => void) | undefined };
  status = 204;
  withCredentials = true;
  timeout = 0;
  method = "";
  url = "";
  headers: Record<string, string> = {};
  body: FormData | null = null;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  onabort: (() => void) | null = null;

  constructor() {
    FakeXHR.instances.push(this);
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(key: string, value: string) {
    this.headers[key] = value;
  }

  send(body: FormData) {
    this.body = body;
    queueMicrotask(() => {
      this.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 });
      this.onload?.();
    });
  }

  abort() {
    this.onabort?.();
  }
}

function file(name = "front.jpg") {
  return new File([new Uint8Array([1, 2, 3])], name, { type: "image/jpeg" });
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("token", "gallery-token");
  localStorage.setItem("role", "admin");
  FakeXHR.instances = [];
  (globalThis as any).XMLHttpRequest = FakeXHR;
});

afterEach(async () => {
  await cleanupAntd();
  api.defaults.adapter = originalAdapter;
  (globalThis as any).XMLHttpRequest = originalXHR;
});

it("无写权限时只读：隐藏上传与图库操作，仍展示缩略图", async () => {
  api.defaults.adapter = async (config) => response(config, gallery);
  render(<PartGalleryPanel partId={42} pn="PN-42" canWrite={false} />);

  expect(await screen.findByAltText("front.jpg")).toBeInTheDocument();
  expect(screen.queryByLabelText("选择图库图片")).toBeNull();
  expect(screen.queryByRole("button", { name: /上移/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /删除/ })).toBeNull();
  expect(screen.getByText("只读：需要「备件主数据」权限才能维护图库")).toBeInTheDocument();
});

it("有写权限时可上传：签名、浏览器直传 OSS、登记各用独立请求号", async () => {
  const onChanged = vi.fn();
  let loads = 0;
  const calls: any[] = [];
  api.defaults.adapter = async (config) => {
    calls.push(config);
    if (config.url === "/parts/42/gallery") {
      loads += 1;
      return response(config, loads === 1 ? { ...gallery, images: [], main_image: null, has_image: false } : gallery);
    }
    if (config.url === "/parts/42/gallery/uploads/sign") return response(config, ticket);
    if (config.url === "/parts/42/gallery/images") return response(config, {
      image, gallery_revision: 4, duplicate: false, restored: false,
    }, 201);
    throw new Error(`unexpected ${config.method} ${config.url}`);
  };
  render(<PartGalleryPanel partId={42} pn="PN-42" canWrite onChanged={onChanged} />);

  await screen.findByText("暂无图片，可上传");
  fireEvent.change(screen.getByLabelText("选择图库图片"), { target: { files: [file()] } });

  await waitFor(() => expect(calls.filter((call) => call.url === "/parts/42/gallery/images")).toHaveLength(1));
  const signCall = calls.find((call) => call.url === "/parts/42/gallery/uploads/sign");
  const registerCall = calls.find((call) => call.url === "/parts/42/gallery/images");
  expect(header(signCall, "X-Request-Id")).toMatch(uuid);
  expect(header(registerCall, "X-Request-Id")).toMatch(uuid);
  expect(header(signCall, "X-Request-Id")).not.toBe(header(registerCall, "X-Request-Id"));
  expect(header(signCall, "Authorization")).toBe("Bearer gallery-token");

  expect(FakeXHR.instances).toHaveLength(1);
  const oss = FakeXHR.instances[0];
  expect(oss.method).toBe("POST");
  expect(oss.url).toBe("https://oss.example/upload");
  expect(oss.withCredentials).toBe(false);
  expect(oss.body?.get("key")).toBe("gallery/front.jpg");
  expect(oss.body?.get("file")).toBeInstanceOf(File);

  expect(await screen.findByText("已登记")).toBeInTheDocument();
  expect(await screen.findByAltText("front.jpg")).toBeInTheDocument();
  expect(onChanged).toHaveBeenCalledTimes(1);
});

it("登记失败后重试复用同一个幂等号，且不重复直传 OSS", async () => {
  let registerCalls = 0;
  const requests: any[] = [];
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/42/gallery") return response(config, gallery);
    if (config.url === "/parts/42/gallery/uploads/sign") return response(config, ticket);
    if (config.url === "/parts/42/gallery/images") {
      requests.push(config);
      registerCalls += 1;
      if (registerCalls === 1) {
        throw Object.assign(new Error("network"), {
          config, response: { status: 503, data: { detail: "图库服务暂不可用" } },
        });
      }
      return response(config, { image, gallery_revision: 4, duplicate: false, restored: false }, 201);
    }
    throw new Error(`unexpected ${config.url}`);
  };
  render(<PartGalleryPanel partId={42} pn="PN-42" canWrite />);

  await screen.findByAltText("front.jpg");
  fireEvent.change(screen.getByLabelText("选择图库图片"), { target: { files: [file("retry.jpg")] } });
  const retry = await screen.findByRole("button", { name: /重\s*试/ });
  expect(FakeXHR.instances).toHaveLength(1);
  fireEvent.click(retry);

  await waitFor(() => expect(registerCalls).toBe(2));
  expect(header(requests[0], "X-Request-Id")).toMatch(uuid);
  expect(header(requests[1], "X-Request-Id")).toBe(header(requests[0], "X-Request-Id"));
  expect(FakeXHR.instances).toHaveLength(1);
  expect(await screen.findByText("已登记")).toBeInTheDocument();
});

it("写操作 409 时提示图库已被他人修改并自动刷新", async () => {
  let loads = 0;
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/42/gallery") {
      loads += 1;
      return response(config, { ...gallery, main_image: null });
    }
    if (config.method === "patch") {
      throw Object.assign(new Error("conflict"), {
        config, response: { status: 409, data: { detail: "图库已被其他人修改，请刷新后重试" } },
      });
    }
    throw new Error(`unexpected ${config.url}`);
  };
  render(<PartGalleryPanel partId={42} pn="PN-42" canWrite />);

  await screen.findByAltText("front.jpg");
  fireEvent.click(screen.getByRole("button", { name: "设为 front.jpg 的主图" }));
  expect(await screen.findByText("图库已被他人修改")).toBeInTheDocument();
  await waitFor(() => expect(loads).toBeGreaterThanOrEqual(2));
});

it("删除走 AntD 确认框并携带 baseline_gallery_revision", async () => {
  const onChanged = vi.fn();
  let deletes = 0;
  let loads = 0;
  let deleteConfig: any = null;
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/42/gallery") {
      loads += 1;
      return response(config, gallery);
    }
    if (config.method === "delete") {
      deletes += 1;
      deleteConfig = config;
      return response(config, null, 204);
    }
    throw new Error(`unexpected ${config.url}`);
  };
  render(<PartGalleryPanel partId={42} pn="PN-42" canWrite onChanged={onChanged} />);

  await screen.findByAltText("front.jpg");
  fireEvent.click(screen.getByRole("button", { name: "删除 front.jpg" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText("删除图片", { selector: ".ant-modal-confirm-title" })).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: /删\s*除/ }));

  await waitFor(() => expect(deletes).toBe(1));
  expect(deleteConfig.params).toEqual({ baseline_gallery_revision: 3 });
  expect(header(deleteConfig, "X-Request-Id")).toMatch(uuid);
  await waitFor(() => expect(loads).toBeGreaterThanOrEqual(2));
  expect(onChanged).toHaveBeenCalledTimes(1);
});

it.each([
  ["排序", "下移 front.jpg", { sort_order: 1 }],
  ["设主图", "设为 back.jpg 的主图", { sort_order: 0 }],
  ["标瑕疵", "标记 front.jpg 瑕疵", { is_defect: true }],
])("%s 成功后通知调用方图库已变更", async (_operation, buttonName, update) => {
  const onChanged = vi.fn();
  const updates: any[] = [];
  const secondImage = { ...image, id: "44444444-4444-4444-8444-444444444444", filename: "back.jpg", sort_order: 1 };
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/42/gallery") {
      return response(config, { ...gallery, images: [image, secondImage] });
    }
    if (config.method === "patch") {
      updates.push(JSON.parse(config.data));
      return response(config, { image, gallery_revision: 4 });
    }
    throw new Error(`unexpected ${config.method} ${config.url}`);
  };
  render(<PartGalleryPanel partId={42} pn="PN-42" canWrite onChanged={onChanged} />);

  await screen.findByAltText("front.jpg");
  fireEvent.click(screen.getByRole("button", { name: buttonName }));

  await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  expect(updates).toEqual([{ ...update, baseline_gallery_revision: 3 }]);
});

it("图库写入已发出时先卸载，成功响应迟到仍通知调用方且不重新加载", async () => {
  const onChanged = vi.fn();
  let resolveWrite!: (value: ReturnType<typeof response>) => void;
  const pending = new Promise<ReturnType<typeof response>>((resolve) => { resolveWrite = resolve; });
  let writeConfig: any;
  let loads = 0;
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/42/gallery") {
      loads += 1;
      return response(config, gallery);
    }
    if (config.method === "patch") {
      writeConfig = config;
      return pending;
    }
    throw new Error(`unexpected ${config.method} ${config.url}`);
  };
  const { unmount } = render(<PartGalleryPanel partId={42} pn="PN-42" canWrite onChanged={onChanged} />);
  await screen.findByAltText("front.jpg");
  fireEvent.click(screen.getByRole("button", { name: "标记 front.jpg 瑕疵" }));
  await waitFor(() => expect(writeConfig).toBeDefined());
  unmount();
  expect(onChanged).not.toHaveBeenCalled();

  await act(async () => {
    resolveWrite(response(writeConfig, { image: { ...image, is_defect: true }, gallery_revision: 4 }));
    await pending;
  });

  await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  expect(loads).toBe(1);
});

it("图库写入失败时不通知调用方资料已变更", async () => {
  const onChanged = vi.fn();
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/42/gallery") return response(config, gallery);
    throw Object.assign(new Error("unavailable"), {
      config, response: { status: 503, data: { detail: "图库服务暂不可用" } },
    });
  };
  render(<PartGalleryPanel partId={42} pn="PN-42" canWrite onChanged={onChanged} />);
  await screen.findByAltText("front.jpg");
  fireEvent.click(screen.getByRole("button", { name: "标记 front.jpg 瑕疵" }));
  expect(await screen.findByText("图库服务暂不可用")).toBeInTheDocument();
  expect(onChanged).not.toHaveBeenCalled();
});
