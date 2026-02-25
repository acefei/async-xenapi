import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { AsyncXenAPISession } from "../src/XenAPI";

describe("getXapiVersion", () => {
    let session: AsyncXenAPISession;

    beforeAll(async () => {
        const { HOST_URL, USERNAME, PASSWORD } = process.env;
        if (!HOST_URL || !USERNAME || !PASSWORD) {
            throw new Error("HOST_URL, USERNAME, PASSWORD must be set in .env");
        }

        console.log(`Login ${HOST_URL} with ${USERNAME}`);
        session = new AsyncXenAPISession(HOST_URL);
        await session.login_with_password(USERNAME, PASSWORD);
        console.log("Login successfully");
    });

    afterAll(async () => {
        if (session) {
            await session.logout();
            console.log("Session Logout.");
        }
    });

    it("should return a valid XAPI version", async () => {
        const pool = await session.xenapi.pool.get_all();
        const host = await session.xenapi.pool.get_master(pool[0]);
        const [major, minor] = await Promise.all([
            session.xenapi.host.get_API_version_major(host),
            session.xenapi.host.get_API_version_minor(host),
        ]);
        const version = `${major}.${minor}`;

        console.log(`Current XAPI Version: ${version}`);
        expect(version).toMatch(/^\d+\.\d+$/);
    });

    it("should list hosts", async () => {
        const records = (await session.xenapi.host.get_all_records()) as Record<
            string,
            Record<string, unknown>
        >;
        const names = Object.values(records).map((r) => r.name_label);
        console.log(`Hosts:\n${names.map((n) => `  - ${n}`).join("\n")}`);
        expect(Object.keys(records).length).toBeGreaterThan(0);
    });

    it("should list VMs", async () => {
        const records = (await session.xenapi.VM.get_all_records()) as Record<
            string,
            Record<string, unknown>
        >;
        const vmInfo = Object.values(records)
            .filter((r) => !r.is_a_template && !r.is_a_snapshot)
            .map((r) => `  - ${r.name_label} (${r.power_state})`);
        console.log(`VMs:\n${vmInfo.join("\n")}`);
        expect(vmInfo.length).toBeGreaterThan(0);
    });

    it("should list storage repositories", async () => {
        const records = (await session.xenapi.SR.get_all_records()) as Record<
            string,
            Record<string, unknown>
        >;
        const srInfo = Object.values(records).map((r) => `  - ${r.name_label} (type: ${r.type})`);
        console.log(`Storage Repositories:\n${srInfo.join("\n")}`);
        expect(Object.keys(records).length).toBeGreaterThan(0);
    });

    it("should list networks", async () => {
        const records = (await session.xenapi.network.get_all_records()) as Record<
            string,
            Record<string, unknown>
        >;
        const names = Object.values(records).map((r) => r.name_label);
        console.log(`Networks:\n${names.map((n) => `  - ${n}`).join("\n")}`);
        expect(Object.keys(records).length).toBeGreaterThan(0);
    });
});
