"""Token storage, credential transfer, and broker gating."""

import asyncio
import base64
import json

import pytest

from bot import broker, broker_auth
from bot.broker_auth import FileTokenStorage
from mcp.shared.auth import OAuthToken


@pytest.fixture
def storage(tmp_path):
    return FileTokenStorage(tmp_path / "tokens.json")


def test_token_roundtrip_and_permissions(storage):
    assert not storage.has_credentials()
    assert asyncio.run(storage.get_tokens()) is None

    tokens = OAuthToken(access_token="abc", token_type="Bearer",
                        refresh_token="def", expires_in=3600)
    asyncio.run(storage.set_tokens(tokens))

    assert storage.has_credentials()
    loaded = asyncio.run(storage.get_tokens())
    assert loaded.access_token == "abc"
    assert loaded.refresh_token == "def"
    assert storage.path.stat().st_mode & 0o777 == 0o600

    storage.clear()
    assert not storage.has_credentials()
    storage.clear()  # idempotent


def test_corrupt_file_reads_as_empty(storage):
    storage.path.parent.mkdir(parents=True, exist_ok=True)
    storage.path.write_text("{not json")
    assert not storage.has_credentials()
    assert asyncio.run(storage.get_tokens()) is None


def test_export_import_roundtrip(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(broker_auth.settings, "robinhood_token_path", storage.path)
    tokens = OAuthToken(access_token="abc", token_type="Bearer", refresh_token="def")
    asyncio.run(storage.set_tokens(tokens))

    blob = broker_auth.export_blob()
    storage.clear()
    path = broker_auth.import_blob(blob)
    assert path == storage.path
    assert asyncio.run(FileTokenStorage(path).get_tokens()).access_token == "abc"


def test_import_rejects_garbage(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_auth.settings, "robinhood_token_path", tmp_path / "t.json")
    with pytest.raises(ValueError):
        broker_auth.import_blob("not base64!!!")
    with pytest.raises(ValueError):
        broker_auth.import_blob(base64.b64encode(json.dumps({"no": "tokens"}).encode()).decode())


def test_export_without_login_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_auth.settings, "robinhood_token_path", tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError):
        broker_auth.export_blob()


def test_broker_gates_on_missing_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(broker.settings, "robinhood_mcp_token", "")
    monkeypatch.setattr(broker_auth.settings, "robinhood_token_path", tmp_path / "none.json")
    with pytest.raises(broker.BrokerNotConfigured):
        broker.get_portfolio()
    assert broker.get_quote("NVDA") is None  # degrades to None, never raises
