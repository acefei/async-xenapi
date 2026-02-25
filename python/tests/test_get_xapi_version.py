"""Integration test: mirrors tests/getXapiVersion.test.ts

Requires a live XenServer host.  Configure via .env at repo root:
    HOST_URL=https://<xen-host>
    USERNAME=root
    PASSWORD=<password>

Run with:
    uv run pytest tests/test_get_xapi_version.py -v
"""

import asyncio
import os

import pytest
import pytest_asyncio

from async_xenapi import AsyncXenAPISession


@pytest.fixture(scope="class")
def host_url():
    url = os.environ.get("HOST_URL")
    if not url:
        pytest.skip("HOST_URL not set")
    return url


@pytest.fixture(scope="class")
def credentials():
    username = os.environ.get("USERNAME")
    password = os.environ.get("PASSWORD")
    if not username or not password:
        pytest.skip("USERNAME or PASSWORD not set")
    return username, password


class TestGetXapiVersion:
    @pytest_asyncio.fixture(autouse=True, scope="class")
    async def setup_session(self, host_url, credentials):
        username, password = credentials
        print(f"\nLogin {host_url} with {username}")
        self.__class__.session = AsyncXenAPISession(host_url)
        await self.__class__.session.login_with_password(username, password)
        print("Login successfully")
        yield
        await self.__class__.session.logout()
        print("Session Logout.")

    @pytest.mark.asyncio
    async def test_should_return_a_valid_xapi_version(self):
        pool = await self.session.xenapi.pool.get_all()
        host = await self.session.xenapi.pool.get_master(pool[0])
        major, minor = await asyncio.gather(
            self.session.xenapi.host.get_API_version_major(host),
            self.session.xenapi.host.get_API_version_minor(host),
        )
        version = f"{major}.{minor}"
        print(f"\nCurrent XAPI Version: {version}")
        assert isinstance(major, (int, str))
        assert isinstance(minor, (int, str))

    @pytest.mark.asyncio
    async def test_should_list_hosts(self):
        records = await self.session.xenapi.host.get_all_records()
        host_names = [rec["name_label"] for rec in records.values()]
        print("\nHosts:\n" + "\n".join(f"  - {n}" for n in host_names))
        assert len(host_names) > 0

    @pytest.mark.asyncio
    async def test_should_list_vms(self):
        records = await self.session.xenapi.VM.get_all_records()
        vm_names = [
            f"  - {rec['name_label']} ({rec['power_state']})"
            for rec in records.values()
            if not rec["is_a_template"] and not rec["is_a_snapshot"]
        ]
        print("\nVMs:\n" + "\n".join(vm_names))
        assert len(vm_names) > 0

    @pytest.mark.asyncio
    async def test_should_list_storage_repositories(self):
        records = await self.session.xenapi.SR.get_all_records()
        sr_info = [f"  - {rec['name_label']} (type: {rec['type']})" for rec in records.values()]
        print("\nStorage Repositories:\n" + "\n".join(sr_info))
        assert len(sr_info) > 0

    @pytest.mark.asyncio
    async def test_should_list_networks(self):
        records = await self.session.xenapi.network.get_all_records()
        net_names = [rec["name_label"] for rec in records.values()]
        print("\nNetworks:\n" + "\n".join(f"  - {n}" for n in net_names))
        assert len(net_names) > 0
