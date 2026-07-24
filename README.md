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

Packaging / CI

- Installer: `scripts/install.sh` installs files under `/opt/totalray`, creates a venv, and installs Python dependencies from `requirements.txt`. It also installs a systemd unit at `/etc/systemd/system/totalray.service` and a CLI wrapper at `/usr/local/bin/totalray`.
- Uninstall: `scripts/uninstall.sh` removes installed files; use `--purge` to remove data.
- CI: there is no CI config in this repository; if you add GitHub Actions or other CI, point build/test steps to run `python -m py_compile totalray/*.py` and `pip install -r requirements.txt` inside the created environment.

Service & logs

- Start the service: `sudo systemctl start totalray`
- Enable at boot: `sudo systemctl enable totalray`
- View logs: `journalctl -u totalray -f` and `journalctl -u sing-box -f`.
- Failed HTTP requests and non-2xx responses are recorded in JSON-lines format at:
	`/var/lib/totalray/totalray_failed_requests.log` (rotate/inspect as needed).

Developer / testing

- Quick syntax check:

```bash
python3 -m py_compile totalray/*.py
```

- To run the CLI from the repository without installing, use:

```bash
cd /path/to/TotalRay && python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m totalray --config config.yaml status
```

