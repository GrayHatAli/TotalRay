# Multi-Instance Leaf Architecture Plan (sing-box)

## Goal

Replace the current model — "one sing-box instance with a mutable outbound set" — with "one fixed main instance + N independent leaf instances", so that:

- Switching the active server no longer requires restarting the main instance
- Packet-loss / failure detection happens in seconds, not on the current multi-minute batch-test cadence
- A restart (when actually needed) only ever happens on a leaf instance carrying no live traffic, never on the main path

This document is deliberately phased. Each phase must be independently tested, deployed, and (if needed) rolled back — with the same discipline used for the Pool A/B concurrency phasing in `ARCHITECTURE_IMPLEMENTATION_PLAN.md`.

## Relationship to the Pool A/B Concurrency Plan

This plan is **independent of, but dependent on**, `ARCHITECTURE_IMPLEMENTATION_PLAN.md`:

- `ApplyCoordinator` in that plan is exactly the component that, in Phase 5 of this document, must call a new leaf-replacement method instead of the current `rebuild_and_apply()`.
- Phases 1–3 of the Pool A/B plan (round-state safety, snapshot/generation, worker/committer/coordinator separation) are recommended to be **complete before Phase 5 of this document** starts; otherwise two concurrent architectural changes will race on the same coordinator.
- Until that ordering is respected, Phases 0–4 of this document (which are self-contained and isolated) can proceed without issue.

## Design Principles (from the premortem)

1. **The health monitor must have its own independent watchdog.** If that process hangs, the main instance must not blindly keep routing traffic to a dead leaf.
2. **nftables bypass (routing_mark/exclude_uid) must be validated under sustained load, not just a short test, starting in Phase 1.** After this change, every leaf is a permanent process, not a transient test.
3. **The replacement loop needs its own circuit breaker, independent of the existing restart circuit breaker.**
4. **The fast hysteresis (seconds-scale) must be fully decoupled from the current slow hysteresis (minutes-scale)** to avoid flapping.
5. **Leaf replacement must be readiness-gated** — a new leaf only enters rotation after a successful health probe, not immediately after its port binds.
6. **No phase may touch production unless it has first run in parallel (shadow mode) and been compared against the baseline.**

---

# Phase 0: Baseline and Resource Budget

## Goal

Before adding N permanent processes, establish what the Pi can actually sustain.

## Tasks

- [ ] Measure CPU/RAM for the current single instance (idle and under load)
- [ ] Benchmark N=2 and N=3 simulated leaf instances over 24 hours; record CPU/RAM/bandwidth baseline
- [ ] Measure the cost of keeping QUIC-based tunnels (Hysteria2/TUIC) alive at idle
- [ ] Record the bandwidth cost of the proposed (hypothetical) health probes on the current DSL uplink
- [ ] Decide the final N (proposed default: start with 2, not 3)

## Acceptance Criteria

- A concrete number for "the ceiling N the Pi can tolerate" is recorded.
- No production code is changed.

## Suggested Commit

```text
chore: record Pi resource baseline for multi-instance leaf design
```

---

# Phase 1: Leaf Process — Isolated Prototype

## Goal

Build and test a standalone leaf instance, fully separate from the production path, with no connection to main or the real Pool B.

## Tasks

- [ ] New module `totalray/leaf.py`: build the leaf config (one SOCKS inbound on a fixed port + one outbound with a permanent exclude_uid/routing_mark)
- [ ] Reuse the `_dnsmasq_uid()` pattern (already proven for the DNS fallback feature) for a dedicated leaf uid
- [ ] Manual test: run a leaf continuously for 24 hours; verify no double-hop or CPU spike occurs via the main auto_redirect
- [ ] Unit test: `build_leaf_config` produces a valid sing-box config (`sing-box check`)
- [ ] Integration test: leaf starts, connects out via its local SOCKS port, and its traffic never re-enters the main nftables auto_redirect (verify via nftables counters or logs)

## Acceptance Criteria

- After 24 hours of continuous run, no abnormal CPU increase or new routes appear on main.
- The automated bypass test (leaf, not main) passes.
- `/etc/sing-box/config.json` for main is not touched at all.

## Suggested Commit

```text
feat: add isolated leaf sing-box process prototype
```

---

# Phase 2: Main Instance — Selector Over Loopback (Parity Test)

## Goal

Prove that a Clash API switch between loopback members behaves exactly like the current stable switch between remote outbounds — before depending on multiple leaves.

## Tasks

- [ ] Add a temporary outbound on main pointing to a fixed loopback port (served by a single leaf)
- [ ] Switch back and forth several times with a single leaf and confirm `interrupt_exist_connections: false` still holds (live connections are not dropped)
- [ ] Measure the added round-trip from this extra hop (main → loopback → leaf → internet) versus the current direct path
- [ ] Run this phase entirely in shadow mode alongside the current production path — production main stays untouched

## Acceptance Criteria

- Switching between a remote outbound and a loopback outbound causes zero connection drops.
- The added latency from the internal hop is acceptable (proposed: under 5ms, since it's loopback-only).
- Rollback is trivial: just remove the temporary outbound.

## Suggested Commit

```text
feat: prototype main selector over loopback leaf (shadow only)
```

---

# Phase 3: Fast Health Monitor + Flap Guard + Watchdog

## Goal

Replace the current slow batch probe with a fast, independent, self-protecting monitor for the leaves.

## Tasks

- [ ] New module `totalray/leaf_monitor.py`, separate from the current `live_monitor.py`
- [ ] Probe each leaf every 3–5 seconds (Clash API `/proxies/{name}/delay` or a lightweight TCP connect)
- [ ] Independent hysteresis: e.g. 2 consecutive failures within a 15-second window (not 2 minutes)
- [ ] Flap guard: a minimum cooldown between consecutive switches (e.g. 30 seconds) to prevent rapid oscillation
- [ ] Self-watchdog: a heartbeat file checked by an external watcher (systemd watchdog or a separate thread in `main.py`); if the heartbeat goes stale, `leaf_monitor` gets restarted
- [ ] Unit test: simulate intermittent (not total) packet loss and verify hysteresis/flap-guard behavior
- [ ] Unit test: kill `leaf_monitor` itself and confirm the watchdog detects it

## Acceptance Criteria

- A total leaf failure is detected and failed over in under 10 seconds.
- A leaf with borderline/oscillating quality does not trigger more than 1 switch per 30 seconds.
- `leaf_monitor` dying is detected and logged within N seconds.

## Suggested Commit

```text
feat: add fast leaf health monitor with flap guard and watchdog
```

---

# Phase 4: Leaf Lifecycle Orchestrator (Replacement + Circuit Breaker)

## Goal

Manage automatic replacement of a failed leaf with the next candidate, without race conditions or an infinite loop.

## Tasks

- [ ] New module `totalray/leaf_orchestrator.py`: owns start/stop/replace for each leaf on its fixed port
- [ ] Safe handoff protocol: a new leaf only enters rotation after a successful health probe, not merely after its port binds
- [ ] During replacement, if main currently points at that port, first switch to another healthy leaf (if one exists), then restart the leaf — main must never be left pointing at a port mid-restart
- [ ] Independent circuit breaker: if a leaf is replaced more than N times within M minutes and keeps failing, stop that slot pending manual review and mark it `removed` in the database
- [ ] Integrate with the existing soft-delete logic in `db.py` (no change to current scoring logic)
- [ ] Test: race condition — a health probe and a replacement happening simultaneously on the same port
- [ ] Test: circuit breaker trips after N consecutive replacement failures

## Acceptance Criteria

- There is no time window in which main points at an unbound port.
- This loop's circuit breaker trips independently of, and is logged separately from, the existing restart circuit breaker.
- The race-condition test passes automatically and reproducibly.

## Suggested Commit

```text
feat: add leaf replacement orchestrator with independent circuit breaker
```

---

# Phase 5: Integration with the Pool A/B Pipeline

## Goal

Wire Pool B's verified output into the leaf orchestrator's "next best candidate" queue, instead of diffing the entire outbound set.

## Prerequisite

Phases 1–3 of `ARCHITECTURE_IMPLEMENTATION_PLAN.md` must be complete (see "Relationship to the Pool A/B Concurrency Plan" above).

## Tasks

- [ ] `ApplyCoordinator` calls a new `leaf_orchestrator.request_replacement(slot, candidate)` method instead of the full `rebuild_and_apply()`
- [ ] Candidate-selection rule: the highest-scoring Pool B entry not currently assigned to any active leaf
- [ ] Remove the old tag-diff/full-restart path for this specific case (keep it only for rare cases like rule-set changes or a main upgrade)
- [ ] Test: end-to-end scenario — a config is tested in Pool A, promoted to Pool B, assigned by the orchestrator to a free leaf, health-probed, and finally selected by main

## Acceptance Criteria

- A new healthy Pool B config enters rotation with zero restarts of main.
- The old full-restart path remains only for genuine main-level changes (rule-set, sing-box version).

## Suggested Commit

```text
feat: wire pool B promotion into leaf replacement queue
```

---

# Phase 6: Observability

## Goal

Extend `totalray status` to show per-leaf state, matching the observability-phase style used in the other plan.

## Tasks

- [ ] Every `leaf_monitor`/orchestrator event is logged with a shared `leaf_slot_id` for easy correlation
- [ ] Add a table to the status output:

```text
Leaf 1 (active):  slot=30001  server=... delay=142ms   since=14:32:07
Leaf 2 (standby): slot=30002  server=... delay=198ms   since=14:20:11
Leaf 3 (replace): slot=30003  status=warming up...
```

- [ ] Show switch counts and circuit-breaker trips over the last 24 hours
- [ ] Include these fields in the `--json` output as well

## Acceptance Criteria

- From an incident, one can determine within seconds which leaf switched, when, and why.

## Suggested Commit

```text
feat: expose leaf pool state in totalray status
```

---

# Phase 7: Staged Rollout on the Pi

## Tasks

1. [ ] Run in shadow mode: leaves are live and monitored, but main still uses the old path (compare delay/uptime with zero risk)
2. [ ] Compare at least 48 hours of shadow data against current behavior
3. [ ] Cut over with N=2 (active + standby); keep the old path as a manual fallback
4. [ ] Monitor for at least one week: switch count, circuit-breaker trips, CPU/RAM
5. [ ] If stable, increase to N=3
6. [ ] Only after N=3 is fully stable, remove the old full-restart path as a fallback (not before)

## Rollback Criteria

Revert to the old path immediately if any of these occur:

- More than 1 trip of the new circuit breaker per hour
- Sustained CPU above the threshold set in Phase 0
- Any user-reported outage that coincides with a leaf switch

## Suggested Commit

```text
chore: staged rollout plan for multi-instance leaf architecture
```

---

# Architecture Decision

The multi-leaf architecture is approved, with these explicit prerequisites drawn from the premortem:

- The health monitor must have its own watchdog (Phase 3)
- nftables bypass must be tested under sustained load, not just a short test (Phase 1)
- The replacement loop must have its own independent circuit breaker (Phase 4)
- Phase 5 of this document must not start before Phase 3 of `ARCHITECTURE_IMPLEMENTATION_PLAN.md` is complete

No phase may replace the current production path directly; the transition must go through shadow mode and the staged rollout (Phase 7).
