"""Daemon scheduler: subscription updates, dual-pool test rounds, rule-set updates for TotalRay."""
from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from . import builder, net, rulesets, subfetch
from .tester import GroupTester
from . import traffic

log = logging.getLogger(__name__)


class Manager:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self._job_lock = threading.Lock()
        self._internet_was_down = False

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
        try:
            log.info("updating subscriptions...")
            summary = subfetch.update_all(self.settings, self.db)
            log.info("subscriptions: %s", summary)
        except Exception:  # noqa: BLE001
            log.exception("error updating subscriptions")
        finally:
            self._job_lock.release()

    def job_test_pool_a(self):
        if not self._job_lock.acquire(blocking=False):
            log.info("another job is already running; skipping pool-A test round")
            return
        try:
            log.info("starting pool-A (candidate) test round...")
            self.run_pool_a_round()
        except Exception:  # noqa: BLE001
            log.exception("error during pool-A test round")
        finally:
            self._job_lock.release()

    def job_test_pool_b(self):
        if not self._job_lock.acquire(blocking=False):
            log.info("another job is already running; skipping pool-B test round")
            return
        try:
            log.info("starting pool-B (verified) test round...")
            self.run_pool_b_round()
        except Exception:  # noqa: BLE001
            log.exception("error during pool-B test round")
        finally:
            self._job_lock.release()

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
        results = tester.test_all(candidates)
        ping_threshold = int(self.settings["test"]["ping_threshold_ms"])
        fail_threshold = int(self.settings["test"]["fail_threshold"])
        stats = self.db.record_pool_a_results(results, ping_threshold, fail_threshold)
        log.info("pool-A round done: %d tested | %d promoted to pool B |"
                 " %d still in pool A | %d removed (threshold %d, ping<=%dms)",
                 stats["total"], stats["ok"],
                 stats["failed"] - len(stats["removed"]),
                 len(stats["removed"]), fail_threshold, ping_threshold)
        ok, msg = builder.rebuild_and_apply(self.settings, self.db)
        log.info("config applied: %s - %s", ok, msg)
        return stats

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

    stop = threading.Event()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown(wait=False)
