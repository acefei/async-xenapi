#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["async-xenapi"]
# ///
"""xsvm.py — create XenServer VMs and open their VNC console, built on async-xenapi.

Subcommands:
  create   clone a template, size CPU/RAM/disk, attach network + install ISO, add a
           vTPM for Windows 11, start the VM, then print how to open its console.
  console  bridge a VM's VNC console to a local TCP port and print vnc://localhost:PORT
           (the connectable address; the raw XS console URL is an authed CONNECT, not a
           vnc:// you can open directly).

Config comes from .env (XS_HOST / XS_USER / XS_PASSWORD / XS_LOCAL_PORT) or flags;
precedence is real env var > .env > default, and a CLI flag overrides all.

Requires: pip install async-xenapi  (or run via `uv run xsvm.py …` — PEP 723 auto-installs).
"""
import argparse, asyncio, getpass, os, socket, sys, threading, urllib.parse

from xs_common import TLS_CONTEXT, connect_async, load_env_files, session_ref_for_relay

GiB = 1024**3
CONNECT_TIMEOUT = 10  # seconds, for the console TLS connect + CONNECT handshake
PROG = os.path.basename(sys.argv[0]) or "xsvm.py"


async def _optional(coro):
    """Await an optional XAPI call, swallowing failure — for cleanup/feature steps
    that legitimately no-op on some pool configs or VM states (e.g. removing a map
    key that isn't present)."""
    try:
        await coro
    except Exception:
        pass


# ───────────────────────────── create: pure pickers ─────────────────────────
def pick_sr(sr_recs, want_name, need_bytes):
    if want_name:
        for ref, sr in sr_recs.items():
            if sr.get("name_label") == want_name and sr.get("type") != "iso":
                return ref, sr.get("name_label")
        sys.exit(f"[sr] no writable SR named '{want_name}'")
    best = None
    for ref, sr in sr_recs.items():
        if sr.get("type") == "iso":
            continue
        try:
            free = int(sr.get("physical_size", "0")) - int(sr.get("physical_utilisation", "0"))
        except ValueError:
            continue
        if free >= need_bytes and (best is None or free > best[0]):
            best = (free, ref, sr.get("name_label"))
    if not best:
        sys.exit(f"[sr] no SR with >= {need_bytes // GiB} GiB free")
    return best[1], best[2]


def pick_network(net_recs, pif_recs, want_name):
    if want_name:
        for ref, n in net_recs.items():
            if n.get("name_label") == want_name:
                return ref, n.get("name_label")
        sys.exit(f"[net] no network named '{want_name}'")
    attached, mgmt = [], []
    for ref, n in net_recs.items():
        for pref in n.get("PIFs", []):
            p = pif_recs.get(pref, {})
            if p.get("currently_attached"):       # skips bonded-slave NICs (attached=False)
                (mgmt if p.get("management") else attached).append((ref, n.get("name_label")))
    chosen = mgmt or attached
    if not chosen:
        sys.exit("[net] no attachable network found (all PIFs detached?)")
    return chosen[0]


def find_iso(vdi_recs, name):
    for ref, vdi in vdi_recs.items():
        if vdi.get("name_label") == name:
            return ref
    sys.exit(f"[iso] ISO '{name}' not found in any ISO SR")


# ───────────────────────────── create: do the work ──────────────────────────
async def do_create(a):
    session = await connect_async(a.host, a.user, a.password)
    print(f"[login] OK ({a.host})")
    try:
        x = session.xenapi
        vm_recs, sr_recs, net_recs, pif_recs = await asyncio.gather(
            x.VM.get_all_records(), x.SR.get_all_records(),
            x.network.get_all_records(), x.PIF.get_all_records())
        vdi_recs = await x.VDI.get_all_records() if a.iso else {}

        tref = next((r for r, v in vm_recs.items()
                     if v.get("is_a_template") and v.get("name_label") == a.template), None)
        if not tref:
            sys.exit(f"[template] '{a.template}' not found")
        srref, srname = pick_sr(sr_recs, a.sr, a.disk_gib * GiB)
        nref, nname = pick_network(net_recs, pif_recs, a.network)
        isoref = find_iso(vdi_recs, a.iso) if a.iso else None
        want_vtpm = a.vtpm if a.vtpm is not None else ("windows 11" in a.template.lower())

        vm = await x.VM.clone(tref, a.name)
        await x.VM.set_is_a_template(vm, False)
        await x.VM.set_name_description(
            vm, f"{a.vcpus} vCPU / {a.memory_gib} GiB / {a.disk_gib} GiB; via xsvm.py")
        await x.VM.set_VCPUs_max(vm, str(a.vcpus))
        await x.VM.set_VCPUs_at_startup(vm, str(a.vcpus))
        mem = str(a.memory_gib * GiB)
        await x.VM.set_memory_limits(vm, mem, mem, mem, mem)
        await _optional(x.VM.remove_from_other_config(vm, "disks"))
        print(f"[vm] {a.name}: {a.vcpus} vCPU, {a.memory_gib} GiB RAM")

        vdi = await x.VDI.create({
            "name_label": f"{a.name} system", "name_description": "", "SR": srref,
            "virtual_size": str(a.disk_gib * GiB), "type": "user",
            "sharable": False, "read_only": False, "other_config": {}, "sm_config": {}, "tags": [],
        })
        await x.VBD.create({
            "VM": vm, "VDI": vdi, "userdevice": "0", "bootable": True, "mode": "RW",
            "type": "Disk", "empty": False, "other_config": {},
            "qos_algorithm_type": "", "qos_algorithm_params": {},
        })
        print(f"[vm] disk {a.disk_gib} GiB on '{srname}'")

        await x.VIF.create({
            "device": "0", "network": nref, "VM": vm, "MAC": "", "MTU": "1500",
            "other_config": {}, "qos_algorithm_type": "", "qos_algorithm_params": {},
        })
        print(f"[vm] VIF on '{nname}'")

        if want_vtpm:
            try:
                if not await x.VM.get_VTPMs(vm):
                    await x.VTPM.create(vm, False)
                print("[vm] vTPM present")
            except Exception as e:
                print(f"[vm] WARNING: vTPM not added ({e})")

        if a.usb_tablet:
            await x.VM.add_to_platform(vm, "usb", "true")
            await x.VM.add_to_platform(vm, "usb_tablet", "true")
            print("[vm] usb_tablet=true (absolute pointer)")

        if isoref:
            await x.VBD.create({
                "VM": vm, "VDI": isoref, "userdevice": "3", "bootable": False, "mode": "RO",
                "type": "CD", "empty": False, "other_config": {},
                "qos_algorithm_type": "", "qos_algorithm_params": {},
            })
            await _optional(x.VM.remove_from_HVM_boot_params(vm, "order"))
            # disk first: an empty disk falls through to CD for the first boot, so reboots
            # after setup land on the disk instead of re-running the installer.
            await x.VM.add_to_HVM_boot_params(vm, "order", "cd")
            print(f"[vm] ISO '{a.iso}' attached, boot disk->CD (avoids install reboot loop)")

        uuid = await x.VM.get_uuid(vm)
        print(f"[vm] uuid={uuid}")

        if not a.start:
            print(f"\n[next] created (not started). Start it, then open the console with:\n"
                  f"  xsvm console --vm-uuid {uuid} --host {a.host}")
            return
        await x.VM.start(vm, False, False)
        print("[vm] STARTED")
        print(f"\n[next] open the console with:\n"
              f"  uv run {PROG} console --vm-uuid {uuid} --host {a.host}")
    finally:
        await session.logout()


# ───────────────────────────── console: lookup ──────────────────────────────
async def resolve_rfb(xenapi, vm_uuid=None, vm_name=None):
    """A VM's CURRENT rfb (VNC) console location — avoids stale console uuids,
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


async def console_lookup(login_host, a):
    """Log in, resolve the console location, return (session_ref, location). The
    server-side session is left logged IN (no logout) so the sync relay can reuse it."""
    session = await connect_async(login_host, a.user, a.password)
    print("[login] OK")
    if a.location:
        location = a.location
    elif a.vm_uuid or a.vm_name:
        location = await resolve_rfb(session.xenapi, vm_uuid=a.vm_uuid, vm_name=a.vm_name)
        print(f"[lookup] current rfb console: {location}")
    else:
        sys.exit("provide --vm-uuid, --vm-name, or --location")
    ref = await session_ref_for_relay(session)   # keep ref valid; release HTTP, no logout
    return ref, location


# ───────────────────────────── console: relay ───────────────────────────────
def open_console(location, session):
    """TLS-connect to the console host, send CONNECT with session_id, read past the
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


def serve_relay(location, session_ref, local_port):
    preflight(location, session_ref)
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(('127.0.0.1', local_port))
    ls.listen(1)
    print(f"ready -> open vnc://localhost:{local_port}"
          f'   (or: open -a "Screen Sharing" vnc://localhost:{local_port})')
    while True:
        c, _ = ls.accept()
        threading.Thread(target=handle, args=(c, location, session_ref), daemon=True).start()


# ───────────────────────────────── cli ──────────────────────────────────────
def main():
    load_env_files()  # .env: real env > .env file > defaults

    p = argparse.ArgumentParser(description="Create XenServer VMs and open their VNC console.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--host', default=os.environ.get("XS_HOST"), help="XS host (default $XS_HOST)")
    common.add_argument('--user', default=os.environ.get("XS_USER", "root"))
    common.add_argument('--password', default=os.environ.get("XS_PASSWORD"))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", parents=[common], help="create + start a VM, then show how to open its console")
    c.add_argument('--name', required=True, help="name-label for the new VM")
    c.add_argument('--template', default="Windows 11", help="source template name")
    c.add_argument('--iso', help="install ISO name to attach as CD")
    c.add_argument('--vcpus', type=int, default=2)
    c.add_argument('--memory-gib', type=int, default=4)
    c.add_argument('--disk-gib', type=int, default=40)
    c.add_argument('--sr', help="SR name for the disk (default: largest writable SR that fits)")
    c.add_argument('--network', help="network name (default: auto — an attached, non-slave NIC)")
    c.add_argument('--vtpm', dest='vtpm', action='store_true', default=None,
                   help="force-add a vTPM (default: auto for Windows 11)")
    c.add_argument('--no-vtpm', dest='vtpm', action='store_false')
    c.add_argument('--no-usb-tablet', dest='usb_tablet', action='store_false', default=True)
    c.add_argument('--no-start', dest='start', action='store_false', default=True)

    v = sub.add_parser("console", parents=[common], help="relay a VM's VNC console to a local port")
    v.add_argument('--vm-uuid', help="VM uuid; resolves its CURRENT rfb console (survives restarts)")
    v.add_argument('--vm-name', help="VM name-label; resolves its current rfb console")
    v.add_argument('--location', help="explicit console URL https://HOST/console?uuid=...")
    v.add_argument('--local-port', type=int, default=int(os.environ.get("XS_LOCAL_PORT", "5901")))

    a = p.parse_args()

    if a.cmd == "console" and not (a.vm_uuid or a.vm_name or a.location):
        p.error("provide --vm-uuid, --vm-name, or --location")
    # console may derive the host from --location; create always needs --host.
    login_host = a.host or (urllib.parse.urlparse(a.location).hostname
                            if getattr(a, "location", None) else None)
    if not login_host:
        p.error("need a host: pass --host/$XS_HOST"
                + (" or a full --location" if a.cmd == "console" else ""))
    a.password = a.password or getpass.getpass(f"{a.user}@{login_host} password: ")

    if a.cmd == "create":
        asyncio.run(do_create(a))
    else:
        session_ref, location = asyncio.run(console_lookup(login_host, a))
        serve_relay(location, session_ref, a.local_port)


if __name__ == '__main__':
    main()
