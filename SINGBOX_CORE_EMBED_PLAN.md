# sing-box Library Embedding + Degradation Detection Plan

## Why this replaces MULTI_INSTANCE_LEAF_ARCHITECTURE_PLAN.md

That plan assumed sing-box could only be controlled as an external CLI process, so
the only way to keep a warm standby server was to run a second (and third) full
OS process, each with its own port, its own nftables bypass, and its own
lifecycle to manage. That assumption turned out to be wrong.

Research (documented in `learnings.md`) found that `sing-box`'s own Go library
exposes `adapter.OutboundManager.Create()` / `.Remove()` on a *running* `Box`
instance -- confirmed by reading the actual implementation in `alireza0/s-ui`
(a ~9.9k-star production panel built on sing-box), whose `core.AddOutbound` /
`core.RemoveOutbound` are thin wrappers around exactly those calls. This means
a small Go daemon embedding sing-box as a library can add, remove, or replace
outbounds on a live instance without ever touching TUN, inbounds, or routing
rules -- which is the same end goal the leaf architecture was chasing, achieved
with one process instead of N, no extra ports, and no duplicated nftables
bypass surface.

What that research also settled, and what this plan is actually about: sing-box
itself has no notion of live degradation. `selector` has zero health-checking
logic at all. `urltest` only starts its ticker when the group is touched, and
even then it just fires a periodic synthetic HEAD request (default: every 3
minutes, 50ms tolerance, against `gstatic.com/generate_204`) and measures
nothing but response-header latency -- confirmed against the sing-box source
and against open, unresolved upstream feature requests (SagerNet/sing-box
#4397, #4110, #4065, #2130) asking for exactly the throughput/packet-loss
awareness that doesn't exist yet. So the actual open problem is not "how do we
apply a new outbound without restarting" (solved) but "how do we know, within
seconds, that the *active* outbound has degraded" (not solved by anything
upstream). That is what most of this plan is about.

## Design Principles

1. **The Go daemon is a thin control surface, not a rewrite.** It embeds
   sing-box, exposes a tiny local socket API (start / stop / add-outbound /
   remove-outbound / is-running), and changes nothing else. Inbounds, TUN,
   routing rules, and the Clash API stay exactly as they are today.
2. **Never actively probe the outbound that is currently carrying live
   traffic.** This is why `urltest` was dropped for `selector` in the first
   place. Active probing is only safe against standby outbounds that aren't
   serving anyone yet.
3. **Prefer signals that cost nothing extra.** Log-pattern observation and
   kernel-level retransmit counters both come from traffic that already
   exists; they should be tried before adding new probe traffic.
4. **A degradation signal needs hysteresis and a flap guard**, exactly like
   the fast-monitor design discussed for the leaf plan -- a single blip must
   not trigger a switch.
5. **No phase touches production until it has run in shadow mode** (observing
   and logging, not acting) and been compared against real behavior.

---

# Phase 1: Go core wrapper (isolated prototype)

## Goal

Build a minimal Go daemon that embeds sing-box as a library and exposes
start/stop/add-outbound/remove-outbound over a local control socket -- fully
isolated from production.

## Tasks

- [ ] New `cmd/singbox-core` Go module. Vendor or directly depend on
      `github.com/sagernet/sing-box` (the same version currently pinned,
      1.13.14, to avoid an unrelated protocol/behavior change alongside this
      one).
- [ ] Study `alireza0/s-ui`'s `core/main.go`, `core/box.go`, and
      `core/endpoint.go` (GPL-3.0, reuse permitted) as the reference
      implementation for `Core.Start`, `Core.Stop`, `Core.AddOutbound`,
      `Core.RemoveOutbound`, and the locking discipline around them
      (`service.lifecycleMu` outer, `Core.mu` inner) -- do not reinvent the
      locking from scratch.
- [ ] Control protocol: a Unix domain socket at
      `/run/totalray/singbox-core.sock`, newline-delimited JSON requests:
      `{"cmd":"start","config":...}`, `{"cmd":"stop"}`,
      `{"cmd":"add_outbound","config":...}`,
      `{"cmd":"remove_outbound","tag":...}`, `{"cmd":"is_running"}`.
- [ ] Unit tests (Go): each control command against a throwaway config,
      asserting the live `Box`'s outbound set actually changed with no
      process restart.
- [ ] Manual test: start with a real config, `AddOutbound` a second real
      server, confirm via Clash API that both tags are selectable, confirm
      via `/connections` that existing connections through the first
      outbound are undisturbed by the `AddOutbound` call.
- [ ] Manual test: `RemoveOutbound` a tag that is *not* currently selected;
      confirm zero impact on live connections through the selected one.

## Acceptance Criteria

- The daemon starts, stops, adds, and removes outbounds correctly against a
  throwaway config with no production impact.
- Adding or removing a non-active outbound never interrupts a live connection
  on the active one (verified via `/connections` count, matching the method
  used to test the SIGHUP reload).
- Runs standalone; nothing in `totalray/builder.py` calls it yet.

## Suggested Commit

```text
feat: add standalone Go sing-box-core daemon with outbound hot-swap control socket
```

---

# Phase 2: Replace the systemd sing-box unit with the daemon

## Goal

Make `singbox-core` what `systemctl start sing-box` actually runs, so the
control socket is always available, while behavior for anything not yet
wired to it stays identical to today.

## Tasks

- [ ] Update `systemd/sing-box.service`'s `ExecStart` to the new daemon
      binary; the daemon reads the same `/etc/sing-box/config.json` on boot
      and starts the embedded Box the same way `sing-box run` did.
- [ ] Keep `write_and_check()` in `builder.py` unchanged -- it still validates
      full configs with `sing-box check` before anything is applied.
- [ ] `reload_singbox()` (shipped in the previous change) becomes the
      fallback path for cases that still need a full restart: rule-set
      changes, main config structural changes, or the daemon itself being
      upgraded. It is not removed.
- [ ] Deploy to the Pi via `scripts/update.sh` and confirm `totalray` and the
      new `sing-box` unit both come up clean, exactly as verified for the
      SIGHUP change.

## Acceptance Criteria

- `systemctl status sing-box` shows the new binary running, Clash API and TUN
  behave identically to before from the user's perspective.
- A full restart (via `reload_singbox`'s fallback path) still works
  end-to-end, unchanged.

## Suggested Commit

```text
feat: run sing-box via the embedded Go daemon instead of the stock CLI
```

---

# Phase 3: Standby outbound pool (replaces the leaf process pool)

## Goal

Always keep more than one verified Pool B server registered as a live,
selectable outbound -- without any of the leaf plan's per-process machinery.

## Tasks

- [ ] Decide N (proposed: 2 standbys + 1 active = 3 outbounds registered at
      once; smaller than the leaf plan's default because there is no
      per-candidate process/port cost anymore).
- [ ] `builder.py`: replace the tag-diff-then-full-rebuild logic with calls to
      the daemon's socket API -- `add_outbound` for a newly promoted Pool B
      candidate, `remove_outbound` for one being retired, `select` via the
      existing Clash API call for switching which one is active.
- [ ] Never `remove_outbound` the tag that is currently selected. If the
      active tag needs to go, `select` to a healthy standby first, then
      remove it.
- [ ] Unit tests: churn scenarios (add while 3 are already registered, remove
      the active one, remove a standby) against a mock of the control socket.
- [ ] Integration test: full pipeline test -- Pool A promotes a candidate,
      it's registered as a standby, and it becomes selectable without any
      restart.

## Acceptance Criteria

- At all times outside of a brief transition window, at least one verified
  standby outbound is registered and selectable.
- Routine Pool B churn causes zero restarts and zero calls to
  `reload_singbox`'s fallback path.

## Suggested Commit

```text
feat: maintain a standby outbound pool via the sing-box-core daemon
```

---

# Phase 4: Passive degradation signal for the active outbound

## Goal

Detect that the *currently active* outbound has degraded, using only signals
that already exist from real traffic -- no extra probe traffic against it.

## Tasks

- [ ] New module `totalray/health_signal.py`: tail the daemon's log stream (or
      journal, if still routed there) for connection-level error patterns on
      the active outbound's tag specifically -- timeouts, resets, "closed
      pipe" style errors (the same pattern observed live during the SIGHUP
      test).
- [ ] Maintain a short rolling error-rate window (e.g. errors per 10s) rather
      than reacting to a single line.
- [ ] Add an OS-level signal alongside the log one: periodically (e.g. every
      5s) sample TCP retransmit counters for the active outbound's
      connections (`ss -ti` or `TCP_INFO` via a small helper), since
      retransmits are the kernel's own reaction to real packet loss and are
      completely independent of anything sing-box reports.
- [ ] Hysteresis: require the error-rate or retransmit-rate to cross a
      threshold for N consecutive samples (not one) before flagging
      degradation, to avoid reacting to a single blip.
- [ ] Unit tests: feed synthetic log lines and retransmit samples, assert the
      hysteresis window behaves correctly for both a real degradation and a
      one-off blip.

## Acceptance Criteria

- A sustained real disruption on the active outbound (simulated via `tc`
  netem packet loss in testing) is flagged within the target window (propose:
  under 15 seconds) without adding any probe traffic of our own.
- A single transient error does not flag degradation.

## Suggested Commit

```text
feat: add passive log- and retransmit-based degradation signal for the active outbound
```

---

# Phase 5: Active bounded probing of standby outbounds only

## Goal

Know which standby is actually healthy *before* it's needed, safely, since
probing a standby carries no risk of disrupting live traffic.

## Tasks

- [ ] Periodic probe (e.g. every 5-10s) of each standby via the existing
      Clash API `/proxies/{tag}/delay` endpoint -- this is sing-box's own
      on-demand urltest-style check for one outbound, safe to call frequently
      against a tag nobody is using yet.
- [ ] Track each standby's last-known-good status and delay.
- [ ] Unit tests: mock the Clash API delay endpoint, verify status tracking
      and staleness handling (a standby not successfully probed in N seconds
      is not considered ready).

## Acceptance Criteria

- At any moment, there is a clear, current answer to "which standby, if any,
  is safe to fail over to right now."

## Suggested Commit

```text
feat: continuously probe standby outbounds via Clash API delay checks
```

---

# Phase 6: Failover trigger

## Goal

Tie Phase 4's degradation signal and Phase 5's standby health together into
an actual switch, plus background refill of the vacated slot.

## Tasks

- [ ] When Phase 4 flags the active outbound as degraded: `select` (via
      Clash API, the already-proven mechanism) to the best ready standby from
      Phase 5.
- [ ] Flap guard: a minimum cooldown between switches (e.g. 30s), independent
      of Phase 4's own hysteresis.
- [ ] After switching, in the background: `remove_outbound` the degraded tag
      and `add_outbound` the next-best untried Pool B candidate to refill the
      standby slot (Phase 3's mechanism).
- [ ] Independent circuit breaker: if the newly-promoted standby also
      degrades within a short window repeatedly, stop auto-switching for that
      slot and mark it for manual review, mirroring the existing
      `ApplyCoordinator` circuit breaker but scoped to this new loop.
- [ ] End-to-end test: simulate degradation (via `tc netem`) on the active
      outbound in a test environment, confirm switch-over, confirm background
      refill, confirm the circuit breaker trips on repeated failure.

## Acceptance Criteria

- A real degradation event results in a switch to a healthy standby within
  the target window, with zero manual intervention.
- Repeated failures on the same slot trip the new circuit breaker rather than
  looping indefinitely.

## Suggested Commit

```text
feat: wire degradation detection to automatic standby failover
```

---

# Phase 7: Observability

## Tasks

- [ ] Extend `totalray status` with: which outbound is active, which
      standbys are registered and their last probe result, recent switch
      history, and circuit-breaker trips for this new loop.
- [ ] Include these fields in `--json` output.

## Acceptance Criteria

- From an incident, one can tell within seconds which outbound was active,
  when it was flagged degraded, and what it switched to.

## Suggested Commit

```text
feat: expose active/standby outbound health in totalray status
```

---

# Phase 8: Staged rollout on the Pi

## Tasks

1. [ ] Shadow mode: Phases 4-5's signals run and log their conclusions, but
       Phase 6 does not act on them yet -- compare against real user-visible
       issues over at least 48 hours.
2. [ ] Enable Phase 6 (actual failover) with conservative thresholds.
3. [ ] Monitor at least one week: switch count, false-positive rate, circuit
       breaker trips.
4. [ ] Tune thresholds based on real data before considering this done.

## Rollback Criteria

Revert Phase 6 to shadow-only (keep detecting, stop acting) immediately if:

- More than 1 false-positive switch per day (a switch with no corresponding
  real user-visible issue).
- Any circuit breaker trip that isn't clearly explained by a genuinely bad
  candidate.

## Suggested Commit

```text
chore: staged rollout plan for degradation-based failover
```

---

# Architecture Decision

The Go-embedded core (Phases 1-3) is approved and supersedes the multi-process
leaf architecture entirely -- it achieves the same "always have a warm
standby" goal with one process instead of N, no extra ports, and no duplicated
nftables bypass surface.

Phases 4-6 (degradation detection and failover) are the actual remaining
problem, since sing-box provides no packet-loss or throughput awareness
natively (confirmed against upstream source and open feature requests). These
phases must not go live acting automatically (Phase 6) until Phase 8's shadow
period has validated the signal against real conditions on this specific
network.
