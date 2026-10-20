import { Skeleton } from "antd";
import "./PageSkeleton.css";

/** 与页面标题同顶对齐，加载期间保留内容骨架与可访问提示。 */
export default function PageSkeleton({ label = "正在加载页面…" }: { label?: string }) {
  return (
    <div className="page-skeleton" role="status" aria-label={label} aria-busy="true">
      <span className="page-skeleton__label">{label}</span>
      <Skeleton active paragraph={{ rows: 1 }} />
      <div className="page-skeleton__body"><Skeleton active title={false} paragraph={{ rows: 6 }} /></div>
    </div>
  );
}
