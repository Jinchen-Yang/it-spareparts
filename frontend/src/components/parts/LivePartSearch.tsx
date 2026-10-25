import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Alert, Button, Empty, Input, Space, Spin, Tag } from "antd";
import type { InputProps } from "antd";
import { suggestParts } from "../../api/search";
import type { PartSuggestion } from "../../api/search";
import "./LivePartSearch.css";

export interface LivePartSearchProps {
  mode?: "single" | "multiple";
  value?: PartSuggestion[];
  onChange?: (items: PartSuggestion[]) => void;
  inputValue?: string;
  onInputChange?: (text: string) => void;
  inputProps?: Omit<InputProps, "value" | "onChange" | "onKeyDown" | "onCompositionStart" | "onCompositionEnd">;
  limit?: number;
  onAddRaw?: (text: string) => void;
  renderThumb?: (item: PartSuggestion) => ReactNode;
  renderBadges?: (item: PartSuggestion) => ReactNode;
}

/** 只在 PN 内匹配字面片段，并把归一化/紧凑匹配映回原文字符区间。 */
function highlightedPn(pn: string, terms: string[]) {
  const words = terms.flatMap((term) => term.normalize("NFKC").toUpperCase().split(/[\s*?]+/)).filter(Boolean);
  if (!words.length) return pn;
  const offsets: { start: number; end: number }[] = [];
  let normalized = "";
  // 连同组合附加符归一化；兼容字符展开后，每个字符仍映到同一原文片段。
  for (const match of pn.matchAll(/.\p{M}*/gsu)) {
    const part = match[0].normalize("NFKC").toUpperCase();
    const start = match.index!;
    normalized += part;
    for (let i = 0; i < part.length; i++) offsets.push({ start, end: start + match[0].length });
  }
  let compact = "";
  const compactOffsets: typeof offsets = [];
  for (let i = 0; i < normalized.length; i++) {
    if (/[A-Z0-9]/.test(normalized[i])) {
      compact += normalized[i];
      compactOffsets.push(offsets[i]);
    }
  }
  const marked = new Array<boolean>(pn.length).fill(false);
  const markMatches = (source: string, word: string, map: typeof offsets) => {
    if (!word) return;
    for (let index = source.indexOf(word); index !== -1; index = source.indexOf(word, index + 1)) {
      marked.fill(true, map[index].start, map[index + word.length - 1].end);
    }
  };
  for (const word of words) {
    markMatches(normalized, word, offsets);
    // 与后端相同：字面 %/_/反斜杠不能被 compact 擦掉后扩大命中。
    if (!/[%_\\]/.test(word)) markMatches(compact, word.replace(/[^A-Z0-9]/g, ""), compactOffsets);
  }
  const result: ReactNode[] = [];
  for (let start = 0; start < pn.length;) {
    let end = start + 1;
    while (end < pn.length && marked[end] === marked[start]) end += 1;
    const part = pn.slice(start, end);
    result.push(marked[start] ? <mark key={start}>{part}</mark> : part);
    start = end;
  }
  return result;
}

/** 独立联想组件，不接触现有 PartPicker 的身份与匹配契约。 */
export default function LivePartSearch({
  mode = "single", value, onChange, inputValue, onInputChange, inputProps,
  limit = 10, onAddRaw, renderThumb, renderBadges,
}: LivePartSearchProps) {
  const [text, setText] = useState(inputValue ?? "");
  const [picked, setPicked] = useState<PartSuggestion[]>([]);
  const selected = value ?? picked;
  const query = inputValue ?? text;
  const [items, setItems] = useState<PartSuggestion[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const listId = useId();
  const currentText = useRef(query);
  const composing = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);

  const cancel = () => {
    generation.current += 1;
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    controller.current?.abort();
    controller.current = null;
  };

  useEffect(() => () => { cancel(); }, []);
  // URL 前进/后退等外部改值不应自动弹出联想，也不能让旧请求覆盖新输入。
  useEffect(() => {
    if (query !== currentText.current) {
      currentText.current = query;
      cancel();
      setItems([]);
      setLoading(false);
      setOpen(false);
    }
  }, [query]);

  const updateText = (next: string) => {
    currentText.current = next;
    setText(next);
    onInputChange?.(next);
  };

  const request = (next: string, immediate = false) => {
    cancel();
    setItems([]);
    setActive(-1);
    setError(false);
    setLoading(false);
    if (!next.trim() || composing.current) { setOpen(false); return; }
    setOpen(true);
    const seq = generation.current;
    const run = async () => {
      timer.current = null;
      const abort = new AbortController();
      controller.current = abort;
      setLoading(true);
      try {
        const data = await suggestParts(next, limit, abort.signal);
        if (seq !== generation.current || abort.signal.aborted) return;
        setItems(data);
      } catch {
        if (seq === generation.current && !abort.signal.aborted) setError(true);
      } finally {
        if (seq === generation.current) setLoading(false);
      }
    };
    if (immediate) void run();
    else timer.current = setTimeout(run, 150);
  };

  const changeSelection = (next: PartSuggestion[]) => {
    setPicked(next);
    onChange?.(next);
  };

  const choose = (item: PartSuggestion) => {
    cancel();
    setLoading(false);
    setOpen(false);
    setActive(-1);
    updateText(mode === "multiple" ? "" : item.pn_std);
    changeSelection(mode === "multiple"
      ? selected.some((existing) => existing.part_id === item.part_id) ? selected : [...selected, item]
      : [item]);
  };

  const shown = open && !!query.trim();
  return (
    <div className="live-part-search" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }}>
      {mode === "multiple" && selected.length > 0 && (
        <Space wrap className="live-part-search-picked">
          {selected.map((item) => <Tag key={item.part_id} closable onClose={() => {
            changeSelection(selected.filter((existing) => existing.part_id !== item.part_id));
          }}>{item.pn_std}</Tag>)}
        </Space>
      )}
      <Input
        placeholder="输入型号 / 品牌 / 描述 / 规格" allowClear {...inputProps}
        value={query} aria-autocomplete="list" aria-controls={shown ? listId : undefined}
        aria-expanded={shown} aria-activedescendant={shown && active >= 0 ? `${listId}-${active}` : undefined}
        onFocus={(event) => {
          inputProps?.onFocus?.(event);
          if (query.trim() && (items.length || error || loading)) setOpen(true);
        }}
        onChange={(event) => {
          const next = event.target.value;
          // 某些浏览器会在 compositionend 后再派发一次相同文本的 change。
          if (next === currentText.current) return;
          updateText(next);
          request(next);
        }}
        onCompositionStart={() => {
          composing.current = true;
          cancel();
          setOpen(false);
          setLoading(false);
        }}
        onCompositionEnd={(event) => {
          composing.current = false;
          const next = event.currentTarget.value;
          updateText(next);
          request(next, true);
        }}
        onKeyDown={(event) => {
          if (composing.current || event.nativeEvent.isComposing || event.keyCode === 229) {
            if (event.key === "Enter") event.preventDefault();
            return;
          }
          if (event.key === "Escape") {
            cancel();
            setLoading(false);
            setOpen(false);
            setActive(-1);
          } else if ((event.key === "ArrowDown" || event.key === "ArrowUp") && items.length) {
            event.preventDefault();
            setOpen(true);
            setActive((index) => event.key === "ArrowDown"
              ? (index + 1) % items.length : (index <= 0 ? items.length : index) - 1);
          } else if (event.key === "Enter" && shown && active >= 0 && items[active]) {
            event.preventDefault();
            choose(items[active]);
          }
        }}
      />
      {shown && <div className="live-part-search-dropdown">
        <div id={listId} role="listbox" aria-label="型号联想" aria-multiselectable={mode === "multiple" || undefined}
          className="live-part-search-options">
          {loading ? <div role="status" className="live-part-search-state"><Spin size="small" /> 搜索中…</div>
            : error ? <Alert type="error" showIcon message="联想加载失败，请重新输入后重试" />
            : !items.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配型号" />
            : items.map((item, index) => <div
              id={`${listId}-${index}`} key={item.part_id} role="option"
              aria-selected={selected.some((existing) => existing.part_id === item.part_id)}
              className={`live-part-search-option${index === active ? " is-active" : ""}`}
              onMouseDown={(event) => event.preventDefault()} onMouseEnter={() => setActive(index)}
              onClick={() => choose(item)}
            >
              {renderThumb?.(item)}
              <div className="live-part-search-details">
                <div className="live-part-search-line">
                  <strong className="live-part-search-pn">{highlightedPn(item.pn_std, item.matched_terms ?? [])}</strong>
                  {item.match_reason && <Tag color="blue">{item.match_reason}</Tag>}
                  {renderBadges?.(item)}
                  {(item.brand || item.manufacturer) && <span className="live-part-search-secondary">
                    {[item.brand, item.manufacturer].filter(Boolean).join(" / ")}
                  </span>}
                </div>
                {(item.summary || item.description) && <div className="live-part-search-summary"
                  title={item.summary || item.description || undefined}>{item.summary || item.description}</div>}
                {(Boolean(item.key_specs?.length) || item.stock_qty != null) && <div className="live-part-search-specs">
                  {item.key_specs?.map((spec, i) => <Tag key={i}>{spec.label} {spec.value}</Tag>)}
                  {item.stock_qty != null && <Tag>库存 {item.stock_qty}</Tag>}
                </div>}
              </div>
            </div>)}
        </div>
        {onAddRaw && <div className="live-part-search-add">
          <Button type="text" block onMouseDown={(event) => event.preventDefault()} onClick={() => {
            cancel(); setOpen(false); setLoading(false); onAddRaw(query);
          }}>没找到？按原文添加「{query}」（待建档）</Button>
        </div>}
      </div>}
    </div>
  );
}
