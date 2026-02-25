"""Pytest configuration — loads .env before any test runs."""

import os
import time
from pathlib import Path

import pytest

# Load .env from the repo root if present (no hard dependency on dotenv)
_env_file = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    item._start_time = time.time()


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item, nextitem):
    duration = time.time() - item._start_time
    if duration >= 1:
        print(f"  ({duration:.1f}s)")
    else:
        print(f"  ({duration * 1000:.0f}ms)")
