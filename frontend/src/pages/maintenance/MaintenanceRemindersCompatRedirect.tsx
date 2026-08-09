import { Navigate, useLocation } from "react-router-dom";

export default function MaintenanceRemindersCompatRedirect() {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  if (!params.has("reminder")) params.set("reminder", "all");
  return (
    <Navigate
      replace
      to={{
        pathname: "/maintenance/projects",
        search: `?${params.toString()}`,
        hash: location.hash,
      }}
    />
  );
}
