from __future__ import annotations

import hmac
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
