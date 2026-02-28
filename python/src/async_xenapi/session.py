#!/usr/bin/env python
"""An async library for XenAPI

Usage mirrors the synchronous XenAPI SDK:

    session = AsyncXenAPISession("https://host-ip")
    await session.login_with_password("root", "password")

    vms = await session.xenapi.VM.get_all()
    for vm in vms:
        record = await session.xenapi.VM.get_record(vm)
        print(record["name_label"])

    await session.logout()
"""

import contextlib
import ssl
import uuid
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# SSL / JSON-RPC helpers
# ---------------------------------------------------------------------------


def _create_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(protocol=ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_ssl_ctx = _create_ssl_ctx()


def _jsonrpc_req(method: str, params: list[Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": str(uuid.uuid4()),
    }


# ---------------------------------------------------------------------------
# Async XenAPI proxy
# ---------------------------------------------------------------------------


class _MethodProxy:
    """Accumulates dotted attribute access (e.g. VM.get_all) then turns the
    final call into an awaitable JSON-RPC request."""

    def __init__(self, session: AsyncXenAPISession, name: str):
        self._session = session
        self._name = name

    def __getattr__(self, attr: str) -> _MethodProxy:
        return _MethodProxy(self._session, f"{self._name}.{attr}")

    async def __call__(self, *args: Any) -> Any:
        return await self._session._call(self._name, list(args))


class _XenAPINamespace:
    """The object returned by ``session.xenapi``."""

    def __init__(self, session: AsyncXenAPISession):
        self._session = session

    def __getattr__(self, attr: str) -> _MethodProxy:
        return _MethodProxy(self._session, attr)


class AsyncXenAPISession:
    """Lightweight async wrapper around XAPI's JSON-RPC endpoint using aiohttp."""

    def __init__(self, url: str):
        self._url = f"{url.rstrip('/')}/jsonrpc"
        self._http: aiohttp.ClientSession | None = None
        self._session_ref: str | None = None
        self.xenapi = _XenAPINamespace(self)

    def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            connector = aiohttp.TCPConnector(ssl=_ssl_ctx)
            self._http = aiohttp.ClientSession(connector=connector)
        return self._http

    async def _post(self, payload: dict[str, Any]) -> Any:
        http = self._ensure_http()
        async with http.post(self._url, json=payload) as resp:
            return await resp.json()

    async def login_with_password(self, user: str, password: str) -> str:
        payload = _jsonrpc_req(
            "session.login_with_password",
            [user, password, "version", "originator"],
        )
        ret = await self._post(payload)
        if "error" in ret:
            raise RuntimeError(f"Login failed: {ret['error']}")
        self._session_ref = ret["result"]
        return self._session_ref

    async def logout(self) -> None:
        if self._session_ref:
            payload = _jsonrpc_req("session.logout", [self._session_ref])
            with contextlib.suppress(Exception):
                await self._post(payload)
            self._session_ref = None
        if self._http and not self._http.closed:
            await self._http.close()
            self._http = None

    async def _call(self, method: str, params: list[Any]) -> Any:
        """Send an authenticated JSON-RPC call and return the result."""
        if not self._session_ref:
            raise RuntimeError("Not logged in")
        payload = _jsonrpc_req(method, [self._session_ref] + params)
        ret = await self._post(payload)
        if "error" in ret:
            raise RuntimeError(f"XAPI {method} failed: {ret['error']}")
        return ret["result"]
