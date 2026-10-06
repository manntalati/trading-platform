"""Request dependencies shared by every router: auth, request origin, the query context."""

from __future__ import annotations

import secrets
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request

from tp_api import queries

CLIENT_HEADER = "x-tp-client"
CLIENT_VALUE = "dashboard"


def require_token(request: Request) -> None:
    token: str | None = request.app.state.token
    if token is None:
        return
    header = request.headers.get("authorization", "")
    if not secrets.compare_digest(header, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="missing or wrong dashboard token")


def require_dashboard_client(request: Request) -> None:
    """Guard for anything that changes state (approvals, the kill switch).

    The API listens on localhost, where any web page you visit could try to POST to it. Two
    checks stop that: a custom header (a browser won't send one cross-site without a CORS
    preflight, which this server never grants) and, when the browser says where the request
    came from, an Origin matching the host it was sent to.
    """
    if request.headers.get(CLIENT_HEADER) != CLIENT_VALUE:
        raise HTTPException(status_code=403, detail=f"missing {CLIENT_HEADER} header")
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


def get_context(request: Request) -> queries.Context:
    return queries.Context(request.app.state.settings)


Ctx = Annotated[queries.Context, Depends(get_context)]
