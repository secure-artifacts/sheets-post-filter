# -*- coding: utf-8 -*-
"""从源表视频链接提取时长，写入日志表 + 按制作人汇总的数据表。"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from fetch_posts import (
    authorize,
    col_letter_to_index,
    index_to_col_letter,
    open_by_url_or_id,
    to_datetime,
    with_retry,
)

LogFn = Callable[[str], None]

DRIVE_ID_RE = re.compile(
    r"(?:drive\.google\.com|docs\.google\.com).*(?:/file/d/|[?&]id=|/open\?id=)([a-zA-Z0-9_-]{20,})",
    re.I,
)
DRIVE_ID_BARE_RE = re.compile(r"(?:/file/d/|[?&]id=|/open\?id=)([a-zA-Z0-9_-]{20,})", re.I)
YT_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/|youtube\.com/embed/)([a-zA-Z0-9_-]{6,})",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s\"'<>)）,，]+", re.I)
HYPERLINK_RE = re.compile(
    r"""(?:HYPERLINK|IMAGE)\s*\(\s*["'“”‘’]([^"'“”‘’]+)["'“”‘’]""",
    re.I,
)
ISO_DUR_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$", re.I)


def extract_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    m = HYPERLINK_RE.search(text)
    if m:
        return m.group(1).strip()
    if text.lower().startswith("http"):
        return text.split()[0].rstrip(").,，。]")
    m = URL_RE.search(text)
    return m.group(0).rstrip(").,，。]") if m else ""


def parse_duration_seconds(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = float(value)
        if n > 100000:
            return n / 1000.0
        return n if n >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    m = ISO_DUR_RE.match(text)
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = float(m.group(3) or 0)
        return h * 3600 + mi * 60 + s
    if re.match(r"^\d+(\.\d+)?$", text):
        return float(text)
    m = re.match(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d+))?$", text)
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2))
        s = int(m.group(3))
        return h * 3600 + mi * 60 + s
    return None


def format_seconds(sec: float | None) -> str:
    if sec is None:
        return ""
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def link_key(url: str) -> str:
    fid = drive_file_id(url)
    if fid:
        return f"d:{fid}"
    yid = youtube_id(url)
    if yid:
        return f"y:{yid}"
    text = (url or "").strip()
    if not text:
        return ""
    return "u:" + text.split("?")[0].rstrip("/").lower()


def _as_date(value: Any):
    dt = to_datetime(value)
    return dt.date() if dt else None


def _norm_type(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _cfg_types(cfg) -> list[str]:
    raw = getattr(cfg, "vd_types", None) or []
    if isinstance(raw, str):
        parts = raw.replace("，", "\n").replace(",", "\n").replace(";", "\n").splitlines()
        return [_norm_type(x) for x in parts if _norm_type(x)]
    out = []
    seen: set[str] = set()
    for x in raw:
        t = _norm_type(x)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _in_date_range(value: Any, start_d, end_d) -> bool:
    if start_d is None and end_d is None:
        return True
    d = _as_date(value)
    if d is None:
        return False
    if start_d and d < start_d:
        return False
    if end_d and d > end_d:
        return False
    return True


def _per_video_count(seconds: float) -> int:
    """按单条原视频计数；超过 60 秒后仅完整增加 30 秒才加 1 个。"""
    value = max(0.0, float(seconds))
    if value < 35:
        return 1
    if value <= 60:
        return 2
    return 2 + int((value - 60) // 30)


def drive_file_id(url: str) -> str:
    text = url or ""
    m = DRIVE_ID_RE.search(text) or DRIVE_ID_BARE_RE.search(text)
    return m.group(1) if m else ""


def youtube_id(url: str) -> str:
    m = YT_RE.search(url or "")
    if m:
        return m.group(1)
    q = parse_qs(urlparse(url or "").query)
    return (q.get("v") or [""])[0]


def _http_get(url: str, timeout: int = 20, headers: dict | None = None) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 sheets-post-filter-duration",
            **(headers or {}),
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def duration_from_drive(session, file_id: str) -> float | None:
    if session is None or not file_id:
        return None

    def _do():
        r = session.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={
                "fields": "id,mimeType,videoMediaMetadata,shortcutDetails",
                "supportsAllDrives": "true",
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Drive {r.status_code}: {r.text[:200]}")
        return r.json()

    try:
        data = with_retry(_do, what="读取 Drive 视频元数据")
    except Exception:
        return None
    meta = data.get("videoMediaMetadata") or {}
    ms = meta.get("durationMillis")
    if ms not in (None, ""):
        try:
            return float(ms) / 1000.0
        except (TypeError, ValueError):
            pass
    shortcut = (data.get("shortcutDetails") or {}).get("targetId") or ""
    if shortcut and shortcut != file_id:
        return duration_from_drive(session, shortcut)
    return None


def _parse_html_duration(html: str) -> float | None:
    if not html:
        return None
    for pat, scale in (
        (r'"lengthSeconds"\s*:\s*"(\d+)"', 1.0),
        (r'"approxDurationMs"\s*:\s*"(\d+)"', 0.001),
        (r'"durationMillis"\s*[:=]\s*"?(\d+)"?', 0.001),
        (r'"playable_duration_time"\s*:\s*"?(\d+(?:\.\d+)?)"?', 1.0),
        (r'itemprop="duration"\s+content="([^"]+)"', 0),
        (r'property="og:duration"\s+content="([^"]+)"', 1.0),
        (r'property="og:video:duration"\s+content="([^"]+)"', 1.0),
        (r'"duration"\s*:\s*"?(PT[^",}]+)"?', 0),
        (r'"duration"\s*:\s*"?(\d+(?:\.\d+)?)"?', 1.0),
    ):
        m = re.search(pat, html, re.I)
        if not m:
            continue
        raw = m.group(1)
        if scale == 0:
            sec = parse_duration_seconds(raw)
        else:
            try:
                sec = float(raw) * scale
            except (TypeError, ValueError):
                sec = parse_duration_seconds(raw)
        if sec and 0 < sec < 12 * 3600:
            return sec
    return None


def duration_from_drive_public(file_id: str) -> float | None:
    if not file_id:
        return None
    for url in (
        f"https://drive.google.com/file/d/{file_id}/view",
        f"https://drive.google.com/file/d/{file_id}/preview",
    ):
        try:
            html = _http_get(url, timeout=20)
        except (URLError, OSError, TimeoutError):
            continue
        sec = _parse_html_duration(html)
        if sec:
            return sec
    return None


def duration_from_youtube(url: str) -> float | None:
    vid = youtube_id(url)
    if not vid:
        return None
    watch = f"https://www.youtube.com/watch?v={vid}"
    try:
        html = _http_get(watch, timeout=20)
    except (URLError, OSError, TimeoutError):
        return None
    return _parse_html_duration(html)


def duration_from_page(url: str) -> float | None:
    try:
        html = _http_get(url, timeout=20)
    except (URLError, OSError, TimeoutError):
        return None
    return _parse_html_duration(html)


def _session_from_gc(gc):
    hc = getattr(gc, "http_client", None)
    if hc is not None:
        sess = getattr(hc, "session", None)
        if sess is not None:
            return sess
        auth = getattr(hc, "auth", None)
        if auth is not None:
            import google.auth.transport.requests

            return google.auth.transport.requests.AuthorizedSession(auth)
    auth = getattr(gc, "auth", None)
    if auth is not None:
        import google.auth.transport.requests

        return google.auth.transport.requests.AuthorizedSession(auth)
    return None


def duration_for_link(
    session,
    url: str,
    cache: dict[str, float | None],
    drive_cache: dict[str, float | None] | None = None,
) -> float | None:
    url = (url or "").strip()
    if not url:
        return None
    if url in cache:
        return cache[url]
    drive_cache = drive_cache if drive_cache is not None else {}
    sec = None
    fid = drive_file_id(url)
    if fid:
        if fid in drive_cache:
            sec = drive_cache[fid]
        else:
            sec = duration_from_drive(session, fid)
            drive_cache[fid] = sec
    if sec is None and youtube_id(url):
        sec = duration_from_youtube(url)
        time.sleep(0.05)
    cache[url] = sec
    return sec


def _prefetch_drive_durations(session, file_ids: list[str], log: LogFn, workers: int = 6) -> dict[str, float | None]:
    """并行读取 Drive videoMediaMetadata，按文件 id 缓存。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    uniq: list[str] = []
    seen: set[str] = set()
    for fid in file_ids:
        if fid and fid not in seen:
            seen.add(fid)
            uniq.append(fid)
    out: dict[str, float | None] = {}
    if not uniq or session is None:
        return out
    log(f"开始查询 {len(uniq)} 个 Drive 视频时长…")
    done = 0
    ok = 0

    def _one(fid: str):
        return fid, duration_from_drive(session, fid)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(_one, fid) for fid in uniq]
        for fut in as_completed(futs):
            fid, sec = fut.result()
            out[fid] = sec
            done += 1
            if sec:
                ok += 1
            if done % 100 == 0 or done == len(uniq):
                log(f"  Drive 时长 {done}/{len(uniq)}，成功 {ok}")
    return out


def _cell(row: list, letter: str) -> Any:
    i = col_letter_to_index(letter)
    if i < 0 or i >= len(row):
        return ""
    return row[i]


def _ensure_ws(ss, name: str, cols: int, rows: int, log: LogFn):
    import gspread

    try:
        return ss.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=name, rows=max(rows, 100), cols=max(cols, 8))
        log(f"已新建工作表「{name}」")
        return ws


def _url_from_cell(cell: dict) -> str:
    hyper = str(cell.get("hyperlink") or "").strip()
    if hyper:
        return extract_url(hyper) or hyper
    for run in cell.get("textFormatRuns") or []:
        uri = str(((run.get("format") or {}).get("link") or {}).get("uri") or "").strip()
        if uri:
            return extract_url(uri) or uri
    for run in cell.get("chipRuns") or []:
        chip = run.get("chip") or {}
        uri = str((chip.get("richLinkProperties") or {}).get("uri") or "").strip()
        if uri:
            return extract_url(uri) or uri
    ue = cell.get("userEnteredValue") or {}
    return (
        extract_url(ue.get("formulaValue") or "")
        or extract_url(ue.get("stringValue") or "")
        or extract_url(cell.get("formattedValue") or "")
    )


def _read_link_column(
    session,
    spreadsheet_id: str,
    sheet_title: str,
    col: str,
    start: int,
    end: int,
    log: LogFn | None = None,
) -> list[str]:
    """读一列真实超链接（单元格常常只显示文件名）。"""
    n = max(0, end - start + 1)
    out = [""] * n
    if session is None or n == 0:
        if log:
            log("无法建立授权会话，B 列超链接读不到")
        return out
    safe = sheet_title.replace("'", "''")
    chunk = 6000
    got = 0
    for cs in range(start, end + 1, chunk):
        ce = min(end, cs + chunk - 1)
        rng = f"'{safe}'!{col}{cs}:{col}{ce}"

        def _do(rng=rng):
            r = session.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
                params={
                    "ranges": rng,
                    "includeGridData": "true",
                    "fields": (
                        "sheets.data.rowData.values.hyperlink,"
                        "sheets.data.rowData.values.formattedValue,"
                        "sheets.data.rowData.values.userEnteredValue,"
                        "sheets.data.rowData.values.textFormatRuns,"
                        "sheets.data.rowData.values.chipRuns"
                    ),
                },
                timeout=120,
            )
            if r.status_code != 200:
                raise RuntimeError(f"Sheets {r.status_code}: {r.text[:200]}")
            return r.json()

        try:
            data = with_retry(_do, what="读取视频超链接", log=log or print)
        except Exception as e:
            if log:
                log(f"读取 {col}{cs}:{col}{ce} 超链接失败：{e}")
            continue
        rows = (((data.get("sheets") or [{}])[0].get("data") or [{}])[0].get("rowData")) or []
        for i, row in enumerate(rows):
            idx = cs - start + i
            if idx >= n:
                break
            vals = row.get("values") or []
            url = _url_from_cell(vals[0] if vals else {})
            out[idx] = url
            if url:
                got += 1
        if log:
            log(f"  已读超链接 {col}{start}:{col}{ce}，取到 {got} 条")
        if ce < end:
            time.sleep(1.2)
    return out


def _write_block(ws, start_row: int, headers: list[str], rows: list[list[Any]], include_headers: bool, log: LogFn):
    payload = ([headers] if include_headers else []) + rows
    end_col = index_to_col_letter(max(len(headers) - 1, 0))
    last = max(ws.row_count, start_row)
    try:
        ws.batch_clear([f"A{start_row}:{end_col}{last}"])
    except Exception:
        pass
    need_rows = start_row - 1 + max(len(payload), 1) + 10
    need_cols = max(len(headers), 8)
    if ws.row_count < need_rows or ws.col_count < need_cols:
        ws.resize(rows=max(ws.row_count, need_rows), cols=max(ws.col_count, need_cols))
    chunk = 4000
    row = start_row
    for i in range(0, len(payload), chunk):
        part = payload[i : i + chunk]

        def _upd(part=part, row=row):
            ws.update(
                range_name=f"A{row}",
                values=part,
                value_input_option="USER_ENTERED",
            )

        with_retry(_upd, log=log, what=f"写入{ws.title}")
        row += len(part)
        if len(payload) > chunk:
            log(f"  写入「{ws.title}」{min(i + chunk, len(payload))}/{len(payload)}")
    log(f"已写入「{ws.title}」A{start_row} 起 {len(rows)} 行")


def _load_log_records(ws, log: LogFn) -> list[dict[str, Any]]:
    vals = with_retry(lambda: ws.get_all_values(), log=log, what="读取日志表") or []
    if not vals:
        return []
    start = 0
    if vals and str(vals[0][0] if vals[0] else "").strip() in ("日期", "date", "Date"):
        start = 1
    out: list[dict[str, Any]] = []
    for row in vals[start:]:
        date_s = str(row[0] if row else "").strip()
        url = extract_url(row[1] if len(row) > 1 else "") or str(row[1] if len(row) > 1 else "").strip()
        name = str(row[2] if len(row) > 2 else "").strip()
        sec = parse_duration_seconds(row[3] if len(row) > 3 else "")
        key = link_key(url)
        if not key and not name and not date_s:
            continue
        typ = str(row[4] if len(row) > 4 else "").strip()
        out.append({"date": date_s, "url": url, "name": name, "sec": sec, "key": key, "type": typ})
    return out


def _append_log_rows(ws, start_row_tracker: list[int], rows: list[list[Any]], log: LogFn) -> None:
    if not rows:
        return
    row = start_row_tracker[0]
    need = row - 1 + len(rows) + 500
    if ws.row_count < need or ws.col_count < 8:
        ws.resize(rows=max(ws.row_count, need), cols=max(ws.col_count, 8))

    def _upd():
        ws.update(
            range_name=f"A{row}",
            values=rows,
            value_input_option="USER_ENTERED",
        )

    with_retry(_upd, log=log, what="追加日志")
    start_row_tracker[0] = row + len(rows)
    log(f"  已追加日志 {len(rows)} 条 → 写到第 {start_row_tracker[0] - 1} 行")


def _report_matrix(
    records: list[dict[str, Any]],
    start_d,
    end_d,
    unit: int,
    range_label: str,
    types: list[str] | None = None,
    preferred_names: list[str] | None = None,
    count_mode: str = "divide_total",
) -> tuple[list[str], list[list[Any]]]:
    """生成横向人员矩阵：姓名占两列，下面按日期列出每日汇总。"""
    totals: dict[str, float] = defaultdict(float)
    daily: dict[tuple[Any, str], float] = defaultdict(float)
    item_totals: dict[str, int] = defaultdict(int)
    item_daily: dict[tuple[Any, str], int] = defaultdict(int)
    dates: set[Any] = set()
    allowed = {_norm_type(x) for x in (types or []) if _norm_type(x)}
    for rec in records:
        sec = rec.get("sec")
        if sec is None:
            continue
        if not _in_date_range(rec.get("date"), start_d, end_d):
            continue
        if allowed and _norm_type(rec.get("type")) not in allowed:
            continue
        name = str(rec.get("name") or "").strip()
        if not name:
            continue
        day = _as_date(rec.get("date"))
        if day is None:
            continue
        value = float(sec)
        totals[name] += value
        daily[(day, name)] += value
        # 逐条模式必须在这里按原视频时长计算，再把每条结果相加；不能用
        # 人员或日期汇总后的总秒数除以 30。
        item_count = _per_video_count(value)
        item_totals[name] += item_count
        item_daily[(day, name)] += item_count
        dates.add(day)

    # 沿用已经生成的姓名顺序。原有人即使本轮没有数据也保留原列，
    # 新出现的人只追加到末尾，避免刷新后人员位置发生变化。
    names: list[str] = []
    seen: set[str] = set()
    for value in preferred_names or []:
        name = str(value or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    names.extend(
        sorted(
            (name for name in totals if name not in seen),
            key=lambda value: value.casefold(),
        )
    )
    width = 1 + len(names) * 2
    rows: list[list[Any]] = [
        ["汇总范围", range_label] + [""] * max(0, width - 2),
        ["日期"] + [cell for name in names for cell in (name, "")],
        ["统计项"]
        + [
            cell
            for _name in names
            for cell in (
                "总秒数",
                "逐条视频计数" if count_mode == "per_video_ceil" else f"总秒数/{unit}",
            )
        ],
        ["本月汇总"]
        + [
            cell
            for name in names
            for cell in (
                int(round(totals[name])),
                item_totals[name]
                if count_mode == "per_video_ceil"
                else round(totals[name] / float(unit), 2),
            )
        ],
    ]
    for day in sorted(dates):
        row: list[Any] = [day.isoformat()]
        for name in names:
            total = daily.get((day, name), 0.0)
            row.extend(
                (
                    int(round(total)),
                    item_daily.get((day, name), 0)
                    if count_mode == "per_video_ceil"
                    else round(total / float(unit), 2),
                )
            )
        rows.append(row)
    return names, rows


def _format_report_sheet(ws, start_row: int, people: int, date_rows: int, log: LogFn) -> None:
    """仅在首次生成时设置紧凑默认样式；后续刷新不再调用。"""
    if people <= 0:
        return
    sheet_id = ws.id
    total_cols = 1 + people * 2
    name_row = start_row + 1
    subhead_row = start_row + 2
    summary_row = start_row + 3
    data_row = start_row + 4
    requests: list[dict[str, Any]] = []

    for person_index in range(people):
        start_col = 1 + person_index * 2
        requests.append(
            {
                "mergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": name_row - 1,
                        "endRowIndex": name_row,
                        "startColumnIndex": start_col,
                        "endColumnIndex": start_col + 2,
                    },
                    "mergeType": "MERGE_ALL",
                }
            }
        )

    def _repeat(row_1based: int, background: dict[str, float], bold: bool = True):
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_1based - 1,
                        "endRowIndex": row_1based,
                        "startColumnIndex": 0,
                        "endColumnIndex": total_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": background,
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                            "textFormat": {"bold": bold},
                        }
                    },
                    "fields": "userEnteredFormat",
                }
            }
        )

    _repeat(start_row, {"red": 0.94, "green": 0.94, "blue": 0.94})
    _repeat(name_row, {"red": 0.80, "green": 0.88, "blue": 0.97})
    _repeat(subhead_row, {"red": 0.90, "green": 0.94, "blue": 0.98})
    _repeat(summary_row, {"red": 0.82, "green": 0.94, "blue": 0.84})
    requests.append(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "frozenRowCount": summary_row,
                        "frozenColumnCount": 1,
                    },
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        }
    )
    requests.extend(
        [
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "properties": {"pixelSize": 86},
                    "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 1,
                        "endIndex": total_cols,
                    },
                    "properties": {"pixelSize": 72},
                    "fields": "pixelSize",
                }
            },
        ]
    )
    if date_rows:
        for col in range(1, total_cols):
            requests.append(
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [
                                {
                                    "sheetId": sheet_id,
                                    "startRowIndex": data_row - 1,
                                    "endRowIndex": data_row - 1 + date_rows,
                                    "startColumnIndex": col,
                                    "endColumnIndex": col + 1,
                                }
                            ],
                            "gradientRule": {
                                "minpoint": {
                                    "type": "MIN",
                                    "color": {"red": 0.94, "green": 0.98, "blue": 0.94},
                                },
                                "maxpoint": {
                                    "type": "MAX",
                                    "color": {"red": 0.20, "green": 0.66, "blue": 0.33},
                                },
                            },
                        },
                        "index": col - 1,
                    }
                }
            )
    with_retry(
        lambda: ws.spreadsheet.batch_update({"requests": requests}),
        log=log,
        what="设置数据表格式",
    )


def _write_report_sheet(ws, out_start: int, include_headers: bool, range_label: str, unit: int, records, start_d, end_d, log: LogFn, types: list[str] | None = None, count_mode: str = "divide_total") -> int:
    # 第 2 行（相对于输出起始行）是姓名表头。存在时把它作为用户确认过
    # 的固定列顺序，并且不再重设列宽、颜色、合并或条件格式。
    existing_names: list[str] = []
    try:
        current_header = ws.row_values(out_start + 1)
        if current_header and str(current_header[0] or "").strip() == "日期":
            existing_names = [
                str(current_header[index] or "").strip()
                for index in range(1, len(current_header), 2)
                if str(current_header[index] or "").strip()
            ]
    except Exception as exc:
        log(f"读取现有姓名顺序失败，将按首次生成处理：{exc}")
    first_generation = not existing_names
    names, payload = _report_matrix(
        records,
        start_d,
        end_d,
        unit,
        range_label,
        types,
        preferred_names=existing_names,
        count_mode=count_mode,
    )
    total_cols = max((len(row) for row in payload), default=1)
    end_col = index_to_col_letter(total_cols - 1)
    old_end_col = index_to_col_letter(max(ws.col_count - 1, total_cols - 1))
    try:
        ws.batch_clear([f"A{out_start}:{old_end_col}{max(ws.row_count, out_start)}"])
    except Exception:
        pass
    need_rows = out_start - 1 + len(payload) + 10
    if ws.row_count < need_rows or ws.col_count < total_cols:
        ws.resize(rows=max(ws.row_count, need_rows), cols=max(ws.col_count, total_cols))
    with_retry(
        lambda: ws.update(
            range_name=f"A{out_start}:{end_col}{out_start + len(payload) - 1}",
            values=payload,
            value_input_option="USER_ENTERED",
        ),
        log=log,
        what="写入数据表矩阵",
    )
    if first_generation:
        _format_report_sheet(ws, out_start, len(names), max(0, len(payload) - 4), log)
        log("数据表首次生成：已应用紧凑默认样式")
    else:
        log("已保留现有姓名列顺序和用户自定义样式，仅更新数据")
    log(f"已写入「{ws.title}」：{len(names)} 人，{max(0, len(payload) - 4)} 个日期")
    return len(names)


def _custom_report_matrix(records, unit: int, configured_types: list[str], count_mode: str, preferred_names=None):
    """自定义分类矩阵：第 1 行姓名、第 3 行类型、第 5 行汇总、第 8 行起日期。"""
    usable = [
        record
        for record in records
        if record.get("sec") is not None and str(record.get("name") or "").strip()
    ]
    discovered_names = {str(record.get("name") or "").strip() for record in usable}
    names: list[str] = []
    for name in preferred_names or []:
        if name and name not in names:
            names.append(name)
    names.extend(sorted(discovered_names - set(names), key=str.casefold))
    types = [_norm_type(value) for value in configured_types if _norm_type(value)]
    if not types:
        types = sorted({_norm_type(record.get("type")) or "未分类" for record in usable}, key=str.casefold)
    if not types:
        types = ["未分类"]

    totals: dict[tuple[str, str], float] = defaultdict(float)
    daily: dict[tuple[date, str, str], float] = defaultdict(float)
    dates: set[date] = set()
    for record in usable:
        day = _as_date(record.get("date"))
        if day is None:
            continue
        name = str(record.get("name") or "").strip()
        typ = _norm_type(record.get("type")) or "未分类"
        if typ not in types:
            continue
        seconds = float(record.get("sec") or 0)
        value = float(_per_video_count(seconds)) if count_mode == "per_video_ceil" else seconds / float(unit)
        totals[(name, typ)] += value
        daily[(day, name, typ)] += value
        dates.add(day)

    width = 1 + len(names) * len(types)
    row1 = [""]
    row3 = ["类型"]
    row5 = ["汇总"]
    for name in names:
        row1.extend([name] + [""] * (len(types) - 1))
        row3.extend(types)
        row5.extend([round(totals[(name, typ)], 2) for typ in types])
    rows: list[list[Any]] = [
        row1,
        [""] * width,
        row3,
        [""] * width,
        row5,
        [""] * width,
        [""] * width,
    ]
    for day in sorted(dates):
        row: list[Any] = [day.isoformat()]
        for name in names:
            row.extend([round(daily[(day, name, typ)], 2) for typ in types])
        rows.append(row)
    return names, types, rows


def _write_custom_report_sheet(ws, out_start: int, unit: int, records, log: LogFn, types, count_mode: str) -> int:
    preferred_names: list[str] = []
    try:
        preferred_names = [str(value or "").strip() for value in ws.row_values(out_start) if str(value or "").strip()]
    except Exception as exc:
        log(f"读取现有姓名顺序失败：{exc}")
    first_generation = not preferred_names
    names, categories, payload = _custom_report_matrix(
        records, unit, types or [], count_mode, preferred_names=preferred_names
    )
    width = max((len(row) for row in payload), default=1)
    old_width = max(ws.col_count, width)
    try:
        ws.batch_clear([f"A{out_start}:{index_to_col_letter(old_width - 1)}{max(ws.row_count, out_start)}"])
    except Exception:
        pass
    needed_rows = out_start - 1 + len(payload) + 10
    if ws.row_count < needed_rows or ws.col_count < width:
        ws.resize(rows=max(ws.row_count, needed_rows), cols=max(ws.col_count, width))
    with_retry(
        lambda: ws.update(range_name=f"A{out_start}", values=payload, value_input_option="USER_ENTERED"),
        log=log,
        what="写入自定义分类数据表",
    )
    if first_generation and names:
        requests: list[dict[str, Any]] = []
        block = len(categories)
        for index, _name in enumerate(names):
            start_col = 1 + index * block
            if block > 1:
                requests.append(
                    {
                        "mergeCells": {
                            "range": {
                                "sheetId": ws.id,
                                "startRowIndex": out_start - 1,
                                "endRowIndex": out_start,
                                "startColumnIndex": start_col,
                                "endColumnIndex": start_col + block,
                            },
                            "mergeType": "MERGE_ALL",
                        }
                    }
                )
        requests.extend(
            [
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                        "properties": {"pixelSize": 82},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": width},
                        "properties": {"pixelSize": 64},
                        "fields": "pixelSize",
                    }
                },
            ]
        )
        data_start_index = out_start + 6
        for col in range(1, width):
            requests.append(
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": ws.id, "startRowIndex": data_start_index, "endRowIndex": data_start_index + max(1, len(payload) - 7), "startColumnIndex": col, "endColumnIndex": col + 1}],
                            "gradientRule": {
                                "minpoint": {"type": "MIN", "color": {"red": 0.95, "green": 0.99, "blue": 0.95}},
                                "maxpoint": {"type": "MAX", "color": {"red": 0.18, "green": 0.68, "blue": 0.32}},
                            },
                        },
                        "index": col - 1,
                    }
                }
            )
        if requests:
            with_retry(lambda: ws.spreadsheet.batch_update({"requests": requests}), log=log, what="设置分类汇总样式")
        log("自定义数据表首次生成：已设置姓名合并、紧凑列宽和绿色渐变")
    else:
        log("已保留现有姓名顺序和表格样式，仅更新数据")
    log(f"已写入「{ws.title}」：{len(names)} 人，{len(categories)} 个分类，{max(0, len(payload) - 7)} 个日期")
    return len(names)


def run_video_duration(cfg, log: LogFn = print) -> dict[str, Any]:
    src_url = (getattr(cfg, "vd_source_url", "") or "").strip()
    dest_url = (getattr(cfg, "vd_dest_url", "") or "").strip()
    src_sheet = (getattr(cfg, "vd_source_sheet", "") or "").strip()
    source_sheets = [str(x).strip() for x in (getattr(cfg, "vd_source_sheets", []) or []) if str(x).strip()]
    if not source_sheets and src_sheet:
        source_sheets = [x.strip() for x in src_sheet.replace("，", "\n").replace(",", "\n").splitlines() if x.strip()]
    if not src_url:
        raise RuntimeError("请填写视频时长源表链接")
    if not dest_url:
        raise RuntimeError("请填写写入目标表格链接（日志表 + 数据表）")
    col_date = (getattr(cfg, "vd_col_date", "A") or "A").strip().upper()
    col_link = (getattr(cfg, "vd_col_link", "B") or "B").strip().upper()
    col_name = (getattr(cfg, "vd_col_name", "H") or "H").strip().upper()
    col_type = (getattr(cfg, "vd_col_type", "E") or "E").strip().upper()
    configured_columns = [item for item in (getattr(cfg, "vd_columns", []) or []) if isinstance(item, dict)]
    if configured_columns:
        normalized_columns = [
            {
                "field": str(item.get("field") or item.get("role") or "分类").strip(),
                "role": str(item.get("role") or "type").strip().lower(),
                "column": str(item.get("column") or "").strip().upper(),
            }
            for item in configured_columns
            if str(item.get("column") or "").strip()
        ]
        def _role_col(role: str, fallback: str) -> str:
            return next((item["column"] for item in normalized_columns if item["role"] == role), fallback)
        col_date = _role_col("date", col_date)
        col_link = _role_col("link", col_link)
        col_name = _role_col("name", col_name)
        type_columns = [item for item in normalized_columns if item["role"] == "type"]
        if type_columns:
            col_type = type_columns[0]["column"]
    else:
        normalized_columns = [
            {"field": "日期", "role": "date", "column": col_date},
            {"field": "视频链接", "role": "link", "column": col_link},
            {"field": "名字", "role": "name", "column": col_name},
            {"field": "类型", "role": "type", "column": col_type},
        ]
        type_columns = [normalized_columns[-1]]
    missing_roles = [role for role in ("date", "link", "name") if not any(item["role"] == role for item in normalized_columns)]
    if missing_roles:
        raise RuntimeError("源表列映射必须保留：日期、视频链接、名字")
    if not type_columns:
        raise RuntimeError("请至少保留一个类型/分类列映射")
    type_filters = _cfg_types(cfg)
    log_sheet = (getattr(cfg, "vd_log_sheet", "日志表") or "日志表").strip()
    report_sheet = (getattr(cfg, "vd_report_sheet", "数据表") or "数据表").strip()
    start_row = max(1, int(getattr(cfg, "vd_start_row", 2) or 2))
    unit = max(1, int(getattr(cfg, "vd_unit_seconds", 30) or 30))
    count_mode = str(getattr(cfg, "vd_count_mode", "divide_total") or "divide_total")
    if count_mode not in ("divide_total", "per_video_ceil"):
        count_mode = "divide_total"
    include_headers = bool(getattr(cfg, "vd_include_headers", True))
    out_start = max(1, int(getattr(cfg, "vd_out_start_row", 1) or 1))
    batch_size = max(20, min(500, int(getattr(cfg, "vd_batch_size", 100) or 100)))
    date_filter_enabled = bool(getattr(cfg, "vd_date_filter_enabled", True))
    start_d = _as_date(getattr(cfg, "vd_start_date", "") or "") if date_filter_enabled else None
    end_d = _as_date(getattr(cfg, "vd_end_date", "") or "") if date_filter_enabled else None
    type_filter_mode = str(getattr(cfg, "vd_type_filter_mode", "include") or "include").strip().lower()
    if type_filter_mode not in ("include", "exclude", "all"):
        type_filter_mode = "include"
    write_log = bool(getattr(cfg, "vd_write_log", False))
    if write_log and log_sheet == report_sheet:
        raise RuntimeError("日志表和数据表请用两个不同的工作表名称")
    try:
        for item in normalized_columns:
            col_letter_to_index(item["column"])
    except ValueError:
        raise RuntimeError("日期/链接/制作人/类型列必须填字母，例如 A、B、H、E")

    gc = authorize(cfg.resolve_credentials())
    session = _session_from_gc(gc)
    src_ss = open_by_url_or_id(gc, src_url, log=log)

    def _pad_col(block) -> list:
        vals = [(row[0] if row else "") for row in (block or [])]
        return [""] * (start_row - 1) + vals

    def _at(xs: list, row_1based: int) -> Any:
        index = row_1based - 1
        return xs[index] if 0 <= index < len(xs) else ""

    selected_sheets = source_sheets or [src_ss.sheet1.title]
    source_items: list[dict[str, Any]] = []
    sample = ""
    total_source_rows = 0
    for selected_sheet in selected_sheets:
        try:
            ws = src_ss.worksheet(selected_sheet)
        except Exception as e:
            raise RuntimeError(f"源表找不到工作表「{selected_sheet}」") from e
        log(f"读取源表「{src_ss.title}」/「{selected_sheet}」")
        value_columns = [item for item in normalized_columns if item["role"] != "link"]
        ranges = [f"{item['column']}{start_row}:{item['column']}" for item in value_columns]
        blocks = with_retry(lambda ws=ws, ranges=ranges: ws.batch_get(ranges), log=log, what="读取源表列") or []
        values_by_key: dict[tuple[str, str], list] = {}
        for index, item in enumerate(value_columns):
            values_by_key[(item["role"], item["column"])] = _pad_col(blocks[index] if index < len(blocks) else [])
        last = max([len(values) for values in values_by_key.values()] + [start_row])
        log(f"读取 {col_link} 列单元格超链接（不是文件名）…")
        link_col = _read_link_column(session, src_ss.id, selected_sheet, col_link, start_row, last, log=log)
        total_source_rows += max(0, last - start_row + 1)
        if not sample:
            sample = next((url for url in link_col if url), "")
        for offset, url in enumerate(link_col):
            row_num = start_row + offset
            date_values = next((values for (role, _column), values in values_by_key.items() if role == "date"), [])
            name_values = next((values for (role, _column), values in values_by_key.items() if role == "name"), [])
            category_values = []
            for item in type_columns:
                value = str(_at(values_by_key.get(("type", item["column"]), []), row_num) or "").strip()
                if value:
                    category_values.append(value)
            source_items.append(
                {
                    "url": url,
                    "date": _at(date_values, row_num),
                    "name": _at(name_values, row_num),
                    "type": " / ".join(category_values),
                }
            )
    n_url = sum(1 for item in source_items if item["url"])
    log(f"共 {total_source_rows} 行，其中 {n_url} 条取到超链接" + (f"，示例：{sample}" if sample else "。若为 0，说明单元格不是超链接"))
    if n_url == 0:
        raise RuntimeError(
            f"{col_link} 列没有读到超链接。表格里看到的蓝字文件名本身不是网址，"
            "需要单元格插入的链接。请确认列字母填对，且服务账号对该表有权限。"
        )

    dest = open_by_url_or_id(gc, dest_url, log=log)
    log_headers = ["日期", "视频链接", "名字", "时长(秒)", "类型"]
    log_ws = _ensure_ws(dest, log_sheet, 8, 200, log) if write_log else None
    report_ws = _ensure_ws(dest, report_sheet, 8, 200, log)

    existing = _load_log_records(log_ws, log) if log_ws is not None else []
    index: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for rec in existing:
        records.append(rec)
        if rec.get("key"):
            index[rec["key"]] = rec
    log(f"日志表已有 {len(existing)} 条")

    if log_ws is not None and not existing:
        if include_headers:
            with_retry(
                lambda: log_ws.update(range_name="A1", values=[log_headers], value_input_option="USER_ENTERED"),
                log=log,
                what="写日志表头",
            )
            next_row = 2
        else:
            next_row = max(1, out_start)
    elif log_ws is not None:
        next_row = (2 if include_headers else 1) + len(existing)
    else:
        next_row = 1
    next_tracker = [next_row]

    pending: list[dict[str, Any]] = []
    seen_src: set[str] = set()
    skipped = 0
    empty_skip = 0
    out_of_range = 0
    type_skip = 0
    allowed_types = set(type_filters)
    for source_item in source_items:
        url = source_item["url"]
        url = extract_url(url) or url
        date_v = source_item["date"]
        dt = to_datetime(date_v)
        date_s = dt.strftime("%Y-%m-%d") if dt else str(date_v or "").strip()
        name = str(source_item["name"] or "").strip()
        typ = _norm_type(source_item["type"])
        if not url:
            empty_skip += 1
            continue
        type_rejected = (
            type_filter_mode == "include" and allowed_types and typ not in allowed_types
        ) or (
            type_filter_mode == "exclude" and allowed_types and typ in allowed_types
        )
        if type_rejected:
            type_skip += 1
            continue
        if not _in_date_range(date_s, start_d, end_d):
            out_of_range += 1
            continue
        key = link_key(url)
        if not key or key in seen_src:
            continue
        seen_src.add(key)
        prev = index.get(key)
        if prev is not None and prev.get("sec") is not None:
            prev["type"] = typ or prev.get("type") or ""
            prev["date"] = date_s or prev.get("date") or ""
            if name:
                prev["name"] = name
            skipped += 1
            continue
        pending.append({"date": date_s, "url": url, "name": name, "sec": None, "key": key, "type": typ})

    range_label = "全部日期"
    if start_d or end_d:
        range_label = f"{start_d or '…'} ~ {end_d or '…'}"
    if type_filters:
        range_label += " · " + "、".join(type_filters)
    log(
        f"{range_label}：待查询 {len(pending)}，已有时长跳过 {skipped}，"
        f"空链接 {empty_skip}，类型不符 {type_skip}，日期外 {out_of_range}"
    )

    cache: dict[str, float | None] = {}
    drive_cache: dict[str, float | None] = {}
    ok = 0
    miss = 0
    appended = 0

    def _flush_report():
        if not write_log:
            return _write_custom_report_sheet(
                report_ws,
                out_start,
                unit,
                records,
                log,
                type_filters if type_filter_mode == "include" else [],
                count_mode,
            )
        people = _write_report_sheet(
            report_ws,
            out_start,
            include_headers,
            range_label,
            unit,
            records,
            start_d,
            end_d,
            log,
            type_filters,
            count_mode,
        )
        return people

    if not pending:
        people = _flush_report()
        log(f"没有新链接需要查询。已按筛选条件重写数据表，{people} 人")
        return {
            "ok": True,
            "mode": "video",
            "log_rows": len(records),
            "people": people,
            "ok_duration": 0,
            "miss_duration": 0,
            "skipped": skipped,
            "target_url": dest_url,
            "log_sheet": log_sheet,
            "report_sheet": report_sheet,
        }

    for bi in range(0, len(pending), batch_size):
        chunk = pending[bi : bi + batch_size]
        ids = [drive_file_id(x["url"]) for x in chunk if drive_file_id(x["url"])]
        log(f"第 {bi // batch_size + 1} 批（{len(chunk)} 条）查询时长…")
        drive_cache.update(_prefetch_drive_durations(session, ids, log))
        rows_out: list[list[Any]] = []
        for item in chunk:
            sec = duration_for_link(session, item["url"], cache, drive_cache)
            item["sec"] = sec
            if sec is None:
                miss += 1
            else:
                ok += 1
            records.append(item)
            if item["key"]:
                index[item["key"]] = item
            rows_out.append(
                [
                    item["date"],
                    item["url"],
                    item["name"],
                    int(round(sec)) if sec is not None else "",
                    item.get("type") or "",
                ]
            )
        if log_ws is not None:
            _append_log_rows(log_ws, next_tracker, rows_out, log)
        appended += len(rows_out)
        batch_i = bi // batch_size + 1
        people = 0
        if batch_i % 5 == 0 or bi + batch_size >= len(pending):
            people = _flush_report()
        log(
            f"  本批完成：累计新增 {appended}/{len(pending)}，成功 {ok}，未识别 {miss}"
            + (f"，数据表 {people} 人" if people else "")
        )

    people = _flush_report()
    log(f"完成：新增日志 {appended}，跳过 {skipped}，成功 {ok}，未识别 {miss}，数据表 {people} 人")
    return {
        "ok": True,
        "mode": "video",
        "log_rows": len(records),
        "people": people,
        "ok_duration": ok,
        "miss_duration": miss,
        "skipped": skipped,
        "appended": appended,
        "target_url": dest_url,
        "log_sheet": log_sheet,
        "report_sheet": report_sheet,
    }
