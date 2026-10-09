# Scripts Odoo Backup

Download Odoo backups from instances listed in project tasks tagged `To backup`.
Each task name must be a hostname (for example `customer.example.com`), optionally
with a port, without a scheme or path.

## Configure

Use Python 3.8+ on Linux (including Synology); the execution lock uses `fcntl`.
Install dependencies with `python3 -m pip install -r requirements.txt`.
Copy `.env.example` to `.env` next to the script and restrict access:

```sh
cp .env.example .env
chmod 600 .env
```

- `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_PASSWORD`: credentials for
  the personal Odoo containing the project tasks. `ODOO_URL` must use HTTPS.
- `ODOO_MASTER_PASSWORD`: database manager master password for the target
  instances, separate from the personal Odoo user password. It is required;
  there is no hard-coded fallback. This version uses one shared master password.
- `ODOO_BACKUP_DB`: database name on the target instances; defaults to `odoo`.
- `ODOO_BACKUP_TIMEOUT`: positive socket timeout in seconds; defaults to 300.
  For large databases this can be increased, e.g. to 1800. Proxy/server timeouts
  are independent and cannot be increased by this setting.
- `BACKUP_PATH`: existing absolute directory on the NAS or its mounted share.
  The script writes directly here; it does not perform a separate SFTP transfer.

**Migration:** rotate the master password previously published in the repository
on every affected instance, then configure its replacement in `.env`.
The previous password remains exposed in Git history; this change does not
rewrite that history or change credentials on your servers.

If using a NAS mount, have the scheduler verify the share is actually mounted
before starting. An existing local mountpoint alone does not prove NAS availability.
Use a dedicated backup directory with restricted access.

## Execute

```sh
python3 make_odoo_backup.py -p daily
python3 make_odoo_backup.py -p monthly
python3 make_odoo_backup.py -p daily --instance customer.example.com
```

Example cron entries (replace the paths with your installation):

```cron
0 18 * * * /usr/bin/python3 /home/XX/backups/scripts_odoo_backup/make_odoo_backup.py -p daily >> /home/XX/backups/odoo_backup.log 2>&1
30 18 1 * * /usr/bin/python3 /home/XX/backups/scripts_odoo_backup/make_odoo_backup.py -p monthly >> /home/XX/backups/odoo_backup.log 2>&1
```

The environment file is loaded relative to the script, independent of cron's
working directory. An execution lock in `BACKUP_PATH` prevents overlapping runs.
Daily and monthly jobs must have separate schedules; the script does not schedule
itself. The monthly example runs on the first day of each month.

The optional `--instance` filter selects an exact hostname from the tagged tasks.
An unknown hostname fails without performing a backup or rotation. Logs record
the start, downloaded byte count, and on failure the stage, duration and error
type or HTTP status. Credentials and server response bodies are not logged.

## Backup and rotation guarantees

- One HTTPS POST per instance to `/web/database/backup`, requesting `zip`.
  The ZIP includes PostgreSQL and the filestore when present in Odoo.
- Master credentials are sent in the request body, not process arguments.
  HTTP errors and redirects fail the backup. The socket timeout is 300 seconds;
  it is configurable with `ODOO_BACKUP_TIMEOUT`, not a total execution deadline.
- Downloads go to a unique `.part` file in the destination directory.
  Before publication, the ZIP CRCs, non-empty `dump.sql`, and `manifest.json`
  database name are checked. The final `.zip` is published by atomic rename.
- Rotation occurs only after publication succeeds for that instance. On download,
  validation, or publication failure, previous backups are preserved and the
  temporary file is removed.
- Rotation recognizes only that instance's timestamped `.zip` and legacy
  `.dump` files. Other files, temporary files, directories, and symlinks are skipped.
- Retention remains age-based: 5 days for daily backups and 155 days for monthly
  backups, using modification time. This does **not** enforce exactly five daily
  dates or five calendar months; repeated executions retain multiple copies.
- An instance failure does not stop the next instance. Any failure, missing
  configuration, held lock, or empty task list produces a non-zero exit code.
  Capture stderr and configure scheduler failure notifications.

Legacy `.dump` backups are not converted to ZIP and may lack filestore files.
ZIP validation does not prove restorability: periodically restore a backup to an
isolated Odoo instance and verify records and attachments. NAS replication,
snapshots, alert delivery, calendar-based retention, and per-instance credentials
are outside this correction.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The regression tests simulate downloads and use temporary directories. They do
not contact production instances or the NAS.
