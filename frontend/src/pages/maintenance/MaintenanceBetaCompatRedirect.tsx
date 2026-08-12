import { Navigate, useLocation } from "react-router-dom";

interface CompatLocation {
  pathname: string;
  search: string;
  hash: string;
}

export function maintenanceBetaCompatTarget({
  pathname,
  search,
  hash,
}: CompatLocation): string {
  try {
    const features = JSON.parse(localStorage.getItem("beta_features") || "{}");
    if (features?.maintenance === true) {
      const betaPath = pathname.replace(/^\/maintenance(?=\/)/, "/maintenance/beta");
      return `${betaPath}${search}${hash}`;
    }
  } catch {
    // A damaged local snapshot fails closed to the production-stable page.
  }
  return "/maintenance";
}

export default function MaintenanceBetaCompatRedirect() {
  const location = useLocation();
  return <Navigate to={maintenanceBetaCompatTarget(location)} replace />;
}
