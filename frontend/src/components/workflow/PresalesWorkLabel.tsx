import { useEffect, useState } from "react";
import { Badge, Space } from "antd";
import { POOL_CHANGED_EVENT, presalesPool } from "../../api/presalesPool";

const POLL_MS = 60_000;

/** 菜单角标：共享池待报价行数（D-0052）。 */
export default function PresalesWorkLabel({ label = "询价报价" }: { label?: string }) {
  const [pending, setPending] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const read = () => {
      if (document.hidden) return;
      presalesPool.counts(controller.signal).then((counts) => setPending(counts.pending)).catch(() => undefined);
    };
    read();
    const timer = window.setInterval(read, POLL_MS);
    window.addEventListener(POOL_CHANGED_EVENT, read);
    return () => { controller.abort(); clearInterval(timer); window.removeEventListener(POOL_CHANGED_EVENT, read); };
  }, []);
  return <Space>{label}<Badge count={pending} /></Space>;
}
