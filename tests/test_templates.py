from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from catalog_merge import (  # noqa: E402
    _attach_source_column,
    _catalog_name_excluded,
    _col_index,
    _combine_catalog_link,
    _detect_date_index,
    _filter_chunk_by_date,
    _find_ws,
    _get_or_create_sheet,
    _pick_catalog_ws,
    _read_source_batches,
    _row_fingerprint,
    _take_new_rows,
    _worksheets_cached,
    parse_catalog_link,
    run_catalog_merge,
)
from fetch_posts import compact_sheet_rows, to_datetime, trim_trailing_empty_columns  # noqa: E402
from roster_fill import (  # noqa: E402
    HOUR_SLOTS,
    _is_checked,
    build_axis,
    build_roster_payload,
    parse_traffic_column,
    sort_dates_desc,
)
from video_duration import (  # noqa: E402
    _apply_earlier_source,
    _custom_buckets,
    _custom_report_matrix,
    _date_key,
    _earliest_iso_date,
    _ingest_video_source_row,
    _merge_video_payload,
    _per_video_count,
    _report_matrix,
    _type_pattern_match,
)


class TemplateConfigTests(unittest.TestCase):
    def test_new_template_config_is_parsed(self):
        cfg = app._cfg_from_payload(
            {
                "catalog_url_col": "c",
                "catalog_sheet_col": "f",
                "roster_config_url": "https://example.com/roster",
                "roster_columns": [{"field": "队别", "role": "team", "column": "A"}, {"field": "名字", "role": "name", "column": "C"}],
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
        self.assertEqual(cfg.roster_config_url, "https://example.com/roster")
        self.assertEqual(cfg.roster_columns[0]["role"], "team")
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

    def test_cfg_from_payload_can_use_fresh_base(self):
        cfg = app._cfg_from_payload(
            {"vd_schedule_enabled": True, "vd_source_url": "https://example.com/src", "vd_types": ["wsp"]},
            base=app.Config(),
        )
        self.assertTrue(cfg.vd_schedule_enabled)
        self.assertEqual(cfg.vd_source_url, "https://example.com/src")
        self.assertEqual(cfg.vd_types, ["wsp"])
        cfg_posts = app._cfg_from_payload(
            {
                "pa_list_url": "https://example.com/list",
                "pa_source_cols": ["J", "M"],
                "pa_include_tag": False,
                "pa_lookup_enabled": True,
            },
            base=app.Config(),
        )
        self.assertEqual(cfg_posts.pa_list_url, "https://example.com/list")
        self.assertEqual(cfg_posts.pa_source_cols, ["J", "M"])
        self.assertFalse(cfg_posts.pa_include_tag)
        cfg_rules = app._cfg_from_payload(
            {
                "vd_types": [
                    {"name": "口播", "in_total": True, "in_item": False},
                    {"name": "wsp", "in_total": False, "in_item": True},
                ]
            },
            base=app.Config(),
        )
        self.assertEqual(cfg_rules.vd_types[0]["in_item"], False)
        self.assertEqual(cfg_rules.vd_types[1]["in_total"], False)

    def test_sync_schedulers_keep_video_when_other_menu_has_no_timer(self):
        app.stop_all_menu_schedulers()
        menus = [
            {"id": "filter-1", "template": "filter", "settings": {"schedule_enabled": False, "schedule_minutes": 60}},
            {
                "id": "video-1",
                "template": "video",
                "settings": {
                    "vd_schedule_enabled": True,
                    "vd_schedule_minutes": 60,
                    "vd_source_url": "https://docs.google.com/spreadsheets/d/aaaaaaaaaaaaaaaaaaaaaaaaaa/edit",
                    "vd_dest_url": "https://docs.google.com/spreadsheets/d/bbbbbbbbbbbbbbbbbbbbbbbbbb/edit",
                },
            },
        ]
        app.sync_schedulers_from_menus(menus)
        self.assertTrue(app.menu_schedule_snapshot("video-1")["enabled"])
        self.assertTrue(app._video_schedule_snapshot()["enabled"])
        app.sync_schedulers_from_menus(menus)
        self.assertTrue(app.menu_schedule_snapshot("video-1")["enabled"])
        self.assertIsNone(
            app._menu_schedule_spec(
                {"id": "video-2", "template": "video", "settings": {"vd_schedule_enabled": True, "vd_schedule_minutes": 30}}
            )
        )
        app.stop_all_menu_schedulers()
        self.assertFalse(app.menu_schedule_snapshot("video-1")["enabled"])

    def test_custom_menu_timer_uses_video_job_kind(self):
        spec = app._menu_schedule_spec(
            {
                "id": "custom-1",
                "template": "custom",
                "settings": {
                    "vd_schedule_enabled": True,
                    "vd_schedule_minutes": "90",
                    "vd_source_url": "https://docs.google.com/spreadsheets/d/aaaaaaaaaaaaaaaaaaaaaaaaaa/edit",
                    "vd_dest_url": "https://docs.google.com/spreadsheets/d/bbbbbbbbbbbbbbbbbbbbbbbbbb/edit",
                },
            }
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "video")
        self.assertEqual(spec["minutes"], 90)

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
            patch("fetch_posts.authorize_cfg", return_value=object()),
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

    def test_catalog_reads_hyperlink_text(self):
        from video_duration import extract_url

        self.assertEqual(
            extract_url('=HYPERLINK("https://docs.google.com/spreadsheets/d/abc1234567890abcdefghij/edit","表")'),
            "https://docs.google.com/spreadsheets/d/abc1234567890abcdefghij/edit",
        )

    def test_catalog_reuses_worksheet_list(self):
        calls = {"n": 0}
        wanted = SimpleNamespace(title="1751-小源", id=995133928)

        def listing():
            calls["n"] += 1
            return [SimpleNamespace(title="目录", id=1), wanted]

        spreadsheet = SimpleNamespace(id="book", worksheets=listing)
        first = _worksheets_cached(spreadsheet)
        second = _worksheets_cached(spreadsheet)
        self.assertIs(first, second)
        self.assertEqual(calls["n"], 1)
        self.assertIs(_find_ws(spreadsheet, gid=995133928), wanted)
        self.assertEqual(calls["n"], 1)

    def test_catalog_parses_internal_gid_hyperlink(self):
        formula = '=HYPERLINK("#gid=995133928","1751-小源")'
        parsed = parse_catalog_link(formula)
        self.assertEqual(parsed["gid"], 995133928)
        self.assertEqual(parsed["label"], "1751-小源")
        self.assertTrue(parsed["internal"])
        self.assertFalse(parsed["spreadsheet_id"])
        combined = _combine_catalog_link("#gid=995133928", formula)
        self.assertEqual(combined["gid"], 995133928)
        self.assertEqual(combined["label"], "1751-小源")
        wanted = SimpleNamespace(title="1751-小源", id=995133928)
        spreadsheet = SimpleNamespace(worksheets=lambda: [SimpleNamespace(title="目录", id=1), wanted])
        self.assertIs(_find_ws(spreadsheet, gid=995133928), wanted)
        self.assertIs(_find_ws(spreadsheet, title="1751-小源"), wanted)

    def test_catalog_merges_internal_gid_sheets(self):
        index_ws = SimpleNamespace(title="目录", id=11)
        wanted_ws = SimpleNamespace(title="1751-小源", id=995133928)
        index_ss = SimpleNamespace(title="专页目录", id="index", worksheets=lambda: [index_ws, wanted_ws])
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
            return {"index-url": index_ss, "target-url": target_ss}[value]

        def fake_read(ws, log=print):
            if ws is index_ws:
                return [
                    ["专页ID", "姓名", "", ""],
                    ["1751", '=HYPERLINK("#gid=995133928","1751-小源")', "", ""],
                ]
            return []

        def fake_batches(ws, log, cancelled=None):
            if ws is wanted_ws:
                yield [["表头"], ["小源数据"]]

        captured = {}

        class FakeWriter:
            def __init__(self, ss, sheet_name, start_row, log):
                self.rows = []
                self.width = 1
                captured["writer"] = self

            def add_rows(self, rows):
                self.rows.extend(rows)
                self.width = max((len(row) for row in self.rows), default=1)

            def finish(self):
                return len(self.rows)

        with (
            patch("catalog_merge.authorize_cfg", return_value=object()),
            patch("catalog_merge.open_by_url_or_id", side_effect=fake_open),
            patch("catalog_merge.pick_source_ws", return_value=index_ws),
            patch("catalog_merge.read_sheet_values", side_effect=fake_read),
            patch("catalog_merge._read_source_batches", side_effect=fake_batches),
            patch("catalog_merge._read_link_column", return_value=["#gid=995133928"]),
            patch("catalog_merge._StreamingWriter", FakeWriter),
        ):
            result = run_catalog_merge(cfg, log=lambda _message: None)
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(captured["writer"].rows, [["来源", "表头"], ["1751-小源", "小源数据"]])
        self.assertTrue(any(item.get("sheet") == "1751-小源" and not item.get("error") for item in result["sources"]))

    def test_catalog_skips_excluded_sheet_names(self):
        self.assertTrue(_catalog_name_excluded("导航", ["导航"]))
        self.assertTrue(_catalog_name_excluded("1751-小源", ["1751*"]))
        self.assertFalse(_catalog_name_excluded("1751-小源", ["导航"]))
        index_ws = SimpleNamespace(title="目录", id=11)
        skip_ws = SimpleNamespace(title="导航", id=22)
        keep_ws = SimpleNamespace(title="1751-小源", id=995133928)
        index_ss = SimpleNamespace(title="专页目录", id="index", worksheets=lambda: [index_ws, skip_ws, keep_ws])
        target_ss = SimpleNamespace(title="目标", id="target")
        cfg = app.Config(
            catalog_index_url="index-url",
            catalog_target_url="target-url",
            catalog_index_sheet="目录",
            catalog_url_col="B",
            catalog_sheet_col="D",
            catalog_start_row=2,
            catalog_exclude_sheets=["导航"],
        )
        cfg.resolve_credentials = lambda: ROOT / "config.example.json"

        def fake_open(_gc, value, log=print):
            return {"index-url": index_ss, "target-url": target_ss}[value]

        def fake_read(ws, log=print):
            if ws is index_ws:
                return [
                    ["专页ID", "姓名", "", ""],
                    ["nav", '=HYPERLINK("#gid=22","导航")', "", ""],
                    ["1751", '=HYPERLINK("#gid=995133928","1751-小源")', "", ""],
                ]
            return []

        def fake_batches(ws, log, cancelled=None):
            if ws is skip_ws:
                yield [["不该出现"]]
            if ws is keep_ws:
                yield [["表头"], ["小源数据"]]

        captured = {}

        class FakeWriter:
            def __init__(self, ss, sheet_name, start_row, log):
                self.rows = []
                self.width = 1
                captured["writer"] = self

            def add_rows(self, rows):
                self.rows.extend(rows)
                self.width = max((len(row) for row in self.rows), default=1)

            def finish(self):
                return len(self.rows)

        with (
            patch("catalog_merge.authorize_cfg", return_value=object()),
            patch("catalog_merge.open_by_url_or_id", side_effect=fake_open),
            patch("catalog_merge.pick_source_ws", return_value=index_ws),
            patch("catalog_merge.read_sheet_values", side_effect=fake_read),
            patch("catalog_merge._read_source_batches", side_effect=fake_batches),
            patch("catalog_merge._read_link_column", return_value=["#gid=22", "#gid=995133928"]),
            patch("catalog_merge._StreamingWriter", FakeWriter),
        ):
            result = run_catalog_merge(cfg, log=lambda _message: None)
        self.assertEqual(captured["writer"].rows, [["来源", "表头"], ["1751-小源", "小源数据"]])
        self.assertFalse(any(item.get("sheet") == "导航" and item.get("rows") for item in result["sources"]))

    def test_catalog_read_stays_inside_grid(self):
        requested: list[str] = []

        class SmallSheet:
            title = "🏠massane-录 "
            row_count = 195
            col_count = 50

            def get(self, range_name):
                requested.append(range_name)
                if "1001" in range_name or "2000" in range_name:
                    raise RuntimeError(f"Range ({range_name}) exceeds grid limits. Max rows: 195, max columns: 50")
                return [["a", "b", ""], ["c", "", ""]]

        rows = list(_read_source_batches(SmallSheet(), log=lambda _m: None))
        self.assertEqual(rows[0][0], ["a", "b"])
        self.assertTrue(requested)
        self.assertTrue(all("1001" not in item for item in requested))
        self.assertTrue(any("195" in item for item in requested))

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
            return []

        def fake_batches(ws, log, cancelled=None):
            if ws is wanted_ws:
                yield [["表头"], ["数据"]]

        captured = {}

        class FakeWriter:
            def __init__(self, ss, sheet_name, start_row, log):
                self.rows = []
                self.width = 1
                captured["writer"] = self

            def add_rows(self, rows):
                self.rows.extend(rows)
                self.width = max((len(row) for row in self.rows), default=1)

            def finish(self):
                return len(self.rows)

        with (
            patch("catalog_merge.authorize_cfg", return_value=object()),
            patch("catalog_merge.open_by_url_or_id", side_effect=fake_open),
            patch("catalog_merge.pick_source_ws", return_value=index_ws),
            patch("catalog_merge.read_sheet_values", side_effect=fake_read),
            patch("catalog_merge._read_source_batches", side_effect=fake_batches),
            patch("catalog_merge._StreamingWriter", FakeWriter),
        ):
            result = run_catalog_merge(cfg, log=lambda _message: None)
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(captured["writer"].rows, [["来源", "表头"], ["目标录", "数据"]])

    def test_catalog_survives_emoji_in_sheet_title(self):
        index_ws = SimpleNamespace(title="目录")
        wanted_ws = SimpleNamespace(title="🌛 Massane 录")
        index_ss = SimpleNamespace(title="索引", id="index", worksheets=lambda: [index_ws])
        source_ss = SimpleNamespace(title="Vila Massane", id="source-a", worksheets=lambda: [wanted_ws])
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
            return {"index-url": index_ss, "url-a": source_ss, "target-url": target_ss}[value]

        def fake_read(ws, log=print):
            if ws is index_ws:
                return [["", "", "", ""], ["", "url-a", "", "🌛 Massane 录"]]
            return []

        def fake_batches(ws, log, cancelled=None):
            if ws is wanted_ws:
                yield [["表头"], ["数据"]]

        def exploding_log(message):
            str(message).encode("gbk")

        captured = {}

        class FakeWriter:
            def __init__(self, ss, sheet_name, start_row, log):
                self.rows = []
                self.width = 1
                captured["writer"] = self

            def add_rows(self, rows):
                self.rows.extend(rows)
                self.width = max((len(row) for row in self.rows), default=1)

            def finish(self):
                return len(self.rows)

        with (
            patch("catalog_merge.authorize_cfg", return_value=object()),
            patch("catalog_merge.open_by_url_or_id", side_effect=fake_open),
            patch("catalog_merge.pick_source_ws", return_value=index_ws),
            patch("catalog_merge.read_sheet_values", side_effect=fake_read),
            patch("catalog_merge._read_source_batches", side_effect=fake_batches),
            patch("catalog_merge._StreamingWriter", FakeWriter),
        ):
            result = run_catalog_merge(cfg, log=exploding_log)
        self.assertTrue(result["ok"])
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(captured["writer"].rows, [["来源", "表头"], ["🌛 Massane 录", "数据"]])


class VideoReportTests(unittest.TestCase):
    def test_video_report_two_or_three_columns(self):
        records = [
            {"name": "甲", "type": "wsp 9:16", "date": "2026-08-01", "sec": 30},
            {"name": "甲", "type": "口播", "date": "2026-08-01", "sec": 90},
        ]
        names, rows = _report_matrix(records, None, None, 30, "全部", types=[], preferred_names=["甲"])
        self.assertEqual(names, ["甲"])
        self.assertEqual(rows[2][1:3], ["总计数", "逐条计数"])
        self.assertEqual(len(rows[3]), 3)
        names, rows = _report_matrix(records, None, None, 30, "全部", types=["wsp"], preferred_names=["甲"])
        self.assertEqual(rows[2][1:4], ["总计数", "逐条计数", "wsp"])
        self.assertEqual(rows[3][1], 4)
        self.assertEqual(rows[3][3], 0)
        names, rows = _report_matrix(
            [{"name": "甲", "type": "wsp", "date": "2026-08-01", "sec": 30}],
            None,
            None,
            30,
            "全部",
            types=["wsp"],
            preferred_names=["甲"],
        )
        self.assertEqual(rows[3][3], 1)
        names, rows = _report_matrix(records, None, None, 30, "全部", types=["wsp", "口播"], preferred_names=["甲"])
        self.assertEqual(rows[2][1:5], ["总计数", "逐条计数", "wsp", "口播"])
        self.assertEqual(len(rows[3]), 5)

    def test_locked_headers_keep_name_order_and_ignore_new_people(self):
        records = [
            {"name": "乙", "type": "口播", "date": "2026-08-23", "sec": 30},
            {"name": "甲", "type": "口播", "date": "2026-08-23", "sec": 30},
        ]
        names, rows = _report_matrix(
            records, None, None, 30, "全部", types=["口播"], preferred_names=["甲"], lock_names=True
        )
        self.assertEqual(names, ["甲"])
        self.assertEqual(rows[1][1], "甲")
        custom_names, cats, custom_rows = _custom_report_matrix(
            records, 30, ["口播"], "per_video_ceil", preferred_names=["甲"], lock_names=True
        )
        self.assertEqual(custom_names, ["甲"])
        self.assertEqual(cats, ["口播"])
        self.assertEqual(custom_rows[0][1], "甲")

    def test_unpadded_dates_parse(self):
        serial = (to_datetime("2026-08-23") - to_datetime("1899-12-30")).days
        for raw in (
            "2026-08-23",
            "2026-8-23",
            "2026/8/23",
            "2026年8月23日",
            "2026-8-23 0:00:00",
            "8/23/2026",
            "8-23-2026",
            "23/8/2026",
            serial,
            str(serial),
        ):
            dt = to_datetime(raw)
            self.assertIsNotNone(dt, raw)
            self.assertEqual(dt.date().isoformat(), "2026-08-23", raw)
            self.assertEqual(_date_key(raw), "2026-08-23", raw)

    def test_unpadded_log_dates_count_in_report(self):
        records = [
            {"name": "甜甜", "type": "形式化口播（祷告，演讲等）", "date": "2026-8-23", "sec": 8},
            {"name": "甜甜", "type": "形式化口播（祷告，演讲等）", "date": "2026-08-23", "sec": 7},
        ]
        _names, rows = _report_matrix(
            records, None, None, 30, "全部", types=["开场口播"], preferred_names=["甜甜"]
        )
        day_row = next(row for row in rows[4:] if row[0] == "2026-08-23")
        self.assertEqual(day_row[2], 2)
        self.assertEqual(day_row[3], 0)

    def test_video_report_keeps_old_dates(self):
        records = [{"name": "甲", "type": "口播", "date": "2026-09-01", "sec": 30}]
        _names, payload = _report_matrix(records, None, None, 30, "全部", types=[], preferred_names=["甲"])
        existing = {
            "names": ["甲"],
            "labels": ["总计数", "逐条计数"],
            "dates": ["2026-08-01"],
            "daily": {("2026-08-01", "甲", "总计数"): 5, ("2026-08-01", "甲", "逐条计数"): 2},
        }
        merged = _merge_video_payload(payload, existing, [])
        dates = [row[0] for row in merged[4:]]
        self.assertIn("2026-08-01", dates)
        self.assertIn("2026-09-01", dates)
        august = next(row for row in merged[4:] if row[0] == "2026-08-01")
        self.assertEqual(august[1], 5)

    def test_duplicate_drive_keeps_earliest_source_date(self):
        self.assertEqual(_earliest_iso_date("2026-08-24", "2026-08-23"), "2026-08-23")
        self.assertEqual(_earliest_iso_date("2026-08-23", "2026-08-24"), "2026-08-23")
        rec = {"date": "2026-08-24", "name": "甜甜", "type": "形式化口播"}
        changed = _apply_earlier_source(rec, "2026-08-23", "甜甜", "形式化口播")
        self.assertEqual(changed["date"], "2026-08-23")
        self.assertEqual(rec["date"], "2026-08-23")
        self.assertEqual(_apply_earlier_source(rec, "2026-08-24"), {})
        self.assertEqual(rec["date"], "2026-08-23")

        def _run(rows, index=None):
            seen, pending, patches = {}, [], []
            idx = dict(index or {})
            actions = [
                _ingest_video_source_row(
                    key=key,
                    date_s=date_s,
                    name=name,
                    typ=typ,
                    url=f"https://drive.google.com/file/d/{key[2:]}/view",
                    seen_src=seen,
                    index=idx,
                    pending=pending,
                    log_patches=patches,
                )
                for key, date_s, name, typ in rows
            ]
            return actions, pending, patches, idx

        # Source lists 8/24 first (upper rows) then 8/23: keep 8/23, one pending.
        actions, pending, patches, _idx = _run(
            [
                ("d:abc", "2026-08-24", "甜甜", "形式化口播"),
                ("d:abc", "2026-08-23", "甜甜", "形式化口播"),
            ]
        )
        self.assertEqual(actions, ["pending", "dup_earlier"])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["date"], "2026-08-23")
        self.assertEqual(patches, [])

        # Already logged as 8/24: later 8/23 must patch the log, not overwrite 23→24.
        logged = {
            "d:abc": {
                "date": "2026-08-24",
                "name": "甜甜",
                "type": "形式化口播",
                "sec": 8,
                "row": 100,
                "key": "d:abc",
            }
        }
        actions, pending, patches, idx = _run(
            [
                ("d:abc", "2026-08-24", "甜甜", "形式化口播"),
                ("d:abc", "2026-08-23", "甜甜", "形式化口播"),
            ],
            index=logged,
        )
        self.assertEqual(actions, ["log_skip", "dup_earlier"])
        self.assertEqual(pending, [])
        self.assertEqual(patches[0]["date"], "2026-08-23")
        self.assertEqual(idx["d:abc"]["date"], "2026-08-23")

        actions, pending, patches, idx = _run(
            [
                ("d:abc", "2026-08-24", "甜甜", "形式化口播"),
                ("d:abc", "2026-08-23", "甜甜", "形式化口播"),
            ],
            index={
                "d:abc": {
                    "date": "2026-08-23",
                    "name": "甜甜",
                    "type": "形式化口播",
                    "sec": 8,
                    "row": 100,
                    "key": "d:abc",
                }
            },
        )
        self.assertEqual(actions, ["log_skip", "dup_later"])
        self.assertEqual(pending, [])
        self.assertEqual(patches, [])
        self.assertEqual(idx["d:abc"]["date"], "2026-08-23")

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

    def test_custom_counts_by_person_day_category_without_duration(self):
        records = [
            {"name": "甲", "type": "开场口播", "date": "2026-08-01"},
            {"name": "甲", "type": "开场口播", "date": "2026-08-01"},
            {"name": "甲", "type": "形式化口播", "date": "2026-08-01"},
            {"name": "乙", "type": "开场口播", "date": "2026-08-02"},
        ]
        names, categories, rows = _custom_report_matrix(
            records, 30, ["开场口播", "形式化口播"], "per_video_ceil"
        )
        self.assertEqual(categories, ["开场口播", "形式化口播"])
        self.assertEqual(set(names), {"甲", "乙"})
        header = rows[0]
        types = rows[2]
        totals = rows[4]
        jia = header.index("甲")
        yi = header.index("乙")
        self.assertEqual(types[jia], "开场口播")
        self.assertEqual(totals[jia], 2.0)
        self.assertEqual(totals[jia + 1], 1.0)
        self.assertEqual(totals[yi], 1.0)
        self.assertEqual(totals[yi + 1], 0.0)
        by_date = {row[0]: row for row in rows[7:]}
        self.assertEqual(by_date["2026-08-01"][jia], 2.0)
        self.assertEqual(by_date["2026-08-01"][jia + 1], 1.0)
        self.assertEqual(by_date["2026-08-02"][yi], 1.0)

    def test_log_accepts_emoji(self):
        job, _lock = app._job_parts("emoji-log-test")
        job["logs"] = []
        app._log("读取「🌛 Massane 录」", job)
        self.assertIn("🌛", job["logs"][-1]["msg"])

    def test_custom_buckets_separate_and_other(self):
        self.assertEqual(_custom_buckets(["口播"], ["口播"], [], "其他"), ["口播"])
        self.assertEqual(_custom_buckets(["横版"], ["口播"], [], "其他"), ["其他"])
        self.assertEqual(_custom_buckets(["测试"], ["口播"], ["测试"], "其他"), [])
        self.assertEqual(_custom_buckets(["口播", "样式A"], ["口播", "样式A"], [], ""), ["口播", "样式A"])
        self.assertEqual(_custom_buckets(["样式A"], ["口播"], [], ""), [])
        self.assertEqual(_custom_buckets([], ["口播"], [], "图片"), ["图片"])
        self.assertEqual(_custom_buckets([""], ["FB", "口播"], [], "图片"), ["图片"])
        self.assertEqual(_custom_buckets(["FB"], ["FB", "口播"], ["FB", "口播"], "图片"), ["FB"])
        self.assertEqual(_custom_buckets(["风景"], [], ["FB", "口播"], "图片"), ["图片"])
        self.assertEqual(_custom_buckets(["FB"], [], ["FB", "口播"], "图片"), [])
        self.assertEqual(_custom_buckets(["wsp 9:16"], ["wsp"], [], "图片"), ["wsp"])
        self.assertEqual(_custom_buckets(["WSP横版"], ["wsp"], [], "图片"), ["wsp"])
        self.assertEqual(_custom_buckets(["开场口播"], ["口播"], [], "图片"), ["口播"])
        self.assertTrue(_type_pattern_match("形式化口播", "口播"))
        self.assertTrue(_type_pattern_match("wsp-a", "wsp*"))
        self.assertFalse(_type_pattern_match("形式化口播", "=口播"))
        self.assertTrue(_type_pattern_match("口播", "=口播"))

    def test_trim_trailing_empty_columns(self):
        rows = [["a", "b", "", ""], ["c", "", "", ""], ["", "", "", ""]]
        trimmed = trim_trailing_empty_columns(rows)
        self.assertEqual([len(row) for row in trimmed], [2, 2, 2])
        self.assertEqual(trimmed[0], ["a", "b"])
        compact = compact_sheet_rows(rows)
        self.assertEqual(compact, [["a", "b"], ["c", ""]])

    def test_roster_header_layout_and_date_fill(self):
        pages = [
            {
                "chat": "https://chat.example/a",
                "data_url": "https://docs.google.com/spreadsheets/d/abc",
                "page_link": "https://facebook.com/page",
                "page_code": "P001",
                "name": "甜甜",
                "type": "口播",
                "values": {("2026-08-27", ""): 35, ("2026-08-27", "00:00-01:00"): 2},
            },
            {
                "chat": "https://chat.example/b",
                "data_url": "https://docs.google.com/spreadsheets/d/abc",
                "page_link": "https://facebook.com/page2",
                "page_code": "P002",
                "name": "甜甜",
                "type": "口播",
                "values": {("2026-08-27", ""): 10},
            },
        ]
        axis = build_axis(["2026-08-26", "2026-08-27"])
        rows = build_roster_payload(pages, axis, 24)
        self.assertEqual(rows[0][1], "https://chat.example/a")
        self.assertEqual(rows[4][1], "P001")
        self.assertEqual(rows[4][2], "P002")
        self.assertEqual(rows[5][1], "甜甜")
        self.assertEqual(rows[5][2], "甜甜")
        self.assertEqual(rows[23][0], "2026-08-27")
        self.assertEqual(rows[23][1], 35)
        self.assertEqual(rows[24][0], "00:00-01:00")
        self.assertEqual(rows[24][1], 2)
        self.assertEqual(rows[23 + 25][0], "2026-08-26")
        self.assertEqual(len(HOUR_SLOTS), 24)
        self.assertTrue(_is_checked(True))
        self.assertTrue(_is_checked("TRUE"))
        self.assertFalse(_is_checked(False))
        self.assertFalse(_is_checked(""))
        traffic = [
            ["编码X", "P001"],
            ["2026-8-27", "", 35],
            ["00:00-01:00", "", 2],
            ["01:00-02:00", "", 2],
        ]
        # make col 1 the page
        traffic = [["x", "P001"], ["2026-8-27", 35], ["00:00-01:00", 2]]
        dates, values = parse_traffic_column(traffic, 1)
        self.assertEqual(dates[0], "2026-08-27")
        self.assertEqual(values[("2026-08-27", "")], 35)
        self.assertEqual(values[("2026-08-27", "00:00-01:00")], 2)
        self.assertEqual(sort_dates_desc(["2026-8-25", "2026-08-27", "2026-8-26"])[0], "2026-08-27")

    def test_video_types_become_extra_columns(self):
        records = [
            {"name": "甜甜", "type": "开场口播", "date": "2026-08-23", "sec": 8},
            {"name": "甜甜", "type": "形式化口播", "date": "2026-08-23", "sec": 7},
        ]
        _names, rows = _report_matrix(
            records,
            None,
            None,
            30,
            "全部",
            types=["开场口播"],
            preferred_names=["甜甜"],
        )
        self.assertEqual(rows[2][1:4], ["总计数", "逐条计数", "开场口播"])
        self.assertEqual(len(rows[3]), 4)

    def test_extra_columns_count_videos_not_duration(self):
        records = [{"name": "甲", "type": "口播", "date": "2026-08-01", "sec": 10} for _ in range(5)]
        _names, rows = _report_matrix(
            records, None, None, 30, "全部", types=["口播"], preferred_names=["甲"]
        )
        self.assertEqual(rows[3][1], 1.67)
        self.assertEqual(rows[3][2], 5)
        self.assertEqual(rows[3][3], 5)
        day_row = next(row for row in rows[4:] if row[0] == "2026-08-01")
        self.assertEqual(day_row[3], 5)

    def test_type_rules_gate_total_and_item_but_extra_still_counts(self):
        records = [
            {"name": "甲", "type": "口播", "date": "2026-08-01", "sec": 90},
            {"name": "甲", "type": "wsp", "date": "2026-08-01", "sec": 30},
        ]
        rules = [
            {"name": "口播", "in_total": True, "in_item": True},
            {"name": "wsp", "in_total": False, "in_item": False},
        ]
        _names, rows = _report_matrix(
            records,
            None,
            None,
            30,
            "全部",
            types=["口播", "wsp"],
            preferred_names=["甲"],
            type_rules=rules,
        )
        self.assertEqual(rows[3][1], 3)
        self.assertEqual(rows[3][2], 3)
        self.assertEqual(rows[3][3], 1)
        self.assertEqual(rows[3][4], 1)

    def test_catalog_dedupe_and_date_filter_helpers(self):
        from datetime import date

        self.assertEqual(_detect_date_index(["姓名", "登记日期", "电话"]), 1)
        self.assertEqual(_row_fingerprint(["1751-小源", "123", ""]), ("1751-小源", "123"))
        kept, skipped = _filter_chunk_by_date(
            [["日期", "电话"], ["2026-08-10", "1"], ["2026-09-01", "2"], ["", "3"]],
            0,
            date(2026, 8, 1),
            date(2026, 8, 31),
            True,
        )
        self.assertEqual(skipped, 2)
        self.assertEqual(kept, [["日期", "电话"], ["2026-08-10", "1"]])
        seen: set[tuple[str, ...]] = set()
        first, n1 = _take_new_rows([["1751-小源", "123"]], seen)
        second, n2 = _take_new_rows([["1751-小源", "123"], ["1626-小瑞", "456"]], seen)
        self.assertEqual(first, [["1751-小源", "123"]])
        self.assertEqual(n1, 0)
        self.assertEqual(n2, 1)
        self.assertEqual(second, [["1626-小瑞", "456"]])

    def test_catalog_merge_skips_out_of_range_dates(self):
        index_ws = SimpleNamespace(title="目录", id=11)
        wanted_ws = SimpleNamespace(title="1751-小源", id=995133928)
        index_ss = SimpleNamespace(title="专页目录", id="index", worksheets=lambda: [index_ws, wanted_ws])
        target_ss = SimpleNamespace(title="目标", id="target")
        cfg = app.Config(
            catalog_index_url="index-url",
            catalog_target_url="target-url",
            catalog_index_sheet="目录",
            catalog_url_col="B",
            catalog_sheet_col="D",
            catalog_start_row=2,
            catalog_date_filter_enabled=True,
            catalog_start_date="2026-08-01",
            catalog_end_date="2026-08-31",
            catalog_date_col="A",
        )
        cfg.resolve_credentials = lambda: ROOT / "config.example.json"

        def fake_open(_gc, value, log=print):
            return {"index-url": index_ss, "target-url": target_ss}[value]

        def fake_read(ws, log=print):
            if ws is index_ws:
                return [["", "", "", ""], ["", '=HYPERLINK("#gid=995133928","1751-小源")', "", ""]]
            return []

        def fake_batches(ws, log, cancelled=None):
            if ws is wanted_ws:
                yield [["日期", "电话"], ["2026-08-10", "111"], ["2026-09-01", "222"]]

        captured = {}

        class FakeWriter:
            def __init__(self, ss, sheet_name, start_row, log):
                self.rows = []
                captured["writer"] = self

            def add_rows(self, rows):
                self.rows.extend(rows)

            def finish(self):
                return len(self.rows)

        with (
            patch("catalog_merge.authorize_cfg", return_value=object()),
            patch("catalog_merge.open_by_url_or_id", side_effect=fake_open),
            patch("catalog_merge.pick_source_ws", return_value=index_ws),
            patch("catalog_merge.read_sheet_values", side_effect=fake_read),
            patch("catalog_merge._read_source_batches", side_effect=fake_batches),
            patch("catalog_merge._read_link_column", return_value=["#gid=995133928"]),
            patch("catalog_merge._StreamingWriter", FakeWriter),
        ):
            result = run_catalog_merge(cfg, log=lambda _message: None)
        self.assertEqual(
            captured["writer"].rows,
            [["来源", "日期", "电话"], ["1751-小源", "2026-08-10", "111"]],
        )
        self.assertEqual(result["date_skipped"], 1)
        self.assertEqual(result["sheet_total"], result["total_rows"])
        self.assertTrue(result["ok"])

    def test_catalog_source_column_uses_sheet_name(self):
        self.assertEqual(
            _attach_source_column([["姓名", "电话"], ["张三", "123"]], "1751-小源", True),
            [["来源", "姓名", "电话"], ["1751-小源", "张三", "123"]],
        )
        self.assertEqual(
            _attach_source_column([["张三", "123"]], "1626-小瑞", False),
            [["1626-小瑞", "张三", "123"]],
        )
        self.assertEqual(
            _attach_source_column([["来源", "姓名"], ["张三"]], "1751-小源", True),
            [["来源", "姓名"], ["1751-小源", "张三"]],
        )

    def test_catalog_keeps_existing_sheet_instead_of_wiping(self):
        resized = []
        ws = SimpleNamespace(
            title="电话号码备份",
            row_count=164003,
            col_count=11,
            resize=lambda **kwargs: resized.append(kwargs),
        )
        spreadsheet = SimpleNamespace(worksheet=lambda _title: ws)
        got = _get_or_create_sheet(spreadsheet, "电话号码备份", log=lambda _message: None)
        self.assertIs(got, ws)
        self.assertEqual(resized, [])


if __name__ == "__main__":
    unittest.main()
