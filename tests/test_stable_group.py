"""The live group must not be reshuffled every round.

Latencies are noisy, so "the N fastest verified servers" changes constantly.
That moved the active server every few minutes and made failover untestable.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from totalray import builder


def _item(i, delay):
    return {"id": i, "name": f"n{i}", "outbound": {"type": "direct"}, "delay": delay}


class FakeDb:
    """pool B as the database returns it: best latency first."""

    def __init__(self, items):
        self.items = sorted(items, key=lambda c: c["delay"])

    def get_pool_configs(self, pool, max_n=0):
        assert pool == "b"
        return list(self.items[:max_n] if max_n else self.items)


def _tags(group):
    return [f"cfg-{c['id']}" for c in group]


def test_first_run_takes_the_fastest():
    db = FakeDb([_item(1, 300), _item(2, 100), _item(3, 200), _item(4, 50)])
    assert _tags(builder.select_group(db, 3, set(), None)) == ["cfg-4", "cfg-2", "cfg-3"]


def test_members_stay_even_when_others_become_faster():
    db = FakeDb([_item(1, 900), _item(2, 800), _item(3, 700),     # current members, now slow
                 _item(4, 10), _item(5, 20)])                      # newcomers, very fast
    group = builder.select_group(db, 3, {"cfg-1", "cfg-2", "cfg-3"}, None)
    assert set(_tags(group)) == {"cfg-1", "cfg-2", "cfg-3"}


def test_only_empty_slots_are_filled_with_the_fastest_candidate():
    db = FakeDb([_item(1, 500), _item(2, 600), _item(4, 30), _item(5, 20)])  # cfg-3 left pool B
    group = builder.select_group(db, 3, {"cfg-1", "cfg-2", "cfg-3"}, None)
    assert set(_tags(group)) == {"cfg-1", "cfg-2", "cfg-5"}


def test_active_server_goes_first_whatever_its_latency():
    db = FakeDb([_item(1, 100), _item(2, 200), _item(3, 900)])
    group = builder.select_group(db, 3, {"cfg-1", "cfg-2", "cfg-3"}, "cfg-3")
    assert _tags(group)[0] == "cfg-3"
    assert _tags(group)[1:] == ["cfg-1", "cfg-2"]   # the rest keep latency order


def test_excluded_server_is_neither_kept_nor_used_as_a_filler():
    db = FakeDb([_item(1, 100), _item(2, 200), _item(3, 300), _item(4, 400)])
    group = builder.select_group(db, 3, {"cfg-1", "cfg-2", "cfg-3"}, "cfg-3", excluded={"cfg-1"})
    assert "cfg-1" not in _tags(group)
    assert set(_tags(group)) == {"cfg-2", "cfg-3", "cfg-4"}


def test_smaller_limit_trims_the_group():
    db = FakeDb([_item(1, 100), _item(2, 200), _item(3, 300)])
    assert len(builder.select_group(db, 2, {"cfg-1", "cfg-2", "cfg-3"}, None)) == 2


SETTINGS = {"test": {"max_in_group": 3}, "proxy_group": {"standby_count": 3},
            "paths": {"data_dir": "/tmp/totalray-test"}}


def _run(db, *, last_tags, active):
    with patch.object(builder, "_load_last_tags", return_value=last_tags), \
         patch.object(builder, "_clash_active_tag", return_value=active), \
         patch.object(builder, "set_active_config", return_value=(True, "switched")) as switch, \
         patch.object(builder, "build_config", return_value={}), \
         patch.object(builder, "_load_live_config", return_value=None), \
         patch.object(builder, "write_and_check", return_value=(True, "/x")), \
         patch.object(builder, "_apply_live_config", return_value=(True, "reconciled")), \
         patch.object(builder, "_save_last_tags"), patch.object(builder, "_save_live_config"):
        result = builder.rebuild_and_apply(SETTINGS, db)
    return result, switch


def test_a_new_latency_ranking_does_not_move_the_active_server():
    # cfg-3 carries traffic; this round cfg-1 measured fastest
    db = FakeDb([_item(1, 50), _item(2, 60), _item(3, 700)])
    (ok, msg), switch = _run(db, last_tags={"cfg-1", "cfg-2", "cfg-3"}, active="cfg-3")
    assert ok and "no restart" in msg
    switch.assert_not_called()


def test_active_server_that_left_the_pool_is_replaced_by_the_best_member():
    db = FakeDb([_item(1, 50), _item(2, 60), _item(4, 70)])   # cfg-3 (active) is gone
    (ok, _), switch = _run(db, last_tags={"cfg-1", "cfg-2", "cfg-3"}, active="cfg-3")
    assert ok
    switch.assert_called_with(SETTINGS, "cfg-1")


def test_active_server_survives_a_membership_change():
    db = FakeDb([_item(2, 60), _item(3, 700), _item(4, 10)])   # cfg-1 left pool B
    (ok, _), switch = _run(db, last_tags={"cfg-1", "cfg-2", "cfg-3"}, active="cfg-3")
    assert ok
    switch.assert_called_with(SETTINGS, "cfg-3")   # re-asserted after the reconcile


def test_refill_after_failover_keeps_the_other_members():
    db = FakeDb([_item(1, 100), _item(2, 200), _item(3, 300), _item(4, 400), _item(5, 50)])
    captured = {}

    def fake_build(settings, group):
        captured["group"] = list(group)
        return {}

    with patch.object(builder, "_load_last_tags", return_value={"cfg-1", "cfg-2", "cfg-3"}), \
         patch.object(builder, "_clash_active_tag", return_value="cfg-2"), \
         patch.object(builder, "build_config", side_effect=fake_build), \
         patch.object(builder, "_load_live_config", return_value=None), \
         patch.object(builder, "write_and_check", return_value=(True, "/x")), \
         patch.object(builder, "_apply_live_config", return_value=(True, "reconciled")), \
         patch.object(builder, "_save_last_tags"), patch.object(builder, "_save_live_config"):
        ok, _ = builder.refill_live_pool(SETTINGS, db, {"cfg-1"})
    assert ok
    tags = _tags(captured["group"])
    assert tags[0] == "cfg-2"                     # the new active server stays first
    assert "cfg-1" not in tags                    # the failed one is out
    assert set(tags) == {"cfg-2", "cfg-3", "cfg-5"}   # cfg-3 kept; fastest candidate fills the gap
