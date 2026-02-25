/**
 * Async XenAPI session via JSON-RPC
 *
 * Usage mirrors the Python XenAPI:
 *
 *   const session = new AsyncXenAPISession("https://host-ip");
 *   await session.login_with_password("root", "password");
 *
 *   const vms = await session.xenapi.VM.get_all();
 *   for (const vm of vms) {
 *     const record = await session.xenapi.VM.get_record(vm);
 *     console.log(record.name_label);
 *   }
 *
 *   await session.logout();
 */

// ---------------------------------------------------------------------------
// JSON-RPC helpers
// ---------------------------------------------------------------------------

// XenServer typically uses self-signed certificates — skip TLS verification
// (mirrors Python's ssl.CERT_NONE behaviour).
process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";

type JsonRpcResponse = { result?: unknown; error?: unknown };

function _jsonrpcReq(method: string, params: unknown[]): object {
    return { jsonrpc: "2.0", method, params, id: crypto.randomUUID() };
}

async function _post(url: string, payload: object): Promise<JsonRpcResponse> {
    const response = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    return response.json() as Promise<JsonRpcResponse>;
}

// ---------------------------------------------------------------------------
// Async XenAPI proxy
// ---------------------------------------------------------------------------

/**
 * Recursive type describing the xenapi namespace proxy.
 * Every property access returns another XenApiProxy;
 * invoking it dispatches an authenticated JSON-RPC call.
 */
export type XenApiProxy = ((...args: unknown[]) => Promise<unknown>) & {
    readonly [key: string]: XenApiProxy;
};

// Accumulates dotted property access (e.g. xenapi.VM.get_all) and dispatches
// the final call as an authenticated JSON-RPC request.
function _xenApiNamespace(session: AsyncXenAPISession, path: string[] = []): XenApiProxy {
    return new Proxy(() => {}, {
        get(_target, property: string) {
            return _xenApiNamespace(session, path.concat(property));
        },
        apply(_target, _self, args) {
            return session._call(path.join("."), args as unknown[]);
        },
    }) as unknown as XenApiProxy;
}

export class AsyncXenAPISession {
    private readonly _url: string;
    private _sessionRef: string | undefined;
    public readonly xenapi: XenApiProxy;

    constructor(url: string | undefined) {
        this._url = `${url}/jsonrpc`;
        this.xenapi = _xenApiNamespace(this);
    }

    async login_with_password(user: string, password: string): Promise<string> {
        const ret = await _post(
            this._url,
            _jsonrpcReq("session.login_with_password", [user, password, "version", "originator"]),
        );
        if (ret.error) throw new Error(`Login failed: ${JSON.stringify(ret.error)}`);
        this._sessionRef = ret.result as string;
        return this._sessionRef;
    }

    async logout(): Promise<void> {
        if (this._sessionRef) {
            const ret = await _post(this._url, _jsonrpcReq("session.logout", [this._sessionRef]));
            if (ret.error) throw new Error(`Logout failed: ${JSON.stringify(ret.error)}`);
            this._sessionRef = undefined;
        }
    }

    async _call(method: string, params: unknown[] = []): Promise<unknown> {
        if (!this._sessionRef) throw new Error("Not logged in");
        const ret = await _post(this._url, _jsonrpcReq(method, [this._sessionRef, ...params]));
        if (ret.error) throw new Error(`XAPI ${method} failed: ${JSON.stringify(ret.error)}`);
        return ret.result;
    }
}
