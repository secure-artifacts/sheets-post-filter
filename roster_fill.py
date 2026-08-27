# -*- coding: utf-8 -*-
"""队别专页对照：按专页编码一列，同名排在一起，引流表按「日期+24小时」写入。"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Callable

from fetch_posts import (
    authorize_cfg,
    index_to_col_letter,
    open_by_url_or_id,
    pick_source_ws,
    safe_resize_ws,
    spreadsheet_url,
    to_datetime,
    with_retry,
)
from video_duration import (
    PERSON_BANDS,
    _date_key,
    _read_link_column,
    _rgb,
    _session_from_gc,
    extract_url,
)

LogFn = Callable[[str], None]

DEFAULT_COLUMNS = [
    {"field": "队别", "role": "team", "column": "A"},
    {"field": "类型", "role": "type", "column": "B"},
    {"field": "名字", "role": "name", "column": "C"},
    {"field": "专页名字", "role": "page_name", "column": "G"},
    {"field": "专页编码", "role": "page_code", "column": "H"},
    {"field": "专页链接", "role": "page_link", "column": "I"},
    {"field": "chat", "role": "chat", "column": "K"},
    {"field": "是否上表", "role": "on_sheet", "column": "Q"},
    {"field": "数据表格", "role": "data_url", "column": "R"},
]

_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")
_SLOT_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*[-–~至到]\s*(\d{1,2}):(\d{2})$")
HOUR_SLOTS = [f"{hour:02d}:00-{(hour + 1) % 24:02d}:00" for hour in range(24)]


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _col_index(letter: str) -> int:
    text = str(letter or "").strip().upper()
    if not text or not text.isalpha():
        raise RuntimeError("队别专页的列必须填字母，例如 A、B、R")
    result = 0
    for char in text:
        result = result * 26 + ord(char) - 64
    return result - 1


def _cell(row: list[Any], index: int) -> str:
    return _norm(row[index] if index < len(row) else "")


def _is_checked(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None or value == "":
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) != 0
    text = _norm(value).casefold()
    return text in {"true", "1", "yes", "y", "是", "勾选", "checked", "on", "✓", "✔", "☑", "x"}


def _slot_key(value: Any) -> str:
    text = _norm(value).replace("：", ":")
    matched = _SLOT_RE.match(text)
    if not matched:
        return ""
    return f"{int(matched.group(1)):02d}:{matched.group(2)}-{int(matched.group(3)):02d}:{matched.group(4)}"


def _sheet_title(team: str, used: set[str]) -> str:
    name = _SHEET_BAD.sub(" ", team or "未分组").strip() or "未分组"
    name = name[:90]
    base = name
    n = 2
    while name in used:
        name = f"{base}-{n}"[:90]
        n += 1
    used.add(name)
    return name


def _columns_from_cfg(cfg) -> list[dict[str, str]]:
    raw = [item for item in (getattr(cfg, "roster_columns", None) or []) if isinstance(item, dict)]
    if not raw:
        raw = DEFAULT_COLUMNS
    out: list[dict[str, str]] = []
    seen_roles: set[str] = set()
    for item in raw:
        role = str(item.get("role") or "").strip().lower()
        column = str(item.get("column") or "").strip().upper()
        if not role or not column:
            continue
        out.append(
            {
                "field": str(item.get("field") or role).strip(),
                "role": role,
                "column": column,
            }
        )
        seen_roles.add(role)
    for item in DEFAULT_COLUMNS:
        if item["role"] not in seen_roles:
            out.append(dict(item))
    return out


def _role_map(columns: list[dict[str, str]]) -> dict[str, int]:
    return {item["role"]: _col_index(item["column"]) for item in columns}


def _find_traffic_ws(spreadsheet, wanted: str):
    import gspread

    wanted_n = _norm(wanted).casefold() or "引流"
    worksheets = spreadsheet.worksheets()
    for ws in worksheets:
        if _norm(ws.title).casefold() == wanted_n:
            return ws
    for ws in worksheets:
        if wanted_n in _norm(ws.title).casefold():
            return ws
    titles = "、".join(f"「{ws.title}」" for ws in worksheets[:8]) or "（没有工作表）"
    raise gspread.exceptions.WorksheetNotFound(f"找不到引流工作表「{wanted}」。该文件中有：{titles}")


def _data_start(rows: list[list[Any]]) -> int:
    for index, row in enumerate(rows):
        raw = row[0] if row else ""
        if _date_key(raw) or _slot_key(raw):
            return index
    return 0


def _find_code_column(rows: list[list[Any]], code: str) -> int | None:
    want = _norm(code).casefold()
    if not want:
        return None
    start = _data_start(rows)
    header = rows[: max(start, 23)]
    for row in header:
        for index, cell in enumerate(row):
            if index == 0:
                continue
            if _norm(cell).casefold() == want:
                return index
    width = max((len(row) for row in rows[:50]), default=1)
    if width == 2:
        return 1
    return None


def parse_traffic_column(rows: list[list[Any]], col: int) -> tuple[list[str], dict[tuple[str, str], Any]]:
    """Return dates newest-first order as seen, and values keyed by (date, slot). Empty slot = day total."""
    values: dict[tuple[str, str], Any] = {}
    dates: list[str] = []
    seen: set[str] = set()
    current = ""
    start = _data_start(rows)
    for row in rows[start:]:
        raw = row[0] if row else ""
        day = _date_key(raw)
        if day:
            current = day
            if day not in seen:
                seen.add(day)
                dates.append(day)
            values[(day, "")] = row[col] if col < len(row) else ""
            continue
        slot = _slot_key(raw)
        if slot and current:
            values[(current, slot)] = row[col] if col < len(row) else ""
    return dates, values


def sort_dates_desc(dates: list[str]) -> list[str]:
    def key(day: str):
        parsed = to_datetime(day)
        return parsed or day

    unique: list[str] = []
    seen: set[str] = set()
    for day in dates:
        if day and day not in seen:
            seen.add(day)
            unique.append(day)
    unique.sort(key=key, reverse=True)
    return unique


def build_axis(dates: list[str]) -> list[tuple[str, str]]:
    """('date', iso) then 24 hour slots, repeating. Dates newest first."""
    axis: list[tuple[str, str]] = []
    for day in sort_dates_desc(dates):
        axis.append(("date", day))
        for slot in HOUR_SLOTS:
            axis.append(("slot", slot))
    return axis


def build_roster_payload(pages: list[dict[str, Any]], axis: list[tuple[str, str]], date_start: int) -> list[list[Any]]:
    """One column per 专页编码. Same name stays adjacent. A24+ follows 引流: date then 24 hours."""
    width = 1 + len(pages)
    body_rows = len(axis)
    rows = [[""] * width for _ in range(max(date_start - 1, 0) + body_rows)]
    for index, page in enumerate(pages):
        col = index + 1
        rows[0][col] = page.get("chat") or ""
        rows[1][col] = page.get("data_url") or ""
        rows[2][col] = page.get("page_link") or ""
        rows[4][col] = page.get("page_code") or ""
        rows[5][col] = page.get("name") or ""
        rows[7][col] = page.get("type") or ""
    current_date = ""
    for offset, (kind, token) in enumerate(axis):
        row_i = date_start - 1 + offset
        while row_i >= len(rows):
            rows.append([""] * width)
        if kind == "date":
            current_date = token
            rows[row_i][0] = token
            lookup = (token, "")
        else:
            rows[row_i][0] = token
            lookup = (current_date, token)
        for index, page in enumerate(pages):
            col = index + 1
            if col >= len(rows[row_i]):
                rows[row_i].extend([""] * (col + 1 - len(rows[row_i])))
            rows[row_i][col] = (page.get("values") or {}).get(lookup, "")
    return rows


def _format_roster_sheet(ws, pages: list[dict[str, Any]], date_start: int, payload_len: int, log: LogFn) -> None:
    if not pages:
        return
    sheet_id = ws.id
    last_row = max(payload_len, date_start)
    name_order: list[str] = []
    for page in pages:
        name = _norm(page.get("name"))
        if name not in name_order:
            name_order.append(name)
    requests: list[dict[str, Any]] = [
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 108},
                "fields": "pixelSize",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": min(8, last_row), "frozenColumnCount": 1},
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        },
    ]
    for index, page in enumerate(pages):
        col = index + 1
        name_i = name_order.index(_norm(page.get("name"))) if _norm(page.get("name")) in name_order else index
        strong, pale = PERSON_BANDS[name_i % len(PERSON_BANDS)]
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                    "properties": {"pixelSize": 64},
                    "fields": "pixelSize",
                }
            }
        )
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 8,
                        "startColumnIndex": col,
                        "endColumnIndex": col + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _rgb(strong),
                            "horizontalAlignment": "CENTER",
                            "wrapStrategy": "WRAP",
                            "textFormat": {"bold": True, "fontSize": 9, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                        }
                    },
                    "fields": "userEnteredFormat",
                }
            }
        )
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": date_start - 1,
                        "endRowIndex": last_row,
                        "startColumnIndex": col,
                        "endColumnIndex": col + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _rgb(pale),
                            "horizontalAlignment": "CENTER",
                            "textFormat": {"fontSize": 9},
                        }
                    },
                    "fields": "userEnteredFormat",
                }
            }
        )
    for offset in range(0, len(requests), 40):
        part = requests[offset : offset + 40]
        with_retry(lambda p=part: ws.spreadsheet.batch_update({"requests": p}), log=log, what="设置队别专页分色")


def run_roster_fill(cfg, log: LogFn = print, cancelled=None) -> dict[str, Any]:
    config_url = str(getattr(cfg, "roster_config_url", "") or "").strip()
    target_url = str(getattr(cfg, "roster_target_url", "") or "").strip()
    if not config_url:
        raise RuntimeError("请填写配置表链接")
    if not target_url:
        raise RuntimeError("请填写写入目标表链接")
    start_row = max(1, int(getattr(cfg, "roster_start_row", 2) or 2))
    date_start = max(2, int(getattr(cfg, "roster_date_start_row", 24) or 24))
    traffic_name = str(getattr(cfg, "roster_traffic_sheet", "引流") or "引流").strip() or "引流"
    columns = _columns_from_cfg(cfg)
    roles = _role_map(columns)
    for required in ("team", "name", "page_code", "data_url"):
        if required not in roles:
            raise RuntimeError("字段映射必须包含队别、名字、专页编码、数据表格")

    gc = authorize_cfg(cfg, log=log)
    session = _session_from_gc(gc)
    config_ss = open_by_url_or_id(gc, config_url, log=log)
    config_ws = pick_source_ws(config_ss, getattr(cfg, "roster_config_sheet", ""))
    log(f"读取配置表「{config_ss.title}」/「{config_ws.title}」")
    values = with_retry(
        lambda: config_ws.get_all_values(value_render_option="UNFORMATTED_VALUE") or [],
        log=log,
        what="读取配置表",
    ) or []
    last = max(start_row + max(0, len(values) - start_row), start_row)

    def _hyper(role: str) -> list[str]:
        if role not in roles:
            return []
        letter = index_to_col_letter(roles[role])
        try:
            return _read_link_column(session, config_ss.id, config_ws.title, letter, start_row, last, log=log)
        except Exception as exc:
            log(f"读取 {letter} 列超链接失败，改用单元格文字：{exc}")
            return []

    data_hypers = _hyper("data_url")
    link_hypers = _hyper("page_link")
    require_check = "on_sheet" in roles

    pages: list[dict[str, Any]] = []
    skipped_check = 0
    for offset, row in enumerate(values[start_row - 1 :]):
        if require_check and not _is_checked(row[roles["on_sheet"]] if roles["on_sheet"] < len(row) else ""):
            skipped_check += 1
            continue
        name = _cell(row, roles["name"])
        team = _cell(row, roles.get("team", 0))
        page_code = _cell(row, roles["page_code"])
        raw_url = _cell(row, roles["data_url"])
        hyper = data_hypers[offset] if offset < len(data_hypers) else ""
        data_url = extract_url(hyper) or extract_url(raw_url) or (hyper or raw_url)
        if not page_code and not name:
            continue
        if not page_code:
            log(f"第 {start_row + offset} 行没有专页编码，已跳过")
            continue
        page_link_raw = _cell(row, roles["page_link"]) if "page_link" in roles else ""
        page_hyper = link_hypers[offset] if offset < len(link_hypers) else ""
        pages.append(
            {
                "team": team or "未分组",
                "type": _cell(row, roles["type"]) if "type" in roles else "",
                "name": name,
                "page_name": _cell(row, roles["page_name"]) if "page_name" in roles else "",
                "page_code": page_code,
                "page_link": extract_url(page_hyper) or extract_url(page_link_raw) or page_hyper or page_link_raw,
                "chat": _cell(row, roles["chat"]) if "chat" in roles else "",
                "data_url": data_url,
                "values": {},
            }
        )
    if skipped_check:
        log(f"Q 列未打勾 / 不为 true 的 {skipped_check} 行已跳过")
    if not pages:
        raise RuntimeError("配置表没有可汇总的专页（需要打勾上表，且有专页编码）")
    log(f"配置表有效专页 {len(pages)} 个（按专页编码一列）")

    cache: dict[str, list[list[Any]]] = {}

    def load_traffic(url: str) -> list[list[Any]]:
        key = (url or "").strip()
        if not key:
            return []
        if key in cache:
            return cache[key]
        if cancelled and cancelled():
            raise RuntimeError("已停止")
        ss = open_by_url_or_id(gc, key, log=log)
        ws = _find_traffic_ws(ss, traffic_name)
        rows = with_retry(
            lambda: ws.get_all_values(value_render_option="UNFORMATTED_VALUE") or [],
            log=log,
            what=f"读取引流「{ss.title}」/{ws.title}",
        ) or []
        cache[key] = rows
        log(f"已读「{ss.title}」/「{ws.title}」{len(rows)} 行")
        return rows

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    miss_code = 0
    miss_sheet = 0
    for page in pages:
        try:
            rows = load_traffic(page["data_url"]) if page["data_url"] else []
        except Exception as exc:
            miss_sheet += 1
            log(f"专页「{page['page_code']}」打开数据表失败，已跳过：{exc}")
            rows = []
        if rows:
            col = _find_code_column(rows, page["page_code"])
            if col is None:
                miss_code += 1
                log(f"专页编码「{page['page_code']}」在引流表表头找不到对应列，该列先留空")
            else:
                dates, values = parse_traffic_column(rows, col)
                page["values"] = values
                page["_dates"] = dates
        grouped[page["team"]].append(page)

    target = open_by_url_or_id(gc, target_url, log=log)
    used_titles: set[str] = set()
    sheets_written = 0
    for team in sorted(grouped, key=lambda value: value.casefold()):
        members = grouped[team]
        members.sort(key=lambda item: (_norm(item.get("name")).casefold(), _norm(item.get("page_code"))))
        unique: list[dict[str, Any]] = []
        seen_codes: set[str] = set()
        for item in members:
            code = _norm(item.get("page_code"))
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            unique.append(item)
        all_dates: list[str] = []
        for item in unique:
            all_dates.extend(item.get("_dates") or [])
        axis = build_axis(all_dates)
        payload = build_roster_payload(unique, axis, date_start)
        title = _sheet_title(team, used_titles)
        import gspread

        try:
            ws = target.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            try:
                ws = target.add_worksheet(
                    title=title,
                    rows=max(len(payload) + 4, date_start + 4),
                    cols=max(len(unique) + 2, 2),
                )
            except Exception as exc:
                if "10000000" in str(exc) or "number of cells" in str(exc).lower():
                    raise RuntimeError(
                        "目标表格超过 1000 万单元格上限，无法按队别新建工作表。请换空表或删掉空白列后再跑。"
                    ) from exc
                raise
        width = max((len(row) for row in payload), default=1)
        try:
            safe_resize_ws(ws, max(len(payload) + 2, date_start + 2), max(width, 1), log=log)
        except RuntimeError:
            try:
                ws.resize(rows=1, cols=1)
                safe_resize_ws(ws, max(len(payload) + 2, date_start + 2), max(width, 1), log=log)
            except Exception as inner:
                raise RuntimeError("目标表单元格超过上限，请换空表或删掉空白列。") from inner
        end_col = index_to_col_letter(width - 1)
        for offset in range(0, len(payload), 1000):
            chunk = payload[offset : offset + 1000]
            start = 1 + offset
            with_retry(
                lambda s=start, values=chunk: ws.update(
                    range_name=f"A{s}:{end_col}{s + len(values) - 1}",
                    values=values,
                    value_input_option="USER_ENTERED",
                ),
                log=log,
                what=f"写入「{title}」第 {start} 行",
            )
        try:
            _format_roster_sheet(ws, unique, date_start, len(payload), log)
        except Exception as exc:
            log(f"「{title}」分色失败，数据已写入：{exc}")
        sheets_written += 1
        log(f"已写入队别「{team}」→ 工作表「{title}」：{len(unique)} 个专页，{len(sort_dates_desc(all_dates))} 个日期")

    log(
        f"完成：按队别写了 {sheets_written} 个工作表，{len(pages)} 个专页编码。"
        f"找不到编码列 {miss_code}，打不开数据表 {miss_sheet}"
    )
    return {
        "ok": True,
        "mode": "roster",
        "people": len(pages),
        "sheets": sheets_written,
        "target_url": spreadsheet_url(target.id),
        "miss_code": miss_code,
        "miss_sheet": miss_sheet,
    }
