from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from post_aggregate import (  # noqa: E402
    build_library_append_rows,
    build_lookup_map,
    collect_list_entries,
    collect_match_values,
    library_state,
    output_headers,
    pick_new_library_values,
    rows_from_subscription,
)


class PostAggregateTests(unittest.TestCase):
    def test_collect_list_entries_reads_link_and_tag(self):
        rows = [
            ["表头"],
            ["", "", "", "", "", "", "", "", "", "", "https://docs.google.com/spreadsheets/d/aaaaaaaaaaaaaaaaaaaaaaaaaa/edit", "一组"],
            ["", "", "", "", "", "", "", "", "", "", "https://docs.google.com/spreadsheets/d/bbbbbbbbbbbbbbbbbbbbbbbbbb/edit", "二组"],
        ]
        entries = collect_list_entries(rows, "K", "L", 2)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["tag"], "一组")
        self.assertEqual(entries[1]["id"], "bbbbbbbbbbbbbbbbbbbbbbbbbb")

    def test_subscription_date_filter_and_lookup(self):
        rows = [
            ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O"],
            ["专页A", "", "", "", "内容", "", "", "", "", "id-1", "", "李四", "2026-08-22", "原N", "备注"],
            ["专页B", "", "", "", "旧", "", "", "", "", "id-2", "", "王五", "2026-08-10", "原N", "备注"],
        ]
        lookup = {"id-1": "视频贴"}
        out, skipped = rows_from_subscription(
            rows,
            ["J", "M", "O", "A", "L", "E", "N"],
            "M",
            date(2026, 8, 22),
            None,
            "一组",
            True,
            "J",
            lookup,
            True,
        )
        self.assertEqual(skipped, 1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "id-1")
        self.assertEqual(out[0][1], "2026-08-22")
        self.assertEqual(out[0][7], "一组")
        self.assertEqual(out[0][8], "视频贴")

    def test_lookup_map_uses_key_column(self):
        rows = [["帖文id", "B", "x"], ["skip", "abc", "no"], ["", "id-9", "视频贴"]]
        mapping = build_lookup_map(rows, "B", "C")
        self.assertEqual(mapping["abc"], "no")
        self.assertEqual(mapping["id-9"], "视频贴")

    def test_headers_follow_flags(self):
        self.assertEqual(
            output_headers(["J", "M"], True, True),
            ["J", "M", "来源标记", "贴文类型"],
        )
        self.assertEqual(output_headers(["J"], False, False), ["J"])

    def test_library_append_skips_existing_b_values(self):
        library = [
            ["表头A", "表头B"],
            ["", "https://fb.com/1"],
            ["", "id-old"],
        ]
        existing, next_row = library_state(library, "B")
        self.assertEqual(existing, {"https://fb.com/1", "id-old"})
        self.assertEqual(next_row, 4)
        new_vals, skipped = pick_new_library_values(
            ["https://fb.com/1", "https://fb.com/new", "id-old", "https://fb.com/new"],
            existing,
        )
        self.assertEqual(skipped, 3)
        self.assertEqual(new_vals, ["https://fb.com/new"])
        rows = build_library_append_rows(new_vals, "B")
        self.assertEqual(rows, [["", "https://fb.com/new"]])

    def test_collect_match_values_prefers_hyperlink(self):
        rows = [
            ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M"],
            ["x", "", "", "", "", "", "", "", "", "显示名", "", "", "2026-08-22"],
        ]
        values = collect_match_values(
            rows,
            "J",
            "M",
            date(2026, 8, 22),
            None,
            ["https://fb.com/post-9"],
        )
        self.assertEqual(values, ["https://fb.com/post-9"])


if __name__ == "__main__":
    unittest.main()
