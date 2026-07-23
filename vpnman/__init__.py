"""vpnman - sing-box client manager for Raspberry Pi.

Fetches subscriptions, scores configs with real connectivity tests,
automatically removes dead ones, and builds the final sing-box config
(transparent gateway mode).

Two-pool model:
  Pool A ("candidates") - every config fetched from subscriptions.
  Pool B ("active")     - configs that passed a real connectivity test
                           with latency under the configured threshold.
                           The load balancer / proxy group is built
                           exclusively from Pool B, so end users are
                           never routed through an untested or flaky
                           config.
"""

__version__ = "1.1.0"
