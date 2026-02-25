"""Benchmark: sync XenAPI (official SDK) vs async-xenapi (aiohttp)

Runs the same 5 operations with both libraries and prints a comparison table.

Requires a live XenServer host.  Configure via .env at repo root:
    HOST_URL=https://<xen-host>
    USERNAME=root
    PASSWORD=<password>

Run with:
    uv run pytest tests/test_benchmark.py -v -s
"""

import asyncio
import os
import time

import pytest
import pytest_asyncio
import XenAPI

from async_xenapi import AsyncXenAPISession

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def host_url():
    url = os.environ.get("HOST_URL")
    if not url:
        pytest.skip("HOST_URL not set")
    return url


@pytest.fixture(scope="module")
def credentials():
    username = os.environ.get("USERNAME")
    password = os.environ.get("PASSWORD")
    if not username or not password:
        pytest.skip("USERNAME or PASSWORD not set")
    return username, password


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STEPS = [
    "login",
    "xapi_version",
    "hosts",
    "vms",
    "storage",
    "networks",
    "all_concurrent",
    "logout",
]


def _fmt_duration(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds >= 1 else f"{seconds * 1000:.0f}ms"


def _print_table(sync_times: dict[str, float], async_times: dict[str, float]):
    """Print a markdown-style comparison table."""
    print("\n" + "=" * 62)
    print(f"{'Step':<22} {'Sync XenAPI':>12} {'Async XenAPI':>14} {'Speedup':>10}")
    print("-" * 62)
    for step in STEPS:
        st = sync_times.get(step, 0)
        at = async_times.get(step, 0)
        speedup = f"{st / at:.1f}x" if at > 0 else "—"
        print(f"  {step:<20} {_fmt_duration(st):>12} {_fmt_duration(at):>14} {speedup:>10}")
    s_total = sum(sync_times.values())
    a_total = sum(async_times.values())
    total_speedup = f"{s_total / a_total:.1f}x" if a_total > 0 else "—"
    print("-" * 62)
    print(
        f"  {'TOTAL':<20} {_fmt_duration(s_total):>12} {_fmt_duration(a_total):>14} {total_speedup:>10}"
    )
    print("=" * 62)


# ---------------------------------------------------------------------------
# Sync XenAPI benchmark
# ---------------------------------------------------------------------------


class TestSyncXenAPI:
    """Run operations using the official sync XenAPI SDK (xmlrpc)."""

    @pytest.fixture(autouse=True, scope="class")
    def setup_session(self, host_url, credentials):
        username, password = credentials
        self.__class__.timings: dict[str, float] = {}

        t0 = time.perf_counter()
        self.__class__.session = XenAPI.Session(host_url, ignore_ssl=True)
        self.__class__.session.login_with_password(username, password, "1.0", "benchmark")
        self.__class__.timings["login"] = time.perf_counter() - t0
        print(f"\n[sync] Login {host_url} — {_fmt_duration(self.__class__.timings['login'])}")
        yield
        t0 = time.perf_counter()
        self.__class__.session.xenapi.session.logout()
        self.__class__.timings["logout"] = time.perf_counter() - t0
        print(f"[sync] Logout — {_fmt_duration(self.__class__.timings['logout'])}")

    def test_xapi_version(self):
        t0 = time.perf_counter()
        pool = self.session.xenapi.pool.get_all()
        host = self.session.xenapi.pool.get_master(pool[0])
        major = self.session.xenapi.host.get_API_version_major(host)
        minor = self.session.xenapi.host.get_API_version_minor(host)
        self.__class__.timings["xapi_version"] = time.perf_counter() - t0
        print(f"\n[sync] XAPI Version: {major}.{minor}")
        assert major and minor

    def test_hosts(self):
        t0 = time.perf_counter()
        records = self.session.xenapi.host.get_all_records()
        self.__class__.timings["hosts"] = time.perf_counter() - t0
        names = [r["name_label"] for r in records.values()]
        print(f"\n[sync] Hosts ({len(names)}): {', '.join(names)}")
        assert len(names) > 0

    def test_vms(self):
        t0 = time.perf_counter()
        records = self.session.xenapi.VM.get_all_records()
        self.__class__.timings["vms"] = time.perf_counter() - t0
        vm_names = [
            r["name_label"]
            for r in records.values()
            if not r["is_a_template"] and not r["is_a_snapshot"]
        ]
        print(f"\n[sync] VMs ({len(vm_names)}): {', '.join(vm_names)}")
        assert len(vm_names) > 0

    def test_storage(self):
        t0 = time.perf_counter()
        records = self.session.xenapi.SR.get_all_records()
        self.__class__.timings["storage"] = time.perf_counter() - t0
        names = [r["name_label"] for r in records.values()]
        print(f"\n[sync] Storage ({len(names)}): {', '.join(names)}")
        assert len(names) > 0

    def test_networks(self):
        t0 = time.perf_counter()
        records = self.session.xenapi.network.get_all_records()
        self.__class__.timings["networks"] = time.perf_counter() - t0
        names = [r["name_label"] for r in records.values()]
        print(f"\n[sync] Networks ({len(names)}): {', '.join(names)}")
        assert len(names) > 0

    def test_all_concurrent(self):
        """Fetch all record types sequentially (sync has no concurrency)."""
        t0 = time.perf_counter()
        hosts = self.session.xenapi.host.get_all_records()
        vms = self.session.xenapi.VM.get_all_records()
        srs = self.session.xenapi.SR.get_all_records()
        nets = self.session.xenapi.network.get_all_records()
        self.__class__.timings["all_concurrent"] = time.perf_counter() - t0
        print(
            f"\n[sync] All records sequential: "
            f"{len(hosts)} hosts, {len(vms)} VMs, {len(srs)} SRs, {len(nets)} networks"
        )
        assert hosts and vms and srs and nets


# ---------------------------------------------------------------------------
# Async XenAPI benchmark
# ---------------------------------------------------------------------------


class TestAsyncXenAPI:
    """Run the same operations using async-xenapi (aiohttp)."""

    @pytest_asyncio.fixture(autouse=True, scope="class")
    async def setup_session(self, host_url, credentials):
        username, password = credentials
        self.__class__.timings: dict[str, float] = {}

        t0 = time.perf_counter()
        self.__class__.session = AsyncXenAPISession(host_url)
        await self.__class__.session.login_with_password(username, password)
        self.__class__.timings["login"] = time.perf_counter() - t0
        print(f"\n[async] Login {host_url} — {_fmt_duration(self.__class__.timings['login'])}")
        yield
        t0 = time.perf_counter()
        await self.__class__.session.logout()
        self.__class__.timings["logout"] = time.perf_counter() - t0
        print(f"[async] Logout — {_fmt_duration(self.__class__.timings['logout'])}")

        # Print comparison table after both suites finish
        sync_timings = getattr(TestSyncXenAPI, "timings", {})
        if sync_timings:
            _print_table(sync_timings, self.__class__.timings)

    @pytest.mark.asyncio
    async def test_xapi_version(self):
        t0 = time.perf_counter()
        pool = await self.session.xenapi.pool.get_all()
        host = await self.session.xenapi.pool.get_master(pool[0])
        major, minor = await asyncio.gather(
            self.session.xenapi.host.get_API_version_major(host),
            self.session.xenapi.host.get_API_version_minor(host),
        )
        self.__class__.timings["xapi_version"] = time.perf_counter() - t0
        print(f"\n[async] XAPI Version: {major}.{minor}")
        assert major and minor

    @pytest.mark.asyncio
    async def test_hosts(self):
        t0 = time.perf_counter()
        records = await self.session.xenapi.host.get_all_records()
        self.__class__.timings["hosts"] = time.perf_counter() - t0
        names = [r["name_label"] for r in records.values()]
        print(f"\n[async] Hosts ({len(names)}): {', '.join(names)}")
        assert len(names) > 0

    @pytest.mark.asyncio
    async def test_vms(self):
        t0 = time.perf_counter()
        records = await self.session.xenapi.VM.get_all_records()
        self.__class__.timings["vms"] = time.perf_counter() - t0
        vm_names = [
            r["name_label"]
            for r in records.values()
            if not r["is_a_template"] and not r["is_a_snapshot"]
        ]
        print(f"\n[async] VMs ({len(vm_names)}): {', '.join(vm_names)}")
        assert len(vm_names) > 0

    @pytest.mark.asyncio
    async def test_storage(self):
        t0 = time.perf_counter()
        records = await self.session.xenapi.SR.get_all_records()
        self.__class__.timings["storage"] = time.perf_counter() - t0
        names = [r["name_label"] for r in records.values()]
        print(f"\n[async] Storage ({len(names)}): {', '.join(names)}")
        assert len(names) > 0

    @pytest.mark.asyncio
    async def test_networks(self):
        t0 = time.perf_counter()
        records = await self.session.xenapi.network.get_all_records()
        self.__class__.timings["networks"] = time.perf_counter() - t0
        names = [r["name_label"] for r in records.values()]
        print(f"\n[async] Networks ({len(names)}): {', '.join(names)}")
        assert len(names) > 0

    @pytest.mark.asyncio
    async def test_all_concurrent(self):
        """Fetch all record types concurrently via asyncio.gather()."""
        t0 = time.perf_counter()
        hosts, vms, srs, nets = await asyncio.gather(
            self.session.xenapi.host.get_all_records(),
            self.session.xenapi.VM.get_all_records(),
            self.session.xenapi.SR.get_all_records(),
            self.session.xenapi.network.get_all_records(),
        )
        self.__class__.timings["all_concurrent"] = time.perf_counter() - t0
        print(
            f"\n[async] All records concurrent: "
            f"{len(hosts)} hosts, {len(vms)} VMs, {len(srs)} SRs, {len(nets)} networks"
        )
        assert hosts and vms and srs and nets
