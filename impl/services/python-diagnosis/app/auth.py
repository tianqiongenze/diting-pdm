"""认证与授权（技术债 #4）。

设计取舍：
- **不引入完整 IdP**（OIDC/LDAP），本系统是车间级工具，重了。
- **不用 JWT 库**，用标准库 hmac + base64 自签 token，够用且零依赖。
- **RBAC 三级角色**，不做 ABAC（"只能管自己车间"属 ABAC，超出本期）。

token 格式：base64url(payload).base64url(hmac_sha256(secret, payload_b64))
payload: {"sub": 用户名, "role": 角色, "tenant": 租户, "exp": 过期时间戳}

启用策略：
- `PDM_AUTH_REQUIRED=1` → 写操作强制校验 token（生产建议开启）
- 默认 0 → 仅记录、不阻断（兼容既有测试与演示）

为什么默认不强制：现有 109 项自动化测试与页面均以 X-Tenant-Id 调用，
突然强制会让全部写操作 401。采用"可渐进启用"而非"一刀切"。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

SECRET = os.getenv("PDM_AUTH_SECRET", "diting-pdm-dev-secret-change-me")
TOKEN_TTL_SECONDS = int(os.getenv("PDM_AUTH_TTL", "43200"))   # 12 小时
AUTH_REQUIRED = os.getenv("PDM_AUTH_REQUIRED", "0") == "1"

# 角色 → 权限集合
PERMISSIONS: dict[str, set[str]] = {
    "operator": {
        "view", "alert.ack", "inspect.execute",
    },
    "supervisor": {
        "view", "alert.ack", "alert.escalate", "card.confirm",
        "inspect.execute", "plan.edit",
    },
    "manager": {
        "view", "alert.ack", "alert.escalate", "alert.close", "card.confirm",
        "inspect.execute", "plan.edit", "plan.approve", "user.view",
    },
}

ROLE_LABELS = {"operator": "值班员", "supervisor": "维修主管", "manager": "设备主管"}


class AuthError(Exception):
    """认证/授权失败。"""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120000).hex()
    return f"pbkdf2${salt}${h}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120000).hex()
    return hmac.compare_digest(calc, digest)


def issue_token(username: str, role: str, tenant: str,
                ttl: int | None = None) -> dict[str, Any]:
    exp = int(time.time()) + (ttl or TOKEN_TTL_SECONDS)
    payload = {"sub": username, "role": role, "tenant": tenant, "exp": exp}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    body = _b64e(raw)
    sig = _b64e(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return {"token": f"{body}.{sig}", "expires_at": exp, **payload}


def decode_token(token: str) -> dict[str, Any]:
    if not token or "." not in token:
        raise AuthError("token 格式错误")
    body, sig = token.rsplit(".", 1)
    expect = _b64e(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        raise AuthError("token 签名无效")
    try:
        payload = json.loads(_b64d(body))
    except Exception as exc:  # noqa: BLE001
        raise AuthError(f"token 解析失败: {exc}") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise AuthError("token 已过期")
    return payload


def has_permission(role: str, perm: str) -> bool:
    return perm in PERMISSIONS.get(role, set())


def require(token: str | None, perm: str) -> dict[str, Any] | None:
    """校验 token 与权限。

    返回 payload（成功）或 None（未启用强制且无 token）。
    失败抛 AuthError。
    """
    if not token:
        if AUTH_REQUIRED:
            raise AuthError("需要登录")
        return None
    payload = decode_token(token)
    role = payload.get("role", "")
    if not has_permission(role, perm):
        raise AuthError(f"角色 {ROLE_LABELS.get(role, role)} 无 {perm} 权限")
    return payload
