import { createContext, useContext, useMemo } from "react";
import { resourceAllowed, useAuthz } from "../../authz/AuthzProvider";
import { catalogClient } from "../../api/pnCatalog";
import actions from "../../authz/pnActions.json";

export interface PartAccess { part_id: number; allowed_actions: string[]; readable_fields: string[]; writable_fields: string[] }
export const PartAccessContext = createContext<PartAccess | null>(null);

export function useCatalogAccess() {
  const { snapshot, status } = useAuthz();
  const token = localStorage.getItem("token") || "";
  const client = useMemo(() => catalogClient(token), [token]);
  const scoped = useContext(PartAccessContext);
  const can = (resource: string, action: string) => status === "ready" && resourceAllowed(snapshot, resource, action)
    && (!scoped || Object.entries(actions).some(([domain, pair]) => pair[0] === resource && pair[1] === action && scoped.allowed_actions.includes(domain)));
  const field = (resource: string, name: string, write = false) => snapshot?.resources[resource]?.field_permissions.includes(name) === true
    && (!scoped || scoped[write ? "writable_fields" : "readable_fields"].includes(name));
  return { client, can, field, token, snapshot, status };
}
