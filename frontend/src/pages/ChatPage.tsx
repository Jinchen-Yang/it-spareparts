import { useEffect, useRef, useState } from "react";
import { Button, Input, Modal, Popconfirm, Spin, Tag, Tooltip, Upload, message } from "antd";
import {
  CopyOutlined, DeleteOutlined, DownloadOutlined, EyeOutlined, FileExcelOutlined,
  PaperClipOutlined, PlusOutlined, RobotOutlined, SendOutlined, StopOutlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  agentDownload, agentFileBlobUrl, agentPreview, agentUpload, attachChatStream, cancelChatStream,
  createChatSession, deleteChatSession, getChatMessages, listChatSessions, sessionChatStream,
} from "../api";
import type {
  AgentToolCall, AgentUploadResult, ChatSessionMeta, FilePreview, SessionStreamEvent,
} from "../api";
import { artifactIdFromFileUrl, parseUploadedAttachmentMessage } from "../artifactIds";
import { clearSessionScopedPreferences } from "../sessionPreferences";
import { COLORS } from "../theme";
import { summarizeToolAudit } from "../toolAudit";

const TOOL_LABEL: Record<string, string> = {
  search_parts: "型号搜索",
  get_part_overview: "型号全景",
  get_profit_ranking: "利润排名",
  list_recent_purchases: "查采购记录",
  inspect_file: "查看文件结构",
  read_file_rows: "读取文件数据",
  read_document: "读取文档",
  lookup_prices_bulk: "批量查价",
  write_excel: "生成 Excel",
  write_report: "生成报表",
};

const EXAMPLES = [
  { icon: "💰", title: "销售报价", q: "ST8000NM000A 客户问报 2200 行不行？" },
  { icon: "📦", title: "采购压价", q: "我要进 50 个 ST8000NM000A，目标价多少合理？" },
  { icon: "🧩", title: "整机拆解", q: "点左下角 📎 传整机配置(Word/PDF/图片)，我拆成单件查PN和近15天采购价、生成报价单" },
  { icon: "🖥️", title: "配整机", q: "帮我配一台 2U 双路服务器：金牌 CPU×2、256G 内存、4×8T 企业盘、双电源，出个配置报价单" },
];

interface Turn {
  role: "user" | "assistant";
  content: string;
  tools?: AgentToolCall[];
  reasoning?: string;     // 思考链（仅本轮活会话内保留，刷新不持久——与 Claude 一致）
  stopped?: boolean;
}
interface ToolRun {
  name: string;
  done: boolean;
}
// 一路活跃流（send 或 attach）的控制块：切会话/停止/打断都作用于它。
interface StreamCtl {
  sessionId: number;
  abort: AbortController;
  settled: boolean;
  conflict: boolean;     // 409：会话锁未释放，交 send 退避重试
  trace: AgentToolCall[];
  userTurn?: Turn;
  done: Promise<void>;
  resolveDone: () => void;
}
const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

const cellStyle = (head: boolean): React.CSSProperties => ({
  border: `1px solid ${COLORS.border}`, padding: "5px 10px",
  background: head ? COLORS.inset : COLORS.surface, fontWeight: head ? 500 : 400,
  whiteSpace: "nowrap", color: COLORS.text,
});

function nodeText(n: React.ReactNode): string {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(nodeText).join("");
  const props = (n as { props?: { children?: React.ReactNode } }).props;
  return props ? nodeText(props.children) : "";
}

/** 生成/上传的文件卡片：在线预览（Excel 表格 / 图片）+ 下载。
 * 替代裸下载按钮；预览/下载都走带鉴权 fetch，绝不整页刷新打断对话。 */
function FileCard({ href, label }: { href: string; label?: string }) {
  const fileId = artifactIdFromFileUrl(href) || "";
  const [open, setOpen] = useState(false);
  const [pv, setPv] = useState<FilePreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [imgUrl, setImgUrl] = useState<string | null>(null);
  const [tab, setTab] = useState(0);
  const name = pv?.filename || label || "结果文件.xlsx";

  useEffect(() => () => { if (imgUrl) URL.revokeObjectURL(imgUrl); }, [imgUrl]);

  const download = () =>
    agentDownload(href).catch((e: Error) =>
      message.error(e.message === "auth-expired"
        ? "登录已过期，请重新登录后再下载（聊天记录不会丢失）"
        : "下载失败，文件可能已清理"));

  const openPreview = async () => {
    setOpen(true);
    if (pv || !fileId) return;
    setLoading(true);
    try {
      const data = await agentPreview(fileId);
      setPv(data);
      if (data.kind === "image") {
        try { setImgUrl(await agentFileBlobUrl(fileId)); } catch { /* 退化为仅下载 */ }
      }
    } catch (e) {
      const msg = (e as Error)?.message;
      message.error(msg === "auth-expired" ? "登录已过期，请重新登录" : "预览失败，文件可能已清理");
      setOpen(false);
    } finally {
      setLoading(false);
    }
  };

  const sheet = pv?.sheets?.[tab];
  return (
    <>
      <span style={{
        display: "inline-flex", alignItems: "center", gap: 8, margin: "6px 0",
        padding: "7px 10px", border: `1px solid ${COLORS.border}`, borderRadius: 10,
        background: COLORS.surface, maxWidth: "100%", verticalAlign: "middle",
      }}>
        <span style={{
          width: 28, height: 28, borderRadius: 8, flexShrink: 0, display: "inline-flex",
          alignItems: "center", justifyContent: "center", background: "#E9F2EB", color: "#41704F",
        }}><FileExcelOutlined /></span>
        <span style={{ fontSize: 13, color: COLORS.text, overflow: "hidden",
                       textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 200 }}>{name}</span>
        <Button size="small" icon={<EyeOutlined />} onClick={openPreview}>预览</Button>
        <Button size="small" type="primary" icon={<DownloadOutlined />} onClick={download}>下载</Button>
      </span>

      <Modal open={open} onCancel={() => setOpen(false)} width={820} title={name} destroyOnHidden
        footer={<Button type="primary" icon={<DownloadOutlined />} onClick={download}>下载文件</Button>}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 40 }}><Spin /></div>
        ) : pv?.kind === "table" ? (
          <>
            {pv.sheets && pv.sheets.length > 1 && (
              <div style={{ marginBottom: 10, display: "flex", gap: 6, flexWrap: "wrap" }}>
                {pv.sheets.map((s, i) => (
                  <Button key={i} size="small" type={i === tab ? "primary" : "default"}
                    onClick={() => setTab(i)}>{s.name}</Button>
                ))}
              </div>
            )}
            <div style={{ maxHeight: 460, overflow: "auto", border: `1px solid ${COLORS.border}`, borderRadius: 8 }}>
              <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
                <tbody>
                  {(sheet?.rows || []).map((r, ri) => (
                    <tr key={ri}>
                      {r.map((c, ci) => (ri === 0
                        ? <th key={ci} style={cellStyle(true)}>{c}</th>
                        : <td key={ci} style={cellStyle(false)}>{c}</td>))}
                    </tr>
                  ))}
                  {!sheet?.rows?.length && <tr><td style={cellStyle(false)}>（空表）</td></tr>}
                </tbody>
              </table>
            </div>
            {sheet?.truncated && (
              <div style={{ color: COLORS.text3, fontSize: 12, marginTop: 8 }}>
                仅预览前 {Math.max((sheet.rows?.length || 1) - 1, 0)} 行（共 {sheet.total_rows} 行），完整内容请下载
              </div>
            )}
          </>
        ) : pv?.kind === "image" ? (
          imgUrl
            ? <img src={imgUrl} alt={name} style={{ maxWidth: "100%", borderRadius: 8 }} />
            : <div style={{ color: COLORS.text3, padding: 20, textAlign: "center" }}>图片加载失败，请下载查看</div>
        ) : (
          <div style={{ color: COLORS.text2, padding: 20, textAlign: "center" }}>
            该文件类型暂不支持在线预览，请点「下载文件」查看。
          </div>
        )}
      </Modal>
    </>
  );
}

/** markdown 里的文件链接渲染成文件卡片（预览+下载）；其余链接走普通 <a>。 */
function MdLink(props: { href?: string; children?: React.ReactNode }) {
  const href = props.href || "";
  if (href.includes("/api/agent/files/")) {
    return <FileCard href={href} label={nodeText(props.children)} />;
  }
  return <a href={href} target="_blank" rel="noreferrer">{props.children}</a>;
}

function Md({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: MdLink }}>
      {text}
    </ReactMarkdown>
  );
}

// 合并连续同名工具 → [{name, count}]，把"型号搜索×5"压成一条，少占地方
function mergeConsec(names: string[]): { name: string; count: number }[] {
  const out: { name: string; count: number }[] = [];
  for (const n of names) {
    const last = out[out.length - 1];
    if (last && last.name === n) last.count += 1;
    else out.push({ name: n, count: 1 });
  }
  return out;
}
const label = (n: string) => TOOL_LABEL[n] || n;

// 思考链：默认折叠的灰色块，点标题展开，流式时标题显示"思考中…"
function ThinkBlock({ text, streaming }: { text: string; streaming?: boolean }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  return (
    <div style={{ marginBottom: 8 }}>
      <div onClick={() => setOpen((o) => !o)}
        style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer",
                 fontSize: 12.5, color: "var(--mb-text-3)", userSelect: "none" }}>
        <span style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .15s", fontSize: 10 }}>▶</span>
        <span>💭 {streaming ? "思考中…" : "已思考"}</span>
      </div>
      {open && (
        <div style={{ marginTop: 4, padding: "8px 12px", background: "#F6F3EE", borderRadius: 8,
                      borderLeft: "2px solid #E9E5DE", fontSize: 12.5, color: "var(--mb-text-3)",
                      whiteSpace: "pre-wrap", lineHeight: 1.7, maxHeight: 320, overflow: "auto" }}>
          {text}
        </div>
      )}
    </div>
  );
}

// 完成态工具轨迹：合并同类，默认折叠成一行标题，点开看每次调用
function ToolTrace({ tools }: { tools?: AgentToolCall[] }) {
  const [open, setOpen] = useState(false);
  if (!tools?.length) return null;
  const groups = mergeConsec(tools.map((t) => t.name));
  const summary = groups.map((g) => `${label(g.name)}${g.count > 1 ? ` ×${g.count}` : ""}`).join(" · ");
  return (
    <div style={{ marginTop: 8 }}>
      <div onClick={() => setOpen((o) => !o)}
        style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer",
                 fontSize: 12, color: "var(--mb-text-3)", userSelect: "none", maxWidth: "100%" }}>
        <span style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .15s", fontSize: 10 }}>▶</span>
        <span>🔧 用了 {tools.length} 次工具</span>
        {!open && <span style={{ color: "var(--mb-text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>· {summary}</span>}
      </div>
      {open && (
        <div style={{ marginTop: 4, paddingLeft: 14, borderLeft: "2px solid #ECE8E1" }}>
          {tools.map((t, i) => {
            const a = summarizeToolAudit(t.args);
            return (
              <div key={i} style={{ fontSize: 12, color: "var(--mb-text-3)", padding: "2px 0" }}>
                {label(t.name)}{a ? <span style={{ color: "var(--mb-text-3)" }}>（{a.slice(0, 30)}）</span> : null}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// 流式中的工具状态：完成的合并成一行灰字，当前一个转圈
function LiveTrace({ runs }: { runs: ToolRun[] }) {
  if (!runs.length) return null;
  const doneGroups = mergeConsec(runs.filter((r) => r.done).map((r) => r.name));
  const active = runs.find((r) => !r.done);
  return (
    <div style={{ marginBottom: 8, fontSize: 12.5 }}>
      {doneGroups.length > 0 && (
        <div style={{ color: "var(--mb-text-3)" }}>
          ✓ {doneGroups.map((g) => `${label(g.name)}${g.count > 1 ? ` ×${g.count}` : ""}`).join(" · ")}
        </div>
      )}
      {active && (
        <div style={{ color: "#3E6FD1", display: "flex", alignItems: "center", gap: 6 }}>
          <Spin size="small" /> 正在{label(active.name)}…
        </div>
      )}
    </div>
  );
}

/** 用户消息：把注入的文件前缀折叠成附件标签显示 */
function UserContent({ text }: { text: string }) {
  const attachment = parseUploadedAttachmentMessage(text);
  if (!attachment) return <>{text}</>;
  return (
    <>
      <div style={{ marginBottom: attachment.body ? 6 : 0 }}>
        <Tag color="rgba(255,255,255,0.25)" style={{ color: "#fff", border: "1px solid rgba(255,255,255,0.4)" }}>
          📎 {attachment.filename}
        </Tag>
      </div>
      {attachment.body}
    </>
  );
}

export default function ChatPage() {
  // 会话与消息都在服务端（平台化 P1）：刷新/换设备记录不丢。
  // activeId=null 表示"未落库的新对话"，首次发送时才真正建会话。
  const [sessions, setSessions] = useState<ChatSessionMeta[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [loadingTurns, setLoadingTurns] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);          // 当前会话视图正在流式（send 或 attach）
  const [streamText, setStreamText] = useState("");
  const [thinkText, setThinkText] = useState("");
  const [toolRuns, setToolRuns] = useState<ToolRun[]>([]);
  const [pendingFile, setPendingFile] = useState<AgentUploadResult | null>(null);
  const [uploading, setUploading] = useState(false);
  const [generatingIds, setGeneratingIds] = useState<Set<number>>(new Set());  // 后台仍在生成的会话

  const streamBufRef = useRef("");
  const thinkBufRef = useRef("");
  const flushTimerRef = useRef<number | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const scrollBoxRef = useRef<HTMLDivElement>(null);
  const activeIdRef = useRef<number | null>(null);
  const openSeqRef = useRef(0);
  const ctlRef = useRef<StreamCtl | null>(null);     // 当前活跃流的控制块

  const markGenerating = (id: number, on: boolean) =>
    setGeneratingIds((prev) => {
      if (on === prev.has(id)) return prev;
      const next = new Set(prev);
      if (on) next.add(id); else next.delete(id);
      return next;
    });

  const switchTo = (id: number | null) => { activeIdRef.current = id; setActiveId(id); };
  const flushStream = () => { setStreamText(streamBufRef.current); setThinkText(thinkBufRef.current); };
  const clearFlush = () => {
    if (flushTimerRef.current) { window.clearInterval(flushTimerRef.current); flushTimerRef.current = null; }
  };
  const bumpTop = (sessionId: number) =>
    setSessions((prev) => {
      const i = prev.findIndex((s) => s.id === sessionId);
      if (i < 0) return prev;
      const next = [...prev];
      const [s] = next.splice(i, 1);
      return [{ ...s, updated_at: new Date().toISOString() }, ...next];
    });

  useEffect(() => {
    listChatSessions()
      .then(({ data }) => {
        setSessions(data.items);
        setGeneratingIds(new Set(data.items.filter((s) => s.generating).map((s) => s.id)));
      })
      .catch(() => message.error("加载会话列表失败"));
    const draft = localStorage.getItem("chat_draft");
    if (draft) {
      localStorage.removeItem("chat_draft");
      setInput(draft);
      message.info("已恢复上次未发送的内容");
    }
    return () => clearFlush();
  }, []);

  // 启动一路流：opts.message 有值=发新消息(send)，无值=重新订阅(attach 续直播)。
  // 闭包/异步 resolve 都在 await 之后，state 已过期 → 全程走 ref + ctl 自带标志。
  const startStream = (sessionId: number, opts: { message?: string; userTurn?: Turn }): StreamCtl => {
    const ctl: StreamCtl = {
      sessionId, abort: new AbortController(), settled: false, conflict: false, trace: [],
      userTurn: opts.userTurn, done: Promise.resolve(), resolveDone: () => {},
    };
    ctl.done = new Promise<void>((res) => { ctl.resolveDone = res; });
    ctlRef.current = ctl;
    const isCurrent = () => ctlRef.current === ctl;
    setBusy(true);
    markGenerating(sessionId, true);
    stickToBottomRef.current = true;
    streamBufRef.current = "";
    thinkBufRef.current = "";
    setStreamText("");
    setThinkText("");
    setToolRuns([]);
    clearFlush();
    flushTimerRef.current = window.setInterval(flushStream, 80);

    const finish = (content: string, stopped: boolean) => {
      if (ctl.settled) return;
      ctl.settled = true;
      if (isCurrent()) {
        clearFlush();
        streamBufRef.current = "";
        setStreamText("");
        setThinkText("");
        setToolRuns([]);
        setBusy(false);
      }
      markGenerating(sessionId, false);
      const reasoning = thinkBufRef.current || undefined;   // 先取值：setTurns 的回调是异步执行的，
      thinkBufRef.current = "";                              // 不能在回调里读会被同步清空的 ref
      if ((content || ctl.trace.length) && activeIdRef.current === sessionId) {
        setTurns((prev) => [...prev, {
          role: "assistant", content: content || "(无内容)", tools: [...ctl.trace], reasoning, stopped,
        }]);
      }
      bumpTop(sessionId);
      ctl.resolveDone();
    };

    const reload = async (id: number) => {
      const seq = ++openSeqRef.current;
      try {
        const { data } = await getChatMessages(id);
        if (seq === openSeqRef.current && activeIdRef.current === id) {
          setTurns(data.items.map((m) => ({
            role: m.role, content: m.content,
            tools: m.tools?.length ? m.tools : undefined, stopped: m.stopped,
          })));
        }
      } catch { /* 忽略 */ }
    };

    const onEvent = (ev: SessionStreamEvent) => {
      if (ctl.settled || !isCurrent()) return;     // 已切走/停止：忽略后续事件
      if (ev.type === "delta") streamBufRef.current += ev.text;
      else if (ev.type === "thinking") thinkBufRef.current += ev.text;
      else if (ev.type === "title")
        setSessions((prev) => prev.map((s) => (s.id === sessionId ? { ...s, title: ev.title } : s)));
      else if (ev.type === "tool") {
        ctl.trace.push({ name: ev.name, args: ev.args });
        setToolRuns((t) => [...t, { name: ev.name, done: false }]);
      } else if (ev.type === "tool_done")
        setToolRuns((t) => {
          const i = [...t].reverse().findIndex((x) => x.name === ev.name && !x.done);
          if (i < 0) return t;
          const idx = t.length - 1 - i;
          return t.map((x, j) => (j === idx ? { ...x, done: true } : x));
        });
      else if (ev.type === "done") finish(streamBufRef.current, !!ev.stopped);
      else if (ev.type === "error") { message.error(ev.message); finish(streamBufRef.current, true); }
      else if (ev.type === "no_active") {
        // attach 时该会话已生成完：之前为防重复去掉了末尾 in-progress 气泡，这里重载补回最终消息
        ctl.settled = true;
        if (isCurrent()) { clearFlush(); setStreamText(""); setThinkText(""); setToolRuns([]); setBusy(false); thinkBufRef.current = ""; }
        markGenerating(sessionId, false);
        if (activeIdRef.current === sessionId) reload(sessionId);
        ctl.resolveDone();
      }
    };

    const run = opts.message != null
      ? sessionChatStream(sessionId, opts.message, onEvent, ctl.abort.signal)
      : attachChatStream(sessionId, onEvent, ctl.abort.signal);

    run.then(() => finish(streamBufRef.current, false)).catch((e: any) => {
      if (ctl.settled) { ctl.resolveDone(); return; }   // detach/停止 已处理过
      if (e?.name === "AbortError") {
        finish(streamBufRef.current + "\n\n*(已停止生成)*", true);   // 用户主动停止
        return;
      }
      const code = /stream http (\d+)/.exec(e?.message || "")?.[1];
      if (code === "409") {   // 会话锁未释放：标记冲突交 send 退避重试，保留用户气泡、不打扰
        ctl.conflict = true;
        ctl.settled = true;
        if (isCurrent()) { clearFlush(); setBusy(false); }
        markGenerating(sessionId, false);
        ctl.resolveDone();
        return;
      }
      if (code === "401") {
        localStorage.setItem("chat_draft", opts.message || "");
        clearSessionScopedPreferences();
        localStorage.removeItem("token");
        message.error("登录已过期，请重新登录");
        setTimeout(() => location.reload(), 800);
      } else if (opts.message != null) message.error("连接失败，请稍后重试");
      markGenerating(sessionId, false);
      if (!streamBufRef.current && !ctl.trace.length) {   // 无产出：回滚乐观追加的用户气泡
        ctl.settled = true;
        if (isCurrent()) { clearFlush(); setBusy(false); }
        if (ctl.userTurn && activeIdRef.current === sessionId)
          setTurns((prev) => (prev.length && prev[prev.length - 1] === ctl.userTurn ? prev.slice(0, -1) : prev));
        ctl.resolveDone();
      } else finish(streamBufRef.current, true);
    });
    return ctl;
  };

  // 切走当前会话：停本地显示，但服务端 worker 续跑（会话留在 generatingIds，后台生成）。
  // keepPartial=true（打断同会话续问时）：把已生成的半截作为「已中断」气泡留在视图，与库内一致。
  const detachActive = (keepPartial = false) => {
    const ctl = ctlRef.current;
    if (!ctl || ctl.settled) return;
    ctl.settled = true;          // run 的 catch 因 settled 短路，不再落「已停止」气泡
    clearFlush();
    const partial = streamBufRef.current;
    const sid = ctl.sessionId;
    thinkBufRef.current = "";
    setStreamText("");
    setThinkText("");
    setToolRuns([]);
    setBusy(false);
    ctlRef.current = null;
    if (keepPartial && partial.trim() && activeIdRef.current === sid) {
      setTurns((prev) => [...prev, {
        role: "assistant", content: partial, tools: [...ctl.trace], stopped: true,
      }]);
    }
    ctl.abort.abort();           // 仅断本地 fetch；未调 cancel → 服务端继续生成
    ctl.resolveDone();
  };

  const openSession = async (id: number) => {
    if (id === activeIdRef.current) return;
    if (busy) detachActive();    // 切走生成中的会话：后台续跑
    const seq = ++openSeqRef.current;
    switchTo(id);
    setLoadingTurns(true);
    try {
      const { data } = await getChatMessages(id);
      if (seq !== openSeqRef.current) return;
      let items: Turn[] = data.items.map((m) => ({
        role: m.role, content: m.content,
        tools: m.tools?.length ? m.tools : undefined, stopped: m.stopped,
      }));
      const isGen = generatingIds.has(id);
      // 后台生成中：末尾若是 in-progress 的 assistant checkpoint，先去掉，由 attach 重建直播
      if (isGen && items.length && items[items.length - 1].role === "assistant") items = items.slice(0, -1);
      setTurns(items);
      setLoadingTurns(false);
      if (isGen) startStream(id, {});   // attach：回放已生成 + 续实时（连续直播）
    } catch (e: any) {
      if (seq !== openSeqRef.current) return;
      setLoadingTurns(false);
      if (e?.response?.status === 404) {
        message.error("会话已不存在");
        setSessions((prev) => prev.filter((s) => s.id !== id));
        markGenerating(id, false);
        switchTo(null);
        setTurns([]);
      } else message.error("加载会话失败，请重试");
    }
  };

  const newChat = () => {
    if (busy) detachActive();    // 留当前会话后台跑，开新空对话
    openSeqRef.current++;
    switchTo(null);
    setTurns([]);
  };

  const removeSession = async (id: number) => {
    if (id === activeIdRef.current && busy) detachActive();
    try {
      await deleteChatSession(id);
    } catch {
      /* 已删/网络错都按本地移除处理 */
    }
    setSessions((prev) => prev.filter((s) => s.id !== id));
    markGenerating(id, false);
    if (id === activeIdRef.current) {
      switchTo(null);
      setTurns([]);
    }
  };

  const scrollToBottom = () => {
    if (stickToBottomRef.current) bottomRef.current?.scrollIntoView({ block: "end" });
  };
  useEffect(scrollToBottom, [streamText, thinkText, toolRuns, turns]);

  const onScroll = () => {
    const el = scrollBoxRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const doUpload = async (file: File) => {
    if (uploading) return false;   // 防在途重复上传：竞态会让先上传那张的 pendingFile 被覆盖丢弃
    setUploading(true);
    try {
      const { data } = await agentUpload(file);
      setPendingFile(data);
      message.success(`已附加 ${data.filename}，输入要求后发送`);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "上传失败");
    } finally {
      setUploading(false);
    }
    return false;
  };

  const send = async (text: string) => {
    let q = text.trim();
    if (!q && !pendingFile) return;

    // 生成中发新消息 = 打断当前轮 + 用新消息续问：服务端历史已含被打断的半截 → 一起考量
    if (busy) {
      const sid0 = activeIdRef.current;
      if (sid0 != null) cancelChatStream(sid0);  // 服务端收束当前轮，半截以「已中断」落库
      detachActive(true);                         // 本地把半截留为「已中断」气泡
      // worker 每个 token 都查取消信号、通常很快释放会话锁；先给个常见时间，
      // 不够则靠下面的 409 退避重试兜底（不会让用户手动重发）
      await delay(350);
    }

    if (pendingFile) {
      const meta = pendingFile.sheets
        ? `sheets: ${pendingFile.sheets.map((s) => `${s.name}(${s.n_rows}行x${s.n_cols}列)`).join("、")}`
        : `类型: ${pendingFile.file_kind}`;
      q = `[已上传文件「${pendingFile.filename}」 file_id=${pendingFile.file_id}，${meta}]\n\n${q || "请处理这个文件"}`;
      setPendingFile(null);
    }

    setInput("");
    let sid = activeIdRef.current;
    if (sid == null) {   // 懒建会话：第一条消息才落库
      try {
        const { data } = await createChatSession();
        sid = data.id;
        switchTo(sid);
        setSessions((prev) => [data, ...prev]);
      } catch {
        message.error("创建会话失败，请稍后重试");
        return;
      }
    }
    const userTurn: Turn = { role: "user", content: q };
    setTurns((prev) => [...prev, userTurn]);
    // 打断后服务端释放会话锁可能稍有延迟：遇 409 退避重试，让「打断续问」尽量无感
    let ctl = startStream(sid, { message: q, userTurn });
    await ctl.done;
    for (let i = 0; ctl.conflict && i < 4; i++) {
      await delay(500 + i * 400);
      ctl = startStream(sid, { message: q, userTurn });
      await ctl.done;
    }
    if (ctl.conflict && activeIdRef.current === sid) {   // 几次仍未拿到锁：回滚气泡 + 提示重发
      message.warning("上一轮还在收尾，请稍后重发");
      setTurns((prev) => (prev.length && prev[prev.length - 1] === userTurn ? prev.slice(0, -1) : prev));
    }
  };

  const stop = () => {
    // 真取消：通知服务端 worker 收束（已生成部分以"已中断"落库），再断开本地流
    const sid = activeIdRef.current;
    if (sid != null) cancelChatStream(sid);
    ctlRef.current?.abort.abort();
  };

  const copyText = (t: string) =>
    navigator.clipboard.writeText(t).then(() => message.success("已复制"));

  const runningTool = toolRuns.find((t) => !t.done);

  return (
    <div style={{ display: "flex", margin: -24, height: "calc(100vh - 64px)", background: "#fff" }}>
      <style>{`
        .chat-md table { border-collapse: collapse; margin: 10px 0; background: #fff; font-size: 13px; }
        .chat-md th, .chat-md td { border: 1px solid #e5e7eb; padding: 5px 12px; }
        .chat-md th { background: #f9fafb; font-weight: 600; }
        .chat-md p { margin: 6px 0; line-height: 1.75; }
        .chat-md h2, .chat-md h3 { margin: 14px 0 6px; font-size: 15px; }
        .chat-md hr { border: none; border-top: 1px solid #f0f0f0; margin: 12px 0; }
        .chat-md ul, .chat-md ol { padding-left: 20px; margin: 6px 0; }
        .chat-md li { margin: 3px 0; }
        .chat-md blockquote { margin: 8px 0; padding: 2px 12px; border-left: 3px solid #c7d2fe; background: #f8faff; color: #4b5563; }
        .chat-md code { background: #f3f4f6; padding: 1px 5px; border-radius: 4px; font-size: 12.5px; }
        .sess-item:hover { background: #eef2ff !important; }
        .sess-item:hover .sess-del { opacity: 1; }
        .turn-assistant:hover .copy-btn { opacity: 1; }
        @keyframes blink { 50% { opacity: 0; } }
        .cursor { display: inline-block; width: 7px; height: 16px; background: #4f46e5; vertical-align: -2px; animation: blink 1s step-start infinite; }
      `}</style>

      {/* 会话栏 */}
      <aside style={{ width: 230, borderRight: "1px solid #f0f0f0", background: "#fafafa",
                      display: "flex", flexDirection: "column", flexShrink: 0 }}>
        <div style={{ padding: 12 }}>
          <Button block icon={<PlusOutlined />} onClick={newChat}>
            新对话
          </Button>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "0 8px 12px" }}>
          {sessions.map((s) => (
            <div key={s.id} className="sess-item" role="button" tabIndex={0}
              onClick={() => s.id !== activeId && openSession(s.id)}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); s.id !== activeId && openSession(s.id); } }}
              style={{
                padding: "8px 10px", borderRadius: 8, cursor: "pointer", marginBottom: 2,
                display: "flex", alignItems: "center", gap: 6,
                background: s.id === activeId ? "#eef2ff" : "transparent",
                color: s.id === activeId ? "#4f46e5" : "#374151",
              }}>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis",
                             whiteSpace: "nowrap", fontSize: 13 }}>{s.title}</span>
              {generatingIds.has(s.id) && (
                <span title="生成中（后台）" style={{
                  width: 7, height: 7, borderRadius: 4, flexShrink: 0, background: "#10b981",
                  animation: "blink 1.2s step-start infinite",
                }} />
              )}
              <Popconfirm title="删除该对话？" onConfirm={(e) => {
                e?.stopPropagation();
                removeSession(s.id);
              }}>
                <DeleteOutlined className="sess-del" onClick={(e) => e.stopPropagation()}
                  style={{ opacity: 0, fontSize: 12, color: "var(--mb-text-3)" }} />
              </Popconfirm>
            </div>
          ))}
        </div>
      </aside>

      {/* 主区 */}
      <main style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <div ref={scrollBoxRef} onScroll={onScroll} style={{ flex: 1, overflowY: "auto" }}>
          <div style={{ maxWidth: 860, margin: "0 auto", padding: "24px 24px 8px" }}>
            {loadingTurns && (
              <div style={{ textAlign: "center", padding: 48 }}><Spin /></div>
            )}
            {!loadingTurns && turns.length === 0 && !busy && (
              <div style={{ textAlign: "center", paddingTop: 48 }}>
                <div style={{
                  width: 56, height: 56, borderRadius: 16, margin: "0 auto 16px",
                  background: "linear-gradient(135deg,#4f46e5,#9333ea)", display: "flex",
                  alignItems: "center", justifyContent: "center",
                }}>
                  <RobotOutlined style={{ fontSize: 28, color: "#fff" }} />
                </div>
                <h2 style={{ marginBottom: 4 }}>AI 定价助手</h2>
                <div style={{ color: "var(--mb-text-3)", marginBottom: 28 }}>
                  基于公司真实采购 / 销售 / 库存数据回答 · 对话记录云端保存，换设备也能接着聊
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12,
                              maxWidth: 640, margin: "0 auto", textAlign: "left" }}>
                  {EXAMPLES.map((ex) => {
                    const clickable = !ex.q.startsWith("点左下角");
                    return (
                    <div key={ex.title} role={clickable ? "button" : undefined} tabIndex={clickable ? 0 : -1}
                      onClick={() => clickable && send(ex.q)}
                      onKeyDown={(e) => { if (clickable && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); send(ex.q); } }}
                      style={{
                        border: "1px solid #e5e7eb", borderRadius: 12, padding: "12px 14px",
                        cursor: clickable ? "pointer" : "default", background: "#fff",
                      }}
                      onMouseEnter={(e) => (e.currentTarget.style.borderColor = "#c7d2fe")}
                      onMouseLeave={(e) => (e.currentTarget.style.borderColor = "#e5e7eb")}>
                      <div style={{ fontWeight: 600, marginBottom: 2 }}>{ex.icon} {ex.title}</div>
                      <div style={{ color: "#6b7280", fontSize: 12.5 }}>{ex.q}</div>
                    </div>
                    );
                  })}
                </div>
              </div>
            )}

            {turns.map((t, i) =>
              t.role === "user" ? (
                <div key={i} style={{ display: "flex", justifyContent: "flex-end", margin: "16px 0" }}>
                  <div style={{
                    maxWidth: "78%", padding: "10px 16px", borderRadius: "16px 16px 4px 16px",
                    background: "#4f46e5", color: "#fff", fontSize: 14, lineHeight: 1.7,
                    whiteSpace: "pre-wrap", wordBreak: "break-word",
                  }}>
                    <UserContent text={t.content} />
                  </div>
                </div>
              ) : (
                <div key={i} className="turn-assistant" style={{ display: "flex", gap: 12, margin: "16px 0" }}>
                  <div style={{
                    width: 30, height: 30, borderRadius: 10, flexShrink: 0, marginTop: 2,
                    background: "linear-gradient(135deg,#4f46e5,#9333ea)", display: "flex",
                    alignItems: "center", justifyContent: "center",
                  }}>
                    <RobotOutlined style={{ color: "#fff", fontSize: 15 }} />
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {t.reasoning && <ThinkBlock text={t.reasoning} />}
                    <div className="chat-md" style={{ fontSize: 14 }}>
                      <Md text={t.content} />
                    </div>
                    <ToolTrace tools={t.tools} />
                    {t.stopped && <Tag style={{ marginTop: 6 }} color="orange">已中断</Tag>}
                    <Tooltip title="复制全文">
                      <Button className="copy-btn" size="small" type="text" icon={<CopyOutlined />}
                        style={{ opacity: 0, color: "var(--mb-text-3)", marginTop: 4 }}
                        onClick={() => copyText(t.content)} />
                    </Tooltip>
                  </div>
                </div>
              ),
            )}

            {/* 流式中的当前回复 */}
            {busy && (
              <div style={{ display: "flex", gap: 12, margin: "16px 0" }}>
                <div style={{
                  width: 30, height: 30, borderRadius: 10, flexShrink: 0, marginTop: 2,
                  background: "linear-gradient(135deg,#4f46e5,#9333ea)", display: "flex",
                  alignItems: "center", justifyContent: "center",
                }}>
                  <RobotOutlined style={{ color: "#fff", fontSize: 15 }} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  {thinkText && <ThinkBlock text={thinkText} streaming />}
                  <LiveTrace runs={toolRuns} />
                  <div className="chat-md" style={{ fontSize: 14 }}>
                    {streamText ? <Md text={streamText} />
                      : (!runningTool && !thinkText) && <span style={{ color: "var(--mb-text-3)" }}>思考中…</span>}
                    <span className="cursor" />
                  </div>
                </div>
              </div>
            )}
            <div ref={bottomRef} style={{ height: 1 }} />
          </div>
        </div>

        {/* 输入区 */}
        <div style={{ borderTop: "1px solid #f5f5f5", background: "#fff" }}>
          <div style={{ maxWidth: 860, margin: "0 auto", padding: "12px 24px 8px" }}>
            {pendingFile && (
              <Tag closable color="purple" onClose={() => setPendingFile(null)}
                style={{ marginBottom: 8, padding: "3px 10px" }}>
                📎 {pendingFile.filename}（{pendingFile.sheets
                  ? pendingFile.sheets.map((s) => `${s.name} ${s.n_rows}行`).join("、")
                  : pendingFile.file_kind}）
              </Tag>
            )}
            <div style={{
              display: "flex", alignItems: "flex-end", gap: 8, border: "1px solid #e5e7eb",
              borderRadius: 14, padding: "8px 10px", boxShadow: "0 2px 10px rgba(0,0,0,0.04)",
            }}>
              <Upload accept=".xlsx,.docx,.pdf,.txt,.csv,.jpg,.jpeg,.png,.webp"
                showUploadList={false} disabled={uploading}
                beforeUpload={(f) => doUpload(f as unknown as File)}>
                <Tooltip title="上传文件：询价单/整机配置（Excel/Word/PDF/txt/图片）；图片可直接 Ctrl+V 粘贴">
                  <Button type="text" icon={<PaperClipOutlined />} loading={uploading}
                    style={{ color: "#6b7280" }} />
                </Tooltip>
              </Upload>
              <Input.TextArea
                autoSize={{ minRows: 1, maxRows: 6 }}
                variant="borderless"
                placeholder={pendingFile ? "对这个文件想做什么？如：查最近采购价填进去发我"
                                         : "问点什么…（Enter 发送，Shift+Enter 换行）"}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onPaste={(e) => {
                  // 只拦"纯图片"粘贴（截图）当附件上传；剪贴板含文本（如从 Excel/网页复制带图内容）
                  // → 放行默认文本粘贴，绝不吞掉用户想粘的文字
                  const dt = e.clipboardData;
                  if (!dt) return;
                  const hasText = !!dt.getData("text/plain")
                    || Array.from(dt.items).some((it) => it.kind === "string");
                  if (hasText) return;
                  const img = Array.from(dt.items).find(
                    (it) => it.kind === "file" && it.type.startsWith("image/"));
                  const f = img?.getAsFile();
                  if (f) {
                    e.preventDefault();
                    const named = f.name && f.name !== "image.png"
                      ? f : new File([f], `粘贴图片-${Date.now()}.png`, { type: f.type || "image/png" });
                    doUpload(named);
                  }
                }}
                onPressEnter={(e) => {
                  if (!e.shiftKey) {
                    e.preventDefault();
                    send(input);
                  }
                }}
                style={{ flex: 1, fontSize: 14 }}
              />
              {busy && !input.trim() && !pendingFile ? (
                <Tooltip title="停止生成">
                  <Button danger type="primary" shape="circle" icon={<StopOutlined />} onClick={stop} />
                </Tooltip>
              ) : (
                <Tooltip title={busy ? "打断当前回答，带上你的新消息继续" : "发送"}>
                  <Button type="primary" shape="circle" icon={<SendOutlined />}
                    disabled={!input.trim() && !pendingFile} onClick={() => send(input)} />
                </Tooltip>
              )}
            </div>
            <div style={{ textAlign: "center", color: "var(--mb-text-2)", fontSize: 11, padding: "6px 0 4px" }}>
              AI 基于库内数据回答，可能出错——重要报价请人工复核
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
