# -*- coding: utf-8 -*-
"""目录表驱动汇总：按索引表中的链接和工作表名称合并全部内容。"""

from __future__ import annotations

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
    return str(row[index] if index < len(row) else "").strip()


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
    entries: list[tuple[str, str]] = []
    for row in index_rows[start_row - 1 :]:
        url = _cell(row, url_col)
        sheet_name = _cell(row, sheet_col)
        if url and sheet_name:
            entries.append((url, sheet_name))
    if not entries:
        raise RuntimeError("目录表中没有找到同时包含链接和工作表名称的数据行")
    log(f"目录表读到 {len(entries)} 项，开始逐项汇总")

    merged: list[list[Any]] = []
    sources: list[dict[str, Any]] = []
    header_written = False
    for number, (url, sheet_name) in enumerate(entries, 1):
        item = {"url": url, "sheet": sheet_name, "rows": 0, "error": None}
        try:
            source_ss = open_by_url_or_id(gc, url, log=log)
            source_ws = pick_source_ws(source_ss, sheet_name)
            values = read_sheet_values(source_ws, log=log)
            if values and header_written and not keep_each_header:
                values = values[1:]
            if values:
                header_written = True
            merged.extend(values)
            item["title"] = source_ss.title
            item["rows"] = len(values)
            log(f"[{number}/{len(entries)}] 「{source_ss.title}」/「{sheet_name}」：{len(values)} 行")
        except Exception as exc:
            item["error"] = str(exc)
            log(f"[{number}/{len(entries)}] 「{sheet_name}」失败，已跳过：{exc}")
        sources.append(item)

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
