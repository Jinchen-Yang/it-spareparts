import { Navigate, useLocation } from "react-router-dom";

import {
  canUseMaintenanceReminderFilter,
} from "../../components/maintenance/maintenancePermissions";

export default function MaintenanceRemindersCompatRedirect() {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  if (!params.has("reminder") && canUseMaintenanceReminderFilter("all")) {
    params.set("reminder", "all");
  }
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
