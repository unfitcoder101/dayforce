"""Startup waits for the database (DB_INIT_RETRY_SECONDS) instead of giving up at once."""

import psycopg
import pytest

import app.db.connection as connection
from app.db.connection import DatabaseNotConfigured, init_schema_with_retry
from app.main import db_init_retry_seconds


class FakeClock:
    """Time that only moves when the code sleeps, so the tests never really wait."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def flaky_connect(failures: int):
    """A connect function that fails `failures` times, then works. Counts its calls."""
    calls = {"n": 0}

    def connect_fn():
        calls["n"] += 1
        if calls["n"] <= failures:
            raise psycopg.OperationalError("database is starting")
        return FakeConn()

    return connect_fn, calls


@pytest.fixture
def schema_runs(monkeypatch):
    runs = []
    monkeypatch.setattr(connection, "init_schema", lambda conn: runs.append(conn))
    return runs


def test_waits_until_the_database_is_ready(schema_runs):
    clock = FakeClock()
    connect_fn, calls = flaky_connect(failures=2)
    init_schema_with_retry(30, 2, connect_fn=connect_fn, sleep=clock.sleep, clock=clock)
    assert calls["n"] == 3
    assert clock.sleeps == [2, 2]
    assert len(schema_runs) == 1


def test_gives_up_when_the_time_is_up(schema_runs):
    clock = FakeClock()
    connect_fn, calls = flaky_connect(failures=100)
    with pytest.raises(psycopg.OperationalError):
        init_schema_with_retry(5, 2, connect_fn=connect_fn, sleep=clock.sleep, clock=clock)
    assert calls["n"] == 3  # tried at 0 s, 2 s and 4 s; a try at 6 s would be past 5 s
    assert clock.now <= 5
    assert schema_runs == []


def test_zero_means_one_try(schema_runs):
    clock = FakeClock()
    connect_fn, calls = flaky_connect(failures=1)
    with pytest.raises(psycopg.OperationalError):
        init_schema_with_retry(0, 2, connect_fn=connect_fn, sleep=clock.sleep, clock=clock)
    assert calls["n"] == 1
    assert clock.sleeps == []


def test_missing_database_url_is_not_retried(schema_runs):
    clock = FakeClock()
    calls = {"n": 0}

    def no_url():
        calls["n"] += 1
        raise DatabaseNotConfigured("DATABASE_URL is not set")

    with pytest.raises(DatabaseNotConfigured):
        init_schema_with_retry(30, 2, connect_fn=no_url, sleep=clock.sleep, clock=clock)
    assert calls["n"] == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 0.0), ("", 0.0), ("30", 30.0), ("-5", 0.0), ("soon", 0.0)],
)
def test_retry_seconds_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("DB_INIT_RETRY_SECONDS", raising=False)
    else:
        monkeypatch.setenv("DB_INIT_RETRY_SECONDS", value)
    assert db_init_retry_seconds() == expected
