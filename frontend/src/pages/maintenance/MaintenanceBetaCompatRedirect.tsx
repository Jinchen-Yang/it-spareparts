import { Navigate, useLocation } from "react-router-dom";

export function maintenanceBetaCompatTarget(pathname: string): string {
  try {
    const features = JSON.parse(localStorage.getItem("beta_features") || "{}");
    if (features?.maintenance === true) {
      return pathname.replace(/^\/maintenance(?=\/)/, "/maintenance/beta");
    }
  } catch {
    // A damaged local snapshot fails closed to the production-stable page.
  }
  return "/maintenance";
}

export default function MaintenanceBetaCompatRedirect() {
  const location = useLocation();
  return <Navigate to={maintenanceBetaCompatTarget(location.pathname)} replace />;
}
