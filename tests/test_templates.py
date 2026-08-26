from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from catalog_merge import _col_index  # noqa: E402
from video_duration import _custom_report_matrix, _per_video_count  # noqa: E402


class TemplateConfigTests(unittest.TestCase):
    def test_new_template_config_is_parsed(self):
        cfg = app._cfg_from_payload(
            {
                "catalog_url_col": "c",
                "catalog_sheet_col": "f",
                "align_mappings": [{"target": "姓名", "source": "名字"}],
                "align_mapping_profiles": {"url-1": [{"target": "姓名", "source": "人员"}]},
                "vd_source_sheets": ["8月份", "9月份"],
                "vd_type_filter_mode": "exclude",
                "vd_date_filter_enabled": False,
                "vd_columns": [
                    {"field": "日期", "role": "date", "column": "A"},
                    {"field": "链接", "role": "link", "column": "B"},
                    {"field": "人员", "role": "name", "column": "H"},
                    {"field": "类型", "role": "type", "column": "E"},
                ],
            }
        )
        self.assertEqual(cfg.catalog_url_col, "c")
        self.assertEqual(cfg.catalog_sheet_col, "f")
        self.assertEqual(cfg.align_headers, ["姓名"])
        self.assertEqual(cfg.align_mappings[0]["source"], "名字")
        self.assertEqual(cfg.vd_source_sheets, ["8月份", "9月份"])
        self.assertFalse(cfg.vd_date_filter_enabled)
        self.assertEqual(cfg.vd_columns[-1]["column"], "E")

    def test_config_api_exposes_all_templates(self):
        response = app.app.test_client().get("/api/config")
        self.assertEqual(response.status_code, 200)
        cfg = response.get_json()["config"]
        for key in ("ui_menus", "catalog_url_col", "align_mappings", "vd_columns"):
            self.assertIn(key, cfg)

    def test_source_specific_field_mapping_overrides_default(self):
        source_url = "https://docs.google.com/spreadsheets/d/abcdefghijklmnopqrstuvwx123456/edit"
        cfg = app.Config(
            align_sources=[{"url": source_url, "sheet": "数据"}],
            align_target_url="https://docs.google.com/spreadsheets/d/targetabcdefghijklmnopqrstuv/edit",
            align_output_sheet="结果",
            align_mappings=[{"target": "姓名", "source": "名字"}, {"target": "数量", "source": "总量"}],
            align_mapping_profiles={source_url: [{"target": "姓名", "source": "人员"}, {"target": "数量", "source": "数目"}]},
        )
        cfg.resolve_credentials = lambda: ROOT / "config.example.json"
        source_ws = SimpleNamespace(title="数据")
        source_ss = SimpleNamespace(title="源表", id="source-id")
        target_ss = SimpleNamespace(title="目标表", id="target-id")
        written = {}

        def fake_open(_gc, value, log=print):
            return source_ss if value == source_url else target_ss

        def fake_write(_ss, **kwargs):
            written.update(kwargs)

        with (
            patch("fetch_posts.authorize", return_value=object()),
            patch("fetch_posts.open_by_url_or_id", side_effect=fake_open),
            patch("fetch_posts.pick_source_ws", return_value=source_ws),
            patch("fetch_posts.read_sheet_values", return_value=[["人员", "数目"], ["小王", 3]]),
            patch("fetch_posts.write_output", side_effect=fake_write),
            patch("fetch_posts.remember_run_state"),
        ):
            result = app.run_align_sync(cfg, log=lambda _message: None)
        self.assertTrue(result["ok"])
        self.assertEqual(written["headers"], ["姓名", "数量"])
        self.assertEqual(written["rows"], [["小王", 3]])


class CatalogTests(unittest.TestCase):
    def test_column_letters(self):
        self.assertEqual(_col_index("A"), 0)
        self.assertEqual(_col_index("D"), 3)
        self.assertEqual(_col_index("AA"), 26)
        with self.assertRaises(RuntimeError):
            _col_index("4")


class VideoReportTests(unittest.TestCase):
    def test_per_video_step_count(self):
        values = [30, 34.9, 35, 60, 70, 89, 90, 120]
        self.assertEqual([_per_video_count(value) for value in values], [1, 1, 2, 2, 2, 2, 3, 4])

    def test_custom_report_layout(self):
        records = [
            {"name": "甲", "type": "图片", "date": "2026-08-01", "sec": 30},
            {"name": "甲", "type": "视频", "date": "2026-08-01", "sec": 60},
            {"name": "乙", "type": "图片", "date": "2026-08-02", "sec": 90},
        ]
        names, categories, rows = _custom_report_matrix(
            records, 30, ["图片", "视频"], "per_video_ceil", preferred_names=["乙", "甲"]
        )
        self.assertEqual(names, ["乙", "甲"])
        self.assertEqual(categories, ["图片", "视频"])
        self.assertEqual(rows[0][1], "乙")
        self.assertTrue(all(value == "" for value in rows[1]))
        self.assertEqual(rows[2][0], "类型")
        self.assertEqual(rows[4][0], "汇总")
        self.assertEqual(rows[7][0], "2026-08-01")


if __name__ == "__main__":
    unittest.main()
