# async-xenapi — examples

`xsvm.py` — a runnable, real-world tool built on `async-xenapi`: create XenServer
VMs and open their VNC console.

| Subcommand | What it does |
|------------|--------------|
| `xsvm create` | Clone a template and create + start a VM: CPU/RAM/disk sizing, auto-pick SR & network (skips bonded-slave NICs), attach an install ISO, add a vTPM for Windows 11, set the USB-tablet pointer and disk-first boot. Reads the inventory concurrently with `asyncio.gather`. Prints the next command to open the console. |
| `xsvm console` | Bridge a VM's VNC console to a local TCP port. Logs in via async-xenapi for the `session_id`, then runs a plain sync `CONNECT` relay so a local VNC client (TigerVNC, macOS Screen Sharing, noVNC) can attach. Resolve the VM by `--vm-uuid`, `--vm-name`, or a raw `--location`. |

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

Run `xsvm.py create -h` / `xsvm.py console -h` for the full options.
