#!/usr/bin/env python3
"""发布只读冒烟：在候选 app 内执行，凭据仅驻留内存，不打印业务内容。"""
import base64
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import select, text

from app import permissions
from app.auth import _sign
from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.models.system import SysUser


def main():
    base = os.environ.get("ITSP_SMOKE_BASE", "http://127.0.0.1:8000")
    if base not in {"http://127.0.0.1:8000", "https://ybwznt.com", "https://www.ybwznt.com"}:
        raise RuntimeError("unapproved smoke destination")
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '10000ms'"))
        user = db.scalar(select(SysUser).where(SysUser.is_active.is_(True),
            SysUser.deleted_at.is_(None), SysUser.role == "admin").order_by(SysUser.id).limit(1))
        if user is None:
            raise RuntimeError("no existing active administrator for readonly smoke")
        # 复用既有账号的实际权限与吊销版本，不登录、不改变账号、不落盘 token。
        payload = {"role": user.role, "sub": user.username, "name": user.salesperson_name,
            "exp": int(time.time()) + 120, "tv": user.token_version or 0,
            "authn": "sys_user", "perms": permissions.runtime_safe(permissions.effective_for_user(user))}
        part = db.execute(select(DimPart.id, DimPart.pn_std).order_by(DimPart.id.desc()).limit(1)).first()
        db.rollback()
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    token = base64.urlsafe_b64encode(encoded).decode().rstrip("=") + "." + _sign(encoded)
    results = []

    def get(label, path, expected):
        started = time.monotonic()
        request = Request(base + path, headers={"Authorization": "Bearer " + token,
            "User-Agent": "itsp-release-readonly-smoke"}, method="GET")
        try:
            with urlopen(request, timeout=30) as response:
                value = json.load(response)
                code = response.status
        except HTTPError as exc:
            raise RuntimeError("readonly smoke HTTP failure: " + label + " " + str(exc.code)) from None
        if code != 200 or not isinstance(value, expected):
            raise RuntimeError("readonly smoke response shape failure: " + label)
        results.append({"check": label, "status": code, "elapsed_ms": round((time.monotonic() - started) * 1000, 1)})
        return value

    inquiries = get("inquiries", "/api/presales/inquiries?scope=all&page_size=1", dict)
    if not isinstance(inquiries.get("items"), list) or not isinstance(inquiries.get("total"), int):
        raise RuntimeError("inquiry pagination contract failed")
    if inquiries["items"]:
        inquiry_id = int(inquiries["items"][0]["id"])
        detail = get("inquiry_current_answers", "/api/presales/inquiries/" + str(inquiry_id), dict)
        if not isinstance(detail.get("line_answers"), list):
            raise RuntimeError("canonical line answers missing")
    if part:
        library = get("quote_library", "/api/presales/library?part_ids=" + str(int(part.id)), dict)
        get("part_suggestion", "/api/parts/suggest?" + urlencode({"q": part.pn_std, "limit": 1}), list)
    else:
        get("quote_library", "/api/presales/library", dict)
    print(json.dumps({"status": "passed", "checks": results, "business_payloads_recorded": False}))


if __name__ == "__main__":
    main()
