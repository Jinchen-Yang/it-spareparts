import PageHeader from "../components/PageHeader";
import { useEffect, useRef, useState } from "react";
import { Alert, App, Button, Drawer, Grid, Input, Modal, Popconfirm, Spin, Tag, Tooltip, Upload } from "antd";
import {
  CopyOutlined, DeleteOutlined, DownloadOutlined, EyeOutlined, FileExcelOutlined,
  PaperClipOutlined, PlusOutlined, RobotOutlined, SendOutlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  agentUpload, cancelChatStream,
  createChatSession, deleteChatSession, getChatMessages, listChatSessions,
} from "../api";
import type {
  AgentToolCall, AgentUploadResult, ChatSessionMeta, FilePreview, SessionStreamEvent,
} from "../api";
import { useLocation, useSearchParams } from "react-router-dom";
import { useContextAdmission, legacyRouteAllowed, withRouteAccess } from "../authz/access";
import { useContextTransport, domainApi } from "../authz/domainTransport";
import { useContextQuery } from "../authz/contextQuery";
import { surfaceRule } from "../authz/rules";
import { useChatStream } from "./chat/useChatStream";
import { AuthorizationCancelled } from "../authz/requestLifecycle";
import type { CanonicalStreamEvent } from "../api/chatStream";
import { COLORS, TAG_STYLES } from "../theme";

import { tokens } from "../tokens";
import "./ChatPage.css";

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
  trace: AgentToolCall[];
  userTurn?: Turn;
  done: Promise<void>;
  resolveDone: () => void;
}

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
  const { message } = App.useApp();
  const fileId = /\/api\/agent\/files\/([a-z0-9]{6,})/i.exec(href)?.[1] || "";
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState(0);
  const [legacyAllowed] = useState(() => legacyRouteAllowed("/chat"));
  const admission = useContextAdmission();
  const canPreview = admission.surface("agent_file.preview", legacyAllowed) !== "deny";
  const canDownload = admission.surface("agent_file.download", legacyAllowed) !== "deny";
  const transport = useContextTransport(JSON.stringify([fileId, open]), legacyAllowed);
  const preview = useContextQuery<FilePreview>({ entryId: "GET /api/agent/files/{file_id}/preview",
    rule: surfaceRule("agent_file.preview"), queryKey: [fileId, open], legacyAllowed,
    enabled: open && !!fileId && canPreview,
    query: async capture => (await domainApi(capture).get<FilePreview>(`/agent/files/${fileId}/preview`)).data,
  });
  const pv = preview.data, loading = preview.loading;
  const name = pv?.filename || label || "结果文件";
  const download = async () => {
    if (!fileId || !canDownload) return;
    try {
      const response = await domainApi(transport).get<Blob>(`/agent/files/${fileId}`, { responseType: "blob" });
      if (!transport.isCurrent()) return;
      const cd = String(response.headers["content-disposition"] || "");
      const match = /filename\*=UTF-8''([^;]+)/.exec(cd) || /filename="?([^";]+)"?/.exec(cd);
      const a = document.createElement("a");
      a.download = match ? decodeURIComponent(match[1]) : name;
      a.href = URL.createObjectURL(response.data); a.click(); URL.revokeObjectURL(a.href);
    } catch (error) { if (transport.isCurrent()) message.error((error as Error)?.message || "下载不可用"); }
  };
  const openPreview = () => { if (canPreview && fileId) setOpen(true); };
  if (!canPreview && !canDownload) return <span>文件操作未授权</span>;
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
          alignItems: "center", justifyContent: "center", background: COLORS.tag.success.background, color: COLORS.tag.success.text,
        }}><FileExcelOutlined /></span>
        <span style={{ fontSize: 13, color: COLORS.text, overflow: "hidden",
                       textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 200 }}>{name}</span>
        {canPreview && <Button size="small" icon={<EyeOutlined />} onClick={openPreview}>预览</Button>}
        {canDownload && <Button size="small" type="primary" icon={<DownloadOutlined />} onClick={download}>下载</Button>}
      </span>

      <Modal open={open} onCancel={() => setOpen(false)} width={820} title={name} destroyOnHidden
        footer={canDownload ? <Button type="primary" icon={<DownloadOutlined />} onClick={download}>下载文件</Button> : null}>
        {preview.error ? <Alert type="error" message="文件预览不可用" /> : loading ? (
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
          <div style={{ color: COLORS.text3, padding: 20, textAlign: "center" }}>这是图片文件。查看原件需要单独的下载权限。</div>
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
const argOf = (a?: Record<string, unknown>) =>
  String((a?.query ?? a?.pn_std ?? a?.dimension ?? a?.file_id ?? "") || "");

// 思考链：默认折叠的灰色块，点标题展开，流式时标题显示"思考中…"
function ThinkBlock({ text, streaming }: { text: string; streaming?: boolean }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  return (
    <div style={{ marginBottom: 8 }}>
      <Button type="text" aria-expanded={open} onClick={() => setOpen((o) => !o)}
        style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer",
                 height: "auto", padding: 0,
                 fontSize: 12.5, color: "var(--mb-text-3)", userSelect: "none" }}>
        <span style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .15s", fontSize: 10 }}>▶</span>
        <span>💭 {streaming ? "思考中…" : "已思考"}</span>
      </Button>
      {open && (
        <div style={{ marginTop: 4, padding: "8px 12px", background: COLORS.inset, borderRadius: 8,
                      borderLeft: "2px solid var(--mb-border)", fontSize: 12.5, color: "var(--mb-text-3)",
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
      <Button type="text" aria-expanded={open} onClick={() => setOpen((o) => !o)}
        style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer",
                 height: "auto", padding: 0,
                 fontSize: 12, color: "var(--mb-text-3)", userSelect: "none", maxWidth: "100%" }}>
        <span style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .15s", fontSize: 10 }}>▶</span>
        <span>🔧 用了 {tools.length} 次工具</span>
        {!open && <span style={{ color: "var(--mb-text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>· {summary}</span>}
      </Button>
      {open && (
        <div style={{ marginTop: 4, paddingLeft: 14, borderLeft: "2px solid var(--mb-border)" }}>
          {tools.map((t, i) => {
            const a = argOf(t.args);
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
        <div style={{ color: COLORS.accent, display: "flex", alignItems: "center", gap: 6 }}>
          <Spin size="small" /> 正在{label(active.name)}…
        </div>
      )}
    </div>
  );
}

/** 用户消息：把注入的文件前缀折叠成附件标签显示 */
function UserContent({ text }: { text: string }) {
  const m = /^\[已上传文件「(.+?)」 file_id=\w+[^\]]*\]\n\n?([\s\S]*)$/.exec(text);
  if (!m) return <>{text}</>;
  return (
    <>
      <div style={{ marginBottom: m[2] ? 6 : 0 }}>
        <Tag color={COLORS.onAccentSoft} style={{ color: COLORS.surface, border: `1px solid ${COLORS.onAccentSoftBorder}` }}>
          📎 {m[1]}
        </Tag>
      </div>
      {m[2]}
    </>
  );
}

function ChatPage() {
  const { message } = App.useApp();
  const screens = Grid.useBreakpoint();
  // 与 AppShell 共用 AntD md 断点（768px），首帧未测量时保留桌面布局。
  const isMobile = screens.md === false;
  const [sessionsOpen, setSessionsOpen] = useState(false);
  useEffect(() => { if (!isMobile) setSessionsOpen(false); }, [isMobile]);
  // 会话与消息都在服务端（平台化 P1）：刷新/换设备记录不丢。
  // activeId=null 表示"未落库的新对话"，首次发送时才真正建会话。
  const [sessions, setSessions] = useState<ChatSessionMeta[]>([]);
  const [search] = useSearchParams();
  const [legacyAllowed] = useState(() => legacyRouteAllowed("/chat"));
  const admission = useContextAdmission();
  const can = (id: string) => admission.surface(id, legacyAllowed) !== "deny";
  const canList = can("private_chat_session.list"), canHistory = can("private_chat_session.messages");
  const canCreate = can("private_chat_session.create"), canRun = can("agent_chat.conversation");
  const canAttach = can("private_chat_session.attach"), canCancel = can("private_chat_session.cancel");
  const canDelete = can("private_chat_session.delete"), canUpload = can("agent_file.upload");
  const initialId = Number(search.get("session_id"));
  const [activeId, setActiveId] = useState<number | null>(Number.isSafeInteger(initialId) && initialId > 0 ? initialId : null);
  const [locator, setLocator] = useState("");
  const [viewGeneration, setViewGeneration] = useState(0);
  const transport = useContextTransport(`chat:${viewGeneration}`, legacyAllowed);
  const stream = useChatStream(`chat:${viewGeneration}`, legacyAllowed);
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
  const activeIdRef = useRef<number | null>(activeId);
  const openSeqRef = useRef(0);
  const ctlRef = useRef<StreamCtl | null>(null);     // 当前活跃流的控制块

  const markGenerating = (id: number, on: boolean) =>
    setGeneratingIds((prev) => {
      if (on === prev.has(id)) return prev;
      const next = new Set(prev);
      if (on) next.add(id); else next.delete(id);
      return next;
    });

  const switchTo = (id: number | null, continueCurrentIntent = false) => {
    if (!continueCurrentIntent) {
      // A target change invalidates uploads/commands as well as the visible stream.
      openSeqRef.current++;
      setViewGeneration(value => value + 1);
      detachActive(); clearFlush();
      streamBufRef.current = ""; thinkBufRef.current = "";
      setStreamText(""); setThinkText(""); setToolRuns([]);
      setTurns([]); setPendingFile(null); setUploading(false); setBusy(false);
    }
    activeIdRef.current = id; setActiveId(id);
  };
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

  const listQuery = useContextQuery<{ items: ChatSessionMeta[] }>({
    entryId: "GET /api/agent/sessions", rule: surfaceRule("private_chat_session.list"), queryKey: [],
    legacyAllowed, enabled: canList, query: async capture => (await listChatSessions(capture)).data,
  });
  useEffect(() => {
    setSessions(listQuery.data?.items ?? []);
    setGeneratingIds(new Set((listQuery.data?.items ?? []).filter(s => s.generating).map(s => s.id)));
  }, [listQuery.data]);
  const historyQuery = useContextQuery<{ items: { role: "user" | "assistant"; content: string; tools?: AgentToolCall[]; stopped?: boolean }[] }>({
    entryId: "GET /api/agent/sessions/{session_id}/messages", rule: surfaceRule("private_chat_session.messages"),
    queryKey: [activeId], legacyAllowed, enabled: canHistory && activeId !== null,
    query: async capture => (await getChatMessages(activeId!, capture)).data,
  });
  useEffect(() => {
    setLoadingTurns(historyQuery.loading);
    setTurns((historyQuery.data?.items ?? []).map(m => ({ role: m.role, content: m.content, tools: m.tools, stopped: m.stopped })));
  }, [historyQuery.data, historyQuery.loading]);
  useEffect(() => () => { clearFlush(); ctlRef.current?.abort.abort(); }, []);

  // 启动一路流：opts.message 有值=发新消息(send)，无值=重新订阅(attach 续直播)。
  // 闭包/异步 resolve 都在 await 之后，state 已过期 → 全程走 ref + ctl 自带标志。
  const startStream = (sessionId: number, opts: { message?: string; userTurn?: Turn }): StreamCtl => {
    const ctl: StreamCtl = {
      sessionId, abort: new AbortController(), settled: false, trace: [],
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

    const onEvent = (ev: SessionStreamEvent | CanonicalStreamEvent) => {
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
      else if (ev.type === "done") finish(streamBufRef.current || ev.answer || "", !!ev.stopped);
      else if (ev.type === "error") {
        ctl.trace = []; thinkBufRef.current = ""; setTurns([]);
        message.error(ev.message); finish("", true);
      }
      else if (ev.type === "no_active") {
        // attach 时该会话已生成完：之前为防重复去掉了末尾 in-progress 气泡，这里重载补回最终消息
        ctl.settled = true;
        if (isCurrent()) { clearFlush(); setStreamText(""); setThinkText(""); setToolRuns([]); setBusy(false); thinkBufRef.current = ""; }
        markGenerating(sessionId, false);
        if (canHistory && activeIdRef.current === sessionId) void historyQuery.retry();
        ctl.resolveDone();
      }
    };

    void stream(sessionId, opts.message, onEvent, ctl.abort.signal).catch((error: unknown) => {
      if (!isCurrent() || ctl.abort.signal.aborted) return;
      // Failed/unauthorized/incomplete streams never become successful assistant history.
      ctl.trace = []; thinkBufRef.current = "";
      if (isCurrent()) {
        setTurns([]);
        if (!(error instanceof AuthorizationCancelled) && (error as Error)?.name !== "AbortError")
          message.error((error as Error)?.message || "聊天连接失败，没有自动重发。");
      }
      ctl.settled = false; finish("", true);
    });
    return ctl;
  };

  // 切走当前会话：停本地显示，但服务端 worker 续跑（会话留在 generatingIds，后台生成）。
  // 任何断连都不把未获终止确认的半截存成成功记录。
  const detachActive = () => {
    const ctl = ctlRef.current;
    if (!ctl || ctl.settled) return;
    ctl.settled = true;          // run 的 catch 因 settled 短路，不再落「已停止」气泡
    clearFlush();
    thinkBufRef.current = "";
    setStreamText("");
    setThinkText("");
    setToolRuns([]);
    setBusy(false);
    ctlRef.current = null;
    ctl.abort.abort();           // 仅断本地 fetch；未调 cancel → 服务端继续生成
    ctl.resolveDone();
  };

  const openSession = async (id: number) => {
    if (!Number.isSafeInteger(id) || id <= 0 || id === activeIdRef.current) return;
    switchTo(id);
  };
  const newChat = async () => {
    if (!canCreate || busy) return;
    try {
      const { data } = await createChatSession(undefined, transport);
      if (!transport.isCurrent()) return;
      switchTo(data.id);
      if (canList) void listQuery.retry();
    } catch (error) { if (transport.isCurrent()) message.error((error as Error)?.message || "创建会话失败"); }
  };
  const removeSession = async (id: number) => {
    if (!canDelete) return;
    try {
      await deleteChatSession(id, transport);
      if (!transport.isCurrent()) return;
      setSessions(prev => prev.filter(s => s.id !== id)); markGenerating(id, false);
      if (id === activeIdRef.current) switchTo(null);
    } catch (error) { if (transport.isCurrent()) message.error((error as Error)?.message || "删除失败；会话未从列表移除"); }
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
    if (!canUpload || uploading || busy) return false;   // 防在途重复上传：竞态会让先上传那张的 pendingFile 被覆盖丢弃
    const targetGeneration = openSeqRef.current;
    const current = () => targetGeneration === openSeqRef.current && transport.isCurrent();
    setUploading(true);
    try {
      const { data } = await agentUpload(file, transport);
      if (!current()) return false;
      setPendingFile({ ...data, filename: file.name });
      message.success(`已附加 ${file.name}，输入要求后发送`);
    } catch (e: any) {
      if (current()) message.error(e?.response?.data?.detail || "上传失败");
    } finally {
      if (current()) setUploading(false);
    }
    return false;
  };

  const send = async (text: string) => {
    let q = text.trim();
    if (!canRun || busy || uploading || (!q && !pendingFile) || (activeIdRef.current === null && !canCreate)) return;
    setBusy(true);
    const attachment = pendingFile;
    if (attachment) q = `[已上传文件 file_id=${attachment.file_id}]\n\n${q || "请处理这个文件"}`;
    try {
      let sid = activeIdRef.current;
      if (sid === null) {
        const { data } = await createChatSession(undefined, transport);
        if (!transport.isCurrent()) return;
        // The session is the result of this same send intent; its captured stream may continue.
        sid = data.id; switchTo(sid, true);
        if (canList) void listQuery.retry();
      }
      if (!transport.isCurrent()) return;
      setInput(""); setPendingFile(null);
      const userTurn: Turn = { role: "user", content: q };
      setTurns(prev => [...prev, userTurn]);
      startStream(sid, { message: q, userTurn });
    } catch (error) {
      if (transport.isCurrent()) { setBusy(false); message.error((error as Error)?.message || "发送失败，没有自动重发。"); }
    }
  };
  const stop = async () => {
    const sid = activeIdRef.current;
    if (sid === null || !canCancel) return;
    try {
      await cancelChatStream(sid, transport);
      if (!transport.isCurrent()) return;
      detachActive(); setTurns([]);
      if (canHistory) void historyQuery.retry();
    } catch (error) { if (transport.isCurrent()) message.error((error as Error)?.message || "停止请求失败，未确认停止。"); }
  };

  const copyText = (t: string) =>
    navigator.clipboard.writeText(t).then(() => message.success("已复制"));

  const runningTool = toolRuns.find((t) => !t.done);

  const sessionList = (
    <>
      <div style={{ padding: 12 }}>
        {canCreate && <Button block icon={<PlusOutlined />} onClick={() => { newChat(); setSessionsOpen(false); }}>
          新对话
        </Button>}
      </div>
      <div style={{ flex: 1, overflowY: "auto", minHeight: 0, padding: "0 8px 12px" }}>
        {sessions.map((s) => (
          <div key={s.id} className="chat-sess-item" data-active={s.id === activeId} style={{
            padding: "2px 6px", borderRadius: 8, marginBottom: 2,
            display: "flex", alignItems: "center", gap: 6,
          }}>
            <Button type="text" onClick={() => {
              if (s.id !== activeId) void openSession(s.id);
              setSessionsOpen(false);
            }} style={{
              flex: 1, minWidth: 0, paddingInline: 4, justifyContent: "flex-start",
              color: s.id === activeId ? COLORS.accentStrong : COLORS.text, fontSize: 13,
            }}>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.title}</span>
            </Button>
            {generatingIds.has(s.id) && (
              <span title="生成中（后台）" style={{
                width: 7, height: 7, borderRadius: 4, flexShrink: 0, background: COLORS.status.confirmed,
                animation: "chat-blink 1.2s step-start infinite",
              }} />
            )}
            {canDelete && <Popconfirm title="删除该对话？" onConfirm={() => removeSession(s.id)}>
              <Button className="chat-sess-del" type="text" size="small" danger
                aria-label={`删除对话：${s.title}`} icon={<DeleteOutlined />}
                style={{ opacity: isMobile ? 1 : undefined, flexShrink: 0 }} />
            </Popconfirm>}
          </div>
        ))}
      </div>
    </>
  );

  return (
    <div className="chat-page-shell" style={{
      display: "flex", margin: "calc(-1 * var(--mb-shell-content-padding))",
      background: COLORS.surface, minWidth: 0,
    }}>


      {/* 会话栏 */}
      {!isMobile && (canList || canCreate) && (
        <aside aria-label="会话列表" style={{ width: 230, borderRight: "1px solid var(--mb-border-soft)", background: COLORS.inset,
                        display: "flex", flexDirection: "column", flexShrink: 0 }}>
          {sessionList}
        </aside>
      )}
      {isMobile && (canList || canCreate) && (
        <Drawer title="会话" placement="left" open={sessionsOpen}
          onClose={() => setSessionsOpen(false)} width="min(320px, 100vw)"
          styles={{ body: { padding: 0, display: "flex", flexDirection: "column" } }}>
          {sessionList}
        </Drawer>
      )}

      {/* 主区 */}
      <main aria-label="AI 助手对话区" style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0 }}>
        <div className="chat-page-header">
          <PageHeader title="AI 定价助手" extra={isMobile && (canList || canCreate)
            ? <Button aria-label="会话" onClick={() => setSessionsOpen(true)} aria-expanded={sessionsOpen}>会话</Button>
            : undefined} />
        </div>
        <div style={{ padding: "0 16px 8px", display: "flex", gap: 8, flexWrap: "wrap" }}>
          <Input aria-label="已知会话 ID" placeholder="输入已知会话 ID" value={locator} style={{ width: 180 }} onChange={e => setLocator(e.target.value)} />
          <Button onClick={() => void openSession(Number(locator))} disabled={!/^\d+$/.test(locator) || Number(locator) <= 0}>打开会话</Button>
          {activeId !== null && <Tag>会话 #{activeId}</Tag>}
          {activeId !== null && canAttach && <Button disabled={busy} onClick={() => startStream(activeId, {})}>接续实时结果</Button>}
          {activeId !== null && canCancel && <Button onClick={stop}>停止生成</Button>}
          {activeId !== null && canDelete && <Popconfirm title="删除该对话？" onConfirm={() => removeSession(activeId)}><Button danger>删除当前会话</Button></Popconfirm>}
        </div>
        {listQuery.error && <Alert type="error" message="会话列表加载失败" action={<Button onClick={() => void listQuery.retry()}>重试</Button>} />}
        {historyQuery.error && <Alert type="error" message="历史正文不可用" action={<Button onClick={() => void historyQuery.retry()}>重试</Button>} />}
        <div ref={scrollBoxRef} onScroll={onScroll} style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
          <div style={{ maxWidth: 860, margin: "0 auto", padding: "24px var(--mb-shell-content-padding) 8px" }}>
            {loadingTurns && (
              <div style={{ textAlign: "center", padding: 48 }}><Spin /></div>
            )}
            {!loadingTurns && turns.length === 0 && !busy && (
              <div style={{ textAlign: "center", paddingTop: 48 }}>
                <div style={{
                  width: 56, height: 56, borderRadius: 16, margin: "0 auto 16px",
                  background: `linear-gradient(135deg,${COLORS.status.ai},${COLORS.accentStrong})`, display: "flex",
                  alignItems: "center", justifyContent: "center",
                }}>
                  <RobotOutlined style={{ fontSize: 28, color: COLORS.surface }} />
                </div>
                <div style={{ color: "var(--mb-text-3)", marginBottom: 28 }}>
                  基于公司真实采购 / 销售 / 库存数据回答 · 对话记录云端保存，换设备也能接着聊
                </div>
                <div style={{ display: "grid", gridTemplateColumns: isMobile ? "minmax(0, 1fr)" : "repeat(2, minmax(0, 1fr))", gap: 12,
                              maxWidth: 640, margin: "0 auto", textAlign: "left" }}>
                  {canRun && (activeId !== null || canCreate) && EXAMPLES.map((ex) => {
                    const clickable = !ex.q.startsWith("点左下角");
                    return (
                    <div key={ex.title} role={clickable ? "button" : undefined} tabIndex={clickable ? 0 : -1}
                      onClick={() => clickable && send(ex.q)}
                      onKeyDown={(e) => { if (clickable && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); send(ex.q); } }}
                      style={{
                        border: "1px solid var(--mb-border)", borderRadius: 12, padding: "12px 14px",
                        cursor: clickable ? "pointer" : "default", background: COLORS.surface,
                      }}
                      onMouseEnter={(e) => (e.currentTarget.style.borderColor = COLORS.accentSoftBorder)}
                      onMouseLeave={(e) => (e.currentTarget.style.borderColor = COLORS.border)}>
                      <div style={{ fontWeight: 600, marginBottom: 2 }}>{ex.icon} {ex.title}</div>
                      <div style={{ color: COLORS.text2, fontSize: 12.5 }}>{ex.q}</div>
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
                    background: COLORS.status.ai, color: COLORS.surface, fontSize: 14, lineHeight: 1.7,
                    whiteSpace: "pre-wrap", wordBreak: "break-word",
                  }}>
                    <UserContent text={t.content} />
                  </div>
                </div>
              ) : (
                <div key={i} className="chat-turn-assistant" style={{ display: "flex", gap: 12, margin: "16px 0" }}>
                  <div style={{
                    width: 30, height: 30, borderRadius: 10, flexShrink: 0, marginTop: 2,
                    background: `linear-gradient(135deg,${COLORS.status.ai},${COLORS.accentStrong})`, display: "flex",
                    alignItems: "center", justifyContent: "center",
                  }}>
                    <RobotOutlined style={{ color: COLORS.surface, fontSize: 15 }} />
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {t.reasoning && <ThinkBlock text={t.reasoning} />}
                    <div className="chat-md" style={{ fontSize: 14 }}>
                      <Md text={t.content} />
                    </div>
                    <ToolTrace tools={t.tools} />
                    {t.stopped && <Tag style={{ ...TAG_STYLES.warning, marginTop: 6 }}>已中断</Tag>}
                    <Tooltip title="复制全文">
                      <Button className="chat-copy-btn" size="small" type="text" icon={<CopyOutlined />}
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
                  background: `linear-gradient(135deg,${COLORS.status.ai},${COLORS.accentStrong})`, display: "flex",
                  alignItems: "center", justifyContent: "center",
                }}>
                  <RobotOutlined style={{ color: COLORS.surface, fontSize: 15 }} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  {thinkText && <ThinkBlock text={thinkText} streaming />}
                  <LiveTrace runs={toolRuns} />
                  <div className="chat-md" style={{ fontSize: 14 }}>
                    {streamText ? <Md text={streamText} />
                      : (!runningTool && !thinkText) && <span style={{ color: "var(--mb-text-3)" }}>思考中…</span>}
                    <span className="chat-cursor" />
                  </div>
                </div>
              </div>
            )}
            <div ref={bottomRef} style={{ height: 1 }} />
          </div>
        </div>

        {/* 输入区 */}
        <div style={{ borderTop: "1px solid var(--mb-border-soft)", background: COLORS.surface, flexShrink: 0 }}>
          <div style={{ maxWidth: 860, margin: "0 auto", padding: "12px var(--mb-shell-content-padding) 8px" }}>
            {pendingFile && (
              <Tag closable onClose={() => setPendingFile(null)}
                style={{ ...TAG_STYLES.info, marginBottom: 8, padding: "3px 10px" }}>
                📎 {pendingFile.filename}（{pendingFile.sheets
                  ? pendingFile.sheets.map((s) => `${s.name} ${s.n_rows}行`).join("、")
                  : pendingFile.file_kind}）
              </Tag>
            )}
            <div style={{
              display: "flex", alignItems: "flex-end", gap: 8, border: "1px solid var(--mb-border)",
              borderRadius: 14, padding: "8px 10px", boxShadow: tokens.shadow.input,
            }}>
              {canUpload && <Upload accept=".xlsx,.docx,.pdf,.txt,.csv,.jpg,.jpeg,.png,.webp"
                showUploadList={false} disabled={uploading || busy}
                beforeUpload={(f) => doUpload(f as unknown as File)}>
                <Tooltip title="上传文件：询价单/整机配置（Excel/Word/PDF/txt/图片）；图片可直接 Ctrl+V 粘贴">
                  <Button type="text" icon={<PaperClipOutlined />} loading={uploading}
                    style={{ color: COLORS.text2 }} />
                </Tooltip>
              </Upload>}
              {canRun && <Input.TextArea
                disabled={busy || (activeId === null && !canCreate)}
                aria-label="聊天消息"
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
                  if (f && canUpload) {
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
                style={{ flex: 1, minWidth: 0, fontSize: 14 }}
              />}
              {canRun && <Tooltip title="发送">
                <Button type="primary" shape="circle" aria-label="发送" icon={<SendOutlined />}
                  disabled={busy || uploading || (!input.trim() && !pendingFile) || (activeId === null && !canCreate)} onClick={() => send(input)} />
              </Tooltip>}
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

const ControlledChatPage = withRouteAccess(ChatPage, "/chat");
export default function ChatRoute() {
  const location = useLocation();
  return <ControlledChatPage key={location.search} />;
}
