# async-xenapi — examples

Runnable, real-world tools built on `async-xenapi`.

| Script | What it does |
|--------|--------------|
| `create-vm-async.py` | Clone a template and create + start a VM: CPU/RAM/disk sizing, auto-pick SR & network (skips bonded-slave NICs), attach an install ISO, add a vTPM for Windows 11, set the USB-tablet pointer and disk-first boot. Reads the inventory concurrently with `asyncio.gather`. |
| `vnc-direct.py` | Bridge a VM's VNC console to a local TCP port. Logs in via async-xenapi to obtain the `session_id`, then runs a plain sync `CONNECT` relay so a local VNC client (TigerVNC, macOS Screen Sharing, noVNC) can attach. |
| `xs_common.py` | Shared helper both import: async login (`connect_async`, with HOST_IS_SLAVE redirect), `.env` loading, and the self-signed TLS context. |

## Run

```
cp .env.example .env          # set XS_HOST / XS_USER / XS_PASSWORD
pip install async-xenapi      # or just `uv run create-vm-async.py …` (PEP 723 auto-installs)

python create-vm-async.py --name win11 --template "Windows 11" \
    --iso win11v24h2-x64_uefi.iso --vcpus 4 --memory-gib 8 --disk-gib 64 --host <host>

python vnc-direct.py --vm-uuid <uuid> --host <host>
# then: open vnc://localhost:5901
```

Run any script with `--help` for the full option list.
