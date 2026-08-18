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

To authenticate with a TLS client certificate instead of a password, pass an
SSL context that presents it. XAPI ignores the credentials on such a
connection and assigns the built-in ``client-cert`` role:

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain("client.crt", "client.key")
    session = AsyncXenAPISession("https://host-ip", ssl_context=ctx)
    await session.login_with_password("ignored", "ignored")

See ``examples/xscert.py`` for the full flow, including installing the CA.
"""

# Defers annotation evaluation (PEP 563). _MethodProxy is annotated with
# AsyncXenAPISession, which is defined further down this file, so without this
# the import raises NameError on every Python before 3.14 (PEP 649 makes the
# deferral the default only from 3.14).
from __future__ import annotations

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


def client_cert_context(
    certfile: str,
    keyfile: str | None = None,
    *,
    cafile: str | None = None,
    check_hostname: bool = True,
) -> ssl.SSLContext:
    """Build an SSL context that presents a TLS client certificate.

    Pass the result as ``AsyncXenAPISession(url, ssl_context=...)`` to
    authenticate with a certificate instead of a password::

        ctx = client_cert_context("client.crt", "client.key")
        session = AsyncXenAPISession("https://pool", ssl_context=ctx)
        await session.login_with_password("ignored", "ignored")

    The certificate's CN/SAN must equal the pool's
    ``client_certificate_auth_name``; XenServer's stunnel enforces that as
    ``checkHost``.

    ``cafile`` verifies the *server* against a CA bundle. Omit it and server
    verification is **disabled**, which is convenient against a lab pool
    presenting a self-signed certificate but leaves the channel encrypted
    without authenticating the peer -- pass ``cafile`` anywhere it matters.
    """
    ctx = ssl.SSLContext(protocol=ssl.PROTOCOL_TLS_CLIENT)
    if cafile is not None:
        ctx.load_verify_locations(cafile)
        ctx.check_hostname = check_hostname
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        # Must be cleared before verify_mode, or CPython raises.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return ctx


class XenAPIError(RuntimeError):
    """A XAPI call returned a JSON-RPC error.

    Subclasses RuntimeError so existing ``except RuntimeError`` keeps working.

    The structured error is preserved so callers do not have to match on the
    rendered string:

        try:
            await session.xenapi.VM.start(vm, False, False)
        except XenAPIError as e:
            if e.code == "RBAC_PERMISSION_DENIED":
                ...

    ``code`` is XAPI's error name (``RBAC_PERMISSION_DENIED``,
    ``HANDLE_INVALID``, ...), which JSON-RPC carries in the ``message`` field;
    ``params`` is XAPI's error parameter list; ``error`` is the raw object.
    """

    def __init__(self, method: str, error: Any):
        self.method = method
        self.error = error
        if isinstance(error, dict):
            self.code = error.get("message")
            self.params = error.get("data") or []
        else:  # a server that does not follow the shape we expect
            self.code = None
            self.params = []
        super().__init__(f"XAPI {method} failed: {error}")


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

    def __init__(self, url: str, ssl_context: ssl.SSLContext | None = None):
        """Create a session against ``url``.

        ``ssl_context`` overrides the module default for this session only. Pass
        one built with ``load_cert_chain()`` to authenticate with a TLS client
        certificate instead of a password; scoping it per instance means a
        certificate cannot leak onto an unrelated password session in the same
        process. Omit it to keep the previous behaviour.
        """
        self._url = f"{url.rstrip('/')}/jsonrpc"
        self._ssl_ctx = ssl_context if ssl_context is not None else _ssl_ctx
        self._http: aiohttp.ClientSession | None = None
        self._session_ref: str | None = None
        self.xenapi = _XenAPINamespace(self)

    @property
    def session_ref(self) -> str | None:
        """The current session ref, or None before login / after logout."""
        return self._session_ref

    def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
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
            raise XenAPIError("session.login_with_password", ret["error"])
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
            raise XenAPIError(method, ret["error"])
        return ret["result"]
