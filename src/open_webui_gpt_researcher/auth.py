from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request, status


@dataclass(frozen=True)
class OpenWebUIPrincipal:
    user_id: str


async def require_openwebui_principal(
    request: Request,
    x_research_service_token: str = Header(),
    x_openwebui_user_id: str = Header(),
) -> OpenWebUIPrincipal:
    """Authenticate the trusted Open WebUI adapter and retain end-user identity."""
    expected = request.app.state.settings.service_token.get_secret_value()
    if not hmac.compare_digest(x_research_service_token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid service token")
    if not x_openwebui_user_id.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing Open WebUI user id")
    return OpenWebUIPrincipal(user_id=x_openwebui_user_id)


def require_runner_token(authorization: str = Header()) -> str:
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "runner bearer token required")
    return value


def create_download_token(
    *, secret: str, job_id: str, artifact_name: str, user_id: str, ttl_seconds: int
) -> str:
    payload = {
        "artifact": artifact_name,
        "exp": int(time.time()) + ttl_seconds,
        "job": job_id,
        "user": user_id,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=")
    signature = hmac.new(secret.encode(), encoded, hashlib.sha256).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def verify_download_token(token: str, *, secret: str, job_id: str, artifact_name: str) -> str:
    try:
        encoded, signature_text = token.split(".", maxsplit=1)
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())
        if payload["job"] != job_id or payload["artifact"] != artifact_name:
            raise ValueError
        if int(payload["exp"]) < int(time.time()):
            raise ValueError
        return str(payload["user"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid download token") from error
