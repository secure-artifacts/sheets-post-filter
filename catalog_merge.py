# -*- coding: utf-8 -*-
"""目录表驱动汇总：按索引表中的链接和工作表名称合并全部内容。"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Callable

from fetch_posts import (
    authorize_cfg,
    compact_sheet_rows,
    drop_empty_rows,
    index_to_col_letter,
    open_by_url_or_id,
    pick_source_ws,
    read_sheet_values,
    safe_resize_ws,
    spreadsheet_url,
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


def _pick_catalog_ws(spreadsheet, requested: str):
    """Match titles robustly while never silently selecting an unrelated sheet."""
    worksheets = spreadsheet.worksheets()
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


def _is_cell_limit_error(exc: Exception) -> bool:
    text = str(exc)
    return "10000000" in text or "number of cells" in text.lower()


def _shrink_output_sheet(ws, log: LogFn) -> None:
    try:
        ws.resize(rows=1, cols=1)
        log(f"已把「{ws.title}」缩成 1×1，腾出单元格后再按实际数据扩展")
    except Exception as exc:
        log(f"缩小「{ws.title}」失败：{exc}")


def _get_or_create_sheet(ss, title: str, log: LogFn):
    import gspread

    try:
        ws = ss.worksheet(title)
        _shrink_output_sheet(ws, log)
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
                _shrink_output_sheet(existing, log)
                return existing
            except Exception:
                continue
    raise RuntimeError(
        "目标表格已达到 Google 1000 万单元格上限，无法再写入。"
        "请打开目标文件，删掉空白列很多的工作表，或换一张空表再汇总。"
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

    def __init__(self, ss, sheet_name: str, start_row: int, log: LogFn) -> None:
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

    def _prepare(self) -> None:
        if self.prepared:
            return
        self.ws = _get_or_create_sheet(self.ss, self.sheet_name, self.log)
        self.prepared = True

    def _ensure_grid(self, extra_rows: int, width: int) -> None:
        self._prepare()
        need_rows = max(self.write_row + extra_rows + 2, self.start_row + 2)
        need_cols = max(width, 1)
        current_rows = int(getattr(self.ws, "row_count", 1) or 1)
        current_cols = int(getattr(self.ws, "col_count", 1) or 1)
        if current_rows < need_rows or current_cols < need_cols:
            try:
                safe_resize_ws(self.ws, max(current_rows, need_rows), max(current_cols, need_cols), log=self.log)
            except RuntimeError:
                self.log("目标表接近单元格上限，先缩到已写入范围再扩展")
                _shrink_output_sheet(self.ws, self.log)
                safe_resize_ws(self.ws, need_rows, need_cols, log=self.log)

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
            self.log("没有可写入的数据（空行已跳过）")
            return 0
        try:
            safe_resize_ws(self.ws, max(self.write_row + 1, self.start_row), max(self.width, 1), log=self.log)
        except Exception as exc:
            self.log(f"收紧目标表大小时跳过：{exc}")
        self.log(f"已写入「{self.ss.title}」/「{self.sheet_name}」：{self.total} 行，{self.width} 列（按最后有数据的列）")
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
    width = min(8, max_cols)
    empty_rounds = 0
    log(f"读取「{ws.title}」实际网格 {max_rows} 行 × {max_cols} 列")
    while start <= max_rows:
        if cancelled and cancelled():
            raise RuntimeError("已停止")
        end = min(start + READ_BATCH - 1, max_rows)
        chunk: list[list[Any]] = []
        hit_limit = False
        for _ in range(6):
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
                    break
                raise
            used = used_column_count(chunk)
            col_cap = min(MAX_SOURCE_COLS, max_cols)
            if used < width or width >= col_cap:
                if used > width:
                    width = min(col_cap, used)
                break
            width = min(col_cap, max(used + 4, width + 20))
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
    seen_urls: set[str] = set()
    for offset, row in enumerate(index_rows[start_row - 1 :]):
        row_number = start_row + offset
        raw = _cell(row, url_col)
        hyper = hyper_urls[offset] if offset < len(hyper_urls) else ""
        url = extract_url(hyper) or extract_url(raw)
        if not url:
            candidate = (hyper or raw).strip()
            if candidate.lower().startswith("http") or "docs.google.com" in candidate.lower():
                url = candidate.split()[0]
            elif candidate and " " not in candidate and "/" not in candidate:
                url = candidate
        sheet_name = _cell(row, sheet_col)
        if url and url not in seen_urls:
            seen_urls.add(url)
            url_entries.append((row_number, url))
        elif raw and not url:
            log(f"目录第 {row_number} 行链接列是「{raw[:40]}」，没有读到网址，已跳过")
        if sheet_name:
            sheet_entries.append((row_number, sheet_name))
    if not url_entries:
        raise RuntimeError("目录表的链接列没有找到表格链接")
    if not sheet_entries:
        raise RuntimeError("目录表的工作表名称列没有找到名称")
    log(f"目录表读到 {len(url_entries)} 个链接、{len(sheet_entries)} 个工作表名；将用每个名称遍历全部链接查找")

    opened: list[tuple[int, Any, list[Any]]] = []
    open_errors: list[dict[str, Any]] = []
    for number, (row_number, url) in enumerate(url_entries, 1):
        if _cancelled(cancelled):
            raise RuntimeError("已停止")
        try:
            source_ss = open_by_url_or_id(gc, url, log=log)
            worksheets = source_ss.worksheets()
            opened.append((row_number, source_ss, worksheets))
            log(f"[链接 {number}/{len(url_entries)}] 目录第 {row_number} 行「{source_ss.title}」：{len(worksheets)} 个工作表")
        except Exception as exc:
            detail = str(exc).strip()
            if isinstance(exc, PermissionError) or not detail:
                detail = "服务账号没有访问权限；请把这个 B 列表格共享给当前服务账号"
            open_errors.append({"row": row_number, "url": url, "sheet": "", "rows": 0, "error": detail})
            log(f"[链接 {number}/{len(url_entries)}] 目录第 {row_number} 行链接无法打开，已跳过：{detail}")
    if not opened:
        raise RuntimeError("目录中的所有表格链接都无法打开")

    sources: list[dict[str, Any]] = list(open_errors)
    header_written = False
    requested: dict[str, tuple[int, str]] = {}
    for row_number, sheet_name in sheet_entries:
        requested.setdefault(_normalized_title(sheet_name), (row_number, sheet_name))
    found_names: set[str] = set()
    match_number = 0
    target_ss = open_by_url_or_id(gc, target_url, log=log)
    output_sheet = str(getattr(cfg, "catalog_output_sheet", "目录汇总") or "目录汇总").strip()
    writer = _StreamingWriter(target_ss, output_sheet, output_start, log)
    log("目录汇总按实际有数据的列写入：空列不写、空行跳过、读一批写一批（每批 1000 行）。列变多时按本轮最大列数扩展。")
    for source_row, source_ss, worksheets in opened:
        by_title = {_normalized_title(ws.title): ws for ws in worksheets}
        for wanted, (name_row, sheet_name) in requested.items():
            source_ws = by_title.get(wanted)
            if source_ws is None:
                continue
            found_names.add(wanted)
            match_number += 1
            item = {
                "row": name_row,
                "source_row": source_row,
                "url": spreadsheet_url(source_ss.id),
                "sheet": sheet_name,
                "title": source_ss.title,
                "rows": 0,
                "error": None,
            }
            try:
                first_batch = True
                got_any = False
                for chunk in _read_source_batches(source_ws, log, cancelled=cancelled):
                    if first_batch:
                        first_batch = False
                        if header_written and not keep_each_header and chunk:
                            chunk = chunk[1:]
                        elif chunk:
                            header_written = True
                    chunk = drop_empty_rows(chunk)
                    if not chunk:
                        continue
                    got_any = True
                    writer.add_rows(chunk)
                    item["rows"] += len(chunk)
                if not got_any and first_batch:
                    log(f"[匹配 {match_number}] 「{source_ss.title}」/「{source_ws.title}」没有有效数据，已跳过")
                else:
                    log(
                        f"[匹配 {match_number}] B 列文件「{source_ss.title}」→ D 列「{source_ws.title}」："
                        f"{item['rows']} 行（空行已跳过）"
                    )
            except Exception as exc:
                item["error"] = str(exc)
                log(f"[匹配 {match_number}] 「{source_ss.title}」/「{source_ws.title}」读取失败，已跳过：{exc}")
            sources.append(item)

    for wanted, (row_number, sheet_name) in requested.items():
        if wanted in found_names:
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
    if total_rows <= 0:
        raise RuntimeError("所有目录项均读取失败或全是空行，没有可写入的数据")
    ok_n = sum(1 for item in sources if not item.get("error") and item.get("rows"))
    perm_n = sum(1 for item in sources if item.get("error") and "权限" in str(item.get("error") or ""))
    miss_n = sum(1 for item in sources if item.get("error") and "找不到" in str(item.get("error") or ""))
    log(
        f"写入完成：成功 {ok_n} 个工作表，无权限跳过 {perm_n} 个，未匹配 {miss_n} 个，共 {total_rows} 行 × {writer.width} 列。"
        "无权限的表格请把 B 列对应文件共享给上面的服务账号。"
    )
    return {
        "ok": True,
        "mode": "catalog",
        "total_rows": total_rows,
        "sources": sources,
        "target_url": spreadsheet_url(target_ss.id),
        "sheet": output_sheet,
    }
