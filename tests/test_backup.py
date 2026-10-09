"""Regression tests for backup integrity and preservation on failure."""

import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
import urllib.error
import urllib.parse
import zipfile

import make_odoo_backup as backup


def zip_payload(database="odoo", sql=b"-- PostgreSQL dump\nSELECT 1;"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("dump.sql", sql)
        archive.writestr("manifest.json", json.dumps({"db_name": database}))
        archive.writestr("filestore/ab/attachment", b"file contents")
    return buffer.getvalue()


class Response(io.BytesIO):
    status = 200


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            "BACKUP_PATH": self.root,
            "ODOO_MASTER_PASSWORD": "test secret + & =",
            "ODOO_BACKUP_DB": "odoo",
        }
        self.host = "example.invalid"
        self.info = {
            "backup_db_url": self.host,
            "backup_root_path": self.root / self.host / "daily",
        }
        self.info["backup_root_path"].mkdir(parents=True)
        self.tasks = [{"id": 1, "name": self.host}]

    def old_backup(self, name=None, days=6):
        path = self.info["backup_root_path"] / (
            name or self.host + "_2025_01_01_00_00_00.dump"
        )
        path.write_bytes(b"previous backup")
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def opener(self, payload):
        opener = Mock()
        opener.open.return_value = Response(payload)
        return patch.object(backup.urllib.request, "build_opener", return_value=opener), opener

    def assert_failed_download_preserves_old(self, opener):
        old = self.old_backup()
        with patch.object(backup.urllib.request, "build_opener", return_value=opener):
            with self.assertLogs(backup.logger, level="ERROR"):
                self.assertEqual(backup.backup_instances(self.tasks, "daily", self.config), 1)
        self.assertEqual(old.read_bytes(), b"previous backup")
        self.assertEqual(list(old.parent.iterdir()), [old])

    def test_single_post_encoded_secret_and_filestore(self):
        payload = zip_payload()
        context, opener = self.opener(payload)
        with context:
            result = backup.make_backup(self.info, "daily", self.config)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://example.invalid/web/database/backup")
        fields = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(fields, {
            "master_pwd": ["test secret + & ="], "name": ["odoo"], "backup_format": ["zip"],
        })
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 300)
        self.assertEqual(result.read_bytes(), payload)
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(archive.read("filestore/ab/attachment"), b"file contents")
        self.assertEqual(list(result.parent.iterdir()), [result])

    def test_http_error_preserves_old(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://example.invalid", 500, "error", {}, None
        )
        self.assert_failed_download_preserves_old(opener)

    def test_html_200_preserves_old(self):
        opener = Mock()
        opener.open.return_value = Response(b"<html>Database backup error</html>")
        self.assert_failed_download_preserves_old(opener)

    def test_timeout_preserves_old(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError("fixture")
        self.assert_failed_download_preserves_old(opener)

    def test_interrupted_download_preserves_old(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.read.side_effect = [b"PK partial", ConnectionError("interrupted")]
        opener = Mock()
        opener.open.return_value = response
        self.assert_failed_download_preserves_old(opener)

    def test_truncated_zip_preserves_old(self):
        opener = Mock()
        opener.open.return_value = Response(zip_payload()[:-30])
        self.assert_failed_download_preserves_old(opener)

    def test_wrong_database_preserves_old(self):
        opener = Mock()
        opener.open.return_value = Response(zip_payload(database="another_database"))
        self.assert_failed_download_preserves_old(opener)

    def test_empty_dump_preserves_old(self):
        opener = Mock()
        opener.open.return_value = Response(zip_payload(sql=b""))
        self.assert_failed_download_preserves_old(opener)

    def test_corrupt_filestore_preserves_old(self):
        payload = zip_payload().replace(b"file contents", b"FILE CONTENTS", 1)
        opener = Mock()
        opener.open.return_value = Response(payload)
        self.assert_failed_download_preserves_old(opener)

    def test_missing_manifest_preserves_old(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("dump.sql", b"SELECT 1;")
        opener = Mock()
        opener.open.return_value = Response(buffer.getvalue())
        self.assert_failed_download_preserves_old(opener)

    def test_failed_publish_preserves_old(self):
        context, _ = self.opener(zip_payload())
        with context, patch.object(backup.os, "replace", side_effect=OSError("disk full")):
            old = self.old_backup()
            with self.assertLogs(backup.logger, level="ERROR"):
                self.assertEqual(backup.backup_instances(self.tasks, "daily", self.config), 1)
            self.assertEqual(list(old.parent.iterdir()), [old])

    def test_valid_backup_triggers_filtered_rotation(self):
        old = self.old_backup()
        unrelated = self.old_backup("notes.txt")
        other_instance = self.old_backup("other.invalid_2025_01_01_00_00_00.dump")
        partial = self.old_backup("interrupted.part")
        recent = self.old_backup(self.host + "_2026_10_08_00_00_00.zip", days=1)
        link = old.parent / (self.host + "_2025_02_01_00_00_00.dump")
        link.symlink_to(unrelated)
        context, _ = self.opener(zip_payload())
        with context:
            self.assertEqual(backup.backup_instances(self.tasks, "daily", self.config), 0)
        self.assertFalse(old.exists())
        for path in [unrelated, other_instance, partial, recent, link]:
            self.assertTrue(path.exists())
        self.assertEqual(len(list(old.parent.glob("*.zip"))), 2)

    def test_monthly_retention_remains_155_days(self):
        self.info["backup_root_path"] = self.root / self.host / "monthly"
        self.info["backup_root_path"].mkdir()
        old = self.old_backup(days=156)
        recent = self.old_backup(self.host + "_2025_02_01_00_00_00.zip", days=154)
        backup.remove_old_backup(self.info, "monthly")
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_next_instance_continues_after_failure(self):
        calls = []
        def download(info, *_):
            calls.append(info["backup_db_url"])
            if len(calls) == 1:
                raise ValueError("fixture")
        tasks = self.tasks + [{"id": 2, "name": "second.invalid"}]
        with patch.object(backup, "make_backup", side_effect=download):
            with patch.object(backup, "remove_old_backup") as purge:
                with self.assertLogs(backup.logger, level="ERROR"):
                    self.assertEqual(backup.backup_instances(tasks, "daily", self.config), 1)
        self.assertEqual(calls, [self.host, "second.invalid"])
        purge.assert_called_once()
        self.assertEqual(purge.call_args.args[0]["backup_db_url"], "second.invalid")

    def test_unsafe_task_name_rejected_before_io(self):
        for host in ["../../outside", "https://example.invalid", "example.invalid/path", "-bad"]:
            with self.subTest(host=host), patch.object(backup, "make_backup") as download:
                with self.assertLogs(backup.logger, level="ERROR"):
                    result = backup.backup_instances([{"name": host}], "daily", self.config)
                self.assertEqual(result, 1)
                download.assert_not_called()

    def test_redirects_are_rejected(self):
        self.assertIsNone(backup.NoRedirect().redirect_request(
            None, None, 307, "redirect", {}, "https://other.invalid"
        ))

    def test_missing_master_secret_fails_before_network(self):
        env = {key: "fixture" for key in backup.REQUIRED_ENV}
        env.pop("ODOO_MASTER_PASSWORD")
        with patch.dict(os.environ, env, clear=True), patch.object(backup, "load_dotenv"):
            with self.assertRaisesRegex(ValueError, "ODOO_MASTER_PASSWORD"):
                backup.load_config()

    def test_existing_lock_prevents_network(self):
        with (self.root / ".backup.lock").open("a") as lock:
            backup.fcntl.flock(lock, backup.fcntl.LOCK_EX | backup.fcntl.LOCK_NB)
            with patch.object(backup, "get_file_params", return_value="daily"):
                with patch.object(backup, "load_config", return_value=self.config):
                    with patch.object(backup, "get_db_to_backup") as lookup:
                        with self.assertLogs(backup.logger, level="ERROR"):
                            self.assertEqual(backup.main(), 1)
                        lookup.assert_not_called()

    def test_relative_backup_path_rejected(self):
        env = {key: "fixture" for key in backup.REQUIRED_ENV}
        env["ODOO_URL"] = "https://personal.invalid"
        env["BACKUP_PATH"] = "relative/backups"
        with patch.dict(os.environ, env, clear=True), patch.object(backup, "load_dotenv"):
            with self.assertRaisesRegex(ValueError, "absolute"):
                backup.load_config()


if __name__ == "__main__":
    unittest.main()
