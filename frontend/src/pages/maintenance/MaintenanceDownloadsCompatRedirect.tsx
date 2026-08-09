import { Navigate, useLocation } from "react-router-dom";

export default function MaintenanceDownloadsCompatRedirect() {
  const location = useLocation();
  return (
    <Navigate
      replace
      to={{
        pathname: "/maintenance/updates",
        search: location.search,
        hash: location.hash,
      }}
    />
  );
}
