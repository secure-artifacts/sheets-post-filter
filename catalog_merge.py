# -*- coding: utf-8 -*-
"""目录表驱动汇总：按索引表中的链接和工作表名称合并全部内容。"""

from __future__ import annotations

import difflib
import fnmatch
import re
import unicodedata
from typing import Any, Callable

from fetch_posts import (
    authorize_cfg,
    col_letter_to_index,
    compact_sheet_rows,
    drop_empty_rows,
    extract_spreadsheet_id,
    index_to_col_letter,
    open_by_url_or_id,
    pick_source_ws,
    read_sheet_values,
    safe_resize_ws,
    spreadsheet_url,
    to_datetime,
    trim_trailing_empty_columns,
    used_column_count,
    with_retry,
)
from video_duration import extract_url, _read_link_column, _session_from_gc

LogFn = Callable[[str], None]


def _safe_log(log: LogFn) -> LogFn:
    def inner(message: str) -> None:
        try:
            log(str(message))
        except Exception:
            pass

    return inner


HYPERLINK_PAIR_RE = re.compile(
    r"""(?:HYPERLINK|IMAGE)\s*\(\s*["'“”‘’]([^"'“”‘’]*)["'“”‘’]\s*,\s*["'“”‘’]([^"'“”‘’]*)["'“”‘’]""",
    re.I,
)


def _sheet_gid(value: Any) -> int | None:
    text = str(value or "")
    match = re.search(r"(?:^|[?#&])gid=(\d+)", text, re.I)
    if match:
        return int(match.group(1))
    return None


def parse_catalog_link(value: Any) -> dict[str, Any]:
    """Parse a catalog cell: full Sheets URL, =HYPERLINK("#gid=…","名称"), or #gid=123."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", text).strip()
    label = ""
    target = ""
    pair = HYPERLINK_PAIR_RE.search(text)
    if pair:
        target, label = pair.group(1).strip(), pair.group(2).strip()
    else:
        extracted = extract_url(text)
        target = extracted or text
    sid = extract_spreadsheet_id(target) or extract_spreadsheet_id(text)
    gid = _sheet_gid(target) or _sheet_gid(text)
    internal = bool(gid is not None and not sid and (target.startswith("#") or "gid=" in target.lower()))
    if target.startswith("#") or re.fullmatch(r"gid=\d+", target, re.I):
        internal = True
        sid = None
    return {
        "target": target,
        "label": label,
        "spreadsheet_id": sid,
        "gid": gid,
        "internal": internal,
    }


def _combine_catalog_link(hyper: str, raw: str) -> dict[str, Any]:
    first = parse_catalog_link(hyper)
    second = parse_catalog_link(raw)
    gid = first["gid"] if first["gid"] is not None else second["gid"]
    sid = first["spreadsheet_id"] or second["spreadsheet_id"]
    target = first["target"] or second["target"]
    internal = bool((first["internal"] or second["internal"]) and not sid)
    if target.startswith("#"):
        internal = True
        sid = None
    return {
        "target": target,
        "label": first["label"] or second["label"],
        "spreadsheet_id": sid,
        "gid": gid,
        "internal": internal,
    }


def _worksheets_cached(spreadsheet, log: LogFn | None = None):
    """One metadata fetch per spreadsheet. Calling worksheets() per row hits the 60 reads/min quota."""
    cached = getattr(spreadsheet, "_spf_ws_cache", None)
    if cached is not None:
        return cached
    logger = log or (lambda _message: None)
    sheets = with_retry(lambda: spreadsheet.worksheets(), log=logger, what="列出工作表")
    try:
        spreadsheet._spf_ws_cache = sheets
    except Exception:
        pass
    return sheets


def _find_ws(spreadsheet, *, gid: int | None = None, title: str = "", worksheets=None, log: LogFn | None = None):
    worksheets = list(worksheets) if worksheets is not None else _worksheets_cached(spreadsheet, log)
    if gid is not None:
        for ws in worksheets:
            try:
                if int(getattr(ws, "id", -1)) == int(gid):
                    return ws
            except (TypeError, ValueError):
                continue
    if title:
        wanted = _normalized_title(title)
        matches = [ws for ws in worksheets if _normalized_title(ws.title) == wanted]
        if matches:
            return matches[0]
    return None


def _col_index(value: str) -> int:
    text = str(value or "").strip().upper()
    if not text or not text.isalpha():
        raise RuntimeError("目录表的链接列和名称列必须填写列字母，例如 B、D")
    result = 0
    for char in text:
        result = result * 26 + ord(char) - 64
    return result - 1


def _cell(row: list[Any], index: int) -> str:
    value = str(row[index] if index < len(row) else "")
    return re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", value).strip()


def _normalized_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", _cell([value], 0))
    value = value.replace("\ufe0f", "").replace("\ufe0e", "")
    return re.sub(r"\s+", "", value).casefold()


def _parse_name_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[\n,，;；]+", value)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _catalog_name_excluded(title: str, patterns: list[str]) -> bool:
    text = _normalized_title(title)
    if not text or not patterns:
        return False
    for pattern in patterns:
        pat = _normalized_title(pattern)
        if not pat:
            continue
        if any(char in pat for char in "*?["):
            if fnmatch.fnmatchcase(text, pat):
                return True
        elif text == pat:
            return True
    return False


def _pick_catalog_ws(spreadsheet, requested: str):
    """Match titles robustly while never silently selecting an unrelated sheet."""
    worksheets = _worksheets_cached(spreadsheet)
    wanted = _normalized_title(requested)
    matches = [ws for ws in worksheets if _normalized_title(ws.title) == wanted]
    if len(matches) == 1:
        return matches[0]
    titles = [ws.title for ws in worksheets]
    normalized = {_normalized_title(title): title for title in titles}
    close_keys = difflib.get_close_matches(wanted, list(normalized), n=4, cutoff=0.45)
    close_titles = [normalized[key] for key in close_keys]
    shown = close_titles or titles[:8]
    hint = "、".join(f"「{title}」" for title in shown) or "（没有工作表）"
    raise RuntimeError(f"找不到工作表「{requested}」。该文件中的相近工作表：{hint}")


WRITE_BATCH = 1000
READ_BATCH = 1000
MAX_SOURCE_COLS = 200
SOURCE_COL_HEADER = "来源"


def _attach_source_column(rows: list[list[Any]], source_name: str, header_in_first: bool) -> list[list[Any]]:
    """Put the catalog worksheet name in column A so merged rows keep their origin."""
    label = str(source_name or "").strip()
    if not rows:
        return rows
    out: list[list[Any]] = []
    for index, row in enumerate(rows):
        cells = list(row or [])
        if header_in_first and index == 0:
            first = str(cells[0] if cells else "").strip()
            if first in (SOURCE_COL_HEADER, "工作表名称", "来源工作表"):
                out.append(cells)
            else:
                out.append([SOURCE_COL_HEADER] + cells)
            continue
        out.append([label] + cells)
    return out


DATE_HEADER_RE = re.compile(r"日期|时间|date|time", re.I)


def _row_fingerprint(row: list[Any] | None) -> tuple[str, ...]:
    cells = [str(value or "").strip() for value in (row or [])]
    while cells and not cells[-1]:
        cells.pop()
    return tuple(cells)


def _as_day(value: Any):
    dt = to_datetime(value)
    return dt.date() if dt else None


def _detect_date_index(header_row: list[Any] | None) -> int:
    for index, cell in enumerate(header_row or []):
        if DATE_HEADER_RE.search(str(cell or "").strip()):
            return index
    return -1


def _row_in_date_range(row: list[Any], date_idx: int, start_d, end_d) -> bool:
    if start_d is None and end_d is None:
        return True
    if date_idx < 0:
        return True
    value = row[date_idx] if date_idx < len(row) else ""
    day = _as_day(value)
    if day is None:
        return False
    if start_d and day < start_d:
        return False
    if end_d and day > end_d:
        return False
    return True


def _filter_chunk_by_date(rows: list[list[Any]], date_idx: int, start_d, end_d, header_in_first: bool) -> tuple[list[list[Any]], int]:
    if start_d is None and end_d is None:
        return rows, 0
    kept: list[list[Any]] = []
    skipped = 0
    for index, row in enumerate(rows):
        if header_in_first and index == 0:
            kept.append(row)
            continue
        if _row_in_date_range(row, date_idx, start_d, end_d):
            kept.append(row)
        else:
            skipped += 1
    return kept, skipped


def _log_catalog_finished(
    log: LogFn,
    *,
    added: int,
    existing: int,
    width: int,
    skipped_dup: int,
    skipped_date: int,
    ok_n: int,
    perm_n: int,
    miss_n: int,
    book: str,
    sheet: str,
) -> None:
    total = existing + added
    log("======== 目录汇总已结束 ========")
    log(f"本轮新增：{added} 行 × {width} 列")
    log(f"目标表一共：{total} 行（原有 {existing} + 新增 {added}）")
    log(f"重复跳过：{skipped_dup} 行")
    log(f"日期外跳过：{skipped_date} 行")
    log(f"工作表：成功 {ok_n} 个 · 无权限 {perm_n} 个 · 未匹配 {miss_n} 个")
    if book or sheet:
        log(f"写入位置：「{book}」/「{sheet}」")
    log(
        f"目录汇总已结束：本轮新增 {added} 行，目标表一共 {total} 行。"
        f"重复跳过 {skipped_dup}，日期外 {skipped_date}。成功 {ok_n} 个工作表。"
    )


def _take_new_rows(rows: list[list[Any]], seen: set[tuple[str, ...]]) -> tuple[list[list[Any]], int]:
    kept: list[list[Any]] = []
    skipped = 0
    for row in rows:
        key = _row_fingerprint(row)
        if not key:
            continue
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        kept.append(row)
    return kept, skipped


def _is_cell_limit_error(exc: Exception) -> bool:
    text = str(exc)
    return "10000000" in text or "number of cells" in text.lower()


def _get_or_create_sheet(ss, title: str, log: LogFn):
    import gspread

    try:
        ws = ss.worksheet(title)
        rows = max(1, int(getattr(ws, "row_count", 1) or 1))
        cols = max(1, int(getattr(ws, "col_count", 1) or 1))
        log(
            f"沿用已有「{title}」（{rows} 行 × {cols} 列），"
            "按 1000 行一批写入，已有行会跳过"
        )
        return ws
    except gspread.exceptions.WorksheetNotFound:
        pass
    last_error: Exception | None = None
    for rows, cols in ((200, 8), (80, 4), (40, 2)):
        try:
            ws = ss.add_worksheet(title=title, rows=rows, cols=cols)
            log(f"已新建目标工作表「{title}」（先 {rows}×{cols}，按实际有数据的列扩展）")
            return ws
        except Exception as exc:
            last_error = exc
            if not _is_cell_limit_error(exc):
                raise
            log("新建工作表会超过 1000 万格上限，改用更小的表重试…")
            try:
                existing = ss.worksheet(title)
                log(f"已有「{title}」，继续在原表上分批写入，不清成 1 格")
                return existing
            except Exception:
                continue
    raise RuntimeError(
        "目标表格已达到 Google 1000 万单元格上限，无法再写入。"
        "请打开目标文件，删掉空白列很多的工作表，或换一张空表再汇总。"
        "已写入的内容不会清成 1 格。"
    ) from last_error


def _pad_width(rows: list[list[Any]], width: int) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in rows:
        clipped = list(row[:width])
        if len(clipped) < width:
            clipped.extend([""] * (width - len(clipped)))
        out.append(clipped)
    return out


class _StreamingWriter:
    """Read a source batch, write it immediately. Width follows the last used column, growing if later batches are wider."""

    def __init__(self, ss, sheet_name: str, start_row: int, log: LogFn, append: bool = False) -> None:
        self.ss = ss
        self.sheet_name = sheet_name
        self.start_row = start_row
        self.write_row = start_row
        self.width = 1
        self.ws = None
        self.buffer: list[list[Any]] = []
        self.total = 0
        self.log = log
        self.prepared = False
        self.append = append
        self.existing_rows = 0

    def _prepare(self) -> None:
        if self.prepared:
            return
        self.ws = _get_or_create_sheet(self.ss, self.sheet_name, self.log)
        self.prepared = True

    def load_existing(self) -> set[tuple[str, ...]]:
        """Read current target rows for dedupe, then append after the last used row."""
        self._prepare()
        keys: set[tuple[str, ...]] = set()
        rows_n = 0
        grid_rows, grid_cols = _sheet_grid(self.ws)
        if grid_rows <= 1 and grid_cols <= 1:
            self.existing_rows = 0
            return keys
        self.log("读取目标表已有数据，已存在的行会跳过…")
        for chunk in _read_source_batches(self.ws, self.log):
            for row in chunk:
                key = _row_fingerprint(row)
                if not key:
                    continue
                keys.add(key)
                rows_n += 1
                self.width = max(self.width, len(key))
        self.existing_rows = rows_n
        self.write_row = self.start_row + rows_n
        self.log(f"目标表已有 {rows_n} 行，从第 {self.write_row} 行起追加新行")
        return keys

    def _ensure_grid(self, extra_rows: int, width: int) -> None:
        self._prepare()
        need_rows = max(self.write_row + extra_rows + 2, self.start_row + 2)
        need_cols = max(width, 1)
        current_rows = int(getattr(self.ws, "row_count", 1) or 1)
        current_cols = int(getattr(self.ws, "col_count", 1) or 1)
        # Grow only this batch. Never shrink here (that used to wipe the sheet to 1×1).
        grow_rows = max(current_rows, min(need_rows, current_rows + max(extra_rows, 0) + 2))
        grow_cols = max(current_cols, need_cols)
        if current_rows < grow_rows or current_cols < grow_cols:
            try:
                safe_resize_ws(self.ws, grow_rows, grow_cols, log=self.log)
            except RuntimeError as exc:
                if self.total > 0 or current_rows > 1 or current_cols > 1:
                    raise RuntimeError(
                        f"已写入 {self.total} 行后无法再扩大「{self.sheet_name}」（工作簿接近 1000 万格）。"
                        "已写入的数据已保留。请换一张新的空表格当目标，用日期筛选接着备份剩余范围。"
                    ) from exc
                raise

    def add_rows(self, rows: list[list[Any]]) -> None:
        rows = compact_sheet_rows(rows)
        if not rows:
            return
        self.buffer.extend(rows)
        while len(self.buffer) >= WRITE_BATCH:
            self._flush(self.buffer[:WRITE_BATCH])
            self.buffer = self.buffer[WRITE_BATCH:]

    def _flush(self, chunk: list[list[Any]]) -> None:
        if not chunk:
            return
        width = max(self.width, used_column_count(chunk))
        padded = _pad_width(chunk, width)
        self._ensure_grid(len(padded), width)
        end_col = index_to_col_letter(width - 1)
        start = self.write_row
        with_retry(
            lambda: self.ws.update(
                range_name=f"A{start}:{end_col}{start + len(padded) - 1}",
                values=padded,
                value_input_option="USER_ENTERED",
            ),
            log=self.log,
            what=f"写入第 {start} 行起 {len(padded)} 行",
        )
        self.write_row += len(padded)
        self.width = width
        self.total += len(padded)
        self.log(f"  已写入 {self.total} 行，当前 {self.width} 列")

    def finish(self) -> int:
        if self.buffer:
            self._flush(self.buffer)
            self.buffer = []
        if not self.prepared:
            self._prepare()
        if self.total == 0:
            if self.append and self.existing_rows:
                self.log("没有新行需要写入，目标表原有数据未改动")
            else:
                self.log("没有可写入的数据（空行已跳过）")
            return 0
        try:
            # Append mode must not shrink below already stored rows.
            safe_resize_ws(self.ws, max(self.write_row + 1, self.start_row), max(self.width, 1), log=self.log)
        except Exception as exc:
            self.log(f"收紧目标表大小时跳过：{exc}")
        extra = f"，原表已有 {self.existing_rows} 行" if self.append and self.existing_rows else ""
        self.log(
            f"已写入「{self.ss.title}」/「{self.sheet_name}」：本轮新增 {self.total} 行，{self.width} 列{extra}"
        )
        return self.total


def _sheet_grid(ws) -> tuple[int, int]:
    rows = max(1, int(getattr(ws, "row_count", 1) or 1))
    cols = max(1, int(getattr(ws, "col_count", 1) or 1))
    return rows, cols


def _is_grid_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "exceeds grid limits" in text or "max rows" in text or "max columns" in text


def _read_source_batches(ws, log: LogFn, cancelled=None):
    """Yield compact 1000-row batches. Never request past the sheet's actual row/column grid."""
    max_rows, max_cols = _sheet_grid(ws)
    start = 1
    width = min(MAX_SOURCE_COLS, max_cols)
    empty_rounds = 0
    log(f"读取「{ws.title}」实际网格 {max_rows} 行 × {max_cols} 列")
    while start <= max_rows:
        if cancelled and cancelled():
            raise RuntimeError("已停止")
        end = min(start + READ_BATCH - 1, max_rows)
        chunk: list[list[Any]] = []
        hit_limit = False
        letter = index_to_col_letter(min(max(width, 1), max_cols) - 1)
        range_name = f"A{start}:{letter}{end}"

        def _get(range_name=range_name):
            return ws.get(range_name) or []

        try:
            chunk = with_retry(_get, log=log, what=f"读取「{ws.title}」{start}-{end} 行") or []
        except Exception as exc:
            if _is_grid_limit_error(exc):
                hit_limit = True
                log(f"「{ws.title}」已到表格网格上限，停止继续往下读")
            else:
                raise
        used = used_column_count(chunk)
        if used and used < width:
            width = max(used, 1)
        if hit_limit:
            break
        compact = compact_sheet_rows(chunk)
        if not compact:
            empty_rounds += 1
            if empty_rounds >= 2 or end >= max_rows:
                break
            start = end + 1
            continue
        empty_rounds = 0
        width = max(width, used_column_count(compact))
        yield compact
        start = end + 1


def _cancelled(flag) -> bool:
    return bool(flag and flag())


def run_catalog_merge(cfg, log: LogFn = print, cancelled=None) -> dict[str, Any]:
    log = _safe_log(log)
    index_url = str(getattr(cfg, "catalog_index_url", "") or "").strip()
    target_url = str(getattr(cfg, "catalog_target_url", "") or "").strip()
    if not index_url:
        raise RuntimeError("请填写目录表链接")
    if not target_url:
        raise RuntimeError("请填写目标表链接")
    url_col = _col_index(getattr(cfg, "catalog_url_col", "B"))
    sheet_col = _col_index(getattr(cfg, "catalog_sheet_col", "D"))
    start_row = max(1, int(getattr(cfg, "catalog_start_row", 2) or 2))
    output_start = max(1, int(getattr(cfg, "catalog_output_start_row", 1) or 1))
    keep_each_header = bool(getattr(cfg, "catalog_keep_each_header", False))
    add_source = bool(getattr(cfg, "catalog_add_source", True))
    skip_existing = bool(getattr(cfg, "catalog_skip_existing", True))
    date_filter_on = bool(getattr(cfg, "catalog_date_filter_enabled", True))
    start_d = _as_day(getattr(cfg, "catalog_start_date", "") or "") if date_filter_on else None
    end_d = _as_day(getattr(cfg, "catalog_end_date", "") or "") if date_filter_on else None
    use_dates = date_filter_on and (start_d is not None or end_d is not None)
    date_col_letter = str(getattr(cfg, "catalog_date_col", "") or "").strip()
    exclude_sheets = _parse_name_list(getattr(cfg, "catalog_exclude_sheets", None))
    if exclude_sheets:
        log("排除工作表：" + "、".join(exclude_sheets))

    gc = authorize_cfg(cfg, log=log)
    index_ss = open_by_url_or_id(gc, index_url, log=log)
    index_ws = pick_source_ws(index_ss, getattr(cfg, "catalog_index_sheet", ""))
    index_rows = read_sheet_values(index_ws, log=log)
    url_letter = index_to_col_letter(url_col)
    last = start_row + max(0, len(index_rows) - start_row)
    hyper_urls: list[str] = []
    try:
        hyper_urls = _read_link_column(
            _session_from_gc(gc),
            index_ss.id,
            index_ws.title,
            url_letter,
            start_row,
            max(last, start_row + len(index_rows) - 1),
            log=log,
        )
    except Exception as exc:
        log(f"读取目录链接列超链接失败，改用单元格文字：{exc}")
    url_entries: list[tuple[int, str]] = []
    sheet_entries: list[tuple[int, str]] = []
    direct_entries: list[tuple[int, dict[str, Any], str]] = []
    seen_urls: set[str] = set()
    seen_direct: set[tuple] = set()
    for offset, row in enumerate(index_rows[start_row - 1 :]):
        row_number = start_row + offset
        raw = _cell(row, url_col)
        hyper = hyper_urls[offset] if offset < len(hyper_urls) else ""
        link = _combine_catalog_link(hyper, raw)
        sheet_name = _cell(row, sheet_col) or link["label"]
        skip_sheet = _catalog_name_excluded(sheet_name, exclude_sheets) or _catalog_name_excluded(link["label"], exclude_sheets)
        if skip_sheet and (sheet_name or link["label"]):
            log(f"目录第 {row_number} 行「{sheet_name or link['label']}」在排除名单中，已跳过")
        if link["internal"] or (link["gid"] is not None and not link["spreadsheet_id"]):
            if skip_sheet:
                continue
            key = ("internal", link["gid"], _normalized_title(sheet_name or link["label"]))
            if key not in seen_direct:
                seen_direct.add(key)
                direct_entries.append((row_number, link, sheet_name))
            if sheet_name:
                sheet_entries.append((row_number, sheet_name))
            continue
        url = ""
        if link["spreadsheet_id"]:
            url = spreadsheet_url(link["spreadsheet_id"])
            if link["gid"] is not None and not skip_sheet:
                key = (link["spreadsheet_id"], link["gid"])
                if key not in seen_direct:
                    seen_direct.add(key)
                    direct_entries.append((row_number, link, sheet_name))
        else:
            url = extract_url(hyper) or extract_url(raw)
            if not url:
                candidate = (hyper or raw).strip()
                if candidate.lower().startswith("http") or "docs.google.com" in candidate.lower():
                    url = candidate.split()[0]
                elif candidate and " " not in candidate and "/" not in candidate and not candidate.startswith("#"):
                    url = candidate
            if url and url.lower().startswith("#"):
                url = ""
        if url and extract_spreadsheet_id(url) and url not in seen_urls:
            seen_urls.add(url)
            url_entries.append((row_number, url))
        elif url and url not in seen_urls and " " not in url and "/" not in url:
            seen_urls.add(url)
            url_entries.append((row_number, url))
        elif raw and not url:
            log(f"目录第 {row_number} 行链接列是「{raw[:80]}」，没有读到网址或 #gid= 工作表链接，已跳过")
        if sheet_name and not skip_sheet:
            sheet_entries.append((row_number, sheet_name))
    if not url_entries and not direct_entries:
        raise RuntimeError(
            "目录表的链接列没有找到表格链接。"
            "B 列可以是完整表格网址，也可以是本表内部链接，例如 =HYPERLINK(\"#gid=995133928\",\"1751-小源\")"
        )
    if not sheet_entries and not direct_entries:
        raise RuntimeError("目录表的工作表名称列没有找到名称，且链接列也没有 #gid= 内部工作表链接")
    log(
        f"目录表读到 {len(url_entries)} 个表格链接、{len(direct_entries)} 个本表/指定工作表链接、"
        f"{len(sheet_entries)} 个工作表名"
    )

    opened: list[tuple[int, Any, list[Any]]] = []
    open_errors: list[dict[str, Any]] = []
    opened_by_id: dict[str, Any] = {}
    for number, (row_number, url) in enumerate(url_entries, 1):
        if _cancelled(cancelled):
            raise RuntimeError("已停止")
        try:
            source_ss = open_by_url_or_id(gc, url, log=log)
            worksheets = _worksheets_cached(source_ss, log)
            opened.append((row_number, source_ss, worksheets))
            opened_by_id[str(source_ss.id)] = source_ss
            log(f"[链接 {number}/{len(url_entries)}] 目录第 {row_number} 行「{source_ss.title}」：{len(worksheets)} 个工作表")
        except Exception as exc:
            detail = str(exc).strip()
            if isinstance(exc, PermissionError) or not detail:
                detail = "服务账号没有访问权限；请把这个 B 列表格共享给当前服务账号"
            open_errors.append({"row": row_number, "url": url, "sheet": "", "rows": 0, "error": detail})
            log(f"[链接 {number}/{len(url_entries)}] 目录第 {row_number} 行链接无法打开，已跳过：{detail}")
    if direct_entries:
        opened_by_id[str(index_ss.id)] = index_ss
        if not any(ss is index_ss or str(getattr(ss, "id", "")) == str(index_ss.id) for _row, ss, _ws in opened):
            opened.append((0, index_ss, _worksheets_cached(index_ss, log)))
            log(f"链接列含本表 #gid= 内部链接，已把目录文件「{index_ss.title}」加入查找范围")
    if not opened and not direct_entries:
        raise RuntimeError("目录中的所有表格链接都无法打开")

    sources: list[dict[str, Any]] = list(open_errors)
    header_written = False
    requested: dict[str, tuple[int, str]] = {}
    for row_number, sheet_name in sheet_entries:
        requested.setdefault(_normalized_title(sheet_name), (row_number, sheet_name))
    found_names: set[str] = set()
    merged_keys: set[tuple] = set()
    excluded_logged: set[str] = set()
    match_number = 0
    target_ss = open_by_url_or_id(gc, target_url, log=log)
    output_sheet = str(getattr(cfg, "catalog_output_sheet", "目录汇总") or "目录汇总").strip()
    writer = _StreamingWriter(target_ss, output_sheet, output_start, log)
    seen: set[tuple[str, ...]] = set()
    skipped_dup = 0
    skipped_date = 0
    date_idx = -1
    date_idx_ready = False
    if skip_existing:
        if hasattr(writer, "append"):
            writer.append = True
        if hasattr(writer, "load_existing"):
            seen.update(writer.load_existing())
            if getattr(writer, "existing_rows", 0):
                header_written = True
        log("已有行会跳过，只追加新行。表满了请换目标表链接，并用日期筛选接着备份。")
    log("目录汇总按实际有数据的列写入：空列不写、空行跳过、读一批写一批（每批 1000 行）。列变多时按本轮最大列数扩展。")
    if add_source:
        log("写入时在 A 列追加来源（目录里的工作表名称）")
    if use_dates:
        log(f"日期筛选：{start_d or '…'} ~ {end_d or '…'}" + (f"，源表列 {date_col_letter.upper()}" if date_col_letter else "（日期列未填则从表头识别）"))

    def _merge_ws(source_row: int, source_ss, source_ws, name_row: int, sheet_name: str) -> None:
        nonlocal header_written, match_number, skipped_dup, skipped_date, date_idx, date_idx_ready
        if source_ss.id == index_ss.id and getattr(source_ws, "id", None) == getattr(index_ws, "id", None):
            return
        title = source_ws.title
        if _catalog_name_excluded(title, exclude_sheets) or _catalog_name_excluded(sheet_name, exclude_sheets):
            key = (str(source_ss.id), str(getattr(source_ws, "id", title)))
            merged_keys.add(key)
            found_names.add(_normalized_title(sheet_name or title))
            found_names.add(_normalized_title(title))
            if title not in excluded_logged:
                excluded_logged.add(title)
                log(f"已排除工作表「{title}」，不汇总")
            return
        key = (str(source_ss.id), str(getattr(source_ws, "id", source_ws.title)))
        if key in merged_keys:
            return
        merged_keys.add(key)
        found_names.add(_normalized_title(sheet_name or source_ws.title))
        match_number += 1
        item = {
            "row": name_row,
            "source_row": source_row,
            "url": spreadsheet_url(source_ss.id),
            "sheet": sheet_name or source_ws.title,
            "title": source_ss.title,
            "rows": 0,
            "error": None,
        }
        try:
            first_batch = True
            got_any = False
            source_label = (sheet_name or source_ws.title or "").strip()
            sheet_dup = 0
            sheet_date = 0
            for chunk in _read_source_batches(source_ws, log, cancelled=cancelled):
                header_in_chunk = False
                if first_batch:
                    first_batch = False
                    if header_written and not keep_each_header and chunk:
                        chunk = chunk[1:]
                    elif chunk:
                        header_written = True
                        header_in_chunk = True
                chunk = drop_empty_rows(chunk)
                if not chunk:
                    continue
                if use_dates:
                    if not date_idx_ready:
                        date_idx_ready = True
                        if date_col_letter:
                            try:
                                date_idx = col_letter_to_index(date_col_letter)
                            except ValueError:
                                date_idx = _detect_date_index(chunk[0] if header_in_chunk else [])
                                log(f"日期列「{date_col_letter}」无效，改从表头识别")
                        else:
                            date_idx = _detect_date_index(chunk[0] if header_in_chunk else [])
                        if date_idx >= 0:
                            log(f"日期列按源表第 {date_idx + 1} 列筛选")
                        else:
                            log("未找到日期列，日期筛选未生效。请填写日期列字母")
                    chunk, n_date = _filter_chunk_by_date(chunk, date_idx, start_d, end_d, header_in_chunk)
                    sheet_date += n_date
                    skipped_date += n_date
                    if not chunk:
                        continue
                if add_source:
                    chunk = _attach_source_column(chunk, source_label, header_in_chunk)
                chunk, n_dup = _take_new_rows(chunk, seen)
                sheet_dup += n_dup
                skipped_dup += n_dup
                if not chunk:
                    continue
                got_any = True
                writer.add_rows(chunk)
                item["rows"] += len(chunk)
            extra = []
            if sheet_dup:
                extra.append(f"重复跳过 {sheet_dup}")
            if sheet_date:
                extra.append(f"日期外 {sheet_date}")
            extra_s = f"（{'，'.join(extra)}）" if extra else ""
            running = int(getattr(writer, "total", 0) or 0) + len(getattr(writer, "buffer", []) or [])
            if not got_any and first_batch and not sheet_dup and not sheet_date:
                log(f"[匹配 {match_number}] 「{source_ss.title}」/「{source_ws.title}」没有有效数据，已跳过")
            else:
                log(
                    f"[匹配 {match_number}] 「{source_ss.title}」→「{source_ws.title}」："
                    f"新增 {item['rows']} 行，本轮累计 {running} 行{extra_s}"
                )
        except Exception as exc:
            item["error"] = str(exc)
            log(f"[匹配 {match_number}] 「{source_ss.title}」/「{source_ws.title}」读取失败，已跳过：{exc}")
        sources.append(item)

    for row_number, link, sheet_name in direct_entries:
        if _cancelled(cancelled):
            raise RuntimeError("已停止")
        source_ss = index_ss
        if link["spreadsheet_id"] and str(link["spreadsheet_id"]) != str(index_ss.id):
            source_ss = opened_by_id.get(str(link["spreadsheet_id"]))
            if source_ss is None:
                try:
                    source_ss = open_by_url_or_id(gc, spreadsheet_url(link["spreadsheet_id"]), log=log)
                    opened.append((row_number, source_ss, _worksheets_cached(source_ss, log)))
                    opened_by_id[str(source_ss.id)] = source_ss
                except Exception as exc:
                    detail = str(exc).strip() or "无法打开链接中的表格"
                    sources.append({"row": row_number, "url": link["target"], "sheet": sheet_name, "rows": 0, "error": detail})
                    log(f"目录第 {row_number} 行内部/指定工作表链接无法打开：{detail}")
                    continue
        source_ws = _find_ws(
            source_ss,
            gid=link["gid"],
            title=sheet_name or link["label"],
            log=log,
        )
        if source_ws is None:
            detail = (
                f"找不到工作表 gid={link['gid']}"
                + (f" / 「{sheet_name or link['label']}」" if (sheet_name or link["label"]) else "")
            )
            sources.append({"row": row_number, "url": spreadsheet_url(source_ss.id), "sheet": sheet_name, "rows": 0, "error": detail})
            log(f"目录第 {row_number} 行 {detail}，已跳过")
            continue
        _merge_ws(row_number, source_ss, source_ws, row_number, sheet_name or source_ws.title)

    for source_row, source_ss, worksheets in opened:
        by_title = {_normalized_title(ws.title): ws for ws in worksheets}
        for wanted, (name_row, sheet_name) in requested.items():
            source_ws = by_title.get(wanted)
            if source_ws is None:
                continue
            _merge_ws(source_row, source_ss, source_ws, name_row, sheet_name)

    for wanted, (row_number, sheet_name) in requested.items():
        if wanted in found_names or _catalog_name_excluded(sheet_name, exclude_sheets):
            continue
        candidates: list[tuple[float, str, str]] = []
        for _source_row, source_ss, worksheets in opened:
            for ws in worksheets:
                score = difflib.SequenceMatcher(None, wanted, _normalized_title(ws.title)).ratio()
                candidates.append((score, source_ss.title, ws.title))
        candidates.sort(reverse=True)
        hints = "、".join(f"「{book}/{title}」" for _score, book, title in candidates[:3]) or "（没有工作表）"
        error = f"遍历 {len(opened)} 个可访问表格后仍找不到；相近项：{hints}"
        sources.append({"row": row_number, "url": "", "sheet": sheet_name, "rows": 0, "error": error})
        log(f"D 列第 {row_number} 行「{sheet_name}」未匹配，已跳过：{error}")

    total_rows = writer.finish()
    existing_n = int(getattr(writer, "existing_rows", 0) or 0)
    width = int(getattr(writer, "width", 1) or 1)
    ok_n = sum(1 for item in sources if not item.get("error") and item.get("rows"))
    perm_n = sum(1 for item in sources if item.get("error") and "权限" in str(item.get("error") or ""))
    miss_n = sum(1 for item in sources if item.get("error") and "找不到" in str(item.get("error") or ""))
    if total_rows <= 0 and not skipped_dup and not existing_n:
        raise RuntimeError("所有目录项均读取失败或全是空行，没有可写入的数据")
    sheet_total = existing_n + total_rows
    _log_catalog_finished(
        log,
        added=total_rows,
        existing=existing_n,
        width=width,
        skipped_dup=skipped_dup,
        skipped_date=skipped_date,
        ok_n=ok_n,
        perm_n=perm_n,
        miss_n=miss_n,
        book=getattr(target_ss, "title", "") or "",
        sheet=output_sheet,
    )
    return {
        "ok": True,
        "mode": "catalog",
        "total_rows": total_rows,
        "sheet_total": sheet_total,
        "skipped": skipped_dup,
        "date_skipped": skipped_date,
        "existing_rows": existing_n,
        "ok_sheets": ok_n,
        "sources": sources,
        "target_url": spreadsheet_url(target_ss.id),
        "sheet": output_sheet,
    }
