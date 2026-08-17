# async-xenapi — examples

Two runnable, real-world tools built on `async-xenapi`.

## `xsvm.py` — create VMs and open their console

| Subcommand | What it does |
|------------|--------------|
| `xsvm create` | Clone a template and create + start a VM: CPU/RAM/disk sizing, auto-pick SR & network (skips bonded-slave NICs), attach an install ISO, add a vTPM for Windows 11, set the USB-tablet pointer and disk-first boot. Reads the inventory concurrently with `asyncio.gather`. Prints the next command to open the console. |
| `xsvm console` | Bridge a VM's VNC console to a local TCP port. Logs in via async-xenapi for the `session_id`, then runs a plain sync `CONNECT` relay so a local VNC client (TigerVNC, macOS Screen Sharing, noVNC) can attach. Resolve the VM by `--vm-uuid`, `--vm-name`, or a raw `--location`. |

## `xscert.py` — log in with a TLS client certificate

Authenticate with no password at all. The pool is told to trust a CA, and any client
presenting a certificate signed by it gets a session in XenServer's built-in
`client-cert` role.

| Subcommand | What it does |
|------------|--------------|
| `xscert setup` | Mint a CA and a client certificate, install the CA as a pool trust anchor (`pool.install_ca_certificate`) and switch the pool into certificate mode (`pool.enable_client_certificate_auth`). The only step that writes to the pool, and the only one that needs a password. |
| `xscert login` | Log in with the certificate alone and read the pool back. The credentials sent are deliberate junk — on a cert-authenticated connection XAPI ignores them. |
| `xscert probe` | Report which pool operations the resulting session may call, without changing anything. |
| `xscert teardown` | Disable certificate auth and remove the trust anchor. |

The whole mechanism is one line — `ssl_context.load_cert_chain(cert, key)`, handed to
the session via `AsyncXenAPISession(url, ssl_context=…)`.

**Scope: pool operations.** Against xen-api master the `client-cert` role is a pool
management role. It inherits read-only access everywhere (it sits above `read-only` in
the role order, and every getter is `_R_READ_ONLY`), and it is granted 18 pool
operations — certificates, repositories, updates and HA. It carries no VM lifecycle
verbs, so a cert session cannot start or stop a VM; `probe` prints a few for contrast.

`probe` never changes the pool. It calls each operation with a reference that cannot
resolve, because XAPI checks RBAC *before* it dereferences: `RBAC_PERMISSION_DENIED`
means the role may not call it, `HANDLE_INVALID` means it got past RBAC. Five of the 18
grants cannot be tested that way and are reported as SKIPPED with the reason — notably
`pool.disable_ha`, which takes no arguments at all, so an allowed call would simply turn
HA off.

`xs_common.py` is the shared helper: async login (`connect_async`, with HOST_IS_SLAVE
redirect), `.env` loading, the self-signed TLS context, and `session_ref_for_relay`.

## Run

```
cp .env.example .env          # set XS_HOST / XS_USER / XS_PASSWORD
pip install async-xenapi      # or just `uv run xsvm.py …` (PEP 723 auto-installs)

# create + start a VM, then it prints how to open the console
python xsvm.py create --name win11 --template "Windows 11" \
    --iso win11v24h2-x64_uefi.iso --vcpus 4 --memory-gib 8 --disk-gib 64 --host <host>

# open a VM's console (prints vnc://localhost:5901)
python xsvm.py console --vm-uuid <uuid> --host <host>
```

```
# certificate auth, end to end
python xscert.py setup                    # needs XS_PASSWORD; writes ./certs
ssh root@$XS_HOST systemctl restart stunnel@xapi   # see below — not optional
python xscert.py login                    # no password used
python xscert.py probe                    # what may this session call?
python xscert.py teardown                 # put the pool back
```

Restarting stunnel after `setup` is required: XAPI reloads it only when the accept
address or the checkHost name changes, so a replaced CA sits unused until you restart
it. Skip it and the new certificate is refused — and because the `[xapi]` stunnel
service sets `redirect = 80`, that refusal arrives as `SESSION_AUTHENTICATION_FAILED`,
looking like a wrong password rather than a TLS error. `grep 'Rejected by CERT'
/var/log/secure` on the host gives the real reason.

Run any subcommand with `-h` for the full options.
