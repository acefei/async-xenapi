#!/usr/bin/env python3
"""Shared helpers for the xs-* XenServer CLI tools.

- load_dotenv / load_env_files : .env loading (stdlib).
- TLS_CONTEXT                  : self-signed TLS context for raw socket work
                                 (vnc-direct.py's console CONNECT tunnel).
- connect_async               : XenAPI login via the async-xenapi library,
                                 following a HOST_IS_SLAVE redirect to the master.

All XenServer login goes through async-xenapi (`pip install async-xenapi`); there
is no hand-rolled sync XML-RPC login anymore. When a tool is run as
`python3 /path/xs-foo.py`, its directory is on sys.path, so this sibling module
imports cleanly regardless of the current directory.
"""
import os, re, ssl, sys

from async_xenapi import AsyncXenAPISession

# XS hosts ship a self-signed cert; these tools target trusted lab hosts.
TLS_CONTEXT = ssl._create_unverified_context()


def load_dotenv(path):
    """Minimal .env loader. Sets vars without overriding real env vars, so
    precedence is: real environment > .env file > built-in defaults."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def load_env_files():
    """Load .env from the current dir, then the caller script's dir (real env wins)."""
    load_dotenv(os.path.join(os.getcwd(), ".env"))
    caller_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    load_dotenv(os.path.join(caller_dir, ".env"))


def _slave_master(exc):
    """Best-effort extraction of the pool master from a HOST_IS_SLAVE error."""
    details = getattr(exc, "details", None) or getattr(exc, "args", None) or []
    if isinstance(details, (list, tuple)) and "HOST_IS_SLAVE" in details:
        i = list(details).index("HOST_IS_SLAVE")
        if i + 1 < len(details):
            return details[i + 1]
    m = re.search(r"HOST_IS_SLAVE.*?(\d+\.\d+\.\d+\.\d+)", str(exc))
    return m.group(1) if m else None


async def connect_async(host, user, pw):
    """Return a logged-in AsyncXenAPISession, following a HOST_IS_SLAVE redirect
    to the pool master. Exits with a clear message on failure."""
    session = AsyncXenAPISession(f"https://{host}")
    try:
        await session.login_with_password(user, pw)
        return session
    except Exception as e:                       # noqa: BLE001 — inspect for slave redirect
        master = _slave_master(e)
        try:
            await session.logout()
        except Exception:
            pass
        if not master:
            sys.exit(f"[login] FAILED: {e}")
        print(f"[login] {host} is a pool member; redirecting to master {master}")
        session = AsyncXenAPISession(f"https://{master}")
        await session.login_with_password(user, pw)
        return session
