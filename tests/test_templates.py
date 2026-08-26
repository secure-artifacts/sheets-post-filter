from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from catalog_merge import _col_index, _pick_catalog_ws, run_catalog_merge  # noqa: E402
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

    def test_original_video_template_keeps_log_by_default(self):
        self.assertTrue(app.Config().vd_write_log)

    def test_each_menu_has_an_independent_job_lock(self):
        first_job, first_lock = app._job_parts("menu-a-test")
        second_job, second_lock = app._job_parts("menu-b-test")
        self.assertIsNot(first_job, second_job)
        self.assertTrue(first_lock.acquire(blocking=False))
        try:
            self.assertTrue(second_lock.acquire(blocking=False))
            second_lock.release()
            self.assertFalse(first_lock.acquire(blocking=False))
        finally:
            if first_lock.locked():
                first_lock.release()

    def test_status_is_scoped_to_menu(self):
        first, _ = app._job_parts("status-menu-a")
        second, _ = app._job_parts("status-menu-b")
        first.update(running=True, logs=[{"t": "10:00:00", "msg": "A"}])
        second.update(running=False, logs=[{"t": "10:00:01", "msg": "B"}])
        client = app.app.test_client()
        self.assertTrue(client.get("/api/status?job_id=status-menu-a").get_json()["running"])
        self.assertEqual(client.get("/api/status?job_id=status-menu-b").get_json()["logs"][0]["msg"], "B")

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

    def test_sheet_title_ignores_hidden_and_full_width_spaces(self):
        expected = SimpleNamespace(title="1Grupo 录")
        spreadsheet = SimpleNamespace(worksheets=lambda: [expected, SimpleNamespace(title="其他")])
        self.assertIs(_pick_catalog_ws(spreadsheet, "1Grupo\u200b　录"), expected)

    def test_sheet_names_are_searched_across_all_links(self):
        index_ws = SimpleNamespace(title="目录")
        wanted_ws = SimpleNamespace(title="目标录")
        other_ws = SimpleNamespace(title="别的表")
        index_ss = SimpleNamespace(title="索引", id="index", worksheets=lambda: [index_ws])
        source_a = SimpleNamespace(title="来源A", id="source-a", worksheets=lambda: [other_ws])
        source_b = SimpleNamespace(title="来源B", id="source-b", worksheets=lambda: [wanted_ws])
        target_ss = SimpleNamespace(title="目标", id="target")
        cfg = app.Config(
            catalog_index_url="index-url",
            catalog_target_url="target-url",
            catalog_index_sheet="目录",
            catalog_url_col="B",
            catalog_sheet_col="D",
            catalog_start_row=2,
        )
        cfg.resolve_credentials = lambda: ROOT / "config.example.json"

        def fake_open(_gc, value, log=print):
            return {"index-url": index_ss, "url-a": source_a, "url-b": source_b, "target-url": target_ss}[value]

        def fake_read(ws, log=print):
            if ws is index_ws:
                return [["", "", "", ""], ["", "url-a", "", ""], ["", "url-b", "", ""], ["", "", "", "目标录"]]
            if ws is wanted_ws:
                return [["表头"], ["数据"]]
            return []

        written = {}
        with (
            patch("catalog_merge.authorize", return_value=object()),
            patch("catalog_merge.open_by_url_or_id", side_effect=fake_open),
            patch("catalog_merge.pick_source_ws", return_value=index_ws),
            patch("catalog_merge.read_sheet_values", side_effect=fake_read),
            patch("catalog_merge._write_matrix", side_effect=lambda _ss, _name, _start, rows, _log: written.update(rows=rows)),
        ):
            result = run_catalog_merge(cfg, log=lambda _message: None)
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(written["rows"], [["表头"], ["数据"]])


class VideoReportTests(unittest.TestCase):
    def test_per_video_step_count(self):
        values = [30, 34.9, 35, 60, 70, 89, 90, 120]
        self.assertEqual([_per_video_count(value) for value in values], [1, 1, 2, 2, 2, 2, 3, 4])

    def test_custom_report_layout(self):
        records = [
            {"name": "甲", "type": "图片", "date": "2026-08-01", "sec": 30},
            {"name": "甲", "type": "图片", "date": "2026-08-01", "sec": 900},
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
        self.assertEqual(rows[4][3], 2.0)
        self.assertEqual(rows[4][4], 1.0)


if __name__ == "__main__":
    unittest.main()
