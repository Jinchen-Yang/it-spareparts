"""导入前作废预演令牌：把 file_hash + 作废指纹 + 合同 绑成一份服务端可验证的承诺。

与 maintenance_warehouse._preview_token 同一范式（HMAC-SHA256、同一把 secret_key）。
令牌是纯函数签发/校验，不落库：提交时 HTTP 层重算实际收到文件的 sha256，只按 hash
命中令牌，把指纹交给 loader 在装载期复核（expense_void.assert_fingerprint）。

用户在预演后去 Excel 里改一改再点确认 → hash 不匹配 → 没有指纹 → run_import 在
require_void_preview 下拒绝；预演与提交之间有人改了相关报销行 → 指纹不符 → loader
抛 VoidPlanDrift。两条路都是「整批不导入、提示重新预演」，绝不静默按新状态执行。
"""
import base64
import hashlib
import hmac
import json

TOKEN_VERSION = 1
TOKEN_TTL_SECONDS = 30 * 60


class VoidPreviewTokenError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload: bytes, hmac_key: bytes) -> bytes:
    return hmac.new(hmac_key, payload, hashlib.sha256).digest()


def issue(*, file_hash: str, mode: str, fingerprint: str, contract: str | None,
          issued_at: int, hmac_key: bytes) -> str:
    payload = json.dumps(
        {"v": TOKEN_VERSION, "h": file_hash, "m": mode, "f": fingerprint,
         "c": contract, "t": int(issued_at)},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return f"{_b64(payload)}.{_b64(_sign(payload, hmac_key))}"


def verify(token: str, *, hmac_key: bytes, now: int, mode: str) -> dict:
    """返回 {file_hash, fingerprint, contract, issued_at}；任何不合法都抛错，绝不降级。"""
    try:
        body, sig = token.split(".", 1)
        payload = _unb64(body)
        given = _unb64(sig)
    except Exception as exc:  # noqa: BLE001 — 格式坏了就是坏了
        raise VoidPreviewTokenError("void_preview_invalid", "预演令牌格式无效") from exc
    if not hmac.compare_digest(given, _sign(payload, hmac_key)):
        raise VoidPreviewTokenError("void_preview_invalid", "预演令牌签名无效")
    data = json.loads(payload.decode("utf-8"))
    if data.get("v") != TOKEN_VERSION:
        raise VoidPreviewTokenError("void_preview_invalid", "预演令牌版本不受支持")
    if data.get("m") != mode:
        raise VoidPreviewTokenError("void_preview_mode_mismatch",
                                    "预演时的导入模式与本次提交不一致，请重新预演")
    issued_at = int(data.get("t", 0))
    if issued_at > now + 60 or now - issued_at > TOKEN_TTL_SECONDS:
        raise VoidPreviewTokenError("void_preview_expired", "预演已过期（30 分钟），请重新预演")
    return {"file_hash": data["h"], "fingerprint": data["f"],
            "contract": data.get("c"), "issued_at": issued_at}
