# -*- coding: utf-8 -*-
"""贴文汇总：按数据列表里的表格链接读取「订阅」表，对照贴文库后写入整合表。"""

from __future__ import annotations

from typing import Any, Callable

from fetch_posts import (
    authorize_cfg,
    col_letter_to_index,
    extract_spreadsheet_id,
    index_to_col_letter,
    open_by_url_or_id,
    pick_source_ws,
    read_sheet_values,
    safe_resize_ws,
    spreadsheet_url,
    to_datetime,
    with_retry,
)
from video_duration import extract_url, _read_link_column, _session_from_gc

LogFn = Callable[[str], None]
WRITE_BATCH = 1000
DEFAULT_SOURCE_COLS = ["J", "M", "O", "A", "L", "E", "N"]


def _safe_log(log: LogFn) -> LogFn:
    def inner(message: str) -> None:
        try:
            log(str(message))
        except Exception:
            pass

    return inner


def _cell(row: list[Any], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _letter_index(letter: str) -> int:
    text = str(letter or "").strip().upper()
    if not text:
        return -1
    return col_letter_to_index(text)


def _parse_col_list(raw) -> list[str]:
    if isinstance(raw, str):
        parts = raw.replace("，", ",").replace(";", ",").replace(" ", ",").split(",")
        return [item.strip().upper() for item in parts if item.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        letter = str(item or "").strip().upper()
        if letter and letter not in seen:
            seen.add(letter)
            out.append(letter)
    return out


def _as_day(value: Any):
    dt = to_datetime(value)
    return dt.date() if dt else None


def _in_date_range(value: Any, start_d, end_d) -> bool:
    if start_d is None and end_d is None:
        return True
    day = _as_day(value)
    if day is None:
        return False
    if start_d and day < start_d:
        return False
    if end_d and day > end_d:
        return False
    return True


def collect_list_entries(
    rows: list[list[Any]],
    link_col: str,
    tag_col: str,
    start_row: int,
    hyperlinks: list[str] | None = None,
) -> list[dict[str, Any]]:
    link_idx = _letter_index(link_col)
    tag_idx = _letter_index(tag_col) if str(tag_col or "").strip() else -1
    if link_idx < 0:
        raise RuntimeError("数据列表的链接列必须填字母，例如 K")
    start = max(1, int(start_row or 2))
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for offset, row in enumerate(rows[start - 1 :]):
        hyper = hyperlinks[offset] if hyperlinks and offset < len(hyperlinks) else ""
        raw = _cell(row, link_idx)
        url = extract_url(hyper) or extract_url(raw)
        if not url:
            text = (hyper or raw).strip()
            if "docs.google.com" in text.lower() or extract_spreadsheet_id(text):
                url = text.split()[0]
        sid = extract_spreadsheet_id(url or raw)
        if not sid:
            continue
        key = sid
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "url": spreadsheet_url(sid),
                "id": sid,
                "tag": _cell(row, tag_idx) if tag_idx >= 0 else "",
                "row": start + offset,
            }
        )
    return entries


def build_lookup_map(rows: list[list[Any]], key_col: str, value_col: str, skip_header: bool = True) -> dict[str, str]:
    key_idx = _letter_index(key_col)
    value_idx = _letter_index(value_col)
    if key_idx < 0 or value_idx < 0:
        raise RuntimeError("贴文库对照的查找列和取值列必须填字母，例如 B、N")
    mapping: dict[str, str] = {}
    body = rows[1:] if skip_header and rows else rows
    for row in body:
        key = _cell(row, key_idx)
        if key and key not in mapping:
            mapping[key] = _cell(row, value_idx)
    return mapping


def rows_from_subscription(
    rows: list[list[Any]],
    source_cols: list[str],
    date_col: str,
    start_d,
    end_d,
    tag: str,
    include_tag: bool,
    match_col: str,
    lookup: dict[str, str],
    include_lookup: bool,
    skip_header: bool = True,
) -> tuple[list[list[str]], int]:
    letters = _parse_col_list(source_cols) or list(DEFAULT_SOURCE_COLS)
    date_idx = _letter_index(date_col) if str(date_col or "").strip() else -1
    match_idx = _letter_index(match_col) if str(match_col or "").strip() else -1
    body = rows[1:] if skip_header and rows else rows
    out: list[list[str]] = []
    skipped_date = 0
    use_dates = start_d is not None or end_d is not None
    for row in body:
        if use_dates:
            raw_date = _cell(row, date_idx) if date_idx >= 0 else ""
            if not _in_date_range(raw_date, start_d, end_d):
                skipped_date += 1
                continue
        cells = [_cell(row, _letter_index(letter)) for letter in letters]
        if not any(cells):
            continue
        if include_tag:
            cells.append(tag)
        if include_lookup:
            key = _cell(row, match_idx) if match_idx >= 0 else (cells[0] if cells else "")
            cells.append(lookup.get(key, ""))
        out.append(cells)
    return out, skipped_date


def library_state(rows: list[list[Any]], key_col: str) -> tuple[set[str], int]:
    """Existing keys in the 贴文库 key column, and the next empty 1-based row."""
    key_idx = _letter_index(key_col)
    if key_idx < 0:
        raise RuntimeError("贴文库排重列必须填字母，例如 B")
    keys: set[str] = set()
    last = 1
    for index, row in enumerate(rows):
        if any(str(cell or "").strip() for cell in row):
            last = index + 1
        if index == 0:
            continue
        key = _cell(row, key_idx)
        if key:
            keys.add(key)
    return keys, last + 1


def collect_match_values(
    rows: list[list[Any]],
    match_col: str,
    date_col: str,
    start_d,
    end_d,
    hyperlinks: list[str] | None = None,
    skip_header: bool = True,
) -> list[str]:
    """J-column links (prefer real hyperlinks) that pass the same date filter as 整合."""
    match_idx = _letter_index(match_col)
    date_idx = _letter_index(date_col) if str(date_col or "").strip() else -1
    if match_idx < 0:
        return []
    body = rows[1:] if skip_header and rows else rows
    use_dates = start_d is not None or end_d is not None
    values: list[str] = []
    for offset, row in enumerate(body):
        if use_dates:
            raw_date = _cell(row, date_idx) if date_idx >= 0 else ""
            if not _in_date_range(raw_date, start_d, end_d):
                continue
        text = _cell(row, match_idx)
        hyper = hyperlinks[offset] if hyperlinks and offset < len(hyperlinks) else ""
        value = extract_url(hyper) or extract_url(text) or text
        if value:
            values.append(value)
    return values


def pick_new_library_values(candidates: list[str], existing: set[str]) -> tuple[list[str], int]:
    """Keep first-seen values that are not already in 贴文库 B."""
    out: list[str] = []
    seen = set(existing)
    skipped = 0
    for raw in candidates:
        value = str(raw or "").strip()
        if not value:
            continue
        if value in seen:
            skipped += 1
            continue
        seen.add(value)
        out.append(value)
    return out, skipped


def build_library_append_rows(values: list[str], write_col: str) -> list[list[str]]:
    write_idx = _letter_index(write_col)
    if write_idx < 0:
        raise RuntimeError("贴文库写入列必须填字母，例如 B")
    rows: list[list[str]] = []
    for value in values:
        row = [""] * write_idx
        row.append(value)
        rows.append(row)
    return rows


def output_headers(source_cols: list[str], include_tag: bool, include_lookup: bool) -> list[str]:
    headers = list(_parse_col_list(source_cols) or DEFAULT_SOURCE_COLS)
    if include_tag:
        headers.append("来源标记")
    if include_lookup:
        headers.append("贴文类型")
    return headers


def _write_matrix(ws, start_row: int, payload: list[list[Any]], log: LogFn) -> None:
    width = max((len(row) for row in payload), default=1)
    end_row = start_row + max(len(payload), 1) - 1
    try:
        safe_resize_ws(ws, max(end_row + 8, 40), max(width, 1), log=log)
    except RuntimeError as exc:
        raise RuntimeError("目标表单元格超过上限，请换空表或删掉空白列后再汇总。已有内容不会清成 1 格。") from exc
    end_col = index_to_col_letter(max(width, 1) - 1)
    old_end = max(int(getattr(ws, "row_count", end_row) or end_row), end_row)
    try:
        with_retry(
            lambda: ws.batch_clear([f"A{start_row}:{end_col}{old_end}"]),
            log=log,
            what="清空整合表旧数据",
        )
    except Exception as exc:
        log(f"清空旧数据时跳过：{exc}")
    if not payload:
        return
    for offset in range(0, len(payload), WRITE_BATCH):
        chunk = payload[offset : offset + WRITE_BATCH]
        row0 = start_row + offset
        with_retry(
            lambda c=chunk, r=row0: ws.update(
                range_name=f"A{r}",
                values=c,
                value_input_option="USER_ENTERED",
            ),
            log=log,
            what=f"写入第 {row0} 行起 {len(chunk)} 行",
        )
        log(f"  已写入 {min(offset + len(chunk), len(payload))} / {len(payload)} 行")


def _insert_matrix(ws, start_row: int, payload: list[list[Any]], log: LogFn) -> None:
    """Insert new rows at start_row (default 2, under the header). Existing data shifts down."""
    if not payload:
        return
    width = max((len(row) for row in payload), default=1)
    current_rows = max(1, int(getattr(ws, "row_count", 1) or 1))
    current_cols = max(1, int(getattr(ws, "col_count", 1) or 1))
    try:
        safe_resize_ws(
            ws,
            max(current_rows + len(payload), start_row + len(payload) + 1),
            max(current_cols, width),
            log=log,
        )
    except RuntimeError as exc:
        raise RuntimeError("贴文库单元格超过上限，已有数据不会清掉。请换空表或删掉空白列后再插入。") from exc
    sheet_id = ws.id
    for offset in range(0, len(payload), WRITE_BATCH):
        chunk = payload[offset : offset + WRITE_BATCH]
        row0 = start_row + offset

        def _do(chunk=chunk, row0=row0):
            ws.spreadsheet.batch_update(
                {
                    "requests": [
                        {
                            "insertDimension": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "ROWS",
                                    "startIndex": row0 - 1,
                                    "endIndex": row0 - 1 + len(chunk),
                                },
                                "inheritFromBefore": False,
                            }
                        }
                    ]
                }
            )
            ws.update(range_name=f"A{row0}", values=chunk, value_input_option="USER_ENTERED")

        with_retry(_do, log=log, what=f"贴文库从第 {row0} 行插入 {len(chunk)} 行")
        log(f"  贴文库已从第 {start_row} 行插入 {min(offset + len(chunk), len(payload))} / {len(payload)} 行")


def run_post_aggregate(cfg, log: LogFn = print, cancelled=None) -> dict[str, Any]:
    log = _safe_log(log)
    list_url = str(getattr(cfg, "pa_list_url", "") or "").strip()
    if not list_url:
        raise RuntimeError("请填写数据列表表格链接")
    list_sheet = str(getattr(cfg, "pa_list_sheet", "") or "数据列表").strip() or "数据列表"
    link_col = str(getattr(cfg, "pa_link_col", "K") or "K").strip().upper() or "K"
    tag_col = str(getattr(cfg, "pa_tag_col", "L") or "L").strip().upper()
    start_row = max(1, int(getattr(cfg, "pa_start_row", 2) or 2))
    sub_sheet = str(getattr(cfg, "pa_sub_sheet", "") or "订阅").strip() or "订阅"
    source_cols = _parse_col_list(getattr(cfg, "pa_source_cols", None)) or list(DEFAULT_SOURCE_COLS)
    date_col = str(getattr(cfg, "pa_date_col", "M") or "M").strip().upper() or "M"
    date_filter_on = bool(getattr(cfg, "pa_date_filter_enabled", True))
    start_d = _as_day(getattr(cfg, "pa_start_date", "") or "") if date_filter_on else None
    end_d = _as_day(getattr(cfg, "pa_end_date", "") or "") if date_filter_on else None
    include_tag = bool(getattr(cfg, "pa_include_tag", True)) and bool(tag_col)
    lookup_enabled = bool(getattr(cfg, "pa_lookup_enabled", True))
    lookup_url = str(getattr(cfg, "pa_lookup_url", "") or "").strip()
    lookup_sheet = str(getattr(cfg, "pa_lookup_sheet", "") or "当月贴文库").strip() or "当月贴文库"
    lookup_key_col = str(getattr(cfg, "pa_lookup_key_col", "B") or "B").strip().upper() or "B"
    lookup_value_col = str(getattr(cfg, "pa_lookup_value_col", "N") or "N").strip().upper() or "N"
    match_col = str(getattr(cfg, "pa_match_col", "J") or "J").strip().upper() or "J"
    write_library = bool(getattr(cfg, "pa_write_library", True))
    library_write_col = str(getattr(cfg, "pa_library_write_col", "") or "").strip().upper() or lookup_key_col
    target_url = str(getattr(cfg, "pa_target_url", "") or "").strip() or list_url
    output_sheet = str(getattr(cfg, "pa_output_sheet", "") or "整合").strip() or "整合"
    out_start = max(1, int(getattr(cfg, "pa_output_start_row", 2) or 2))
    include_headers = bool(getattr(cfg, "pa_include_headers", False))

    gc = authorize_cfg(cfg, log=log)
    list_ss = open_by_url_or_id(gc, list_url, log=log)
    list_ws = pick_source_ws(list_ss, list_sheet)
    log(f"读取数据列表「{list_ss.title}」/「{list_ws.title}」")
    list_rows = read_sheet_values(list_ws, log=log)
    hyperlinks: list[str] = []
    try:
        last = max(start_row + max(0, len(list_rows) - start_row), start_row)
        hyperlinks = _read_link_column(
            _session_from_gc(gc),
            list_ss.id,
            list_ws.title,
            link_col,
            start_row,
            last,
            log=log,
        )
    except Exception as exc:
        log(f"读取链接列超链接失败，改用单元格文字：{exc}")
    entries = collect_list_entries(list_rows, link_col, tag_col, start_row, hyperlinks)
    if not entries:
        raise RuntimeError(f"数据列表「{list_sheet}」的 {link_col} 列没有读到表格链接")
    log(f"数据列表读到 {len(entries)} 个订阅表链接")
    if date_filter_on and (start_d or end_d):
        log(f"日期筛选：{start_d or '…'} ~ {end_d or '…'}（订阅表 {date_col} 列）")

    lookup: dict[str, str] = {}
    lookup_ws = None
    lookup_ss = None
    existing_library_keys: set[str] = set()
    library_next_row = 2
    if lookup_enabled or write_library:
        if not lookup_url:
            raise RuntimeError("请填写贴文库表格链接（对照或追加新链接都需要）")
        lookup_ss = open_by_url_or_id(gc, lookup_url, log=log)
        lookup_ws = pick_source_ws(lookup_ss, lookup_sheet)
        log(f"读取贴文库「{lookup_ss.title}」/「{lookup_ws.title}」")
        lookup_rows = read_sheet_values(lookup_ws, log=log)
        existing_library_keys, library_next_row = library_state(lookup_rows, lookup_key_col)
        if lookup_enabled:
            lookup = build_lookup_map(lookup_rows, lookup_key_col, lookup_value_col)
            log(f"贴文库对照 {len(lookup)} 条（{lookup_key_col} 列）")
        if write_library:
            log(f"贴文库 {lookup_key_col} 列已有 {len(existing_library_keys)} 条，新链接从第 2 行插入，原有数据下移")

    aggregated: list[list[str]] = []
    library_candidates: list[str] = []
    skipped_date = 0
    ok_n = 0
    fail_n = 0
    sources: list[dict[str, Any]] = []
    session = _session_from_gc(gc)
    for number, item in enumerate(entries, 1):
        if cancelled and cancelled():
            raise RuntimeError("已停止")
        log(f"[{number}/{len(entries)}] 读取订阅表 {item['url']}")
        rec = {"url": item["url"], "tag": item["tag"], "rows": 0, "error": None}
        try:
            source_ss = open_by_url_or_id(gc, item["url"], log=log)
            source_ws = pick_source_ws(source_ss, sub_sheet)
            values = with_retry(
                lambda ws=source_ws: ws.get_all_values(),
                log=log,
                what=f"读取「{source_ws.title}」",
            ) or []
            chunk, n_date = rows_from_subscription(
                values,
                source_cols,
                date_col,
                start_d,
                end_d,
                item["tag"],
                include_tag,
                match_col,
                lookup,
                lookup_enabled,
            )
            skipped_date += n_date
            rec["rows"] = len(chunk)
            aggregated.extend(chunk)
            if write_library:
                j_links: list[str] = []
                try:
                    last_sub = max(2, len(values))
                    j_links = _read_link_column(
                        session,
                        source_ss.id,
                        source_ws.title,
                        match_col,
                        2,
                        last_sub,
                        log=log,
                    )
                except Exception as exc:
                    log(f"读取订阅表 {match_col} 列超链接失败，改用单元格文字：{exc}")
                library_candidates.extend(
                    collect_match_values(
                        values,
                        match_col,
                        date_col,
                        start_d,
                        end_d,
                        j_links,
                    )
                )
            ok_n += 1
            log(
                f"[{number}/{len(entries)}] 「{source_ss.title}」/「{source_ws.title}」："
                f"新增 {len(chunk)} 行，本轮累计 {len(aggregated)} 行"
                + (f"（日期外 {n_date}）" if n_date else "")
            )
        except Exception as exc:
            fail_n += 1
            rec["error"] = str(exc)
            log(f"[{number}/{len(entries)}] 无法读取订阅表，已跳过：{exc}")
        sources.append(rec)

    headers = output_headers(source_cols, include_tag, lookup_enabled)
    payload = ([headers] + aggregated) if include_headers else aggregated
    target_ss = open_by_url_or_id(gc, target_url, log=log)
    try:
        out_ws = target_ss.worksheet(output_sheet)
    except Exception:
        out_ws = target_ss.add_worksheet(title=output_sheet, rows=200, cols=max(len(headers), 8))
        log(f"已新建目标工作表「{output_sheet}」")
    log(f"写入「{target_ss.title}」/「{out_ws.title}」，从第 {out_start} 行起")
    _write_matrix(out_ws, out_start, payload, log)
    library_added = 0
    library_skipped = 0
    if write_library and lookup_ws is not None:
        new_vals, library_skipped = pick_new_library_values(library_candidates, existing_library_keys)
        library_added = len(new_vals)
        log(
            f"贴文库排重：订阅表 {match_col} 列对照 {lookup_key_col} 列，"
            f"已有跳过 {library_skipped}，待插入 {library_added}"
        )
        if new_vals:
            append_rows = build_library_append_rows(new_vals, library_write_col)
            library_start = 2
            log(
                f"向「{getattr(lookup_ss, 'title', '')}」/「{lookup_ws.title}」"
                f"第 {library_start} 行插入 {library_added} 条到 {library_write_col} 列（原有数据下移）"
            )
            _insert_matrix(lookup_ws, library_start, append_rows, log)
        else:
            log("贴文库没有新链接需要插入")
    log("======== 贴文汇总已结束 ========")
    log(f"整合表写入：{len(aggregated)} 行 × {len(headers)} 列")
    log(f"贴文库从第 2 行插入：{library_added} 行（已有跳过 {library_skipped}）")
    log(f"订阅表：成功 {ok_n} 个 · 失败 {fail_n} 个 · 日期外 {skipped_date} 行")
    log(f"整合表：「{target_ss.title}」/「{output_sheet}」")
    if write_library and lookup_ws is not None:
        log(f"贴文库：「{getattr(lookup_ss, 'title', '')}」/「{lookup_ws.title}」")
    log(
        f"贴文汇总已结束：整合表 {len(aggregated)} 行，贴文库新增 {library_added} 行，成功 {ok_n} 个订阅表。"
    )
    return {
        "ok": True,
        "mode": "posts",
        "total_rows": len(aggregated),
        "sheet_total": len(payload),
        "library_added": library_added,
        "library_skipped": library_skipped,
        "date_skipped": skipped_date,
        "ok_sheets": ok_n,
        "failed_sheets": fail_n,
        "sources": sources,
        "target_url": spreadsheet_url(getattr(target_ss, "id", "") or ""),
        "sheet": output_sheet,
    }
