import { useEffect, useRef, useState } from "react";
import { Button, Input, Popconfirm, Tag, Tooltip, message } from "antd";
import {
  CopyOutlined, DeleteOutlined, PaperClipOutlined, PlusOutlined,
  RobotOutlined, SendOutlined, StopOutlined, CheckOutlined,
  LoadingOutlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Upload } from "antd";
import { agentChatStream, agentDownload, agentUpload } from "../api";
import type { AgentToolCall, AgentUploadResult, ChatMessage } from "../api";

const TOOL_LABEL: Record<string, string> = {
  search_parts:      "型号搜索",
  get_part_overview: "型号全景",
  get_profit_ranking:"利润排名",
  inspect_file:      "查看文件结构",
  read_file_rows:    "读取文件数据",
  lookup_prices_bulk:"批量查价",
  write_excel:       "生成 Excel",
};

const TOOL_ICON: Record<string, string> = {
  search_parts:      "🔍",
  get_part_overview: "📊",
  get_profit_ranking:"📈",
  inspect_file:      "🗂",
  read_file_rows:    "📋",
  lookup_prices_bulk:"💹",
  write_excel:       "📝",
};

const EXAMPLES = [
  { icon: "💰", title: "销售报价", q: "ST8000NM000A 客户问报 2200 行不行？" },
  { icon: "📦", title: "采购压价", q: "我要进 50 个 ST8000NM000A，目标价多少合理？" },
  { icon: "🧩", title: "整机拆解", q: "点左下角 📎 传整机配置(Word/PDF/图片)，我拆成单件查PN和近15天采购价、生成报价单" },
  { icon: "📄", title: "询价单处理", q: "点左下角 📎 上传客户询价单，我来批量查价回填" },
];

interface Turn extends ChatMessage {
  tools?: AgentToolCall[];
  stopped?: boolean;
}
interface Session {
  id: string;
  title: string;
  turns: Turn[];
  updatedAt: number;
}
interface ToolRun {
  name: string;
  done: boolean;
}

const LS_KEY = "agent_sessions_v1";
const loadSessions = (): Session[] => {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || "[]"); } catch { return []; }
};
const newSession = (): Session => ({
  id: Math.random().toString(36).slice(2, 10),
  title: "新对话",
  turns: [],
  updatedAt: Date.now(),
});

function MdLink(props: { href?: string; children?: React.ReactNode }) {
  const href = props.href || "";
  if (href.includes("/api/agent/files/")) {
    return (
      <Button
        size="small"
        type="primary"
        style={{
          margin: "4px 0",
          borderRadius: 7,
          background: "linear-gradient(135deg,#4f46e5,#7c3aed)",
          border: "none",
          boxShadow: "0 2px 8px rgba(79,70,229,0.3)",
        }}
        onClick={() =>
          agentDownload(href).catch((e: Error) =>
            message.error(e.message === "auth-expired"
              ? "登录已过期，请重新登录后再下载（聊天记录不会丢失）"
              : "下载失败，文件可能已清理"))
        }
      >
        ⬇ 下载 Excel
      </Button>
    );
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

function ToolChips({ tools }: { tools?: AgentToolCall[] }) {
  if (!tools?.length) return null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 10 }}>
      {tools.map((t, i) => {
        const arg = (t.args?.query ?? t.args?.pn_std ?? t.args?.dimension ?? "") as string;
        return (
          <span
            key={i}
            className="tool-chip"
            style={{
              display: "inline-flex", alignItems: "center", gap: 4,
              fontSize: 11.5, color: "#6b7280",
              background: "#f3f4f6", border: "1px solid #e8eaed",
              borderRadius: 20, padding: "2px 10px",
            }}
          >
            <span>{TOOL_ICON[t.name] || "⚙"}</span>
            {TOOL_LABEL[t.name] || t.name}
            {arg ? <span style={{ color: "#9ca3af" }}>· {String(arg).slice(0, 20)}</span> : null}
          </span>
        );
      })}
    </div>
  );
}

function UserContent({ text }: { text: string }) {
  const m = /^\[已上传文件「(.+?)」 file_id=\w+[^\]]*\]\n\n?([\s\S]*)$/.exec(text);
  if (!m) return <>{text}</>;
  return (
    <>
      <div style={{ marginBottom: m[2] ? 6 : 0 }}>
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 4,
          background: "rgba(255,255,255,0.18)", border: "1px solid rgba(255,255,255,0.3)",
          borderRadius: 16, padding: "2px 10px", fontSize: 12, color: "#fff",
        }}>
          📎 {m[1]}
        </span>
      </div>
      {m[2]}
    </>
  );
}

/* 流式中工具状态行 */
function ToolRunList({ runs }: { runs: ToolRun[] }) {
  if (!runs.length) return null;
  return (
    <div style={{ marginBottom: 10, display: "flex", flexDirection: "column", gap: 4 }}>
      {runs.map((t, i) => (
        <div
          key={i}
          style={{
            display: "inline-flex", alignItems: "center", gap: 6,
            fontSize: 12.5,
            color: t.done ? "#9ca3af" : "#4f46e5",
            animation: t.done ? "none" : "fade-in 0.2s ease",
          }}
        >
          {t.done
            ? <CheckOutlined style={{ fontSize: 11, color: "#10b981" }} />
            : <LoadingOutlined style={{ fontSize: 11 }} spin />
          }
          <span>
            {t.done
              ? <s style={{ color: "#c4c4c4" }}>{TOOL_LABEL[t.name] || t.name}</s>
              : <b>{TOOL_LABEL[t.name] || t.name}</b>
            }
          </span>
        </div>
      ))}
    </div>
  );
}

/* 渐变动态光标 */
function Cursor() {
  return (
    <span style={{
      display: "inline-block", width: 2.5, height: 16,
      background: "linear-gradient(180deg,#4f46e5,#9333ea)",
      borderRadius: 2, verticalAlign: "-3px", marginLeft: 2,
      animation: "blink-cursor 0.9s ease infinite",
      boxShadow: "0 0 6px rgba(99,102,241,0.6)",
    }} />
  );
}

/* 助手头像 */
function BotAvatar({ size = 32 }: { size?: number }) {
  return (
    <div style={{
      width: size, height: size, borderRadius: Math.round(size * 0.3),
      flexShrink: 0, marginTop: 2,
      background: "linear-gradient(135deg,#4f46e5,#9333ea)",
      display: "flex", alignItems: "center", justifyContent: "center",
      boxShadow: "0 2px 8px rgba(79,70,229,0.35)",
    }}>
      <RobotOutlined style={{ color: "#fff", fontSize: size * 0.48 }} />
    </div>
  );
}

export default function ChatPage() {
  const [sessions, setSessions] = useState<Session[]>(() => {
    const s = loadSessions();
    return s.length ? s : [newSession()];
  });
  const [activeId, setActiveId] = useState<string>(() => sessions[0]?.id);
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
  const stickRef = useRef(true);
  const scrollBoxRef = useRef<HTMLDivElement>(null);

  const active = sessions.find((s) => s.id === activeId) || sessions[0];

  const persist = (updater: (prev: Session[]) => Session[]) => {
    setSessions((prev) => {
      const next = updater(prev)
        .sort((a, b) => b.updatedAt - a.updatedAt)
        .slice(0, 30)
        .map((s) => ({ ...s, turns: s.turns.slice(-60) }));
      localStorage.setItem(LS_KEY, JSON.stringify(next));
      return next;
    });
  };

  const patchSession = (sid: string, fn: (s: Session) => Session) =>
    persist((prev) => prev.map((s) => (s.id === sid ? fn(s) : s)));

  const scrollToBottom = () => {
    if (stickRef.current) bottomRef.current?.scrollIntoView({ block: "end" });
  };
  useEffect(scrollToBottom, [streamText, toolRuns, sessions]);

  const onScroll = () => {
    const el = scrollBoxRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const doUpload = async (file: File) => {
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
    const sid = active.id;
    const userTurn: Turn = { role: "user", content: q };
    const history: ChatMessage[] = [...active.turns, userTurn]
      .map(({ role, content }) => ({ role, content }))
      .slice(-20);
    patchSession(sid, (s) => ({
      ...s,
      title: s.turns.length ? s.title : q.replace(/^\[已上传文件[^\]]*\]\n*/, "").slice(0, 20) || "文件处理",
      turns: [...s.turns, userTurn],
      updatedAt: Date.now(),
    }));
    setInput("");
    setBusy(true);
    stickRef.current = true;
    streamBufRef.current = "";
    const trace: AgentToolCall[] = [];
    let settled = false;
    abortRef.current = new AbortController();
    flushTimerRef.current = window.setInterval(() => setStreamText(streamBufRef.current), 80);

    const finishTurn = (content: string, stopped = false) => {
      if (settled) return;
      settled = true;
      if (flushTimerRef.current) { window.clearInterval(flushTimerRef.current); flushTimerRef.current = null; }
      streamBufRef.current = "";
      setStreamText("");
      setToolRuns([]);
      setBusy(false);
      if (content || trace.length) {
        patchSession(sid, (s) => ({
          ...s,
          turns: [...s.turns, { role: "assistant", content: content || "(无内容)", tools: [...trace], stopped }],
          updatedAt: Date.now(),
        }));
      }
    };

    try {
      await agentChatStream(history, (ev) => {
        if (ev.type === "delta") {
          streamBufRef.current += ev.text;
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
      finishTurn(streamBufRef.current);
    } catch (e: any) {
      if (e?.name === "AbortError") {
        finishTurn(streamBufRef.current + "\n\n*(已停止生成)*", true);
      } else {
        message.error("连接失败，请稍后重试");
        finishTurn(streamBufRef.current, true);
      }
    }
  };

  const stop = () => abortRef.current?.abort();
  const copyText = (t: string) => navigator.clipboard.writeText(t).then(() => message.success("已复制"));
  const runningTool = toolRuns.find((t) => !t.done);

  return (
    <div style={{ display: "flex", height: "calc(100vh - 60px)", background: "#fff" }}>
      {/* ─── 会话栏 ─── */}
      <aside style={{
        width: 228, borderRight: "1px solid #f0f0f0",
        background: "#fafbfc", display: "flex", flexDirection: "column", flexShrink: 0,
      }}>
        <div style={{ padding: "12px 12px 8px" }}>
          <Button
            block
            icon={<PlusOutlined />}
            disabled={busy}
            onClick={() => {
              const s = newSession();
              persist((prev) => [s, ...prev]);
              setActiveId(s.id);
            }}
            style={{
              borderRadius: 9, height: 36, fontWeight: 500,
              background: "linear-gradient(135deg,#4f46e5,#6366f1)",
              color: "#fff", border: "none",
              boxShadow: "0 2px 8px rgba(79,70,229,0.25)",
            }}
          >
            新对话
          </Button>
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: "0 8px 12px" }}>
          {sessions.map((s) => {
            const isActive = s.id === active.id;
            return (
              <div
                key={s.id}
                className="sess-item"
                onClick={() => !busy && setActiveId(s.id)}
                style={{
                  padding: "8px 10px",
                  borderRadius: 9,
                  cursor: "pointer",
                  marginBottom: 2,
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  background: isActive ? "#eef2ff" : "transparent",
                  border: isActive ? "1px solid #c7d2fe" : "1px solid transparent",
                  color: isActive ? "#4f46e5" : "#374151",
                  transition: "all 0.15s ease",
                }}
              >
                <span style={{
                  flex: 1, overflow: "hidden", textOverflow: "ellipsis",
                  whiteSpace: "nowrap", fontSize: 13,
                }}>
                  {s.title}
                </span>
                <Popconfirm
                  title="删除该对话？"
                  onConfirm={(e) => {
                    e?.stopPropagation();
                    const fallback = newSession();
                    persist((prev) => {
                      const rest = prev.filter((x) => x.id !== s.id);
                      return rest.length ? rest : [fallback];
                    });
                    if (s.id === active.id) {
                      const rest = sessions.filter((x) => x.id !== s.id);
                      setActiveId(rest.length ? rest[0].id : fallback.id);
                    }
                  }}
                >
                  <DeleteOutlined
                    className="sess-del"
                    onClick={(e) => e.stopPropagation()}
                    style={{ opacity: 0, fontSize: 11, color: "#9ca3af", transition: "opacity 0.15s" }}
                  />
                </Popconfirm>
              </div>
            );
          })}
        </div>
      </aside>

      {/* ─── 主区 ─── */}
      <main style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, background: "#fff" }}>
        {/* 消息流 */}
        <div
          ref={scrollBoxRef}
          onScroll={onScroll}
          style={{ flex: 1, overflowY: "auto", background: "#fff" }}
        >
          <div style={{ maxWidth: 840, margin: "0 auto", padding: "28px 28px 12px" }}>

            {/* 欢迎页 */}
            {active.turns.length === 0 && !busy && (
              <div style={{ textAlign: "center", paddingTop: 52, animation: "fade-up 0.4s ease" }}>
                <div style={{
                  width: 64, height: 64, borderRadius: 20, margin: "0 auto 20px",
                  background: "linear-gradient(135deg,#4f46e5,#9333ea)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  boxShadow: "0 8px 32px rgba(79,70,229,0.35), 0 0 0 8px rgba(79,70,229,0.08)",
                }}>
                  <RobotOutlined style={{ fontSize: 30, color: "#fff" }} />
                </div>
                <h2 style={{ marginBottom: 6, fontSize: 20, fontWeight: 700, color: "#111827" }}>
                  AI 定价助手
                </h2>
                <p style={{ color: "#9ca3af", marginBottom: 32, fontSize: 13.5 }}>
                  基于公司真实采购 / 销售 / 库存数据回答 · 型号写不准会自动近似匹配并和你确认
                </p>
                <div style={{
                  display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12,
                  maxWidth: 600, margin: "0 auto", textAlign: "left",
                }}>
                  {EXAMPLES.map((ex) => (
                    <div
                      key={ex.title}
                      onClick={() => !ex.q.startsWith("点左下角") && send(ex.q)}
                      style={{
                        border: "1px solid #e8eaed", borderRadius: 12, padding: "14px 16px",
                        cursor: ex.q.startsWith("点左下角") ? "default" : "pointer",
                        background: "#fafbfc", transition: "all 0.18s ease",
                      }}
                      onMouseEnter={(e) => {
                        e.currentTarget.style.borderColor = "#a5b4fc";
                        e.currentTarget.style.background = "#f5f3ff";
                        e.currentTarget.style.boxShadow = "0 4px 16px rgba(79,70,229,0.1)";
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.borderColor = "#e8eaed";
                        e.currentTarget.style.background = "#fafbfc";
                        e.currentTarget.style.boxShadow = "none";
                      }}
                    >
                      <div style={{ fontWeight: 600, marginBottom: 4, fontSize: 14 }}>{ex.icon} {ex.title}</div>
                      <div style={{ color: "#6b7280", fontSize: 12.5, lineHeight: 1.5 }}>{ex.q}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 消息列表 */}
            {active.turns.map((t, i) =>
              t.role === "user" ? (
                <div key={i} className="msg-user" style={{ display: "flex", justifyContent: "flex-end", margin: "18px 0" }}>
                  <div style={{
                    maxWidth: "76%", padding: "11px 16px",
                    borderRadius: "18px 18px 5px 18px",
                    background: "linear-gradient(135deg,#4f46e5,#6366f1)",
                    color: "#fff", fontSize: 14, lineHeight: 1.7,
                    whiteSpace: "pre-wrap", wordBreak: "break-word",
                    boxShadow: "0 4px 14px rgba(79,70,229,0.28)",
                  }}>
                    <UserContent text={t.content} />
                  </div>
                </div>
              ) : (
                <div key={i} className="turn-assistant msg-assistant" style={{ display: "flex", gap: 12, margin: "18px 0" }}>
                  <BotAvatar />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="chat-md" style={{ fontSize: 14, lineHeight: 1.75 }}>
                      <Md text={t.content} />
                    </div>
                    <ToolChips tools={t.tools} />
                    {t.stopped && (
                      <Tag color="orange" style={{ marginTop: 6, borderRadius: 6 }}>已中断</Tag>
                    )}
                    <Tooltip title="复制">
                      <Button
                        className="copy-btn"
                        size="small"
                        type="text"
                        icon={<CopyOutlined />}
                        style={{ opacity: 0, color: "#c4c4c4", marginTop: 6, transition: "opacity 0.15s" }}
                        onClick={() => copyText(t.content)}
                      />
                    </Tooltip>
                  </div>
                </div>
              )
            )}

            {/* 流式中 */}
            {busy && (
              <div className="msg-assistant" style={{ display: "flex", gap: 12, margin: "18px 0" }}>
                <BotAvatar />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <ToolRunList runs={toolRuns} />
                  <div className="chat-md" style={{ fontSize: 14, lineHeight: 1.75 }}>
                    {streamText
                      ? <><Md text={streamText} /><Cursor /></>
                      : !runningTool && (
                        <span style={{ color: "#9ca3af", fontStyle: "italic" }}>
                          思考中<Cursor />
                        </span>
                      )
                    }
                    {runningTool && !streamText && <Cursor />}
                  </div>
                </div>
              </div>
            )}

            <div ref={bottomRef} style={{ height: 8 }} />
          </div>
        </div>

        {/* ─── 输入区 ─── */}
        <div style={{
          borderTop: "1px solid #f0f0f0",
          background: "#fff",
          padding: "12px 20px 10px",
        }}>
          <div style={{ maxWidth: 840, margin: "0 auto" }}>
            {pendingFile && (
              <Tag
                closable
                color="purple"
                onClose={() => setPendingFile(null)}
                style={{ marginBottom: 8, padding: "3px 12px", borderRadius: 16, fontSize: 12 }}
              >
                📎 {pendingFile.filename}{pendingFile.sheets
                  ? `（${pendingFile.sheets.map((s) => `${s.name} ${s.n_rows}行`).join("、")}）`
                  : ""}
              </Tag>
            )}

            <div
              className="chat-input-wrap"
              style={{
                display: "flex",
                alignItems: "flex-end",
                gap: 8,
                border: "1.5px solid #e5e7eb",
                borderRadius: 16,
                padding: "8px 10px",
                background: "#fff",
                boxShadow: "0 2px 12px rgba(0,0,0,0.05)",
                transition: "border-color 0.2s, box-shadow 0.2s",
              }}
            >
              <Upload
                accept=".xlsx,.docx,.pdf,.txt,.csv,.jpg,.jpeg,.png,.webp"
                showUploadList={false}
                disabled={busy || uploading}
                beforeUpload={(f) => doUpload(f as unknown as File)}
              >
                <Tooltip title="上传文件：询价单/整机配置（Excel/Word/PDF/图片）">
                  <Button
                    type="text"
                    icon={<PaperClipOutlined />}
                    loading={uploading}
                    style={{ color: uploading ? "#4f46e5" : "#9ca3af", borderRadius: 8 }}
                  />
                </Tooltip>
              </Upload>

              <Input.TextArea
                autoSize={{ minRows: 1, maxRows: 6 }}
                variant="borderless"
                placeholder={
                  pendingFile
                    ? "对这个文件想做什么？如：查最近采购价填进去发我"
                    : "问点什么… （Enter 发送，Shift+Enter 换行）"
                }
                value={input}
                disabled={busy}
                onChange={(e) => setInput(e.target.value)}
                onPressEnter={(e) => {
                  if (!e.shiftKey) { e.preventDefault(); send(input); }
                }}
                style={{ flex: 1, fontSize: 14, padding: "4px 4px" }}
              />

              {busy ? (
                <Button
                  danger
                  type="primary"
                  shape="circle"
                  icon={<StopOutlined />}
                  onClick={stop}
                  style={{ flexShrink: 0 }}
                />
              ) : (
                <Button
                  type="primary"
                  shape="circle"
                  icon={<SendOutlined />}
                  disabled={!input.trim() && !pendingFile}
                  onClick={() => send(input)}
                  style={{
                    flexShrink: 0,
                    background: "linear-gradient(135deg,#4f46e5,#7c3aed)",
                    border: "none",
                    boxShadow: "0 2px 8px rgba(79,70,229,0.35)",
                  }}
                />
              )}
            </div>

            <div style={{ textAlign: "center", color: "#d1d5db", fontSize: 11, padding: "6px 0 2px" }}>
              AI 基于库内数据回答，重要报价请人工复核
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
