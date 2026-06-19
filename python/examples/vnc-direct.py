#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["async-xenapi"]
# ///
"""vnc-direct.py — bridge a known XenServer console location URL to a local VNC port.

Usage: ./vnc-direct.py --vm-uuid <VM-UUID> --host <XS-HOST>          # looks up the live console (recommended)
       ./vnc-direct.py --vm-name <name> --host <XS-HOST>
       ./vnc-direct.py --location 'https://HOST/console?uuid=...'    # explicit console URL
       (host/user/password also read from .env: XS_HOST/XS_USER/XS_PASSWORD)
Then : open vnc://localhost:5901

The XenServer VM console URL is an authenticated HTTP CONNECT endpoint, not a plain
URL or a VNC port. XenServer's console proxy authenticates via a XenAPI **session_id**
(Basic auth is rejected with HTTP 500 on current builds), so this tool logs into the
API, then performs CONNECT?...&session_id=<ref> over TLS and pumps raw RFB to a local
TCP port a Mac VNC client can use.

A startup preflight reports immediately whether auth + console are good.

Requires: pip install async-xenapi  (login goes through it; the raw CONNECT/relay
stays plain sync sockets, reusing the session_ref the login returns).
"""
import argparse, asyncio, os, socket, threading, sys, getpass, urllib.parse

from xs_common import TLS_CONTEXT, load_env_files, connect_async

CONNECT_TIMEOUT = 10  # seconds, for the TLS connect + CONNECT handshake only


async def _resolve_rfb(xenapi, vm_uuid=None, vm_name=None):
    """Look up a VM's CURRENT rfb (VNC) console location — avoids stale console uuids,
    which change on every VM restart."""
    if vm_uuid:
        try:
            vm = await xenapi.VM.get_by_uuid(vm_uuid)
        except Exception as e:
            sys.exit(f"[lookup] no VM with uuid {vm_uuid}: {e}")
    else:
        recs = await xenapi.VM.get_by_name_label(vm_name)
        if not recs:
            sys.exit(f"[lookup] no VM named '{vm_name}'")
        if len(recs) > 1:
            sys.exit(f"[lookup] name '{vm_name}' is ambiguous ({len(recs)} VMs) — use --vm-uuid")
        vm = recs[0]
    if await xenapi.VM.get_power_state(vm) != "Running":
        sys.exit("[lookup] VM is not running — no live console")
    for cref in await xenapi.VM.get_consoles(vm):
        rec = await xenapi.console.get_record(cref)
        if rec.get("protocol") == "rfb":
            return rec["location"]
    sys.exit("[lookup] VM has no rfb (VNC) console (serial-only guest?)")


async def _startup(login_host, user, pw, a):
    """Log in via async-xenapi and resolve the console location. Returns
    (session_ref, location). The server-side session is left logged IN (no logout)
    so the sync relay can reuse session_ref in its CONNECT calls."""
    session = await connect_async(login_host, user, pw)
    print("[login] OK")
    # Precedence: --location > --vm-uuid/--vm-name > --uuid+host (validated in main()).
    if a.location:
        location = a.location
    elif a.vm_uuid or a.vm_name:
        location = await _resolve_rfb(session.xenapi, vm_uuid=a.vm_uuid, vm_name=a.vm_name)
        print(f"[lookup] current rfb console: {location}")
    else:
        location = f"https://{a.host}/console?uuid={a.uuid}"
    ref = session._session_ref
    try:                       # close HTTP conns but DON'T logout — ref must stay valid
        await session._http.close()
    except Exception:
        pass
    return ref, location


def open_console(location, session):
    """TLS-connect to the console's host, send CONNECT with session_id, read past the
    HTTP headers. Returns (tls_socket, status_line). Raises OSError on network failure."""
    u = urllib.parse.urlparse(location)
    path = u.path + (('?' + u.query) if u.query else '')
    path += ('&' if u.query else '?') + f"session_id={session}"
    tls = TLS_CONTEXT.wrap_socket(
        socket.create_connection((u.hostname, u.port or 443), timeout=CONNECT_TIMEOUT),
        server_hostname=u.hostname,
    )
    tls.sendall(f"CONNECT {path} HTTP/1.0\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        ch = tls.recv(1)
        if not ch:
            tls.close()
            raise ConnectionError("connection closed during CONNECT (bad uuid / VM moved / powered off?)")
        buf += ch
    return tls, buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")


def preflight(location, session):
    try:
        tls, status = open_console(location, session)
    except OSError as e:
        sys.exit(f"[preflight] FAILED — cannot reach console endpoint: {e}\n"
                 f"            verify the host is reachable on :443 and the location isn't stale.")
    if "200" in status:
        banner = b""
        tls.settimeout(5)
        try:
            banner = tls.recv(16)
        except OSError:
            pass
        tls.close()
        ok = banner.startswith(b"RFB")
        print(f"[preflight] OK — session accepted, console reachable ({status})"
              + (f"  banner={banner!r}" if ok else "  (warning: no RFB banner yet)"))
        return
    tls.close()
    parts = status.split()
    code = parts[1] if len(parts) > 1 else ""
    hint = {
        "401": "session rejected — token expired? re-run to get a fresh session",
        "403": "authenticated but not permitted for this console",
        "404": "console uuid not found — VM powered off, or location is stale (re-read it)",
        "500": "xapi could not open the console — VM not running, console is serial (vt100) "
               "not VNC (rfb), or the location is stale",
    }.get(code, "")
    sys.exit(f"[preflight] FAILED — {status}" + (f"\n            -> {hint}" if hint else ""))


def pump(a, b):
    try:
        while (d := a.recv(65536)):
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client, location, session):
    try:
        tls, status = open_console(location, session)
    except OSError as e:
        print(f"[client] CONNECT failed: {e}")
        client.close()
        return
    if "200" not in status:
        print(f"[client] CONNECT failed: {status}")
        tls.close()
        client.close()
        return
    tls.settimeout(None)      # clear the connect timeout — VNC sessions are long-lived/idle
    client.settimeout(None)
    threading.Thread(target=pump, args=(client, tls), daemon=True).start()
    pump(tls, client)


def main():
    load_env_files()  # .env: real env > .env file > defaults

    p = argparse.ArgumentParser()
    p.add_argument('--location', help="full console URL https://HOST/console?uuid=...")
    p.add_argument('--vm-uuid', help="VM uuid; looks up its CURRENT rfb console (survives restarts)")
    p.add_argument('--vm-name', help="VM name-label; looks up its current rfb console")
    p.add_argument('--uuid', help="console uuid; combined with --host/$XS_HOST")
    p.add_argument('--host', default=os.environ.get("XS_HOST"),
                   help="XS host for lookup/console (default $XS_HOST)")
    p.add_argument('--user', default=os.environ.get("XS_USER", "root"))
    p.add_argument('--password', default=os.environ.get("XS_PASSWORD"))
    p.add_argument('--login-host', help="API login host (default: --host or the host in --location; "
                                        "auto-redirects to pool master if needed)")
    p.add_argument('--local-port', type=int, default=int(os.environ.get("XS_LOCAL_PORT", "5901")))
    p.add_argument('--no-preflight', action='store_true', help="skip the startup auth check")
    a = p.parse_args()

    # Where to log into the API.
    login_host = (a.login_host or a.host
                  or (urllib.parse.urlparse(a.location).hostname if a.location else None))
    if not login_host:
        p.error("need a host: pass --host/$XS_HOST, --login-host, or a full --location")
    if not (a.location or a.vm_uuid or a.vm_name or (a.uuid and a.host)):
        p.error("provide --location, --vm-uuid, --vm-name, or --uuid (+ --host/$XS_HOST)")

    pw = a.password or getpass.getpass(f"{a.user}@{login_host} password: ")
    session_ref, location = asyncio.run(_startup(login_host, a.user, pw, a))

    if not a.no_preflight:
        preflight(location, session_ref)

    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(('127.0.0.1', a.local_port))
    ls.listen(1)
    print(f"ready -> open vnc://localhost:{a.local_port}")
    while True:
        c, _ = ls.accept()
        threading.Thread(target=handle, args=(c, location, session_ref), daemon=True).start()


if __name__ == '__main__':
    main()
