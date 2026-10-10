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
                  active_tag: str | None = None) -> str:
        """Apply the desired outbounds and return a human-readable summary."""
        if not self.client.is_running():
            raise SingboxCoreError("singbox-core is not running")

        wanted = self._outbounds(desired)
        old = self._outbounds(previous or {})
        wanted_tags = set(wanted)
        old_tags = set(old)
        new_leaf_tags = (wanted_tags - old_tags) - set(self.GROUP_TAGS)
        removed_leaf_tags = (old_tags - wanted_tags) - set(self.GROUP_TAGS)
        if active_tag and active_tag in removed_leaf_tags:
            raise SingboxCoreError(
                f"refusing to remove currently selected outbound {active_tag}")
        groups_changed = any(wanted.get(tag) != old.get(tag) for tag in self.GROUP_TAGS)

        for tag in sorted(new_leaf_tags):
            self.client.add_outbound(wanted[tag])

        if groups_changed:
            # select depends on auto, so remove it first. Route rules refer to
            # the tag and are unchanged; the transition is intentionally tiny.
            for tag in reversed(self.GROUP_TAGS):
                if tag in old:
                    self.client.remove_outbound(tag)
            for tag in self.GROUP_TAGS:
                if tag in wanted:
                    self.client.add_outbound(wanted[tag])

        for tag in sorted(removed_leaf_tags):
            self.client.remove_outbound(tag)

        changed = len(new_leaf_tags) + len(removed_leaf_tags)
        if groups_changed:
            changed += len(self.GROUP_TAGS)
        return f"live pool reconciled ({changed} outbound changes)"
