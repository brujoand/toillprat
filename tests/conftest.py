"""Test fixtures.

DATA_DIR is pointed at a throwaway directory BEFORE the app is imported, because
the app reads it once at import time. Each test then starts from an empty store
and no live sessions.
"""

import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="toillprat-test-"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from toillprat import main  # noqa: E402


@pytest.fixture(autouse=True)
def clean_data():
    main._ensure_dirs()
    for directory in (main.CHARACTERS_DIR, main.CHATS_DIR):
        for path in directory.glob("*.json"):
            path.unlink()
    if main.SETTINGS_PATH.exists():
        main.SETTINGS_PATH.unlink()
    main.sessions.players.clear()
    main.sessions.seen.clear()
    yield


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def auth_client(client):
    """A client that has claimed a name, so it carries a valid session cookie."""
    resp = client.post("/api/login", json={"name": "tester"})
    assert resp.status_code == 200
    return client
