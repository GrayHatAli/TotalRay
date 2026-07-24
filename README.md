# TotalRay

TotalRay is a small manager for running and rotating sing-box outbound
configs on a Raspberry Pi gateway. It fetches subscription lists,
verifies server connectivity, builds the active sing-box configuration,
and provides a CLI for administration.

Quick start

1. Install (on Debian-based system):

```bash
sudo bash scripts/install.sh
```

2. Commands:

- `totalray init` — create database and directories
- `totalray add-sub <url>` — register a subscription
- `totalray update-rules` — download rule-sets
- `totalray status` — show overall status and per-device stats

Config: `/etc/totalray/config.yaml`
Data & logs: `/var/lib/totalray/` (rotating `totalray.log`) 

