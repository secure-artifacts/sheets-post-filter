# -*- coding: utf-8 -*-
"""目录表驱动汇总：按索引表中的链接和工作表名称合并全部内容。"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Callable

from fetch_posts import (
    authorize,
    index_to_col_letter,
    open_by_url_or_id,
    pick_source_ws,
    read_sheet_values,
    service_account_email,
    spreadsheet_url,
    with_retry,
)

LogFn = Callable[[str], None]


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


def _write_matrix(ss, sheet_name: str, start_row: int, rows: list[list[Any]], log: LogFn) -> None:
    import gspread

    width = max((len(row) for row in rows), default=1)
    try:
        ws = ss.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(
            title=sheet_name,
            rows=max(100, start_row + len(rows) + 20),
            cols=max(8, width),
        )
        log(f"已新建目标工作表「{sheet_name}」")
    end_col = index_to_col_letter(max(width - 1, 0))
    if ws.row_count >= start_row:
        with_retry(
            lambda: ws.batch_clear([f"A{start_row}:{end_col}{ws.row_count}"]),
            log=log,
            what="清空旧汇总数据",
        )
    if not rows:
        log("没有可写入的数据，目标输出区已清空")
        return
    if ws.row_count < start_row + len(rows) or ws.col_count < width:
        ws.resize(
            rows=max(ws.row_count, start_row + len(rows) + 20),
            cols=max(ws.col_count, width),
        )
    for offset in range(0, len(rows), 500):
        chunk = rows[offset : offset + 500]
        with_retry(
            lambda r=start_row + offset, values=chunk: ws.update(
                range_name=f"A{r}", values=values, value_input_option="USER_ENTERED"
            ),
            log=log,
            what=f"写入第 {start_row + offset} 行起",
        )
    log(f"已写入「{ss.title}」/「{sheet_name}」：{len(rows)} 行，最多 {width} 列")


def run_catalog_merge(cfg, log: LogFn = print) -> dict[str, Any]:
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

    cred = cfg.resolve_credentials()
    log(f"服务账号: {service_account_email(cred) or cred}")
    gc = authorize(cred)
    index_ss = open_by_url_or_id(gc, index_url, log=log)
    index_ws = pick_source_ws(index_ss, getattr(cfg, "catalog_index_sheet", ""))
    index_rows = read_sheet_values(index_ws, log=log)
    url_entries: list[tuple[int, str]] = []
    sheet_entries: list[tuple[int, str]] = []
    seen_urls: set[str] = set()
    for row_number, row in enumerate(index_rows[start_row - 1 :], start_row):
        url = _cell(row, url_col)
        sheet_name = _cell(row, sheet_col)
        if url and url not in seen_urls:
            seen_urls.add(url)
            url_entries.append((row_number, url))
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

    merged: list[list[Any]] = []
    sources: list[dict[str, Any]] = list(open_errors)
    header_written = False
    requested: dict[str, tuple[int, str]] = {}
    for row_number, sheet_name in sheet_entries:
        requested.setdefault(_normalized_title(sheet_name), (row_number, sheet_name))
    found_names: set[str] = set()
    match_number = 0
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
                values = read_sheet_values(source_ws, log=log)
                if values and header_written and not keep_each_header:
                    values = values[1:]
                if values:
                    header_written = True
                merged.extend(values)
                item["rows"] = len(values)
                log(f"[匹配 {match_number}] B 列文件「{source_ss.title}」→ D 列「{source_ws.title}」：{len(values)} 行")
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

    if not merged:
        raise RuntimeError("所有目录项均读取失败，没有可写入的数据")
    target_ss = open_by_url_or_id(gc, target_url, log=log)
    output_sheet = str(getattr(cfg, "catalog_output_sheet", "目录汇总") or "目录汇总").strip()
    _write_matrix(target_ss, output_sheet, output_start, merged, log)
    return {
        "ok": any(not item["error"] for item in sources),
        "mode": "catalog",
        "total_rows": len(merged),
        "sources": sources,
        "target_url": spreadsheet_url(target_ss.id),
        "sheet": output_sheet,
    }
