from pathlib import Path
from typing import Any

import httpx
import pytest

from pmlab import cli
from tests.conftest import fixture, fixture_client

MARKET = fixture("gamma_markets.json")[0]


def test_record_survives_a_network_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = fixture_client()
    real_book, calls = client.book, {"n": 0}

    def flaky_book(token: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:  # second snapshot, first book request
            raise httpx.ConnectError("nodename nor servname provided")
        return real_book(token)

    monkeypatch.setattr(client, "book", flaky_book)
    monkeypatch.setattr(cli, "Client", lambda: client)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    out = tmp_path / "snaps.jsonl"
    cli.main(["record", MARKET["conditionId"], "--count", "3", "--out", str(out)])
    assert len(out.read_text().splitlines()) == 2  # one skipped, recording kept going


def test_watch_does_not_flag_a_gap_as_a_spike(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = fixture_client().markets()[0]
    rounds = iter(range(100))

    def markets(**_: Any) -> list[Any]:
        r = next(rounds)
        if r in (40, 41, 42):  # market falls out of the top list for three rounds
            return []
        return [base.model_copy(update={"volume": 1000.0 * r + (0 if r < 43 else 3000)})]

    client = fixture_client()
    monkeypatch.setattr(client, "markets", markets)
    monkeypatch.setattr(cli, "Client", lambda: client)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    cli.main(["watch", "--rounds", "60", "--window", "10", "--min-volume", "0"])
    assert "volume spike" not in capsys.readouterr().out
