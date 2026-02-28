# async-xenapi (Python)

An async library for [XenAPI](https://xapi-project.github.io/xen-api).

## Install

```
pip install async-xenapi
```

Or with `uv`:

```
uv add async-xenapi
```

Requires **Python 3.12+**.

## Usage

```python
import asyncio
from async_xenapi import AsyncXenAPISession

async def main():
    session = AsyncXenAPISession("https://xen-host")
    await session.login_with_password("root", "password")
    try:
        vms = await session.xenapi.VM.get_all()
        for vm in vms:
            record = await session.xenapi.VM.get_record(vm)
            print(record["name_label"])
    finally:
        await session.logout()

asyncio.run(main())
```

Any dotted method path under `session.xenapi` maps directly to the corresponding JSON-RPC call.
See the [XenAPI Reference](https://xapi-project.github.io/xen-api) for all available classes and fields.

## Best Practices

### 1. Use `get_all_records()` instead of N+1 queries

The single biggest performance win. Instead of fetching a list of refs then querying each one individually, fetch everything in one call:

```python
# SLOW — N+1 round-trips (1 for get_all + N for each get_name_label)
vms = await session.xenapi.VM.get_all()
for vm in vms:
    name = await session.xenapi.VM.get_name_label(vm)
    print(name)

# FAST — 1 round-trip, returns {ref: {field: value, ...}, ...}
records = await session.xenapi.VM.get_all_records()
for ref, rec in records.items():
    if not rec["is_a_template"] and not rec["is_a_snapshot"]:
        print(f"{rec['name_label']} ({rec['power_state']})")
```

This applies to every XenAPI class: `host`, `SR`, `network`, `VM`, `pool`, etc.

### 2. Use `asyncio.gather()` for independent calls

When you need results from multiple independent API calls, run them concurrently:

```python
# SLOW — sequential, each awaits the previous
major = await session.xenapi.host.get_API_version_major(host)
minor = await session.xenapi.host.get_API_version_minor(host)

# FAST — concurrent, both requests in flight at the same time
major, minor = await asyncio.gather(
    session.xenapi.host.get_API_version_major(host),
    session.xenapi.host.get_API_version_minor(host),
)
```

You can also gather across different classes:

```python
hosts, vms, networks = await asyncio.gather(
    session.xenapi.host.get_all_records(),
    session.xenapi.VM.get_all_records(),
    session.xenapi.network.get_all_records(),
)
```

### 3. Always clean up the session

Use `try/finally` to ensure `logout()` is called, which closes both the server-side session and the underlying HTTP connection:

```python
session = AsyncXenAPISession("https://xen-host")
await session.login_with_password("root", "password")
try:
    # ... your code ...
finally:
    await session.logout()
```

### Key Takeaways

| Pattern                      | Calls        | Approach             |
|------------------------------|--------------|----------------------|
| List objects with fields     | 1            | `get_all_records()`  |
| Multiple independent values  | N concurrent | `asyncio.gather()`   |
| Lookup one field for one ref | 1            | `get_<field>(ref)`   |

## Run Tests

```
git clone git@github.com:acefei/async-xenapi.git
cd async-xenapi
cp .env.example .env  # then edit .env with your credentials
cd python
uv sync
uv run pytest tests/test_get_xapi_version.py -v
```

## License

LGPL-2.1-only
