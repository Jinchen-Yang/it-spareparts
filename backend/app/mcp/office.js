"use strict";
let auth = "",
  uploaded = [];
const $ = (id) => document.getElementById(id),
  params = new URLSearchParams(location.search);
function details(x) {
  const d = document.createElement("details"),
    s = document.createElement("summary"),
    p = document.createElement("pre");
  s.textContent = "查看技术回执";
  p.textContent = JSON.stringify(x, null, 2);
  d.append(s, p);
  return d;
}
function show(id, x) {
  if (id === "uploadResult" && typeof x !== "string") {
    $(id).replaceChildren();
    const rows = Array.isArray(x) ? x : x.entries || [];
    $(id).append(
      table(
        ["文件", "处理结果"],
        rows.map((e) => [
          e.name,
          e.error || (e.state === "ready" ? "已暂存" : "待上传"),
        ]),
      ),
    );
    return;
  }
  if (id === "auditResult" && typeof x !== "string") {
    $(id).replaceChildren(
      table(
        ["时间", "操作", "状态"],
        x.events.map((e) => [
          new Date(e.at).toLocaleString("zh-CN"),
          auditNames[e.operation] || e.operation,
          stageNames[e.stage] || e.stage,
        ]),
      ),
      details(x),
    );
    return;
  }
  $(id).textContent = typeof x === "string" ? x : JSON.stringify(x, null, 2);
}
const stageNames = {
  replayed: "重复请求，已返回原回执",
  requested: "已发起",
  completed: "已完成",
  approved: "已确认",
  failed: "失败",
  download_started: "开始下载",
  download_completed: "传输完成",
  download_aborted: "传输中断",
};
const auditNames = {
  confirm_import: "确认导入",
  file_upload: "上传文件",
  download: "下载文件",
  worker_preview: "后台预检",
  worker_export: "后台导出",
  credential_create: "创建连接授权",
  credential_revoke: "撤销授权",
  pf_search_audit: "查询操作记录",
  pf_get_job: "查看任务",
  pf_get_import_preview: "查看导入差异",
  pf_create_upload_session: "准备上传",
  pf_preview_import: "申请预检",
  pf_create_export: "申请导出",
  pf_get_download: "获取下载入口",
  pf_get_capabilities: "查询可用功能",
  pf_search_projects: "查找项目",
};
function error(e) {
  show("error", e.message || e);
}
async function request(path, options = {}) {
  const r = await fetch(path, {
    ...options,
    headers: {
      ...(options.body instanceof ArrayBuffer
        ? {}
        : { "Content-Type": "application/json" }),
      Authorization: "Bearer " + auth,
      ...options.headers,
    },
    credentials: "omit",
  });
  if (!r.ok) {
    let e;
    try {
      e = await r.json();
    } catch {
      e = { message: "请求失败 " + r.status };
    }
    throw new Error(e.message || JSON.stringify(e.detail) || "请求失败");
  }
  return r;
}
async function tool(name, args = {}) {
  return (
    await (
      await request("/api/mcp-office/tools", {
        method: "POST",
        body: JSON.stringify({ name, arguments: args }),
      })
    ).json()
  ).data;
}
function action(id, fn) {
  $(id).onclick = async () => {
    show("error", "");
    $(id).disabled = true;
    try {
      await fn();
    } catch (e) {
      error(e);
    } finally {
      $(id).disabled = false;
    }
  };
}
$("loginForm").onsubmit = async (e) => {
  e.preventDefault();
  show("error", "");
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: $("username").value,
        password: $("password").value,
      }),
    });
    if (!r.ok) throw new Error("登录失败，请检查账号密码");
    const j = await r.json();
    auth = j.token;
    $("password").value = "";
    const c = await tool("pf_get_capabilities");
    show("identity", "已登录：" + c.actor_display);
    $("login").classList.add("hide");
    $("work").classList.remove("hide");
    if (params.has("upload")) {
      const x = await tool("pf_get_upload_session", {
        upload_session_id: params.get("upload"),
      });
      uploaded = x.entries
        .filter((e) => e.state === "ready")
        .map((e) => e.file_id);
      show("uploadResult", x);
    }
    if (params.has("review")) await review(params.get("review"));
    if (params.has("download")) await artifact(params.get("download"));
  } catch (e) {
    // A stale deep link must not discard a successful login.
    if ($("work").classList.contains("hide")) auth = "";
    error(e);
  }
};
action("logout", async () => {
  auth = "";
  location.reload();
});
action("issue", async () => {
  const c = await tool("pf_get_capabilities");
  const scopes = [
    "mcp.use",
    "mcp.files.upload",
    "mcp.files.read",
    "mcp.files.download",
    "mcp.projects.read",
    "mcp.import.preview",
    "mcp.jobs.read",
    "mcp.export",
    "mcp.parts.read",
    "mcp.inventory.read",
    "mcp.audit.read",
  ];
  const r = await (
    await request("/api/mcp-office/credentials", {
      method: "POST",
      body: JSON.stringify({ scopes, days: 30 }),
    })
  ).json();
  show("token", r.token + "\n到期：" + r.expires_at + "\n关闭页面后不再显示。");
});
action("listCredentials", async () => {
  const j = await (await request("/api/mcp-office/credentials")).json();
  $("credentials").replaceChildren();
  for (const x of j.credentials) {
    const p = document.createElement("p");
    p.textContent = x.id + " — " + (x.revoked ? "已撤销" : x.expires_at);
    if (!x.revoked) {
      const b = document.createElement("button");
      b.textContent = "撤销";
      b.onclick = async () => {
        try {
          await request("/api/mcp-office/credentials/" + x.id, {
            method: "DELETE",
          });
          b.disabled = true;
          b.textContent = "已撤销";
        } catch (e) {
          error(e);
        }
      };
      p.append(b);
    }
    $("credentials").append(p);
  }
});
action("upload", async () => {
  const files = [...$("files").files];
  if (!files.length) throw new Error("请选择文件");
  let s;
  if (params.has("upload"))
    s = await tool("pf_get_upload_session", {
      upload_session_id: params.get("upload"),
    });
  else
    s = await tool("pf_create_upload_session", {
      files: files.map((f) => ({
        name: f.name,
        size_bytes: f.size,
        mime: f.type || "application/octet-stream",
      })),
      purpose: "document_import",
      idempotency_key: crypto.randomUUID(),
    });
  uploaded = [];
  const results = [];
  for (const entry of s.entries) {
    const f = files.find(
      (f) => f.name === entry.name && f.size === entry.size_bytes,
    );
    if (!f) {
      results.push({ name: entry.name, error: "未选择匹配文件" });
      continue;
    }
    try {
      const r = await request(
        "/api/mcp-office/uploads/" + s.upload_session_id + "/" + entry.entry_id,
        {
          method: "PUT",
          headers: { "Content-Type": "application/octet-stream" },
          body: await f.arrayBuffer(),
        },
      );
      const data = await r.json();
      uploaded.push(data.file_id);
      results.push(data);
    } catch (e) {
      results.push({ name: entry.name, error: e.message });
    }
  }
  show("uploadResult", results);
  show("uploadStatus", "暂存成功 " + uploaded.length + " 个，未入账。");
});
action("findProjects", async () => {
  const j = await tool("pf_search_projects", {
    query: $("projectQuery").value,
  });
  $("projects").replaceChildren(new Option("请选择项目", ""));
  for (const p of j.items)
    $("projects").append(
      new Option(p.display_name + " · " + p.project_code, p.project_id),
    );
  if (j.truncated) show("error", "结果较多，请缩小查询范围");
});
action("preview", async () => {
  if (!uploaded.length) throw new Error("请先上传原件");
  const args = {
    file_ids: uploaded,
    family: $("family").value,
    idempotency_key: crypto.randomUUID(),
  };
  if ($("projects").value) args.project_id = $("projects").value;
  await job((await tool("pf_preview_import", args)).job_id);
});
action("export", async () => {
  if (!$("projects").value) throw new Error("请先选择项目");
  await job(
    (
      await tool("pf_create_export", {
        export_kind: $("exportKind").value,
        project_ids: [$("projects").value],
        idempotency_key: crypto.randomUUID(),
      })
    ).job_id,
  );
});
async function job(id) {
  const panel = document.createElement("div"),
    status = document.createElement("pre"),
    b = document.createElement("button");
  b.textContent = "刷新任务";
  panel.append(status, b);
  $("jobs").prepend(panel);
  let terminal = false;
  async function refresh() {
    const r = await tool("pf_get_job", { job_id: id });
    status.textContent =
      "任务编号：" +
      id +
      "\n状态：" +
      ({
        queued: "排队中",
        running: "处理中",
        ready: "预览已生成，尚未入账",
        partial: "部分文件处理失败",
        succeeded: "导出已生成",
        failed: "处理失败",
      }[r.status] || r.status) +
      (r.error ? "\n" + r.error.message : "");
    if (["queued", "running"].includes(r.status)) return;
    if (terminal) return;
    terminal = true;
    for (const i of r.result?.items || [])
      if (i.preview_id) await review(i.preview_id);
    if (r.result?.artifact_id) await artifact(r.result.artifact_id);
  }
  b.onclick = () => refresh().catch(error);
  await refresh();
  // Bounded polling; tab may be closed while the durable worker continues.
  let count = 0;
  const timer = setInterval(async () => {
    if (terminal || ++count > 100 || !auth) {
      clearInterval(timer);
      return;
    }
    try {
      await refresh();
    } catch (e) {
      clearInterval(timer);
      error(e);
    }
  }, 3000);
}
const labels = {
  item_rows: "清单条目",
  issue_rows: "问题条目",
  will_replace_rows: "将替换原条目",
  expense_creates: "新增报销",
  expense_updates: "更新报销",
  collection_creates: "新增回款",
  collection_updates: "更新回款",
  collection_voids: "作废回款",
  project_id: "项目编号",
  raw_line_id: "原始行编号",
  is_create: "是否新增",
  bxd_no: "报销单号",
  line_no: "行号",
  expense_date: "费用日期",
  person: "报销人",
  expense_type: "费用类型",
  fee_category: "费用分类",
  reason: "事由",
  contract_no: "合同号",
  amount_ex_tax: "不含税金额（元）",
  amount_inc_tax: "含税金额（元）",
  tax_basis: "税额口径",
  data_status: "单据状态",
  remark: "备注",
  operation: "操作",
  project_contract_id: "合同编号",
  report_month: "报表月份",
  cumulative_amount: "累计回款（元）",
  receipt_reference: "回款凭据",
  collection_status: "回款状态",
};
function table(headers, rows) {
  const wrap = document.createElement("div");
  wrap.style.overflowX = "auto";
  const t = document.createElement("table"),
    head = document.createElement("tr");
  for (const v of headers) {
    const th = document.createElement("th");
    th.textContent = v;
    head.append(th);
  }
  t.append(head);
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const v of row) {
      const td = document.createElement("td");
      td.textContent = v == null ? "—" : String(v);
      tr.append(td);
    }
    t.append(tr);
  }
  wrap.append(t);
  return wrap;
}
async function review(id) {
  const r = await tool("pf_get_import_preview", { preview_id: id });
  const panel = document.createElement("section"),
    h = document.createElement("h3"),
    summary = document.createElement("p"),
    b = document.createElement("button");
  h.textContent = r.filename;
  summary.textContent = Object.entries(r.summary || {})
    .map(
      ([k, v]) =>
        (labels[k] || k) +
        "：" +
        (typeof v === "object" ? JSON.stringify(v) : v),
    )
    .join(" · ");
  panel.append(h, summary);
  if (r.family === "acceptance_checklist") {
    const note = document.createElement("p");
    note.textContent =
      "确认后，以下清单将替换该项目的当前清单。旧清单保留在历史记录中。";
    panel.append(
      note,
      table(
        ["原表行号", "验收需求", "完成状态", "检查问题"],
        (r.changes || []).map((x) => [
          x.row_no,
          x.requirement,
          x.done ? "已完成" : "未完成",
          (x.issues || []).join("；"),
        ]),
      ),
    );
  } else if (r.family === "expense_collection") {
    for (const [key, title] of [
      ["expense_updates", "报销变更"],
      ["collection_ops", "回款变更"],
    ]) {
      const rows = r.changes?.[key] || [],
        titleNode = document.createElement("h4");
      titleNode.textContent = title + "（" + rows.length + "条）";
      panel.append(titleNode);
      if (rows.length) {
        const keys = Object.keys(rows[0]);
        panel.append(
          table(
            keys.map((k) => labels[k] || k),
            rows.map((row) => keys.map((k) => row[k])),
          ),
        );
      }
    }
  } else {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(r.summary, null, 2);
    panel.append(pre);
  }
  for (const w of r.warnings || []) {
    const note = document.createElement("p");
    note.textContent = w;
    panel.append(note);
  }
  b.textContent =
    r.status === "applied" ? "已确认提交" : "确认以上差异并正式提交";
  b.disabled = !r.can_apply || r.status === "applied";
  panel.append(b);
  $("reviews").append(panel);
  b.onclick = async () => {
    b.disabled = true;
    try {
      const data = await (
        await request("/api/mcp-office/reviews/" + id + "/confirm", {
          method: "POST",
          body: JSON.stringify({ preview_hash: r.preview_hash }),
        })
      ).json();
      b.textContent = "已确认提交";
      const receipt = document.createElement("p");
      receipt.textContent =
        "提交完成，回执编号：" + (data.receipt_id || data.preview_id || id);
      panel.append(receipt);
    } catch (e) {
      error(e);
      b.textContent = "请刷新预览核实结果";
    }
  };
}
async function artifact(id) {
  const r = await tool("pf_get_download", { artifact_id: id });
  const b = document.createElement("button");
  b.textContent = "下载 " + r.filename;
  b.onclick = async () => {
    b.disabled = true;
    try {
      const response = await request(
        "/api/mcp-office/artifacts/" + id + "/download",
      );
      const bytes = await response.arrayBuffer();
      const hash = [
        ...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
      ]
        .map((x) => x.toString(16).padStart(2, "0"))
        .join("");
      if (hash !== r.sha256) throw new Error("下载校验失败");
      const url = URL.createObjectURL(new Blob([bytes]));
      const a = document.createElement("a");
      a.href = url;
      a.download = r.filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      b.textContent = "下载已发起（校验通过）";
    } catch (e) {
      error(e);
    } finally {
      b.disabled = false;
    }
  };
  $("artifacts").append(b);
}
action("audit", async () => {
  const date = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  show(
    "auditResult",
    await tool("pf_search_audit", {
      date_from: date,
      date_to: date,
      limit: 100,
    }),
  );
});
