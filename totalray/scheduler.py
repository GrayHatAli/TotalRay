"""Daemon scheduler: subscription updates, dual-pool test rounds, rule-set updates for TotalRay."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler

from . import builder, net, rulesets, subfetch
from .tester import GroupTester
from . import traffic
from .live_monitor import create_monitor

log = logging.getLogger(__name__)


class Manager:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self._job_lock = threading.Lock()
        # Pool A and Pool B may test concurrently, but sing-box config writes
        # and restarts must remain serialized to avoid route/config races.
        self._apply_lock = threading.Lock()
        self._internet_was_down = False
        self._live_monitor = None
        self._round_status_path = os.path.join(
            self.settings.data_dir, "round_status.json")
        self._reset_round_status()

    def _set_round_status(self, kind: str, running: bool) -> None:
        """Publish daemon progress for the short-lived status command."""
        state = {}
        try:
            with open(self._round_status_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            pass
        current = state.get(kind, {})
        current["running"] = running
        if running:
            current["started_at"] = time.time()
        else:
            current["finished_at"] = time.time()
        state[kind] = current
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(self._round_status_path), prefix=".round-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.chmod(tmp_path, 0o664)
            os.replace(tmp_path, self._round_status_path)
        except OSError as exc:
            log.debug("could not write round status: %s", exc)

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
        if not self._job_lock.acquire(blocking=False):
            log.info("another job is already running; skipping subscription update")
            return
        self._set_round_status("subscriptions", True)
        try:
            log.info("updating subscriptions...")
            summary = subfetch.update_all(self.settings, self.db)
            log.info("subscriptions: %s", summary)
        except Exception:  # noqa: BLE001
            log.exception("error updating subscriptions")
        finally:
            self._set_round_status("subscriptions", False)
            self._job_lock.release()

    def job_test_pool_a(self):
        if not self._job_lock.acquire(blocking=False):
            log.info("another job is already running; skipping pool-A test round")
            return
        self._set_round_status("pool_a", True)
        try:
            log.info("starting pool-A (candidate) test round...")
            self.run_pool_a_round()
        except Exception:  # noqa: BLE001
            log.exception("error during pool-A test round")
        finally:
            self._set_round_status("pool_a", False)
            self._job_lock.release()

    def job_test_pool_b(self):
        # Pool B is deliberately independent from Pool A. It must continue
        # checking the live verified set while Pool A scans new candidates.
        self._set_round_status("pool_b", True)
        try:
            log.info("starting pool-B (verified) test round...")
            self.run_pool_b_round()
        except Exception:  # noqa: BLE001
            log.exception("error during pool-B test round")
        finally:
            self._set_round_status("pool_b", False)

    def job_update_rules(self):
        if not self._job_lock.acquire(blocking=False):
            return
        try:
            log.info("updating rule-sets...")
            rulesets.update_rulesets(self.settings)
            ok, msg = builder.rebuild_and_apply(self.settings, self.db, force=True)
            log.info("config applied after rule-set update: %s %s", ok, msg)
        except Exception:  # noqa: BLE001
            log.exception("error updating rule-sets")
        finally:
            self._job_lock.release()

    def job_sample_traffic(self):
        if not self._job_lock.acquire(blocking=False):
            log.debug("another job is already running; skipping traffic sample")
            return
        try:
            try:
                devs = traffic.get_device_stats(self.settings)
                if devs:
                    self.db.record_device_stats(devs)
                    log.debug("sampled %d devices", len(devs))
            except Exception:  # noqa: BLE001
                log.exception("error sampling device traffic")
        finally:
            self._job_lock.release()

    # ---------------------------------------------------- core logic
    def run_pool_a_round(self) -> dict:
        if not self._internet_ok():
            return {"total": 0, "skipped": "internet_down"}
        candidates = self.db.get_pool_candidates("a")
        if not candidates:
            log.warning("no configs in pool A to test")
            return {"total": 0}
        tester = GroupTester(self.settings)
        ping_threshold = int(self.settings["test"]["ping_threshold_ms"])
        fail_threshold = int(self.settings["test"]["fail_threshold"])
        aggregate = {"total": 0, "ok": 0, "failed": 0, "removed": []}

        def persist_chunk(_items, chunk_results):
            chunk_stats = self.db.record_pool_a_results(
                chunk_results, ping_threshold, fail_threshold)
            aggregate["total"] += chunk_stats["total"]
            aggregate["ok"] += chunk_stats["ok"]
            aggregate["failed"] += chunk_stats["failed"]
            aggregate["removed"].extend(chunk_stats["removed"])
            # Apply promotions immediately so the verified group can use a
            # healthy config from this chunk while later chunks are tested.
            with self._apply_lock:
                ok, msg = builder.rebuild_and_apply(self.settings, self.db)
            log.info("chunk config applied: %s - %s", ok, msg)

        tester.test_all(candidates, on_chunk=persist_chunk)
        log.info("pool-A round done: %d tested | %d promoted to pool B |"
                 " %d still in pool A | %d removed (threshold %d, ping<=%dms)",
                 aggregate["total"], aggregate["ok"],
                 aggregate["failed"] - len(aggregate["removed"]),
                 len(aggregate["removed"]), fail_threshold, ping_threshold)
        return aggregate

    def run_pool_b_round(self) -> dict:
        if not self._internet_ok():
            return {"total": 0, "skipped": "internet_down"}
        candidates = self.db.get_pool_candidates("b")
        if not candidates:
            log.warning("no configs in pool B to test yet"
                        " (nothing has been promoted from pool A)")
            return {"total": 0}
        tester = GroupTester(self.settings)
        results = tester.test_all(candidates)
        ping_threshold = int(self.settings["test"]["ping_threshold_ms"])
        fail_threshold = int(self.settings["test"]["fail_threshold"])
        stats = self.db.record_pool_b_results(results, ping_threshold, fail_threshold)
        log.info("pool-B round done: %d tested | %d still healthy |"
                 " %d demoted to pool A | %d removed (threshold %d, ping<=%dms)",
                 stats["total"], stats["ok"],
                 stats["failed"] - len(stats["removed"]),
                 len(stats["removed"]), fail_threshold, ping_threshold)
        with self._apply_lock:
            ok, msg = builder.rebuild_and_apply(self.settings, self.db)
        log.info("config applied: %s - %s", ok, msg)
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
    manager.bootstrap()

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
