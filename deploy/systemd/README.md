# GISMO systemd service

This directory provides production-safe systemd units for running the GISMO daemon on a Linux host.
The units assume a dedicated service user and a read-only policy by default.

## 1) Create the service user/group

```bash
sudo useradd --system --user-group --home /opt/gismo --shell /usr/sbin/nologin gismo
```

## 2) Install GISMO under /opt/gismo

```bash
sudo mkdir -p /opt/gismo
sudo rsync -a --delete /path/to/GISMO/ /opt/gismo/
```

Alternatively, clone directly:

```bash
sudo git clone https://example.com/GISMO.git /opt/gismo
```

## 3) Prepare the state directory

The service writes its SQLite state database under `/var/lib/gismo`.

```bash
sudo mkdir -p /var/lib/gismo
sudo chown gismo:gismo /var/lib/gismo
sudo chmod 0750 /var/lib/gismo
```

## 4) Install the unit file

```bash
sudo cp /opt/gismo/deploy/systemd/gismo.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Optionally configure overrides via `/etc/gismo/gismo.env` (see below).

## 5) Enable and start

```bash
sudo systemctl enable --now gismo.service
```

## 6) Logs

```bash
journalctl -u gismo -f
```

## 7) Template instance (optional)

The template unit lets you run isolated instances that map to policy files and DB paths:

```bash
sudo cp /opt/gismo/deploy/systemd/gismo@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gismo@dev-safe.service
```

This starts the daemon with:

- Policy: `/opt/gismo/policy/dev-safe.json`
- Database: `/var/lib/gismo/gismo-dev-safe.db`

## 8) Enqueue work against the same database

Use the same DB path the daemon is configured with:

```bash
/usr/bin/python3 -m gismo.cli.main enqueue "echo: hi" --db /var/lib/gismo/gismo.db
```

For the templated instance:

```bash
/usr/bin/python3 -m gismo.cli.main enqueue "echo: hi" --db /var/lib/gismo/gismo-dev-safe.db
```

## 9) Optional environment overrides

The unit reads `/etc/gismo/gismo.env` if present. Override values like:

```bash
GISMO_DB_PATH=/var/lib/gismo/gismo.db
GISMO_POLICY_PATH=/opt/gismo/policy/readonly.json
GISMO_SLEEP_SECONDS=2.0
```

Apply changes with:

```bash
sudo systemctl daemon-reload
sudo systemctl restart gismo.service
```
