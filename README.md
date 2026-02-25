# async-xenapi

Async bindings for [XenAPI](https://xapi-project.github.io/xen-api) — available in both Python and JavaScript/TypeScript.

## Structure

```
async-xenapi/
├── python/          # Python async library (PyPI: async-xenapi)
│   ├── src/
│   ├── tests/
│   └── README.md
├── javascript/      # TypeScript async library (npm: async-xenapi)
│   ├── src/
│   ├── tests/
│   └── README.md
└── README.md
```

## Quick Start

**Python** — see [python/README.md](python/README.md)

```python
from async_xenapi import AsyncXenAPISession
import asyncio

async def main():
    session = AsyncXenAPISession("https://xen-host")
    await session.login_with_password("root", "password")
    hosts = await session.xenapi.host.get_all()
    print(hosts)
    await session.logout()

asyncio.run(main())
```

**JavaScript / TypeScript** — see [javascript/README.md](javascript/README.md)

```typescript
import { AsyncXenAPISession } from "async-xenapi";

async function main() {
  const session = new AsyncXenAPISession(process.env.HOST_URL);
  await session.login_with_password(process.env.USERNAME, process.env.PASSWORD);
  const hosts = await session.xenapi.host.get_all();
  console.log(hosts);
  await session.logout();
}

main();
```

Both implementations follow the same API conventions as the official [XenAPI SDK](https://xapi-project.github.io/xen-api/usage.html).

## Best Practices

For detailed examples with code, see the language-specific READMEs:

- [Python best practices](python/README.md#best-practices)
- [JavaScript best practices](javascript/README.md#best-practices)

### Key Takeaways

1. **Prefer `get_all_records()` over `get_all()` + N×`get_field()`** — one round-trip instead of N+1.
2. **Use `asyncio.gather()` / `Promise.all()`** to fire independent calls concurrently — **~2x faster** than sequential.
3. **Always call `logout()`** in a `finally` block to release the server-side session.

## Benchmark

A benchmark suite compares the official sync [XenAPI SDK](https://pypi.org/project/XenAPI/) (XML-RPC) against async-xenapi (JSON-RPC over aiohttp):

```
make py-bench
```

Tested against a single XenServer host (33 VMs, 6 SRs, 4 networks):

| Step               | Sync XenAPI | Async XenAPI | Speedup  |
|--------------------|------------:|-------------:|---------:|
| login              |        1.5s |         1.3s |     1.1x |
| xapi_version       |        1.3s |         1.7s |     0.7x |
| hosts              |        1.9s |         1.5s |     1.2x |
| vms                |        3.8s |         3.9s |     1.0x |
| storage            |       628ms |        617ms |     1.0x |
| networks           |       315ms |        278ms |     1.1x |
| **all_concurrent** |    **3.2s** |     **1.7s** | **1.9x** |
| logout             |       313ms |        310ms |     1.0x |
| **TOTAL**          |   **12.8s** |    **11.3s** | **1.1x** |

For individual sequential calls, sync and async perform similarly — the bottleneck is network latency. The async advantage appears when firing multiple independent requests concurrently via `asyncio.gather()`, where `all_concurrent` (fetching hosts, VMs, SRs, and networks in parallel) runs **~2x faster** than the equivalent sequential sync calls.

## License

LGPL-2.1-only
