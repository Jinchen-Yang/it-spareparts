import { Navigate, useLocation, useParams } from "react-router-dom";

/** 旧链接保留单据与待办上下文，交给工作台加载当前单据。 */
export default function TaskRedirect() {
  const { id } = useParams();
  const { state } = useLocation();
  return <Navigate replace state={state} to={`/presales/workbench${id ? `?id=${encodeURIComponent(id)}` : ""}`} />;
}
