"""导出 FastAPI 路由目录 JSON（供 itdata-dsh 生成 agent 业务操作表）：cd backend && uv run python scripts/export_routes.py routes.json"""
import inspect, json, sys, typing
from app.main import app
from fastapi.routing import APIRoute
from fastapi import params as fparams
from pydantic import BaseModel

def type_name(t):
    try:
        s = typing.get_origin(t)
        if s is typing.Union or str(type(t)) == "<class 'types.UnionType'>":
            args = [a for a in typing.get_args(t) if a is not type(None)]
            return " | ".join(type_name(a) for a in args) + (" | None" if len(args) != len(typing.get_args(t)) else "")
        if s is not None:
            return f"{getattr(s,'__name__',str(s))}[{', '.join(type_name(a) for a in typing.get_args(t))}]"
        if isinstance(t, type) and issubclass(t, BaseModel):
            return t.__name__
        return getattr(t, "__name__", str(t)).replace("typing.", "")
    except Exception:
        return str(t)

def model_fields(t, depth=0):
    """pydantic 模型字段展开（一层嵌套）。"""
    if not (isinstance(t, type) and issubclass(t, BaseModel)):
        return None
    out = []
    for name, f in t.model_fields.items():
        ann = f.annotation
        desc = f.description or ""
        default = None if f.is_required() else (None if f.default is ... else (repr(f.default)[:60] if f.default_factory is None else "factory"))
        entry = {"name": name, "type": type_name(ann), "required": f.is_required(), "default": default, "desc": desc}
        inner = ann
        o = typing.get_origin(ann)
        if o in (list, typing.List):
            inner = typing.get_args(ann)[0]
        if depth < 1 and isinstance(inner, type) and issubclass(inner, BaseModel):
            entry["fields"] = model_fields(inner, depth + 1)
        out.append(entry)
    return out

def dep_info(dependant, acc):
    for d in dependant.dependencies:
        call = d.call
        name = getattr(call, "__name__", str(call))
        qual = getattr(call, "__qualname__", name)
        closure = {}
        if getattr(call, "__closure__", None) and call.__code__.co_freevars:
            for k, c in zip(call.__code__.co_freevars, call.__closure__):
                try: closure[k] = c.cell_contents
                except Exception: pass
        entry = {"dep": qual}
        for key in ("page_key", "action_key", "require_data", "roles", "allowed", "feature", "flag"):
            if key in closure:
                v = closure[key]
                entry[key] = sorted(v) if isinstance(v, (set, frozenset)) else v
        acc.append(entry)
        dep_info(d, acc)

def _src(fn):
    try: return inspect.getsource(fn)
    except Exception: return ""

rows = []
for r in app.routes:
    if not isinstance(r, APIRoute): continue
    for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
        dep = r.dependant
        deps = []
        dep_info(dep, deps)
        # 也包含路由器级依赖（已合并进 dependant.dependencies）
        body = None
        if dep.body_params:
            bps = []
            for p in dep.body_params:
                fi = p.field_info
                ann = getattr(p, "type_", None) or fi.annotation
                is_file = isinstance(fi, fparams.File) or "UploadFile" in type_name(ann)
                is_form = isinstance(fi, fparams.Form)
                bps.append({"name": p.name, "type": type_name(ann), "required": bool(getattr(p, "required", getattr(fi, "is_required", lambda: False)())), "file": is_file, "form": is_form,
                            "fields": model_fields(ann)})
            multipart = any(b["file"] or b["form"] for b in bps)
            body = {"content_type": "multipart" if multipart else "json", "params": bps}
        qp = [{"name": p.name, "type": type_name(getattr(p, "type_", None) or p.field_info.annotation), "required": bool(getattr(p, "required", p.field_info.is_required())), "default": (None if p.field_info.default is ... or getattr(p, "required", p.field_info.is_required()) else repr(p.field_info.default)[:60]), "desc": p.field_info.description or ""} for p in dep.query_params]
        pp = [{"name": p.name, "type": type_name(getattr(p, "type_", None) or p.field_info.annotation)} for p in dep.path_params]
        doc = inspect.getdoc(r.endpoint) or ""
        rc = r.response_class
        rc = getattr(rc, "value", rc)
        resp = getattr(rc, "__name__", None)
        rows.append({
            "method": method, "path": r.path, "name": r.name, "func": f"{r.endpoint.__module__}.{r.endpoint.__name__}",
            "tags": list(r.tags or []), "doc": doc, "summary": (doc.splitlines()[0] if doc else ""),
            "path_params": pp, "query_params": qp, "body": body, "deps": deps,
            "response_class": resp, "response_model": type_name(r.response_model) if r.response_model else None,
            "file_response": resp in ("FileResponse", "StreamingResponse") or any(k in _src(r.endpoint) for k in ("FileResponse", "StreamingResponse", "Response(content=", "media_type=")),
        })
json.dump(rows, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
print(len(rows), "routes")
