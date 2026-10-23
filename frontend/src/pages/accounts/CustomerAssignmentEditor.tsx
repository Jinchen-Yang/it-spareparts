import { useEffect, useState } from "react";
import { Alert, Button } from "antd";
import { searchAuthzCustomers, type AuthzCustomer } from "../../api/authz";
import { explainApiError } from "../../api/accounts";
import CheckboxMultiSelect from "../../components/CheckboxMultiSelect";
const customerLabel = (c: AuthzCustomer) => `${c.name}（ID ${c.id}）`;
const customerOption = (c: AuthzCustomer) => ({
  value: c.id,
  label: customerLabel(c),
  description:
    c.raw_name && c.raw_name !== c.name ? `原始名：${c.raw_name}` : undefined,
});
export default function CustomerAssignmentEditor({
  owner,
  selected,
  customers,
  disabled,
  onConfirm,
}: {
  owner: string;
  selected: number[];
  customers: AuthzCustomer[];
  disabled: boolean;
  onConfirm: (ids: number[], customers: AuthzCustomer[]) => void;
}) {
  const [known, setKnown] = useState(
    () => new Map(customers.map((c) => [c.id, c])),
  );
  const [query, setQuery] = useState<string | null>(null);
  const [items, setItems] = useState<AuthzCustomer[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    setKnown(
      (previous) =>
        new Map([...previous, ...customers.map((c) => [c.id, c] as const)]),
    );
  }, [customers]);
  useEffect(() => {
    if (query === null) return;
    let active = true;
    const abort = new AbortController();
    setLoading(true);
    setError("");
    setItems([]);
    const timer = window.setTimeout(() => {
      if (localStorage.getItem("token") !== owner) return;
      void searchAuthzCustomers(query.trim(), owner, abort.signal)
        .then(({ data }) => {
          if (!active || localStorage.getItem("token") !== owner) return;
          setItems(data.items);
          setKnown(
            (previous) =>
              new Map([
                ...previous,
                ...data.items.map((c) => [c.id, c] as const),
              ]),
          );
        })
        .catch((e) => {
          if (active && localStorage.getItem("token") === owner)
            setError(explainApiError(e, "客户搜索失败"));
        })
        .finally(() => {
          if (active && localStorage.getItem("token") === owner)
            setLoading(false);
        });
    }, 250);
    return () => {
      active = false;
      abort.abort();
      window.clearTimeout(timer);
    };
  }, [query, owner, retry]);
  return (
    <>
      <CheckboxMultiSelect
        label="负责客户"
        value={selected}
        selectionUnit="个"
        disabled={disabled}
        options={items.map(customerOption)}
        knownOptions={[...known.values()].map(customerOption)}
        onSearch={setQuery}
        loading={loading}
        onChange={(ids) => {
          if (localStorage.getItem("token") !== owner) return;
          onConfirm(
            ids,
            ids.map((id) => known.get(id) || { id, name: `客户 #${id}` }),
          );
        }}
      />
      {error && (
        <Alert
          type="error"
          message={error}
          action={
            <Button onClick={() => setRetry((n) => n + 1)}>重试搜索</Button>
          }
        />
      )}
      <p>
        每次最多显示 50
        个候选，请输入名称缩小范围。搜索不会清除已选客户；确定后仍需保存授权。
      </p>
    </>
  );
}
