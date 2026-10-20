import type { ReactNode } from "react";
import "./PageHeader.css";

/** 统一页面标题、说明与可换行的动作区。 */
export default function PageHeader({ title, subtitle, extra }: {
  title: ReactNode;
  subtitle?: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div className="page-header__context">
        <h1 className="page-header__title">{title}</h1>
        {subtitle && <div className="page-header__subtitle">{subtitle}</div>}
      </div>
      {extra && <div className="page-header__actions">{extra}</div>}
    </div>
  );
}
