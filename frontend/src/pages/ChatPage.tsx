import { useEffect, useRef, useState } from "react";
import { Button, Input, Modal, Popconfirm, Spin, Tag, Tooltip, Upload, message } from "antd";
import {
  CopyOutlined, DeleteOutlined, DownloadOutlined, EyeOutlined, FileExcelOutlined,
  PaperClipOutlined, PlusOutlined, RobotOutlined, SendOutlined, StopOutlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  agentDownload, agentFileBlobUrl, agentPreview, agentUpload, cancelChatStream,
  createChatSession, deleteChatSession, getChatMessages, listChatSessions, sessionChatStream,
} from "../api";
import type { AgentToolCall, AgentUploadResult, ChatSessionMeta, FilePreview } from "../api";
import { COLORS } from "../theme";

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
  stopped?: boolean;
}
interface ToolRun {
  name: string;
  done: boolean;
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
  const fileId = /\/api\/agent\/files\/([a-z0-9]{6,})/i.exec(href)?.[1] || "";
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

function toolChips(tools?: AgentToolCall[]) {
  if (!tools?.length) return null;
  return (
    <div style={{ marginTop: 8 }}>
      {tools.map((t, i) => {
        const arg = (t.args?.query ?? t.args?.pn_std ?? t.args?.dimension ?? "") as string;
        return (
          <Tag key={i} style={{ fontSize: 11, color: "#6b7280", background: "#f3f4f6", border: "none" }}>
            {TOOL_LABEL[t.name] || t.name}{arg ? `（${String(arg).slice(0, 26)}）` : ""}
          </Tag>
        );
      })}
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
        <Tag color="rgba(255,255,255,0.25)" style={{ color: "#fff", border: "1px solid rgba(255,255,255,0.4)" }}>
          📎 {m[1]}
        </Tag>
      </div>
      {m[2]}
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
  const [busy, setBusy] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [toolRuns, setToolRuns] = useState<ToolRun[]>([]);
  const [pendingFile, setPendingFile] = useState<AgentUploadResult | null>(null);
  const [uploading, setUploading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const streamBufRef = useRef("");
  const flushTimerRef = useRef<number | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const scrollBoxRef = useRef<HTMLDivElement>(null);
  // 流式回调/异步 resolve 都发生在 await 之后，闭包里的 state 已过期——
  // 当前会话与加载序号必须走 ref（教训同旧版 localStorage 时代的闭包陷阱）
  const activeIdRef = useRef<number | null>(null);
  const openSeqRef = useRef(0);

  const switchTo = (id: number | null) => {
    activeIdRef.current = id;
    setActiveId(id);
  };

  useEffect(() => {
    listChatSessions()
      .then(({ data }) => setSessions(data.items))
      .catch(() => message.error("加载会话列表失败"));
    // 401 reload 前暂存的草稿：登录回来恢复到输入框
    const draft = localStorage.getItem("chat_draft");
    if (draft) {
      localStorage.removeItem("chat_draft");
      setInput(draft);
      message.info("已恢复上次未发送的内容");
    }
  }, []);

  const openSession = async (id: number) => {
    if (busy) return;
    const seq = ++openSeqRef.current;
    switchTo(id);
    setLoadingTurns(true);
    try {
      const { data } = await getChatMessages(id);
      if (seq !== openSeqRef.current) return; // 已切到别的会话：丢弃过期响应
      setTurns(data.items.map((m) => ({
        role: m.role, content: m.content,
        tools: m.tools?.length ? m.tools : undefined, stopped: m.stopped,
      })));
    } catch (e: any) {
      if (seq !== openSeqRef.current) return;
      if (e?.response?.status === 404) {
        message.error("会话已不存在");
        setSessions((prev) => prev.filter((s) => s.id !== id));
        switchTo(null);
        setTurns([]);
      } else {
        message.error("加载会话失败，请重试");
      }
    } finally {
      if (seq === openSeqRef.current) setLoadingTurns(false);
    }
  };

  const newChat = () => {
    if (busy) return;
    openSeqRef.current++;
    switchTo(null);
    setTurns([]);
  };

  const removeSession = async (id: number) => {
    if (busy) return; // 流式中删除会话：worker 还在写、视图会注入孤儿气泡
    try {
      await deleteChatSession(id);
    } catch {
      /* 已删/网络错都按本地移除处理 */
    }
    setSessions((prev) => prev.filter((s) => s.id !== id));
    if (id === activeIdRef.current) {
      switchTo(null);
      setTurns([]);
    }
  };

  const scrollToBottom = () => {
    if (stickToBottomRef.current) bottomRef.current?.scrollIntoView({ block: "end" });
  };
  useEffect(scrollToBottom, [streamText, toolRuns, turns]);

  const onScroll = () => {
    const el = scrollBoxRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const flushStream = () => {
    setStreamText(streamBufRef.current);
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
    if ((!q && !pendingFile) || busy) return;
    if (pendingFile) {
      const meta = pendingFile.sheets
        ? `sheets: ${pendingFile.sheets.map((s) => `${s.name}(${s.n_rows}行x${s.n_cols}列)`).join("、")}`
        : `类型: ${pendingFile.file_kind}`;
      q = `[已上传文件「${pendingFile.filename}」 file_id=${pendingFile.file_id}，${meta}]\n\n${q || "请处理这个文件"}`;
      setPendingFile(null);
    }

    setBusy(true);
    setInput("");
    // 懒建会话：第一条消息才落库
    let sid = activeIdRef.current;
    if (sid == null) {
      try {
        const { data } = await createChatSession();
        sid = data.id;
        switchTo(sid);
        setSessions((prev) => [data, ...prev]);
      } catch {
        message.error("创建会话失败，请稍后重试");
        setBusy(false);
        return;
      }
    }
    const sessionId = sid;
    const userTurnObj: Turn = { role: "user", content: q };
    setTurns((prev) => [...prev, userTurnObj]);
    stickToBottomRef.current = true;
    streamBufRef.current = "";
    const trace: AgentToolCall[] = [];
    let settled = false;  // done/error/abort 只收尾一次（busy 闭包过期，不能用它判断）
    abortRef.current = new AbortController();
    flushTimerRef.current = window.setInterval(flushStream, 80);

    const finishTurn = (content: string, stopped = false) => {
      if (settled) return;
      settled = true;
      if (flushTimerRef.current) window.clearInterval(flushTimerRef.current);
      flushTimerRef.current = null;
      streamBufRef.current = "";
      setStreamText("");
      setToolRuns([]);
      setBusy(false);
      // 视图可能已切走（理论上 busy 挡住了切换，此为最后防线）：不往别的会话注入气泡
      if ((content || trace.length) && activeIdRef.current === sessionId) {
        setTurns((prev) => [...prev, {
          role: "assistant", content: content || "(无内容)", tools: [...trace], stopped,
        }]);
      }
      // 会话置顶（服务端 updated_at 已更新，这里同步本地排序）
      setSessions((prev) => {
        const i = prev.findIndex((s) => s.id === sessionId);
        if (i < 0) return prev;
        const next = [...prev];
        const [s] = next.splice(i, 1);
        return [{ ...s, updated_at: new Date().toISOString() }, ...next];
      });
    };

    try {
      await sessionChatStream(sessionId, q, (ev) => {
        if (ev.type === "delta") {
          streamBufRef.current += ev.text;
        } else if (ev.type === "title") {
          setSessions((prev) => prev.map((s) =>
            s.id === sessionId ? { ...s, title: ev.title } : s));
        } else if (ev.type === "tool") {
          trace.push({ name: ev.name, args: ev.args });
          setToolRuns((t) => [...t, { name: ev.name, done: false }]);
        } else if (ev.type === "tool_done") {
          setToolRuns((t) => {
            const i = [...t].reverse().findIndex((x) => x.name === ev.name && !x.done);
            if (i < 0) return t;
            const idx = t.length - 1 - i;
            return t.map((x, j) => (j === idx ? { ...x, done: true } : x));
          });
        } else if (ev.type === "done") {
          finishTurn(streamBufRef.current);
        } else if (ev.type === "error") {
          message.error(ev.message);
          finishTurn(streamBufRef.current, true);
        }
      }, abortRef.current.signal);
      // 流结束但没收到 done 事件（服务端异常断流）：兜底收尾
      finishTurn(streamBufRef.current);
    } catch (e: any) {
      if (e?.name === "AbortError") {
        finishTurn(streamBufRef.current + "\n\n*(已停止生成)*", true);
      } else {
        const httpStatus = /stream http (\d+)/.exec(e?.message || "")?.[1];
        if (httpStatus === "409") {
          message.warning("上一轮回答还在生成中，请稍候几秒再发");
        } else if (httpStatus === "401") {
          // 先把刚输入的内容存草稿再 reload，登录回来不丢稿
          localStorage.setItem("chat_draft", text);
          localStorage.removeItem("token");
          message.error("登录已过期，请重新登录");
          setTimeout(() => location.reload(), 800);
        } else {
          message.error("连接失败，请稍后重试");
        }
        // 本轮没有任何内容产生：回滚乐观追加的用户气泡，保持与服务端一致
        if (!streamBufRef.current && !trace.length) {
          settled = true;
          if (flushTimerRef.current) window.clearInterval(flushTimerRef.current);
          flushTimerRef.current = null;
          setBusy(false);
          if (activeIdRef.current === sessionId) {
            setTurns((prev) => (prev.length && prev[prev.length - 1] === userTurnObj
              ? prev.slice(0, -1) : prev));
          }
        } else {
          finishTurn(streamBufRef.current, true);
        }
      }
    }
  };

  const stop = () => {
    // 真取消：通知服务端 worker 收束（已生成部分以"已中断"落库），再断开本地流
    const sid = activeIdRef.current;
    if (sid != null) cancelChatStream(sid);
    abortRef.current?.abort();
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
          <Button block icon={<PlusOutlined />} disabled={busy} onClick={newChat}>
            新对话
          </Button>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "0 8px 12px" }}>
          {sessions.map((s) => (
            <div key={s.id} className="sess-item" onClick={() => s.id !== activeId && openSession(s.id)}
              style={{
                padding: "8px 10px", borderRadius: 8, cursor: "pointer", marginBottom: 2,
                display: "flex", alignItems: "center", gap: 6,
                background: s.id === activeId ? "#eef2ff" : "transparent",
                color: s.id === activeId ? "#4f46e5" : "#374151",
              }}>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis",
                             whiteSpace: "nowrap", fontSize: 13 }}>{s.title}</span>
              <Popconfirm title="删除该对话？" onConfirm={(e) => {
                e?.stopPropagation();
                removeSession(s.id);
              }}>
                <DeleteOutlined className="sess-del" onClick={(e) => e.stopPropagation()}
                  style={{ opacity: 0, fontSize: 12, color: "#9ca3af" }} />
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
                <div style={{ color: "#9ca3af", marginBottom: 28 }}>
                  基于公司真实采购 / 销售 / 库存数据回答 · 对话记录云端保存，换设备也能接着聊
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12,
                              maxWidth: 640, margin: "0 auto", textAlign: "left" }}>
                  {EXAMPLES.map((ex) => (
                    <div key={ex.title} onClick={() => !ex.q.startsWith("点左下角") && send(ex.q)}
                      style={{
                        border: "1px solid #e5e7eb", borderRadius: 12, padding: "12px 14px",
                        cursor: ex.q.startsWith("点左下角") ? "default" : "pointer", background: "#fff",
                      }}
                      onMouseEnter={(e) => (e.currentTarget.style.borderColor = "#c7d2fe")}
                      onMouseLeave={(e) => (e.currentTarget.style.borderColor = "#e5e7eb")}>
                      <div style={{ fontWeight: 600, marginBottom: 2 }}>{ex.icon} {ex.title}</div>
                      <div style={{ color: "#6b7280", fontSize: 12.5 }}>{ex.q}</div>
                    </div>
                  ))}
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
                    <div className="chat-md" style={{ fontSize: 14 }}>
                      <Md text={t.content} />
                    </div>
                    {toolChips(t.tools)}
                    {t.stopped && <Tag style={{ marginTop: 6 }} color="orange">已中断</Tag>}
                    <Tooltip title="复制全文">
                      <Button className="copy-btn" size="small" type="text" icon={<CopyOutlined />}
                        style={{ opacity: 0, color: "#9ca3af", marginTop: 4 }}
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
                  {toolRuns.length > 0 && (
                    <div style={{ marginBottom: 8 }}>
                      {toolRuns.map((t, i) => (
                        <div key={i} style={{ fontSize: 12.5, color: t.done ? "#9ca3af" : "#4f46e5",
                                              marginBottom: 2 }}>
                          {t.done ? "✓" : <Spin size="small" style={{ marginRight: 4 }} />}{" "}
                          {t.done ? `${TOOL_LABEL[t.name] || t.name} 完成`
                                  : `正在${TOOL_LABEL[t.name] || t.name}…`}
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="chat-md" style={{ fontSize: 14 }}>
                    {streamText ? <Md text={streamText} /> : !runningTool && (
                      <span style={{ color: "#9ca3af" }}>思考中…</span>
                    )}
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
                showUploadList={false} disabled={busy || uploading}
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
                disabled={busy}
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
              {busy ? (
                <Button danger type="primary" shape="circle" icon={<StopOutlined />} onClick={stop} />
              ) : (
                <Button type="primary" shape="circle" icon={<SendOutlined />}
                  disabled={!input.trim() && !pendingFile} onClick={() => send(input)} />
              )}
            </div>
            <div style={{ textAlign: "center", color: "#c4c4c4", fontSize: 11, padding: "6px 0 4px" }}>
              AI 基于库内数据回答，可能出错——重要报价请人工复核
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
