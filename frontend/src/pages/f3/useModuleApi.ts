import { useMemo } from "react";
import type { AxiosRequestConfig, AxiosResponse } from "axios";
import api from "../../api";
import { useAuthorizedRunner } from "../../authz/access";
import { actionRule, type Rule } from "../../authz/rules";
import { transportConfig } from "../../authz/transport";

/** F3 requests use the shared capture/abort runner; callers still own data and UI rules. */
export function useModuleApi(
  resource: string,
  legacyAllowed: boolean,
  readAction = "read",
) {
  const run = useAuthorizedRunner();
  return useMemo(() => {
    const legacy = (rule: Rule) =>
      Object.fromEntries(
        rule.anyOf.flat().map((need) => [need.resource_id, legacyAllowed]),
      );
    function request<T>(
      rule: Rule,
      send: (config: AxiosRequestConfig) => Promise<AxiosResponse<T>>,
    ) {
      return run({
        rule,
        legacy: legacy(rule),
        request: (capture) => send(transportConfig(capture)),
      });
    }
    return {
      get<T = any>(
        url: string,
        config: AxiosRequestConfig = {},
        rule = actionRule(resource, readAction),
      ) {
        return request<T>(rule, (captured) =>
          api.get<T>(url, { ...config, ...captured }),
        );
      },
      post<T = any>(
        url: string,
        body: unknown,
        rule: Rule,
        config: AxiosRequestConfig = {},
      ) {
        return request<T>(rule, (captured) =>
          api.post<T>(url, body, { ...config, ...captured }),
        );
      },
      put<T = any>(
        url: string,
        body: unknown,
        rule: Rule,
        config: AxiosRequestConfig = {},
      ) {
        return request<T>(rule, (captured) =>
          api.put<T>(url, body, { ...config, ...captured }),
        );
      },
      delete<T = any>(
        url: string,
        rule: Rule,
        config: AxiosRequestConfig = {},
      ) {
        return request<T>(rule, (captured) =>
          api.delete<T>(url, { ...config, ...captured }),
        );
      },
    };
  }, [run, resource, legacyAllowed, readAction]);
}
