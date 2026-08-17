#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["async-xenapi"]
# ///
"""xscert.py — authenticate to XenServer with a TLS client certificate, built on async-xenapi.

Subcommands:
  setup     mint a CA + client certificate, install the CA as a pool trust anchor and
            switch the pool into client-certificate mode. The only step that writes to
            the pool, and the only one that needs a password.
  login     log in with the certificate ALONE — no password — and read the pool.
  probe     report which pool operations that certificate session may call.
  teardown  disable certificate auth and remove the trust anchor.

Scope: pool operations only. Against xen-api master the built-in `client-cert` role is
a pool-management role — it carries no VM lifecycle verbs beyond a handful of migration
helpers, so a cert session cannot start or stop a VM. See `probe` for the exact list.

The one line that makes this work is `ssl_context.load_cert_chain(cert, key)`: XenServer's
stunnel treats such a connection as client-cert authenticated, and XAPI then ignores the
credentials sent with session.login_with_password.

Config comes from .env (XS_HOST / XS_USER / XS_PASSWORD / XS_CERT_NAME / XS_CERT_DIR) or
flags; precedence is real env var > .env > default, and a CLI flag overrides all.

Requires: pip install async-xenapi  (or run via `uv run xscert.py …` — PEP 723 auto-installs).
"""
import argparse
import asyncio
import os
import ssl
import subprocess
import sys
from pathlib import Path

from async_xenapi import AsyncXenAPISession
from xs_common import connect_async, load_env_files

PROG = os.path.basename(sys.argv[0]) or "xscert.py"

KEY_BITS = "4096"
CERT_DAYS = "3650"

# A reference that cannot resolve. XAPI checks RBAC *before* it dereferences, so a
# call made with this ref reports whether the role may call the method at all,
# without the method ever acting on anything.
BAD_REF = "OpaqueRef:00000000-0000-0000-0000-000000000000"


# ───────────────────────────── what the role may call ────────────────────────
# Pool operations that xen-api master grants to the client-cert role, i.e. every
# `~allowed_roles:(… ++ _R_CLIENT_CERT)` in ocaml/idl/datamodel_pool.ml.
#
# Argument lists must match the datamodel exactly: XAPI raises
# MESSAGE_PARAMETER_COUNT_MISMATCH *before* the RBAC check, so a short list would
# tell us nothing about permissions.
POOL_PROBES = [
    # (method, args) — first argument is a pool ref, so a bad ref stops the call
    # dead once RBAC has passed.
    ("pool.set_repositories", [BAD_REF, []]),
    ("pool.add_repository", [BAD_REF, BAD_REF]),
    ("pool.remove_repository", [BAD_REF, BAD_REF]),
    ("pool.check_update_readiness", [BAD_REF, False]),
    ("pool.enable_client_certificate_auth", [BAD_REF, "probe-never-applied"]),
    ("pool.disable_client_certificate_auth", [BAD_REF]),
    ("pool.configure_repository_proxy", [BAD_REF, "", "", ""]),
    ("pool.disable_repository_proxy", [BAD_REF]),
    ("pool.install_trusted_certificate", [BAD_REF, True, "", []]),
    ("pool.uninstall_trusted_certificate", [BAD_REF, BAD_REF]),
    # Parameter list grew in 25.7.0; an older host answers MESSAGE_PARAMETER_COUNT_MISMATCH,
    # which the classifier reports as inconclusive rather than as a permission result.
    ("pool.sync_updates", [BAD_REF, False, "", "", "", ""]),
    # These two take no pool ref, so the guard has to be the value itself.
    # A name without a .pem suffix is rejected by certificates.ml:is_unsafe, which
    # runs after RBAC — so the call still reports permission, and installs nothing.
    ("pool.install_ca_certificate", ["probe-invalid-name", ""]),
    ("pool.uninstall_ca_certificate", ["async-xenapi-probe-absent.pem", False]),
]

# Granted to the role, but deliberately never called. Listed so the report covers
# every grant rather than quietly showing the convenient subset.
POOL_NOT_PROBED = [
    ("pool.enable_ha", "takes no pool ref — an allowed call would try to turn HA on"),
    ("pool.disable_ha", "takes NO arguments — an allowed call would turn HA off"),
    ("pool.sync_trusted_certificates_from", "versioned parameter list; arity varies by host"),
    ("pool.exchange_trusted_certificates_on_join", "internal, pool-join only"),
    ("pool.exchange_crls_on_join", "internal, pool-join only"),
]


# A few VM verbs shown alongside the pool results, to make the boundary of the
# role visible: master grants it pool management, not VM lifecycle. Arities are
# from datamodel_vm.ml — VM.start takes (self, start_paused, force).
VM_CONTRAST = [
    ("VM.start", [BAD_REF, False, False]),
    ("VM.clean_shutdown", [BAD_REF]),
    ("VM.hard_shutdown", [BAD_REF]),
    ("VM.pause", [BAD_REF]),
]


def classify(err: str) -> str:
    """Turn an XAPI error into a permission verdict."""
    if "RBAC_PERMISSION_DENIED" in err or "PERMISSION_DENIED" in err:
        return "DENIED  (RBAC)"
    if "HANDLE_INVALID" in err or "UUID_INVALID" in err:
        return "ALLOWED (passed RBAC, bad ref as expected)"
    # Reaching argument validation means the call already cleared RBAC.
    if "CERTIFICATE_NAME_INVALID" in err or "CERTIFICATE_DOES_NOT_EXIST" in err:
        return "ALLOWED (passed RBAC, rejected at validation)"
    if "MESSAGE_PARAMETER_COUNT_MISMATCH" in err:
        return "INCONCLUSIVE (wrong arity — fix the args in POOL_PROBES)"
    # Never guess "allowed" from an error we do not recognise: this table gets
    # quoted as evidence, so an unknown result has to look unknown.
    return f"UNKNOWN ({err.splitlines()[0][:60]})"


# ──────────────────────────────── certificates ───────────────────────────────

def run(*cmd: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        sys.exit(f"[{cmd[0]}] failed ({proc.returncode}): {proc.stderr.strip()}")


def ca_name(name: str) -> str:
    """XAPI rejects a trust-anchor name that does not end in .pem.

    certificates.ml:is_unsafe — the name must not start with '.', must end with
    '.pem' and must contain only safe characters, else the call fails with
    CERTIFICATE_NAME_INVALID.
    """
    return f"{name}-ca.pem"


def mint_certs(certdir: Path, name: str) -> None:
    """Create a self-signed CA and a client certificate whose CN/SAN is `name`.

    `name` must equal the pool's client_certificate_auth_name: stunnel enforces
    `checkHost = <name>` against the certificate the client presents.
    """
    certdir.mkdir(parents=True, exist_ok=True)
    ca_key, ca_crt = certdir / "ca.key", certdir / "ca.crt"
    cl_key, cl_csr, cl_crt = certdir / "client.key", certdir / "client.csr", certdir / "client.crt"

    print(f"[setup] minting CA and client certificate (CN={name}) in {certdir}")
    run("openssl", "genrsa", "-out", str(ca_key), KEY_BITS)
    run("openssl", "req", "-x509", "-new", "-key", str(ca_key), "-days", CERT_DAYS,
        "-subj", "/CN=async-xenapi-demo-ca", "-out", str(ca_crt))
    run("openssl", "genrsa", "-out", str(cl_key), KEY_BITS)
    run("openssl", "req", "-new", "-key", str(cl_key), "-subj", f"/CN={name}", "-out", str(cl_csr))
    # Modern TLS matches the SAN and falls back to the CN only when no SAN is
    # present, so set both rather than relying on the fallback.
    ext = certdir / "client.ext"
    ext.write_text(f"subjectAltName=DNS:{name}\nextendedKeyUsage=clientAuth\n")
    run("openssl", "x509", "-req", "-in", str(cl_csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
        "-CAcreateserial", "-days", CERT_DAYS, "-extfile", str(ext), "-out", str(cl_crt))
    for f in (ca_key, cl_key):
        f.chmod(0o600)
    print(f"[setup] wrote {ca_crt.name}, {cl_crt.name}, {cl_key.name}")


def cert_ssl_context(certdir: Path) -> ssl.SSLContext:
    """A context that presents our client certificate.

    Server verification stays off because a lab pool presents its own self-signed
    certificate. A production client would verify the pool instead of disabling it.
    """
    missing = [p.name for p in (certdir / "client.crt", certdir / "client.key") if not p.exists()]
    if missing:
        sys.exit(f"[cert] {', '.join(missing)} not found in {certdir} — run `{PROG} setup` first")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(certfile=str(certdir / "client.crt"), keyfile=str(certdir / "client.key"))
    return ctx


async def cert_session(host: str, certdir: Path) -> AsyncXenAPISession:
    """Log in presenting only the client certificate.

    The credentials are deliberately junk. On a cert-authenticated connection XAPI
    skips password verification and assigns the client-cert role, so the login
    succeeding is itself the result.
    """
    session = AsyncXenAPISession(f"https://{host}", ssl_context=cert_ssl_context(certdir))
    try:
        await session.login_with_password("not-a-real-user", "not-a-real-password")
    except RuntimeError as e:
        if "SESSION_AUTHENTICATION_FAILED" not in str(e):
            raise
        # stunnel's [xapi] service sets `redirect = 80`, so a certificate it refuses
        # is not rejected at the TLS layer: the connection is quietly re-pointed at
        # the password path, and the junk credentials then fail. The error says
        # "authentication failed" when the real cause is "certificate not accepted".
        await session.logout()
        sys.exit(
            "[login] the certificate was NOT accepted (XAPI fell back to password auth).\n"
            "  - does the pool's client_certificate_auth_name equal the cert CN/SAN?\n"
            "  - was the CA replaced without restarting stunnel?\n"
            f"      ssh root@{host} systemctl restart stunnel@xapi\n"
            "  - on the host, `grep 'Rejected by CERT' /var/log/secure` gives the real reason"
        )
    return session


# ───────────────────────────────── subcommands ───────────────────────────────

async def drop_anchor(session, name: str, tag: str) -> None:
    """Remove the trust anchor. Absent is the normal case, anything else is real."""
    try:
        await session.xenapi.pool.uninstall_ca_certificate(ca_name(name), False)
        print(f"[{tag}] removed {ca_name(name)}")
    except RuntimeError as e:
        if "CERTIFICATE_DOES_NOT_EXIST" not in str(e):
            raise
        print(f"[{tag}] no existing {ca_name(name)} to remove")


async def cmd_setup(args) -> None:
    certdir = Path(args.certdir)
    mint_certs(certdir, args.name)
    ca_pem = (certdir / "ca.crt").read_text()

    session = await connect_async(args.host, args.user, args.password)
    try:
        pool = (await session.xenapi.pool.get_all())[0]
        # mint_certs() makes a NEW CA every run, so a stale anchor would leave the
        # pool trusting a CA that did not sign this certificate.
        await drop_anchor(session, args.name, "setup")
        print("[setup] installing the CA as a pool trust anchor")
        await session.xenapi.pool.install_ca_certificate(ca_name(args.name), ca_pem)
        print(f"[setup] enabling client-certificate auth, checkHost name = {args.name}")
        await session.xenapi.pool.enable_client_certificate_auth(pool, args.name)
        current = await session.xenapi.pool.get_client_certificate_auth_name(pool)
        print(f"[setup] pool client_certificate_auth_name = {current!r}")
    finally:
        await session.logout()

    print(
        "[setup] done. Restart stunnel before logging in — XAPI reloads it only when\n"
        "        the accept address or checkHost name changes, so a replaced CA sits\n"
        f"        unused until:  ssh root@{args.host} systemctl restart stunnel@xapi"
    )


async def cmd_login(args) -> None:
    session = await cert_session(args.host, Path(args.certdir))
    try:
        print("[login] authenticated with the client certificate — no password was used")
        record = await session.xenapi.session.get_record(session.session_ref)
        print(f"[login]   auth_user_name     : {record.get('auth_user_name')!r}")
        print(f"[login]   subject            : {record.get('subject')!r}"
              "   <- Ref.null: no accountable identity")
        print(f"[login]   client_certificate : {record.get('client_certificate')}")
        print(f"[login]   is_local_superuser : {record.get('is_local_superuser')}")

        # The client-cert role sits above read-only in the role order, and every
        # getter is _R_READ_ONLY, so reads work without any explicit grant.
        pools = await session.xenapi.pool.get_all_records()
        for ref, pool in pools.items():
            print(f"[login] pool read: {pool.get('name_label')!r} "
                  f"(cert auth name {pool.get('client_certificate_auth_name')!r})")
    finally:
        await session.logout()


async def probe_one(session, method: str, call_args: list) -> str:
    cls, name = method.split(".", 1)
    try:
        await getattr(getattr(session.xenapi, cls), name)(*call_args)
    except RuntimeError as e:
        return classify(str(e))
    # Nothing here should succeed: every probe is armed with an argument that
    # cannot resolve. A success means the guard failed, not that the call is fine.
    return "UNEXPECTED SUCCESS — the probe argument resolved; check POOL_PROBES"


async def cmd_probe(args) -> None:
    session = await cert_session(args.host, Path(args.certdir))
    try:
        print("\nPool operations granted to the client-cert role by xen-api master:")
        for method, call_args in POOL_PROBES:
            print(f"  {method:<42} {await probe_one(session, method, call_args)}")

        print("\nGranted, but not called by this probe:")
        for method, why in POOL_NOT_PROBED:
            print(f"  {method:<42} SKIPPED ({why})")

        print("\nVM lifecycle, for contrast — the role does NOT carry these:")
        for method, call_args in VM_CONTRAST:
            print(f"  {method:<42} {await probe_one(session, method, call_args)}")
    finally:
        await session.logout()


async def cmd_teardown(args) -> None:
    session = await connect_async(args.host, args.user, args.password)
    try:
        print("[teardown] disabling client-certificate auth")
        pool = (await session.xenapi.pool.get_all())[0]
        await session.xenapi.pool.disable_client_certificate_auth(pool)
        await drop_anchor(session, args.name, "teardown")
    finally:
        await session.logout()


# ─────────────────────────────────── plumbing ────────────────────────────────

def main() -> None:
    load_env_files()
    parser = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, handler, needs in (
        ("setup", cmd_setup, ("certdir", "name", "password")),
        ("login", cmd_login, ("certdir",)),
        ("probe", cmd_probe, ("certdir",)),
        ("teardown", cmd_teardown, ("name", "password")),
    ):
        sp = sub.add_parser(name, help=handler.__doc__)
        sp.add_argument("--host", default=os.environ.get("XS_HOST"),
                        help="XenServer host or pool master (default: $XS_HOST)")
        if "certdir" in needs:
            sp.add_argument("--certdir", default=os.environ.get("XS_CERT_DIR", "./certs"),
                            help="where the CA and client certificate live (default: ./certs)")
        if "name" in needs:
            sp.add_argument("--name", default=os.environ.get("XS_CERT_NAME", "async-xenapi-demo"),
                            help="checkHost name / certificate CN (default: async-xenapi-demo)")
        if "password" in needs:
            sp.add_argument("--user", default=os.environ.get("XS_USER", "root"))
            sp.add_argument("--password", default=os.environ.get("XS_PASSWORD"))
        sp.set_defaults(func=handler)

    args = parser.parse_args()
    if not args.host:
        sys.exit(f"set XS_HOST in .env or pass --host (see `{PROG} {args.cmd} -h`)")
    if getattr(args, "password", "unused") is None:
        sys.exit(f"`{PROG} {args.cmd}` needs a password: set XS_PASSWORD in .env or pass --password")

    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
