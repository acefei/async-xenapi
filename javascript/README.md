# async-xenapi (JavaScript / TypeScript)

An async JavaScript/TypeScript library for [XenAPI](https://xapi-project.github.io/xen-api).

## Install

```
npm install async-xenapi
```

Requires **Node.js 24+** (uses the global `fetch` API).

## Usage

```typescript
import { AsyncXenAPISession } from "async-xenapi";

async function main() {
  const session = new AsyncXenAPISession(process.env.HOST_URL);
  try {
    await session.login_with_password(process.env.USERNAME, process.env.PASSWORD);
    const hosts = await session.xenapi.host.get_all();
    console.log(hosts);
  } finally {
    await session.logout();
  }
}

main();
```

The API mirrors the Python XenAPI SDK — any dotted method path under `session.xenapi` is translated directly to the corresponding JSON-RPC call. See the [XenAPI Reference](https://xapi-project.github.io/xen-api) for all available classes and fields.

## Best Practices

### 1. Use `get_all_records()` instead of N+1 queries

The single biggest performance win. Instead of fetching a list of refs then querying each one individually, fetch everything in one call:

```typescript
// SLOW — N+1 round-trips (1 for get_all + N for each getter)
const vms = await session.xenapi.VM.get_all();
for (const vm of vms as string[]) {
  const name = await session.xenapi.VM.get_name_label(vm);
  console.log(name);
}

// FAST — 1 round-trip, returns { ref: { field: value, ... }, ... }
const records = await session.xenapi.VM.get_all_records() as Record<string, Record<string, unknown>>;
for (const rec of Object.values(records)) {
  if (!rec.is_a_template && !rec.is_a_snapshot) {
    console.log(`${rec.name_label} (${rec.power_state})`);
  }
}
```

This applies to every XenAPI class: `host`, `SR`, `network`, `VM`, `pool`, etc.

### 2. Use `Promise.all()` for independent calls

When you need results from multiple independent API calls, run them concurrently:

```typescript
// SLOW — sequential, each awaits the previous
const major = await session.xenapi.host.get_API_version_major(host);
const minor = await session.xenapi.host.get_API_version_minor(host);

// FAST — concurrent, both requests in flight at the same time
const [major, minor] = await Promise.all([
  session.xenapi.host.get_API_version_major(host),
  session.xenapi.host.get_API_version_minor(host),
]);
```

You can also gather across different classes:

```typescript
const [hosts, vms, networks] = await Promise.all([
  session.xenapi.host.get_all_records(),
  session.xenapi.VM.get_all_records(),
  session.xenapi.network.get_all_records(),
]);
```

### 3. Always clean up the session

Use `try/finally` to ensure `logout()` is called, which closes the server-side session:

```typescript
const session = new AsyncXenAPISession("https://xen-host");
await session.login_with_password("root", "password");
try {
  // ... your code ...
} finally {
  await session.logout();
}
```

### Key Takeaways

| Pattern                      | Calls        | Approach             |
|------------------------------|--------------|----------------------|
| List objects with fields     | 1            | `get_all_records()`  |
| Multiple independent values  | N concurrent | `Promise.all()`      |
| Lookup one field for one ref | 1            | `get_<field>(ref)`   |

## Run Tests

```
git clone git@github.com:acefei/async-xenapi.git
cd async-xenapi
cp .env.example .env  # then edit .env with your credentials
cd javascript
npm install
npm test
```

## License

LGPL-2.1-only
