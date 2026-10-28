import { Navigate, useLocation, useParams } from "react-router-dom";

/** 旧链接保留单据与待办上下文，进入独立报价页。 */
export default function TaskRedirect() {
  const { id } = useParams();
  const { state } = useLocation();
  return <Navigate replace state={state} to={`/presales/workbench${id ? `/${encodeURIComponent(id)}` : ""}`} />;
}
