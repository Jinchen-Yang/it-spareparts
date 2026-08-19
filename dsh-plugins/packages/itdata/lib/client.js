window.__ModuleLoader__.load({
	id: "dsh-itdata",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// src/client.jsx
var client_exports = {};
__export(client_exports, {
  apply: () => apply,
  inject: () => inject
});
module.exports = __toCommonJS(client_exports);
var import_react = __toESM(require("react"), 1);
var RPC = "/plugins/itdata/rpc";
async function rpc(action, extra = {}) {
  const resp = await fetch(RPC, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ action, ...extra })
  });
  return resp.json();
}
var card = {
  border: "1px solid rgba(128,128,128,0.25)",
  borderRadius: 10,
  padding: "14px 16px",
  margin: "12px 0"
};
var label = { display: "block", fontSize: 12, opacity: 0.7, marginBottom: 4 };
var input = {
  width: "100%",
  boxSizing: "border-box",
  padding: "7px 10px",
  borderRadius: 8,
  border: "1px solid rgba(128,128,128,0.4)",
  background: "transparent",
  color: "inherit",
  fontSize: 13,
  marginBottom: 10
};
var button = {
  padding: "7px 16px",
  borderRadius: 8,
  border: "1px solid rgba(128,128,128,0.4)",
  background: "rgba(128,128,128,0.12)",
  color: "inherit",
  fontSize: 13,
  cursor: "pointer"
};
var dot = (ok) => ({
  display: "inline-block",
  width: 8,
  height: 8,
  borderRadius: "50%",
  marginRight: 6,
  background: ok ? "#3fb27f" : "#d05663"
});
function fmtTime(unix) {
  if (!unix) return "-";
  return new Date(unix * 1e3).toLocaleString();
}
function permSummary(perms) {
  if (perms === null || typeof perms !== "object") return null;
  const entries = Object.entries(perms);
  const on = entries.filter(([, v]) => v === true).map(([k]) => k);
  const off = entries.filter(([, v]) => v !== true).map(([k]) => k);
  return { on, off, total: entries.length };
}
var inject = ["slots"];
function apply(ctx) {
  const slots = ctx.get("slots");
  if (slots === void 0) return;
  slots.inject("settings.section", () => slots.register(
    { name: "settings.section", id: "itdata", order: 60, label: "IT \u5907\u4EF6\u7CFB\u7EDF" },
    function ItDataSettingsPage() {
      const [state, setState] = (0, import_react.useState)({ loading: true });
      const [username, setUsername] = (0, import_react.useState)("");
      const [password, setPassword] = (0, import_react.useState)("");
      const [busy, setBusy] = (0, import_react.useState)(false);
      const [error, setError] = (0, import_react.useState)(null);
      const refresh = (0, import_react.useCallback)(async () => {
        try {
          const r = await rpc("status");
          setError(null);
          setState({ loading: false, auth: r.auth, backend: r.backend });
        } catch (e) {
          setState({ loading: false });
          setError(String(e?.message ?? e));
        }
      }, []);
      (0, import_react.useEffect)(() => {
        void refresh();
      }, [refresh]);
      const doLogin = async () => {
        setBusy(true);
        setError(null);
        try {
          const r = await rpc("login", { username, password });
          if (r.ok !== true) setError(r.error ?? "\u767B\u5F55\u5931\u8D25");
          else {
            setPassword("");
            void refresh();
          }
        } catch (e) {
          setError(String(e?.message ?? e));
        } finally {
          setBusy(false);
        }
      };
      const doLogout = async () => {
        setBusy(true);
        try {
          await rpc("logout");
        } catch {
        } finally {
          setBusy(false);
          void refresh();
        }
      };
      if (state.loading) {
        return import_react.default.createElement("div", { style: { padding: 24, opacity: 0.7 } }, "\u52A0\u8F7D\u4E2D\u2026");
      }
      const auth = state.auth ?? { loggedIn: false };
      const backend = state.backend ?? { state: "unknown" };
      const summary = permSummary(auth.permissions);
      const children = [];
      children.push(import_react.default.createElement(
        "div",
        { key: "head", style: { fontSize: 14, fontWeight: 600 } },
        "IT \u5907\u4EF6\u7BA1\u7406\u7CFB\u7EDF \xB7 \u6743\u9650\u95E8"
      ));
      children.push(import_react.default.createElement(
        "div",
        { key: "backend", style: { ...card, fontSize: 13 } },
        import_react.default.createElement("span", { style: dot(backend.state === "up") }),
        `\u540E\u7AEF\uFF08${backend.state === "up" ? "\u5728\u7EBF" : backend.state === "down" ? "\u4E0D\u53EF\u8FBE" : "\u672A\u77E5"}\uFF09`,
        backend.detail ? import_react.default.createElement("span", { style: { opacity: 0.6, marginLeft: 8 } }, backend.detail) : null
      ));
      if (auth.loggedIn) {
        const rows = [
          ["\u8D26\u53F7", `${auth.name ?? "-"}\uFF08${auth.username ?? auth.role ?? "-"}\uFF09`],
          ["\u89D2\u8272", auth.role ?? "-"],
          ["\u767B\u5F55\u65F6\u95F4", fmtTime(auth.loggedInAt)],
          ["\u4EE4\u724C\u5230\u671F", fmtTime(auth.expiresAt)]
        ];
        children.push(import_react.default.createElement(
          "div",
          { key: "auth", style: card },
          import_react.default.createElement(
            "div",
            { style: { ...label, fontSize: 13, opacity: 1 } },
            import_react.default.createElement("span", { style: dot(true) }),
            "\u5DF2\u767B\u5F55"
          ),
          ...rows.map(([k, v]) => import_react.default.createElement(
            "div",
            { key: k, style: { display: "flex", fontSize: 13, margin: "4px 0" } },
            import_react.default.createElement("span", { style: { width: 84, opacity: 0.6 } }, k),
            import_react.default.createElement("span", null, v)
          )),
          summary !== null && summary.total > 0 ? import_react.default.createElement(
            "details",
            { style: { marginTop: 8, fontSize: 12 } },
            import_react.default.createElement(
              "summary",
              { style: { cursor: "pointer", opacity: 0.75 } },
              `\u6743\u9650\u6458\u8981\uFF1A${summary.on.length}/${summary.total} \u9879\u5F00\u542F`
            ),
            import_react.default.createElement(
              "div",
              { style: { marginTop: 6, lineHeight: 1.7, wordBreak: "break-all", opacity: 0.8 } },
              summary.on.map((k) => import_react.default.createElement("code", { key: k, style: { marginRight: 8 } }, k))
            )
          ) : null,
          import_react.default.createElement(
            "div",
            { style: { marginTop: 12, display: "flex", gap: 10 } },
            import_react.default.createElement("button", { style: button, onClick: doLogout, disabled: busy }, "\u9000\u51FA\u767B\u5F55"),
            import_react.default.createElement("button", {
              style: button,
              onClick: () => window.open("/itd/", "_blank", "noopener"),
              title: "\u5D4C\u5165\u9762\u677F\uFF08P5 \u90E8\u7F72\u524D\u7AEF\u540E\u53EF\u7528\uFF09"
            }, "\u6253\u5F00\u6570\u636E\u9762\u677F")
          )
        ));
      } else {
        children.push(import_react.default.createElement(
          "div",
          { key: "login", style: card },
          import_react.default.createElement("div", { style: { ...label, fontSize: 13, opacity: 1 } }, "\u767B\u5F55\uFF08agent \u6570\u636E\u5DE5\u5177\u5C06\u4EE5\u6B64\u8EAB\u4EFD\u901A\u8FC7\u6743\u9650\u95E8\uFF09"),
          import_react.default.createElement("label", { style: label }, "\u7528\u6237\u540D"),
          import_react.default.createElement("input", {
            style: input,
            value: username,
            autoComplete: "username",
            onChange: (e) => setUsername(e.target.value)
          }),
          import_react.default.createElement("label", { style: label }, "\u5BC6\u7801"),
          import_react.default.createElement("input", {
            style: input,
            type: "password",
            value: password,
            autoComplete: "current-password",
            onChange: (e) => setPassword(e.target.value),
            onKeyDown: (e) => {
              if (e.key === "Enter" && username !== "" && password !== "") void doLogin();
            }
          }),
          import_react.default.createElement("button", {
            style: { ...button, opacity: busy || username === "" || password === "" ? 0.5 : 1 },
            onClick: doLogin,
            disabled: busy || username === "" || password === ""
          }, busy ? "\u767B\u5F55\u4E2D\u2026" : "\u767B\u5F55")
        ));
      }
      if (error !== null) {
        children.push(import_react.default.createElement("div", {
          key: "error",
          style: { ...card, borderColor: "#d05663", color: "#d05663", fontSize: 13 }
        }, String(error)));
      }
      return import_react.default.createElement("div", { style: { maxWidth: 520 } }, children);
    }
  ));
}
		return module.exports;
	}
});
