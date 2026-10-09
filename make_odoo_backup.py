"""Download validated Odoo backups before rotating older copies."""

import argparse
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.parse
import urllib.request
import xmlrpc.client
import zipfile

from dotenv import load_dotenv


logger = logging.getLogger(__name__)
REQUIRED_ENV = (
    "ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_PASSWORD",
    "ODOO_MASTER_PASSWORD", "BACKUP_PATH",
)
HOST_PATTERN = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?::[0-9]{1,5})?"
)


def get_file_params():
    """Require an explicit daily or monthly backup period."""
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--period", required=True,
                        choices=("daily", "monthly"))
    return parser.parse_args().period


def load_config():
    """Fail before doing any work when credentials or storage are missing."""
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    config = {key: os.getenv(key) for key in REQUIRED_ENV}
    missing = [key for key, value in config.items() if not value]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))
    if not config["ODOO_URL"].startswith("https://"):
        raise ValueError("ODOO_URL must use HTTPS")
    backup_path = Path(config["BACKUP_PATH"])
    if not backup_path.is_absolute():
        raise ValueError("BACKUP_PATH must be absolute")
    config["BACKUP_PATH"] = backup_path.resolve()
    if not config["BACKUP_PATH"].is_dir():
        raise ValueError("BACKUP_PATH must be an existing backup directory")
    config["ODOO_BACKUP_DB"] = os.getenv("ODOO_BACKUP_DB") or "odoo"
    return config


def get_db_to_backup(config):
    """Read instance hostnames from project tasks tagged To backup."""
    url = config["ODOO_URL"].rstrip("/")
    common = xmlrpc.client.ServerProxy(url + "/xmlrpc/2/common")
    uid = common.authenticate(config["ODOO_DB"], config["ODOO_USERNAME"],
                              config["ODOO_PASSWORD"], {})
    if not uid:
        raise ValueError("Odoo authentication failed")
    models = xmlrpc.client.ServerProxy(url + "/xmlrpc/2/object")
    return models.execute_kw(
        config["ODOO_DB"], uid, config["ODOO_PASSWORD"],
        "project.task", "search_read", [[("tag_ids.name", "=", "To backup")]],
        {"fields": ["name"]},
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Reject redirects rather than forwarding database credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_backup(path, database):
    """Check ZIP CRCs and the expected Odoo database/manifest members."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if names.count("dump.sql") != 1 or names.count("manifest.json") != 1:
            raise ValueError("Backup is missing unique dump.sql/manifest.json")
        if archive.getinfo("dump.sql").file_size == 0:
            raise ValueError("Database dump is empty")
        if archive.testzip() is not None:
            raise ValueError("Backup ZIP failed its integrity check")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("db_name") != database:
            raise ValueError("Backup manifest does not match the requested database")


def make_backup(db_info, backup_type, config):
    """Send one POST and publish the ZIP only after successful validation."""
    root = Path(db_info["backup_root_path"])
    root.mkdir(parents=True, exist_ok=True)
    host = db_info["backup_db_url"]
    filename = host + "_" + datetime.now().strftime("%Y_%m_%d_%H_%M_%S_%f") + ".zip"
    destination = root / filename
    payload = urllib.parse.urlencode({
        "master_pwd": config["ODOO_MASTER_PASSWORD"],
        "name": config["ODOO_BACKUP_DB"],
        "backup_format": "zip",
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://" + host + "/web/database/backup", data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    opener = urllib.request.build_opener(NoRedirect())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, suffix=".part", delete=False) as output:
            temporary = Path(output.name)
            with opener.open(request, timeout=300) as response:
                if response.status != 200:
                    raise ValueError("Unexpected backup HTTP status")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        validate_backup(temporary, config["ODOO_BACKUP_DB"])
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    logger.info("Validated %s backup for %s", backup_type, host)
    return destination


def remove_old_backup(db_info, backup_type):
    """Keep the existing age policy, deleting only recognized backup files."""
    root = Path(db_info["backup_root_path"])
    pattern = re.compile(
        re.escape(db_info["backup_db_url"])
        + r"_\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}(?:_\d{6})?\.(?:zip|dump)"
    )
    days = 5 if backup_type == "daily" else 155
    cutoff = time.time() - days * 86400
    for path in root.iterdir():
        if (pattern.fullmatch(path.name) and not path.is_symlink()
                and path.is_file() and path.stat().st_mtime < cutoff):
            path.unlink()
            logger.info("Removed old %s backup: %s", backup_type, path.name)


def backup_instances(backup_dbs, backup_type, config):
    """Continue after an instance fails and report an overall failing status."""
    failed = False
    for backup_db in backup_dbs:
        host = backup_db["name"]
        if not isinstance(host, str) or not HOST_PATTERN.fullmatch(host):
            logger.error("Invalid instance hostname in task %s", backup_db.get("id"))
            failed = True
            continue
        db_info = {
            "backup_db_url": host,
            "backup_root_path": config["BACKUP_PATH"] / host / backup_type,
        }
        try:
            make_backup(db_info, backup_type, config)
            remove_old_backup(db_info, backup_type)
        except Exception:  # Each failed instance must preserve its previous backups.
            logger.error("Backup or rotation failed for %s; check storage and Odoo", host)
            failed = True
    return 1 if failed else 0


def main():
    """Run under a storage-wide lock and return a status suitable for cron."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    backup_type = get_file_params()
    try:
        config = load_config()
        with (config["BACKUP_PATH"] / ".backup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            backup_dbs = get_db_to_backup(config)
            if not backup_dbs:
                logger.error("No instances tagged To backup; nothing was backed up")
                return 1
            return backup_instances(backup_dbs, backup_type, config)
    except Exception as error:
        logger.error("Backup run failed (%s); check configuration, lock, storage and Odoo",
                     type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
