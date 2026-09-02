from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fetch_posts as fp  # noqa: E402
from fetch_posts import Config, discover_credential_files, normalize_credential_paths, save_config  # noqa: E402


def _write_sa(path: Path, email: str) -> Path:
    path.write_text(json.dumps({"client_email": email, "type": "service_account"}), encoding="utf-8")
    return path


class CredentialDiscoverTests(unittest.TestCase):
    def test_normalize_skips_missing_and_dedupes(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_sa(Path(td) / "a.json", "a@x.iam.gserviceaccount.com")
            out = normalize_credential_paths([str(path), str(path), str(Path(td) / "missing.json"), ""])
            self.assertEqual(out, [str(path.resolve())])

    def test_top_level_is_source_of_truth_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            kept = _write_sa(root / "credentials-keep.json", "keep@x.iam.gserviceaccount.com")
            extra = _write_sa(root / "credentials-extra.json", "extra@x.iam.gserviceaccount.com")
            cfg = Config(credentials_file=str(kept), credentials_files=[str(kept)])
            menus = [{"settings": {"credentials_files": [str(extra)]}}]
            with patch.object(fp, "SCRIPT_DIR", root):
                out = discover_credential_files(cfg, menus)
            self.assertEqual(out, [str(kept.resolve())])
            self.assertNotIn(str(extra.resolve()), out)

    def test_recovers_menu_and_copied_files_when_top_level_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            menu_file = _write_sa(root / "credentials-menu.json", "menu@x.iam.gserviceaccount.com")
            copied = _write_sa(root / "credentials-copied.json", "copied@x.iam.gserviceaccount.com")
            cfg = Config(credentials_file="", credentials_files=[])
            menus = [{"settings": {"credentials_file": str(menu_file), "credentials_files": [str(menu_file)]}}]
            with patch.object(fp, "SCRIPT_DIR", root):
                out = discover_credential_files(cfg, menus)
            self.assertEqual(out, [str(menu_file.resolve()), str(copied.resolve())])

    def test_save_config_fills_top_level_from_menus(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sa = _write_sa(root / "credentials-a.json", "a@x.iam.gserviceaccount.com")
            cfg = Config()
            cfg.ui_menus = [
                {"id": "m1", "settings": {"credentials_files": [str(sa)], "credentials_file": str(sa)}}
            ]
            dest = root / "config.json"
            with patch.object(fp, "SCRIPT_DIR", root):
                save_config(cfg, dest)
            data = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(data["credentials_files"], [str(sa.resolve())])
            self.assertEqual(data["credentials_file"], str(sa.resolve()))


if __name__ == "__main__":
    unittest.main()
