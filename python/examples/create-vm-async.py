#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["async-xenapi"]
# ///
"""create-vm-async.py — create (and start) a VM on a XenServer host using the
async-xenapi library (https://github.com/acefei/async-xenapi).

It clones a template, sizes CPU/RAM/disk, attaches a network and optional install
ISO, adds a vTPM for Windows 11, enables the USB-tablet absolute pointer, and starts
the VM — using `await session.xenapi.<Class>.<method>(...)` and fetching the inventory
concurrently with asyncio.gather() (the library's recommended idiom).

Requirements:
  - pip install async-xenapi
  - Verified working on Python 3.11 (the README says 3.12+, but older 3.x runs fine).
  - The constructor is AsyncXenAPISession(url) and does NOT verify the host's
    self-signed TLS cert, so no extra SSL option is needed (confirmed against a live host).

Examples:
  ./create-vm-async.py --name win11-24h2 --template "Windows 11" \
      --iso win11v24h2-x64_uefi.iso --vcpus 4 --memory-gib 32 --disk-gib 512 \
      --host 10.71.56.204
  ./create-vm-async.py --name srv --template "Windows Server 2022 (64-bit)" \
      --iso ws22-x64.iso          # host/user/password from .env
"""
import argparse, asyncio, getpass, os, sys

from xs_common import connect_async, load_env_files

GiB = 1024**3


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


async def create_vm(session, a, here):
    x = session.xenapi

    # Inventory reads, concurrently (the library's recommended pattern).
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

    # clone + basic config
    vm = await x.VM.clone(tref, a.name)
    await x.VM.set_is_a_template(vm, False)
    await x.VM.set_name_description(
        vm, f"{a.vcpus} vCPU / {a.memory_gib} GiB / {a.disk_gib} GiB; via create-vm-async.py")
    await x.VM.set_VCPUs_max(vm, str(a.vcpus))
    await x.VM.set_VCPUs_at_startup(vm, str(a.vcpus))
    mem = str(a.memory_gib * GiB)
    await x.VM.set_memory_limits(vm, mem, mem, mem, mem)
    try:
        await x.VM.remove_from_other_config(vm, "disks")
    except Exception:
        pass
    print(f"[vm] {a.name}: {a.vcpus} vCPU, {a.memory_gib} GiB RAM")

    # disk
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

    # network
    await x.VIF.create({
        "device": "0", "network": nref, "VM": vm, "MAC": "", "MTU": "1500",
        "other_config": {}, "qos_algorithm_type": "", "qos_algorithm_params": {},
    })
    print(f"[vm] VIF on '{nname}'")

    # vTPM (Windows 11)
    if want_vtpm:
        try:
            if not await x.VM.get_VTPMs(vm):
                await x.VTPM.create(vm, False)
            print("[vm] vTPM present")
        except Exception as e:
            print(f"[vm] WARNING: vTPM not added ({e})")

    # absolute pointer for a usable console mouse
    if a.usb_tablet:
        await x.VM.add_to_platform(vm, "usb", "true")
        await x.VM.add_to_platform(vm, "usb_tablet", "true")
        print("[vm] usb_tablet=true (absolute pointer)")

    # install ISO + boot order
    if isoref:
        await x.VBD.create({
            "VM": vm, "VDI": isoref, "userdevice": "3", "bootable": False, "mode": "RO",
            "type": "CD", "empty": False, "other_config": {},
            "qos_algorithm_type": "", "qos_algorithm_params": {},
        })
        try:
            await x.VM.remove_from_HVM_boot_params(vm, "order")
        except Exception:
            pass
        # disk first: an empty disk falls through to the CD for the first boot, so
        # reboots *after* setup land on the disk instead of re-running the installer.
        await x.VM.add_to_HVM_boot_params(vm, "order", "cd")
        print(f"[vm] ISO '{a.iso}' attached, boot disk->CD (avoids install reboot loop)")

    uuid = await x.VM.get_uuid(vm)
    print(f"[vm] uuid={uuid}")

    if not a.start:
        print("[vm] created (not started; --no-start)")
        return
    try:
        await x.VM.start(vm, False, False)
        print("[vm] STARTED")
    except Exception as e:
        sys.exit(f"[vm] created but FAILED to start: {e}")

    for cref in await x.VM.get_consoles(vm):
        c = await x.console.get_record(cref)
        if c.get("protocol") == "rfb":
            print(f"[console] {c.get('location')}")
    print(f"\nConnect the console with:\n"
          f"  python3 {os.path.join(here, 'vnc-direct.py')} --vm-uuid {uuid} --host {a.host}")


async def amain():
    here = os.path.dirname(os.path.abspath(__file__))
    load_env_files()  # .env: real env > .env file > defaults

    p = argparse.ArgumentParser(description="Create a VM on a XenServer host via async-xenapi.")
    p.add_argument('--name', required=True, help="name-label for the new VM")
    p.add_argument('--template', default="Windows 11", help="source template name")
    p.add_argument('--iso', help="install ISO name to attach as CD")
    p.add_argument('--vcpus', type=int, default=2)
    p.add_argument('--memory-gib', type=int, default=4)
    p.add_argument('--disk-gib', type=int, default=40)
    p.add_argument('--sr', help="SR name for the disk (default: largest writable SR that fits)")
    p.add_argument('--network', help="network name (default: auto — an attached, non-slave NIC)")
    p.add_argument('--host', default=os.environ.get("XS_HOST"), help="XS host (default $XS_HOST)")
    p.add_argument('--user', default=os.environ.get("XS_USER", "root"))
    p.add_argument('--password', default=os.environ.get("XS_PASSWORD"))
    p.add_argument('--vtpm', dest='vtpm', action='store_true', default=None,
                   help="force-add a vTPM (default: auto for Windows 11)")
    p.add_argument('--no-vtpm', dest='vtpm', action='store_false')
    p.add_argument('--no-usb-tablet', dest='usb_tablet', action='store_false', default=True)
    p.add_argument('--no-start', dest='start', action='store_false', default=True)
    a = p.parse_args()

    if not a.host:
        p.error("need a host: pass --host or set XS_HOST in .env")
    pw = a.password or getpass.getpass(f"{a.user}@{a.host} password: ")

    session = await connect_async(a.host, a.user, pw)
    print(f"[login] OK ({a.host})")
    try:
        await create_vm(session, a, here)
    finally:
        await session.logout()


if __name__ == '__main__':
    asyncio.run(amain())
