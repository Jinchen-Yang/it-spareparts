import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Button, Checkbox, Input, Popover, Spin, type InputRef } from "antd";
import { DownOutlined, SearchOutlined } from "@ant-design/icons";

import "./CheckboxMultiSelect.css";

export interface CheckboxMultiSelectOption<T extends string | number> {
  value: T;
  label: string;
  description?: ReactNode;
  disabled?: boolean;
}

export interface CheckboxMultiSelectProps<T extends string | number> {
  label: string;
  value: T[];
  options: CheckboxMultiSelectOption<T>[];
  onChange: (next: T[]) => void;
  disabled?: boolean;
  loading?: boolean;
  selectionUnit?: string;
  /** Remote search keeps selected labels independent of the current candidate page. */
  knownOptions?: CheckboxMultiSelectOption<T>[];
  onSearch?: (query: string) => void;
}

const optionKey = (value: string | number) => JSON.stringify([typeof value, value]);

/** Draft selection is local to the panel; only Confirm commits it. */
export default function CheckboxMultiSelect<T extends string | number>({
  label,
  value,
  options,
  onChange,
  disabled = false,
  loading = false,
  selectionUnit = "项",
  knownOptions = [],
  onSearch,
}: CheckboxMultiSelectProps<T>) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<T[]>([]);
  const [query, setQuery] = useState("");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<InputRef>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const dialogId = useId();
  // Compare contents, not array identity: parent renders and option refreshes must
  // not erase pending choices. Types are retained (number 1 !== string "1").
  const valueKey = JSON.stringify(value);
  const openedValueKey = useRef(valueKey);
  const optionMap = new Map([...knownOptions, ...options].map((option) => [option.value, option]));
  const getLabel = (item: T) => optionMap.get(item)?.label ?? String(item);

  useEffect(() => {
    setOpen(false);
  }, [valueKey, disabled]);

  useEffect(() => {
    if (open) searchRef.current?.focus();
  }, [open]);

  function close(restoreFocus = false) {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }

  function changeOpen(next: boolean) {
    if (next && !disabled) {
      openedValueKey.current = valueKey;
      setDraft([...new Set(value)]);
      setQuery("");
      onSearch?.("");
      setOpen(true);
    } else {
      close();
    }
  }

  function toggle(item: T) {
    setDraft((previous) => previous.includes(item)
      ? previous.filter((selected) => selected !== item)
      : [...previous, item]);
  }

  function confirm() {
    // Reject stale drafts even if an external value/disabled update arrived
    // before the closing effect had a chance to run.
    if (!open || disabled || openedValueKey.current !== valueKey) return;
    close(true);
    onChange([...draft]);
  }

  function panelKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === "Tab") {
      const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
        'input:not(:disabled), button:not(:disabled), [tabindex="0"]',
      );
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }

  const missingOptions = [...new Set([...value, ...draft])].filter((item) => !optionMap.has(item))
    .map((item) => ({ value: item, label: String(item) }));
  const candidates: CheckboxMultiSelectOption<T>[] = [...options, ...missingOptions];
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filtered = onSearch ? options : candidates.filter((option) =>
    option.label.toLocaleLowerCase().includes(normalizedQuery));
  const summary = value.map(getLabel).join("、");

  const content = (
    <div
      id={dialogId}
      ref={panelRef}
      className="checkbox-multiselect-panel"
      hidden={!open}
      role="dialog"
      aria-label={`${label}筛选`}
      onKeyDown={panelKeyDown}
    >
      <div className="checkbox-multiselect-heading">{label}</div>
      <Input
        ref={searchRef}
        aria-label={`搜索${label}`}
        placeholder={`搜索${label}`}
        prefix={<SearchOutlined />}
        value={query}
        onChange={(event) => { setQuery(event.target.value); onSearch?.(event.target.value); }}
      />
      <div className="checkbox-multiselect-selected" aria-label="已选列表">
        <div className="checkbox-multiselect-count" aria-live="polite">
          已选 {draft.length} {selectionUnit}
        </div>
        {draft.length > 0 && (
          <div className="checkbox-multiselect-selected-items">
            {draft.map((item) => (
              <button
                key={optionKey(item)}
                type="button"
                className="checkbox-multiselect-chip"
                aria-label={`移除${getLabel(item)}`}
                disabled={optionMap.get(item)?.disabled}
                onClick={() => toggle(item)}
              >
                <span>{getLabel(item)}</span><span aria-hidden="true">×</span>
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="checkbox-multiselect-options" aria-label="候选列表" aria-busy={loading}>
        {loading && <div className="checkbox-multiselect-empty"><Spin size="small" /> 加载中</div>}
        {filtered.map((option) => (
          <Checkbox
            key={optionKey(option.value)}
            className="checkbox-multiselect-option"
            aria-label={option.label}
            checked={draft.includes(option.value)}
            disabled={option.disabled}
            onChange={() => toggle(option.value)}
          >
            <span className="checkbox-multiselect-option-label">{option.label}</span>
            {option.description != null && (
              <span className="checkbox-multiselect-description">{option.description}</span>
            )}
          </Checkbox>
        ))}
        {!loading && filtered.length === 0 && (
          <div className="checkbox-multiselect-empty">{query ? "无匹配结果" : "暂无选项"}</div>
        )}
      </div>
      <div className="checkbox-multiselect-actions">
        <Button aria-label="重置" onClick={() => { setDraft([]); setQuery(""); onSearch?.(""); searchRef.current?.focus(); }}>重置</Button>
        <Button aria-label="确定" type="primary" onClick={confirm}>确定</Button>
      </div>
    </div>
  );

  return (
    <Popover
      content={content}
      trigger="click"
      placement="bottom"
      open={open && !disabled}
      onOpenChange={changeOpen}
      styles={{ body: { padding: 0 } }}
    >
      <Button
        ref={triggerRef}
        className="checkbox-multiselect-trigger"
        aria-label={label}
        aria-haspopup="dialog"
        aria-expanded={open && !disabled}
        aria-controls={open ? dialogId : undefined}
        title={summary || `选择${label}`}
        disabled={disabled}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") { event.preventDefault(); changeOpen(true); }
          if (event.key === "Escape") close(true);
        }}
      >
        <span className="checkbox-multiselect-trigger-label">
          {value.length === 0 ? `选择${label}`
            : value.length === 1 ? getLabel(value[0]) : `已选 ${value.length} ${selectionUnit}`}
        </span>
        <DownOutlined />
      </Button>
    </Popover>
  );
}
