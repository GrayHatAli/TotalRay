"""Removing and disabling subscriptions."""
from __future__ import annotations

import importlib

import pytest

from totalray.db import Database

main = importlib.import_module("totalray.main")  # the package re-exports a function named main


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def _add(db, url, n=2):
    sub = db.add_subscription(url)
    db.sync_configs(sub, [
        {"fingerprint": f"{url}-{i}", "name": f"n{i}", "link": "",
         "outbound": {"type": "direct"}} for i in range(n)])
    return sub


def test_remove_subscription_retires_its_configs_only(db):
    gone = _add(db, "http://a.invalid")
    kept = _add(db, "http://b.invalid")
    before = db.pool_generation("a")

    assert db.remove_subscription(gone) == (True, 2)

    assert [s["id"] for s in db.list_subscriptions()] == [kept]
    assert len(db.get_pool_snapshot("a")["configs"]) == 2   # only b's remain
    assert db.pool_generation("a") > before                 # pool change is visible


def test_remove_unknown_subscription_reports_it(db):
    assert db.remove_subscription(999) == (False, 0)


def test_enabled_subscriptions_honours_the_flag(db):
    a = _add(db, "http://a.invalid")
    b = _add(db, "http://b.invalid")
    with db._lock, db._conn:
        db._conn.execute("UPDATE subscriptions SET enabled=0 WHERE id=?", (a,))
    assert [s["id"] for s in db.enabled_subscriptions()] == [b]
    assert len(db.list_subscriptions()) == 2


def test_cli_del_sub_removes_and_reports(tmp_path, capsys, monkeypatch):
    database = Database(str(tmp_path / "cli.db"))
    sub = _add(database, "http://a.invalid")
    monkeypatch.setattr(main, "_load", lambda args: (None, database))
    main.cmd_del_sub(type("Args", (), {"id": sub})())
    assert "removed (2 configs retired)" in capsys.readouterr().out


def test_cli_del_sub_unknown_id_exits_nonzero(tmp_path, capsys, monkeypatch):
    database = Database(str(tmp_path / "cli.db"))
    monkeypatch.setattr(main, "_load", lambda args: (None, database))
    with pytest.raises(SystemExit) as exc:
        main.cmd_del_sub(type("Args", (), {"id": 42})())
    assert exc.value.code == 1
