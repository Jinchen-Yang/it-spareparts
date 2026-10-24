import { useLocation } from "react-router-dom";
import type { ComponentType } from "react";
import { useAccess } from "./access";
import { surfaceRule, anyRule, type Rule } from "./rules";

/** A resolution shell only. Data and commands still require their own server context. */
export function withContextSurface<P extends object>(Component: ComponentType<P>, rule: Rule,
  identity: (props: P) => unknown = () => null) {
  return function ContextSurface(props: P) {
    const legacy = Object.fromEntries(rule.anyOf.flat().map(need => [need.resource_id, true]));
    const access = useAccess(rule, { legacy });
    if (access.entryState === "deny") return null;
    return <Component {...props} key={JSON.stringify([
      access.token, access.generation, access.snapshot?.version, identity(props),
    ])} />;
  };
}
export const contextSurfaces = (...ids: string[]) => anyRule(...ids.map(surfaceRule));

/** URL/object changes dispose only this migrated page, including portals and pending queries. */
export function withContextPage(Component: ComponentType, rule: Rule) {
  const Bound = withContextSurface(Component, rule);
  return function ContextPage() {
    const location = useLocation();
    return <Bound key={`${location.pathname}${location.search}`} />;
  };
}
