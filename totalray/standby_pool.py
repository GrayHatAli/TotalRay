"""Live selector-group reconciliation for the embedded sing-box core."""
from __future__ import annotations

import logging

from .singbox_core import SingboxCoreClient, SingboxCoreError

log = logging.getLogger(__name__)


class StandbyPool:
    """Keep a small active-plus-standby set registered without restarting.

    sing-box selector membership is fixed at selector start. The stable
    ``auto`` and ``select`` tags are therefore replaced as a pair whenever
    membership changes. New leaf outbounds are created before the groups are
    removed; stale leaves are removed only after the groups no longer depend
    on them.
    """

    GROUP_TAGS = ("auto", "select")

    def __init__(self, client: SingboxCoreClient):
        self.client = client

    @staticmethod
    def _outbounds(config: dict) -> dict[str, dict]:
        return {item["tag"]: item for item in config.get("outbounds", [])
                if item.get("tag")}

    def reconcile(self, desired: dict, previous: dict | None = None,
                  active_tag: str | None = None,
                  retire_active: bool = False) -> str:
        """Apply the desired outbounds and return a human-readable summary.

        ``retire_active`` permits removing the currently selected leaf, but only
        when the groups are being replaced in the same call: the new ``auto``
        group no longer references the old leaf, so it is retired (its
        connections close) instead of forcing a full sing-box restart.
        """
        if not self.client.is_running():
            raise SingboxCoreError("singbox-core is not running")

        wanted = self._outbounds(desired)
        old = self._outbounds(previous or {})
        wanted_tags = set(wanted)
        old_tags = set(old)
        new_leaf_tags = (wanted_tags - old_tags) - set(self.GROUP_TAGS)
        removed_leaf_tags = (old_tags - wanted_tags) - set(self.GROUP_TAGS)
        groups_changed = any(wanted.get(tag) != old.get(tag) for tag in self.GROUP_TAGS)
        if active_tag and active_tag in removed_leaf_tags:
            if not (retire_active and groups_changed):
                raise SingboxCoreError(
                    f"refusing to remove currently selected outbound {active_tag}")
            log.warning("retiring active outbound %s together with its group "
                        "replacement (no surviving standby)", active_tag)

        for tag in sorted(new_leaf_tags):
            self.client.add_outbound(wanted[tag])

        dns_rebound = 0
        dns_failed: list[str] = []
        if groups_changed:
            # select depends on auto, so remove it first. Route rules refer to
            # the tag and are unchanged; the transition is intentionally tiny.
            for tag in reversed(self.GROUP_TAGS):
                if tag in old:
                    self.client.remove_outbound(tag)
            for tag in self.GROUP_TAGS:
                if tag in wanted:
                    self.client.add_outbound(wanted[tag])
            # A DNS server that detours through a group keeps the OLD group
            # object (sing-box resolves a detour once), so it would die with
            # the retired chain. Rebind it to the groups just re-added.
            dns_rebound, dns_failed = self._rebind_dns(desired)

        for tag in sorted(removed_leaf_tags):
            self.client.remove_outbound(tag)

        changed = len(new_leaf_tags) + len(removed_leaf_tags)
        if groups_changed:
            changed += len(self.GROUP_TAGS)
        summary = f"live pool reconciled ({changed} outbound changes)"
        if dns_rebound:
            summary += f", {dns_rebound} DNS server(s) rebound"
        if dns_failed:
            summary += f", DNS rebind FAILED for {', '.join(dns_failed)}"
        return summary

    def _rebind_dns(self, desired: dict) -> tuple[int, list[str]]:
        """Reload every DNS server whose detour is a replaced group.

        Best effort: outbound reconciliation has already succeeded by the
        time this runs, so a failure here is reported but never raised.
        """
        servers = (desired.get("dns") or {}).get("servers") or []
        rebound = 0
        failed: list[str] = []
        for server in servers:
            tag = server.get("tag")
            if not tag or server.get("detour") not in self.GROUP_TAGS:
                continue
            try:
                self.client.reload_dns_server(server)
            except (SingboxCoreError, OSError) as exc:
                log.warning("could not rebind DNS server %s to the live groups: %s",
                            tag, exc)
                failed.append(tag)
            else:
                rebound += 1
        return rebound, failed
