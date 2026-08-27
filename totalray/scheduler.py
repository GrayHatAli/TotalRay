"""Daemon scheduler: subscription updates, dual-pool test rounds, rule-set updates for TotalRay."""
from __future__ import annotations

import logging
import os
import threading
import uuid

from apscheduler.schedulers.background import BackgroundScheduler

from . import net, rulesets, subfetch
from .coordinator import ApplyCoordinator, REASON_MEMBERSHIP_CHANGED, REASON_RULES_UPDATED
from .tester import GroupTester
from . import traffic
from .live_monitor import create_monitor
from .round_state import RoundStateStore

log = logging.getLogger(__name__)


class Manager:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self._subscription_lock = threading.Lock()
        self._rules_lock = threading.Lock()
        # Pool A and Pool B may test concurrently, but sing-box config writes
        # and restarts must remain serialized to avoid route/config races.
        self._coordinator = ApplyCoordinator(settings, db)
        self._internet_was_down = False
        self._live_monitor = None
        self._round_status_path = os.path.join(
            self.settings.data_dir, "round_status.json")
        self._round_state = RoundStateStore(self._round_status_path)
        self._round_state.recover()

    def start_live_monitor(self) -> None:
        """Start the live connection monitor for real-time failover.
        
        This monitor watches active connections and automatically switches
        to a backup server when it detects packet drops or connection failures,
        providing much faster reaction than the periodic pool-B test rounds.
        
        Only starts if enabled in config (live_monitor.enabled).
        """
        lm_cfg = self.settings.data.get("live_monitor", {})
        if not lm_cfg.get("enabled", True):
            log.info("Live connection monitor disabled in configuration")
            return
        
        if self._live_monitor is not None:
            log.warning("Live monitor already running")
            return
        
        self._live_monitor = create_monitor(self.settings, self.db)
        
        # Set up callback to log failovers
        def on_failover(new_tag: str) -> None:
            log.info("Live monitor triggered failover to %s", new_tag)
        
        self._live_monitor.set_failover_callback(on_failover)
        self._live_monitor.start()
        log.info("Live connection monitor enabled (checks every %.1fs, failover on degradation)",
                 lm_cfg.get("check_interval_seconds", 2.0))

    def _internet_ok(self) -> bool:
        """Guard for pool-A/pool-B test rounds: if the ISP link itself is
        down (as opposed to individual configs being blocked/dead), every
        config in a round would fail its test for reasons that have
        nothing to do with the config -- and without this check, a real
        outage looks identical to "every server is bad" and mass-demotes
        or removes the entire pool. Only logs on state transitions so a
        prolonged outage does not spam the log every couple of minutes.
        """
        up = net.internet_up()
        if not up and not self._internet_was_down:
            log.warning("no internet connectivity detected (all well-known"
                       " probe IPs unreachable) -- pausing pool test rounds"
                       " until connectivity returns")
        elif up and self._internet_was_down:
            log.info("internet connectivity restored; resuming pool test rounds")
        self._internet_was_down = not up
        return up

    # ---------------------------------------------------- jobs
    def job_update_subs(self):
        if not self._subscription_lock.acquire(blocking=False):
            self._round_state.skip("subscriptions", reason="busy", blocked_by="subscription_update")
            log.info("another subscription update is already running; skipping")
            return
        round_id = uuid.uuid4().hex[:8]
        self._round_state.start("subscriptions", round_id=round_id)
        try:
            log.info("updating subscriptions...")
            summary = subfetch.update_all(self.settings, self.db)
            log.info("subscriptions: %s", summary)
            self._round_state.finish("subscriptions", success=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("error updating subscriptions")
            self._round_state.finish("subscriptions", success=False, error=str(exc)[:400])
        finally:
            self._subscription_lock.release()

    def job_test_pool_a(self):
        current = self._round_state.snapshot().get("pool_a") or {}
        if current.get("state") == "running":
            self._round_state.skip("pool_a", reason="already_running", blocked_by="pool_a")
            log.info("pool-A round already in progress; skipping duplicate schedule")
            return
        try:
            log.info("starting pool-A (candidate) test round...")
            self.run_pool_a_round()
        except Exception:  # noqa: BLE001
            log.exception("error during pool-A test round")

    def job_test_pool_b(self):
        # Pool B is deliberately independent from Pool A. It must continue
        # checking the live verified set while Pool A scans new candidates.
        # Guard against concurrent Pool B rounds stacking up.
        current = self._round_state.snapshot().get("pool_b") or {}
        if current.get("state") == "running":
            self._round_state.skip("pool_b", reason="already_running", blocked_by="pool_b")
            log.info("pool-B round already in progress; skipping duplicate schedule")
            return
        try:
            log.info("starting pool-B (verified) test round...")
            self.run_pool_b_round()
        except Exception:  # noqa: BLE001
            log.exception("error during pool-B test round")

    def job_update_rules(self):
        if not self._rules_lock.acquire(blocking=False):
            self._round_state.skip("rules", reason="busy", blocked_by="rules_update")
            return
        round_id = uuid.uuid4().hex[:8]
        self._round_state.start("rules", round_id=round_id)
        try:
            log.info("updating rule-sets...")
            rulesets.update_rulesets(self.settings)
            ok, msg = self._coordinator.apply(reason=REASON_RULES_UPDATED, force=True)
            log.info("config applied after rule-set update: %s %s", ok, msg)
            self._round_state.finish("rules", success=ok, error=None if ok else msg[:400])
        except Exception as exc:  # noqa: BLE001
            log.exception("error updating rule-sets")
            self._round_state.finish("rules", success=False, error=str(exc)[:400])
        finally:
            self._rules_lock.release()

    def job_sample_traffic(self):
        try:
            devs = traffic.get_device_stats(self.settings)
            if devs:
                self.db.record_device_stats(devs)
                log.debug("sampled %d devices", len(devs))
        except Exception:  # noqa: BLE001
            log.exception("error sampling device traffic")

    # ---------------------------------------------------- core logic
    def run_pool_a_round(self, round_id: str | None = None) -> dict:
        if not self._internet_ok():
            self._round_state.skip("pool_a", reason="internet_down", blocked_by="internet")
            return {"total": 0, "skipped": "internet_down"}
        test_cfg = self.settings["test"]
        schedule_cfg = self.settings["schedule"]
        max_items = int(schedule_cfg.get("pool_a_max_items_per_round", 0))
        retry_backoff = int(schedule_cfg.get("pool_a_retry_backoff_minutes", 0))
        snapshot = self.db.get_pool_snapshot(
            "a", max_n=max_items,
            retry_backoff_minutes=retry_backoff)
        snapshot_generation = snapshot["generation"]
        candidates = snapshot["configs"]
        rid = round_id or uuid.uuid4().hex[:8]
        if not candidates:
            log.warning("no configs in pool A to test")
            self.db.start_test_round("a", snapshot_generation, rid, total=0)
            self._round_state.start("pool_a", round_id=rid, total=0,
                                    snapshot_generation=snapshot_generation)
            self._round_state.finish("pool_a", success=True)
            return {"total": 0}
        self.db.start_test_round("a", snapshot_generation, rid, total=len(candidates))
        existing = (self._round_state.snapshot().get("pool_a") or {}).get("state")
        if existing != "running":
            self._round_state.start("pool_a", round_id=rid, total=len(candidates),
                                    snapshot_generation=snapshot_generation)
        tester = GroupTester(self.settings)
        ping_threshold = int(test_cfg["ping_threshold_ms"])
        fail_threshold = int(test_cfg["fail_threshold"])
        aggregate: dict = {"total": 0, "ok": 0, "failed": 0, "stale": 0, "removed": []}

        def persist_chunk(_items, chunk_results):
            # A chunk commit may advance pool A's generation by promoting
            # configs. Validate the first chunk against the immutable
            # snapshot; subsequent chunks use the post-commit generation so
            # the round does not invalidate its own remaining work.
            chunk_generation = (snapshot_generation if aggregate["total"] == 0
                                else self.db.pool_generation("a"))
            chunk_stats = self.db.record_pool_a_results(
                chunk_results, ping_threshold, fail_threshold,
                round_id=rid, snapshot_generation=chunk_generation)
            aggregate["total"] += chunk_stats["total"]
            aggregate["ok"] += chunk_stats["ok"]
            aggregate["failed"] += chunk_stats["failed"]
            aggregate["stale"] = aggregate.get("stale", 0) + chunk_stats.get("stale", 0)
            aggregate["removed"].extend(chunk_stats["removed"])
            self._round_state.progress(
                "pool_a", processed=aggregate["total"],
                ok=aggregate["ok"], failed=aggregate["failed"],
                stale=chunk_stats.get("stale", 0))
            ok, msg = self._coordinator.apply(reason=REASON_MEMBERSHIP_CHANGED)
            log.info("chunk config applied: %s - %s", ok, msg)

        try:
            tester.test_all(candidates, on_chunk=persist_chunk)
        except Exception as exc:
            self.db.finish_test_round(
                rid, state="failed", total=aggregate["total"],
                ok=aggregate["ok"], failed=aggregate["failed"],
                stale=aggregate.get("stale", 0), error=str(exc)[:400])
            self._round_state.finish("pool_a", success=False, error=str(exc)[:400])
            raise
        log.info("pool-A round done: %d tested | %d promoted to pool B |"
                 " %d still in pool A | %d removed (threshold %d, ping<=%dms)",
                 aggregate["total"], aggregate["ok"],
                 aggregate["failed"] - len(aggregate["removed"]),
                 len(aggregate["removed"]), fail_threshold, ping_threshold)
        self.db.finish_test_round(
            rid, total=aggregate["total"], ok=aggregate["ok"],
            failed=aggregate["failed"], stale=aggregate.get("stale", 0))
        self._round_state.finish(
            "pool_a", success=True,
            items_total=aggregate["total"],
            items_processed=aggregate["total"],
            items_ok=aggregate["ok"],
            items_failed=aggregate["failed"], stale=aggregate.get("stale", 0))
        return aggregate

    def run_pool_b_round(self, round_id: str | None = None) -> dict:
        if not self._internet_ok():
            self._round_state.skip("pool_b", reason="internet_down", blocked_by="internet")
            return {"total": 0, "skipped": "internet_down"}
        snapshot = self.db.get_pool_snapshot("b")
        snapshot_generation = snapshot["generation"]
        candidates = snapshot["configs"]
        rid = round_id or uuid.uuid4().hex[:8]
        if not candidates:
            log.warning("no configs in pool B to test yet"
                        " (nothing has been promoted from pool A)")
            self.db.start_test_round("b", snapshot_generation, rid, total=0)
            self._round_state.start("pool_b", round_id=rid, total=0,
                                    snapshot_generation=snapshot_generation)
            self._round_state.finish("pool_b", success=True)
            return {"total": 0}
        self.db.start_test_round("b", snapshot_generation, rid, total=len(candidates))
        existing = (self._round_state.snapshot().get("pool_b") or {}).get("state")
        if existing != "running":
            self._round_state.start("pool_b", round_id=rid, total=len(candidates),
                                    snapshot_generation=snapshot_generation)
        tester = GroupTester(self.settings)
        results = tester.test_all(candidates)
        ping_threshold = int(self.settings["test"]["ping_threshold_ms"])
        fail_threshold = int(self.settings["test"]["fail_threshold"])
        try:
            stats = self.db.record_pool_b_results(
                results, ping_threshold, fail_threshold,
                round_id=rid, snapshot_generation=snapshot_generation)
        except Exception as exc:
            self.db.finish_test_round(
                rid, state="failed", total=len(results), ok=0, failed=0,
                stale=0, error=str(exc)[:400])
            self._round_state.finish("pool_b", success=False, error=str(exc)[:400])
            raise
        log.info("pool-B round done: %d tested | %d still healthy |"
                 " %d demoted to pool A | %d removed (threshold %d, ping<=%dms)",
                 stats["total"], stats["ok"],
                 stats["failed"] - len(stats["removed"]),
                 len(stats["removed"]), fail_threshold, ping_threshold)
        self._round_state.progress(
            "pool_b", processed=stats["total"],
            ok=stats["ok"], failed=stats["failed"], stale=stats.get("stale", 0))
        ok, msg = self._coordinator.apply(reason=REASON_MEMBERSHIP_CHANGED)
        log.info("config applied: %s - %s", ok, msg)
        self.db.finish_test_round(
            rid, total=stats["total"], ok=stats["ok"],
            failed=stats["failed"], stale=stats.get("stale", 0))
        self._round_state.finish(
            "pool_b", success=True,
            items_total=stats["total"],
            items_processed=stats["total"],
            items_ok=stats["ok"],
            items_failed=stats["failed"], stale=stats.get("stale", 0))
        return stats

    def bootstrap(self):
        log.info("bootstrap: downloading rule-sets...")
        rulesets.update_rulesets(self.settings)
        self.job_update_subs()
        self.job_test_pool_a()


def run_daemon(settings, db):
    manager = Manager(settings, db)

    # Start monitoring before bootstrap: bootstrap includes a potentially long
    # Pool-A test and must not leave a stale heartbeat during that work.
    manager.start_live_monitor()

    sch = settings["schedule"]
    scheduler = BackgroundScheduler()
    scheduler.add_job(manager.job_update_subs, "interval",
                      minutes=int(sch["sub_update_minutes"]),
                      id="subs", max_instances=1, coalesce=True)
    scheduler.add_job(manager.job_test_pool_a, "interval",
                      minutes=int(sch["pool_a_test_minutes"]),
                      id="pool_a", max_instances=1, coalesce=True)
    scheduler.add_job(manager.job_test_pool_b, "interval",
                      minutes=int(sch["pool_b_test_minutes"]),
                      id="pool_b", max_instances=1, coalesce=True)
    scheduler.add_job(manager.job_update_rules, "interval",
                      hours=int(sch["rules_update_hours"]),
                      id="rules", max_instances=1, coalesce=True)
    # traffic sampler (best-effort): sample every N seconds (default 60)
    scheduler.add_job(manager.job_sample_traffic, "interval",
                      seconds=int(sch.get("traffic_sample_seconds", 60)),
                      id="traffic", max_instances=1, coalesce=True)
    scheduler.start()
    log.info("scheduler active: subs every %d min | pool-A test every %d min |"
             " pool-B test every %d min | rule-sets every %d h",
             int(sch["sub_update_minutes"]), int(sch["pool_a_test_minutes"]),
             int(sch["pool_b_test_minutes"]), int(sch["rules_update_hours"]))

    # Start scheduled jobs before bootstrap so Pool B can run on schedule
    # while the initial Pool A scan is still processing.
    manager.bootstrap()

    stop = threading.Event()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown(wait=False)
        if manager._live_monitor:
            manager._live_monitor.stop()
