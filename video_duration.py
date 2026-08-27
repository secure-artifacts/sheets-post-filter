# -*- coding: utf-8 -*-
"""从源表视频链接提取时长，写入日志表 + 按制作人汇总的数据表。"""

from __future__ import annotations

import fnmatch
import re
import time
import unicodedata
from collections import defaultdict
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from fetch_posts import (
    authorize_cfg,
    col_letter_to_index,
    index_to_col_letter,
    open_by_url_or_id,
    safe_resize_ws,
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


def _date_key(value: Any) -> str:
    day = _as_date(value)
    return day.isoformat() if day else ""


def _norm_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split())


def _norm_type(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _type_pattern_match(value: str, pattern: str) -> bool:
    """Contains match by default; `*` / `?` enable glob; prefix `=` for exact."""
    text = _norm_type(value).casefold()
    pat = _norm_type(pattern)
    if not text or not pat:
        return False
    if pat.startswith("="):
        return text == pat[1:].strip().casefold()
    pat = pat.casefold()
    if any(char in pat for char in "*?["):
        return fnmatch.fnmatchcase(text, pat)
    return pat in text


def _type_equals(value: str, pattern: str) -> bool:
    """Video template uses exact type match (no wildcard)."""
    return _norm_type(value).casefold() == _norm_type(pattern).casefold()


def _matched_patterns(value: str, patterns: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in patterns:
        key = _norm_type(raw)
        if key and key not in seen and _type_pattern_match(value, key):
            out.append(key)
            seen.add(key)
    return out


def _cfg_type_list(raw) -> list[str]:
    if isinstance(raw, str):
        parts = raw.replace("，", "\n").replace(",", "\n").replace(";", "\n").splitlines()
        values = parts
    else:
        values = raw or []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _norm_type(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _custom_buckets(
    type_values: list[str],
    separate: list[str],
    exclude: list[str],
    other: str,
    empty_to_other: bool = True,
) -> list[str]:
    """单独成列的分类各算一列；排除的不算；空白和其他可归入 other。

    同一分类同时出现在「单独成列」和「排除」时，单独成列优先。
    """
    separate_list = [_norm_type(x) for x in separate if _norm_type(x)]
    exclude_list = [_norm_type(x) for x in exclude if _norm_type(x)]
    separate_set = set(separate_list)
    exclude_list = [item for item in exclude_list if item not in separate_set]
    other = _norm_type(other)
    texts = [_norm_type(raw) for raw in type_values]
    texts = [text for text in texts if text]
    if not texts:
        if other and empty_to_other:
            return [other]
        return []
    buckets: list[str] = []
    seen: set[str] = set()
    leftover = False
    for text in texts:
        matched = _matched_patterns(text, separate_list)
        if matched:
            for item in matched:
                if item not in seen:
                    buckets.append(item)
                    seen.add(item)
            continue
        if _matched_patterns(text, exclude_list):
            continue
        leftover = True
    if other and leftover and other not in seen:
        buckets.append(other)
    elif not separate_list and leftover and not other:
        for text in texts:
            if not _matched_patterns(text, exclude_list) and text not in seen:
                buckets.append(text)
                seen.add(text)
    return buckets


def _cfg_types(cfg) -> list[str]:
    return _cfg_type_list(getattr(cfg, "vd_types", None))


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


def _drive_lookup(session, file_id: str) -> tuple[float | None, str]:
    """Return (seconds, reason). reason is empty on success."""
    if session is None:
        return None, "未连接到 Drive"
    if not file_id:
        return None, "没有 Drive 文件 id"

    def _do():
        r = session.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={
                "fields": "id,name,mimeType,videoMediaMetadata,shortcutDetails",
                "supportsAllDrives": "true",
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Drive {r.status_code}: {r.text[:200]}")
        return r.json()

    try:
        data = with_retry(_do, what="读取 Drive 视频元数据")
    except Exception as exc:
        text = str(exc)
        if "403" in text or "401" in text:
            return None, "服务账号没有这个文件的权限"
        if "404" in text:
            return None, "Drive 找不到这个文件"
        return None, f"Drive 读取失败：{text[:80]}"
    mime = str(data.get("mimeType") or "")
    name = str(data.get("name") or "")
    meta = data.get("videoMediaMetadata") or {}
    ms = meta.get("durationMillis")
    if ms not in (None, ""):
        try:
            return float(ms) / 1000.0, ""
        except (TypeError, ValueError):
            pass
    shortcut = (data.get("shortcutDetails") or {}).get("targetId") or ""
    if shortcut and shortcut != file_id:
        return _drive_lookup(session, shortcut)
    if mime.startswith("image/"):
        return None, f"不是视频（{name or mime}）"
    if mime.startswith("audio/"):
        return None, f"是音频不是视频（{name or mime}）"
    if "folder" in mime:
        return None, f"这是文件夹（{name or 'Drive 文件夹'}）"
    if mime.startswith("video/") or mime.endswith("octet-stream"):
        return None, f"Drive 还没有时长元数据（{name or mime}，可稍后再试）"
    return None, f"无视频时长（{name or mime or '未知类型'}）"


def duration_from_drive(session, file_id: str) -> float | None:
    seconds, _reason = _drive_lookup(session, file_id)
    return seconds


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
    reasons: dict[str, str] | None = None,
    id_reasons: dict[str, str] | None = None,
) -> float | None:
    url = (url or "").strip()
    if not url:
        if reasons is not None:
            reasons[url] = "空链接"
        return None
    if url in cache:
        return cache[url]
    drive_cache = drive_cache if drive_cache is not None else {}
    sec = None
    reason = ""
    fid = drive_file_id(url)
    if fid:
        if fid in drive_cache:
            sec = drive_cache[fid]
            if sec is None:
                reason = (id_reasons or {}).get(fid) or "Drive 无时长元数据"
        else:
            sec, reason = _drive_lookup(session, fid)
            drive_cache[fid] = sec
        if sec is None:
            pub = duration_from_drive_public(fid)
            if pub:
                sec = pub
                reason = ""
                drive_cache[fid] = pub
    if sec is None and youtube_id(url):
        sec = duration_from_youtube(url)
        time.sleep(0.05)
        if sec is None:
            reason = reason or "无法解析 YouTube 时长"
    if sec is None and not fid and not youtube_id(url):
        reason = "不是 Google Drive / YouTube 链接"
        page = duration_from_page(url)
        if page:
            sec = page
            reason = ""
    cache[url] = sec
    if reasons is not None and sec is None:
        reasons[url] = reason or "未识别时长"
    return sec


def _prefetch_drive_durations(
    session, file_ids: list[str], log: LogFn, workers: int = 6, reasons: dict[str, str] | None = None
) -> dict[str, float | None]:
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
        return fid, _drive_lookup(session, fid)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(_one, fid) for fid in uniq]
        for fut in as_completed(futs):
            fid, (sec, reason) = fut.result()
            out[fid] = sec
            if reasons is not None and reason:
                reasons[fid] = reason
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
        ws = ss.add_worksheet(title=name, rows=max(min(rows, 200), 40), cols=max(min(cols, 26), 2))
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
    vals = with_retry(
        lambda: ws.get_all_values(value_render_option="UNFORMATTED_VALUE") or [],
        log=log,
        what="读取日志表",
    ) or []
    if not vals:
        return []
    start = 0
    if vals and str(vals[0][0] if vals[0] else "").strip() in ("日期", "date", "Date"):
        start = 1
    out: list[dict[str, Any]] = []
    bad_dates = 0
    for offset, row in enumerate(vals[start:]):
        raw_date = row[0] if row else ""
        date_s = _date_key(raw_date)
        url = extract_url(row[1] if len(row) > 1 else "") or str(row[1] if len(row) > 1 else "").strip()
        name = _norm_name(row[2] if len(row) > 2 else "")
        sec = parse_duration_seconds(row[3] if len(row) > 3 else "")
        key = link_key(url)
        if not key and not name and not date_s:
            continue
        if name and not date_s and raw_date not in ("", None):
            bad_dates += 1
        typ = str(row[4] if len(row) > 4 else "").strip()
        out.append(
            {
                "date": date_s,
                "url": url,
                "name": name,
                "sec": sec,
                "key": key,
                "type": typ,
                "row": start + offset + 1,
            }
        )
    if bad_dates:
        log(f"日志里有 {bad_dates} 条日期无法识别，这些行不会进入数据表。已兼容 2026-8-23 / 8/23/2026 / 序列号")
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
            value_input_option="RAW",
        )

    with_retry(_upd, log=log, what="追加日志")
    start_row_tracker[0] = row + len(rows)
    log(f"  已追加日志 {len(rows)} 条 → 写到第 {start_row_tracker[0] - 1} 行")


def _patch_log_fields(ws, patches: list[dict[str, Any]], log: LogFn) -> None:
    """Rewrite date/name/type on existing log rows as plain text so Sheets timezone cannot shift the day."""
    if not patches:
        return
    ranges: list[dict[str, Any]] = []
    for item in patches:
        row = int(item.get("row") or 0)
        if row < 1:
            continue
        if item.get("date"):
            ranges.append({"range": f"A{row}", "values": [[item["date"]]]})
        if item.get("name"):
            ranges.append({"range": f"C{row}", "values": [[item["name"]]]})
        if item.get("type"):
            ranges.append({"range": f"E{row}", "values": [[item["type"]]]})
    if not ranges:
        return
    for offset in range(0, len(ranges), 200):
        chunk = ranges[offset : offset + 200]
        with_retry(
            lambda c=chunk: ws.batch_update(c, value_input_option="RAW"),
            log=log,
            what="更正日志日期",
        )
    log(f"已按源表更正日志 {len(patches)} 条日期（纯文本写入，避免 2026-8-23 被表格挪到第二天）")


def _qty_total(seconds: float, unit: int):
    value = round(float(seconds) / float(max(1, unit)), 2)
    return int(value) if value == int(value) else value


def _report_matrix(
    records: list[dict[str, Any]],
    start_d,
    end_d,
    unit: int,
    range_label: str,
    types: list[str] | None = None,
    preferred_names: list[str] | None = None,
    count_mode: str = "divide_total",
    lock_names: bool = False,
) -> tuple[list[str], list[list[Any]]]:
    """每人固定两列（总计数=秒÷除数，逐条计数），后面每加一个分类多一列。"""
    extra = [_norm_type(value) for value in (types or []) if _norm_type(value)]
    totals: dict[str, float] = defaultdict(float)
    daily: dict[tuple[Any, str], float] = defaultdict(float)
    item_totals: dict[str, int] = defaultdict(int)
    item_daily: dict[tuple[Any, str], int] = defaultdict(int)
    cat_item: dict[tuple[str, str], int] = defaultdict(int)
    cat_daily: dict[tuple[Any, str, str], int] = defaultdict(int)
    dates: set[Any] = set()
    for rec in records:
        sec = rec.get("sec")
        if sec is None:
            continue
        if not _in_date_range(rec.get("date"), start_d, end_d):
            continue
        name = _norm_name(rec.get("name"))
        if not name:
            continue
        day = _as_date(rec.get("date"))
        if day is None:
            continue
        typ = rec.get("type")
        value = float(sec)
        totals[name] += value
        daily[(day, name)] += value
        item_count = _per_video_count(value)
        item_totals[name] += item_count
        item_daily[(day, name)] += item_count
        for pattern in extra:
            if _type_equals(typ, pattern):
                cat_item[(name, pattern)] += item_count
                cat_daily[(day, name, pattern)] += item_count
        dates.add(day)

    names: list[str] = []
    seen: set[str] = set()
    for value in preferred_names or []:
        name = _norm_name(value)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    if not lock_names or not names:
        names.extend(
            sorted(
                (name for name in totals if name not in seen),
                key=lambda value: value.casefold(),
            )
        )
    block = 2 + len(extra)
    width = 1 + len(names) * block
    stat_labels = ["总计数", "逐条计数"] + extra

    def _person_summary(name: str) -> list[Any]:
        cells: list[Any] = [_qty_total(totals[name], unit), item_totals[name]]
        cells.extend(cat_item[(name, pattern)] for pattern in extra)
        return cells

    def _person_day(day, name: str) -> list[Any]:
        total = daily.get((day, name), 0.0)
        cells: list[Any] = [_qty_total(total, unit), item_daily.get((day, name), 0)]
        cells.extend(cat_daily.get((day, name, pattern), 0) for pattern in extra)
        return cells

    name_cells: list[Any] = []
    stat_cells: list[Any] = []
    for name in names:
        name_cells.extend([name] + [""] * (block - 1))
        stat_cells.extend(stat_labels)
    rows: list[list[Any]] = [
        ["汇总范围", range_label] + [""] * max(0, width - 2),
        ["日期"] + name_cells,
        ["统计项"] + stat_cells,
        ["本月汇总"] + [cell for name in names for cell in _person_summary(name)],
    ]
    for day in sorted(dates):
        row: list[Any] = [day.isoformat()]
        for name in names:
            row.extend(_person_day(day, name))
        rows.append(row)
    return names, rows


def _format_report_sheet(ws, start_row: int, names: list[str], extra_types: list[str], payload_len: int, log: LogFn) -> None:
    """每人一块颜色：姓名合并，总计数+逐条+分类列，人与人之间分隔线。"""
    if not names:
        return
    sheet_id = ws.id
    block = 2 + len(extra_types)
    width = 1 + len(names) * block
    name_row = start_row + 1
    last_row = start_row - 1 + max(payload_len, 4)
    try:
        ws.spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "unmergeCells": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": name_row - 1,
                                "endRowIndex": name_row,
                                "startColumnIndex": 1,
                                "endColumnIndex": width,
                            }
                        }
                    }
                ]
            }
        )
    except Exception:
        pass
    requests: list[dict[str, Any]] = [
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 90},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": name_row - 1, "endIndex": name_row + 1},
                "properties": {"pixelSize": 42},
                "fields": "pixelSize",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": start_row - 1,
                    "endRowIndex": start_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": width,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.94, "green": 0.96, "blue": 0.95},
                        "textFormat": {"bold": True, "fontSize": 9},
                    }
                },
                "fields": "userEnteredFormat",
            }
        },
    ]
    for col in range(1, width):
        offset = (col - 1) % max(block, 1)
        if offset < 2:
            pixels = 58
        else:
            label = extra_types[offset - 2] if offset - 2 < len(extra_types) else ""
            chars = min(max(len(label) or 2, 2), 4)
            pixels = 28 + chars * 16
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                    "properties": {"pixelSize": pixels},
                    "fields": "pixelSize",
                }
            }
        )
    merge_requests: list[dict[str, Any]] = []
    for index, _name in enumerate(names):
        start_col = 1 + index * block
        end_col = start_col + block
        strong, pale = PERSON_BANDS[index % len(PERSON_BANDS)]
        if block > 1:
            merge_requests.append(
                {
                    "mergeCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": name_row - 1,
                            "endRowIndex": name_row,
                            "startColumnIndex": start_col,
                            "endColumnIndex": end_col,
                        },
                        "mergeType": "MERGE_ALL",
                    }
                }
            )
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": name_row - 1,
                        "endRowIndex": name_row + 1,
                        "startColumnIndex": start_col,
                        "endColumnIndex": end_col,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _rgb(strong),
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
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
                        "startRowIndex": name_row + 1,
                        "endRowIndex": last_row,
                        "startColumnIndex": start_col,
                        "endColumnIndex": end_col,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _rgb(pale),
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                            "textFormat": {"fontSize": 9, "foregroundColor": {"red": 0.12, "green": 0.16, "blue": 0.18}},
                        }
                    },
                    "fields": "userEnteredFormat",
                }
            }
        )
        if index < len(names) - 1:
            requests.append(
                {
                    "updateBorders": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": name_row - 1,
                            "endRowIndex": last_row,
                            "startColumnIndex": end_col - 1,
                            "endColumnIndex": end_col,
                        },
                        "right": {"style": "SOLID_MEDIUM", "width": 2, "color": {"red": 1, "green": 1, "blue": 1}},
                    }
                }
            )
    requests.append(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": min(start_row + 3, last_row), "frozenColumnCount": 1},
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        }
    )
    # 先上颜色，再合并姓名。合并失败也不能把分色弄丢。
    def _send(parts: list[dict[str, Any]], what: str) -> None:
        for offset in range(0, len(parts), 30):
            part = parts[offset : offset + 30]
            try:
                with_retry(
                    lambda p=part: ws.spreadsheet.batch_update({"requests": p}),
                    log=log,
                    what=what,
                )
            except Exception as exc:
                log(f"{what}第 {offset // 30 + 1} 批失败：{exc}")

    _send(requests, "设置按时长分色")
    _send(merge_requests, "合并姓名单元格")


def _as_number(value: Any):
    if value in ("", None):
        return 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).strip().replace(",", "")
    if not text:
        return 0
    try:
        number = float(text)
    except ValueError:
        return 0
    return int(number) if number == int(number) else number


def _parse_existing_video_report(ws, out_start: int) -> dict[str, Any] | None:
    """Read the current 数据表 so a later rewrite can keep dates/people the log failed to reload."""
    try:
        rows = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
    except Exception:
        return None
    body = rows[max(0, out_start - 1) :]
    if len(body) < 4 or str(body[1][0] or "").strip() != "日期":
        return None
    name_row = body[1]
    stat_row = body[2]
    names: list[str] = []
    starts: list[int] = []
    for index in range(1, len(name_row)):
        name = str(name_row[index] or "").strip()
        if name:
            names.append(name)
            starts.append(index)
    if not names:
        return None
    block = starts[1] - starts[0] if len(starts) >= 2 else max(2, len(stat_row) - starts[0])
    labels = []
    for offset in range(block):
        idx = starts[0] + offset
        labels.append(str(stat_row[idx] or "").strip() if idx < len(stat_row) else "")
    daily: dict[tuple[str, str, str], Any] = {}
    dates: list[str] = []
    for row in body[4:]:
        day = _date_key(row[0] if row else "")
        if not day:
            continue
        dates.append(day)
        for name, start in zip(names, starts):
            for offset, label in enumerate(labels):
                col = start + offset
                daily[(day, name, label or f"#{offset}")] = row[col] if col < len(row) else ""
    return {"names": names, "labels": labels, "dates": dates, "daily": daily}


def _merge_video_payload(payload: list[list[Any]], existing: dict[str, Any] | None, extra: list[str]) -> list[list[Any]]:
    """Keep historical date rows from the current sheet when the log rebuild omits them."""
    if not existing or not payload or len(payload) < 4:
        return payload
    block = 2 + len(extra)
    labels = ["总计数", "逐条计数"] + extra
    name_row = payload[1]
    names: list[str] = []
    index = 1
    while index < len(name_row):
        name = str(name_row[index] or "").strip()
        if name:
            names.append(name)
        index += block
    for name in existing.get("names") or []:
        if name and name not in names:
            names.append(name)
    grid: dict[tuple[str, str, str], Any] = {}
    computed_pairs: set[tuple[str, str]] = set()
    for row in payload[4:]:
        day = _date_key(row[0] if row else "")
        if not day:
            continue
        for name_index, name in enumerate(names):
            base = 1 + name_index * block
            if base >= len(row):
                continue
            computed_pairs.add((day, name))
            for offset, label in enumerate(labels):
                col = base + offset
                grid[(day, name, label)] = row[col] if col < len(row) else ""
    for (day, name, label), value in (existing.get("daily") or {}).items():
        day = _date_key(day)
        if not day or (day, name) in computed_pairs or label not in labels:
            continue
        grid[(day, name, label)] = value
    dates = sorted({day for day, _name, _label in grid}, key=lambda day: (_as_date(day) or day))
    width = 1 + len(names) * block
    name_cells: list[Any] = []
    stat_cells: list[Any] = []
    for name in names:
        name_cells.extend([name] + [""] * (block - 1))
        stat_cells.extend(labels)
    date_rows: list[list[Any]] = []
    summary = [0] * (width - 1)
    for day in dates:
        row: list[Any] = [day]
        for name in names:
            for label in labels:
                row.append(grid.get((day, name, label), ""))
        date_rows.append(row)
        for index, value in enumerate(row[1:]):
            summary[index] = _as_number(summary[index]) + _as_number(value)
    range_row = list(payload[0]) + [""] * max(0, width - len(payload[0]))
    return [range_row[:width], ["日期"] + name_cells, ["统计项"] + stat_cells, ["本月汇总"] + summary, *date_rows]


def _write_report_sheet(ws, out_start: int, include_headers: bool, range_label: str, unit: int, records, start_d, end_d, log: LogFn, types: list[str] | None = None, count_mode: str = "divide_total") -> int:
    existing_names: list[str] = []
    try:
        current_header = ws.row_values(out_start + 1)
        if current_header and str(current_header[0] or "").strip() == "日期":
            existing_names = [str(value).strip() for value in current_header[1:] if str(value or "").strip()]
    except Exception as exc:
        log(f"读取现有姓名顺序失败，将按首次生成处理：{exc}")
    extra = [_norm_type(value) for value in (types or []) if _norm_type(value)]
    previous = _parse_existing_video_report(ws, out_start)
    header_locked = False
    if previous and previous.get("names"):
        existing_names = [str(name).strip() for name in previous.get("names") or [] if str(name).strip()]
        header_labels = [str(label).strip() for label in previous.get("labels") or []]
        if len(header_labels) >= 2:
            extra = [_norm_type(label) for label in header_labels[2:] if _norm_type(label)]
        header_locked = True
        missing = sorted({_norm_name(rec.get("name")) for rec in records} - set(existing_names) - {""})
        if missing:
            log("表头已固定，未自动加列的人：" + "、".join(missing[:12]) + ("…" if len(missing) > 12 else ""))
    # 数据表按日志全量汇总。日期筛选只影响新查询，避免每次重跑把历史日期刷掉。
    names, payload = _report_matrix(
        records,
        None,
        None,
        unit,
        range_label,
        extra,
        preferred_names=existing_names,
        count_mode=count_mode,
        lock_names=header_locked,
    )
    payload = _merge_video_payload(payload, previous, extra)
    labels = ["总计数", "逐条计数"] + extra
    header_locked = header_locked and list(names) == list(existing_names)
    total_cols = max((len(row) for row in payload), default=1)
    end_col = index_to_col_letter(total_cols - 1)
    old_end_col = index_to_col_letter(max(int(getattr(ws, "col_count", 1) or 1) - 1, total_cols - 1))
    need_rows = out_start - 1 + len(payload) + 10
    try:
        # 按实际人数和分类列收紧，避免旧空白列把颜色刷没、也避免单元格爆炸。
        safe_resize_ws(ws, max(need_rows, 40), max(total_cols, 1), log=log)
    except RuntimeError:
        try:
            ws.resize(rows=1, cols=1)
            safe_resize_ws(ws, max(need_rows, 40), max(total_cols, 1), log=log)
        except Exception as inner:
            raise RuntimeError("数据表单元格超过上限，请换空表或删掉空白列后再提取。") from inner
        header_locked = False
    if header_locked and len(payload) > 3:
        body = payload[3:]
        write_start = out_start + 3
        log("表头第 1-3 行已保留（姓名和分类列不动），只刷新本月汇总和各日期数量")
    else:
        body = payload
        write_start = out_start
        if previous:
            log("表头人数或分类列有变化，已连同第 1-3 行一起重写")
        else:
            log("首次写入数据表表头（第 1-3 行），之后只刷新日期数量")
    with_retry(
        lambda: ws.update(
            range_name=f"A{write_start}:{end_col}{write_start + len(body) - 1}",
            values=body,
            value_input_option="USER_ENTERED",
        ),
        log=log,
        what="写入数据表矩阵",
    )
    try:
        leftover = []
        last_data = write_start + len(body) - 1
        if ws.row_count > last_data:
            leftover.append(f"A{last_data + 1}:{old_end_col}{ws.row_count}")
        if int(getattr(ws, "col_count", 1) or 1) > total_cols:
            leftover.append(
                f"{index_to_col_letter(total_cols)}{write_start}:{old_end_col}{max(ws.row_count, write_start)}"
            )
        if leftover:
            ws.batch_clear(leftover)
    except Exception:
        pass
    if not header_locked:
        try:
            _format_report_sheet(ws, out_start, names, extra, len(payload), log)
            log("已按人分色、合并姓名，并加上总计数 / 逐条 / 分类列")
        except Exception as exc:
            log(f"设置时长数据表样式失败，数据已写入：{exc}")
    log(f"已写入「{ws.title}」：{len(names)} 人，{max(0, len(payload) - 4)} 个日期")
    return len(names)


def _custom_report_matrix(records, unit: int, configured_types: list[str], count_mode: str, preferred_names=None, value_mode: str = "count", lock_names: bool = False):
    """自定义分类矩阵：第 1 行姓名、第 3 行类型、第 5 行汇总、第 8 行起日期。按条数计，不用时长。"""
    usable = [
        record
        for record in records
        if str(record.get("name") or "").strip()
        and (value_mode == "count" or record.get("sec") is not None)
    ]
    discovered_names = {str(record.get("name") or "").strip() for record in usable}
    names: list[str] = []
    for name in preferred_names or []:
        if name and name not in names:
            names.append(name)
    if not lock_names or not names:
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
        value = 1.0 if value_mode == "count" else (
            float(_per_video_count(seconds)) if count_mode == "per_video_ceil" else seconds / float(unit)
        )
        totals[(name, typ)] += value
        daily[(day, name, typ)] += value
        dates.add(day)

    def _num(value: float):
        if value_mode == "count":
            return int(round(value))
        return round(value, 2)

    width = 1 + len(names) * len(types)
    row1 = [""]
    row3 = ["类型"]
    row5 = ["汇总"]
    for name in names:
        row1.extend([name] + [""] * (len(types) - 1))
        row3.extend(types)
        row5.extend([_num(totals[(name, typ)]) for typ in types])
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
            row.extend([_num(daily[(day, name, typ)]) for typ in types])
        rows.append(row)
    return names, types, rows


PERSON_BANDS = [
    ((0.18, 0.67, 0.47), (0.85, 0.95, 0.88)),   # green
    ((0.90, 0.42, 0.39), (0.98, 0.86, 0.84)),   # coral
    ((0.35, 0.53, 0.89), (0.86, 0.90, 0.98)),   # blue
    ((0.62, 0.32, 0.86), (0.92, 0.84, 0.97)),   # purple
    ((0.29, 0.56, 0.85), (0.84, 0.91, 0.98)),   # blue 2
    ((0.36, 0.70, 0.42), (0.86, 0.94, 0.84)),   # green 2
    ((0.93, 0.64, 0.18), (0.99, 0.92, 0.78)),   # amber
    ((0.90, 0.36, 0.57), (0.98, 0.86, 0.90)),   # pink
]


def _rgb(values: tuple[float, float, float]) -> dict[str, float]:
    return {"red": values[0], "green": values[1], "blue": values[2]}


def _format_custom_person_sheet(ws, out_start: int, names: list[str], categories: list[str], payload: list[list[Any]], log: LogFn) -> None:
    if not names or not categories:
        return
    sheet_id = ws.id
    block = len(categories)
    width = 1 + len(names) * block
    last_row = out_start - 1 + len(payload)
    try:
        ws.spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "unmergeCells": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": out_start - 1,
                                "endRowIndex": out_start,
                                "startColumnIndex": 1,
                                "endColumnIndex": width,
                            }
                        }
                    }
                ]
            }
        )
    except Exception:
        pass
    requests: list[dict[str, Any]] = [
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 86},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": width},
                "properties": {"pixelSize": 38},
                "fields": "pixelSize",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": out_start - 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {"fontSize": 9, "foregroundColor": {"red": 0.12, "green": 0.16, "blue": 0.18}},
                    }
                },
                "fields": "userEnteredFormat.horizontalAlignment,userEnteredFormat.verticalAlignment,userEnteredFormat.textFormat",
            }
        },
    ]
    for index, _name in enumerate(names):
        start_col = 1 + index * block
        end_col = start_col + block
        strong, pale = PERSON_BANDS[index % len(PERSON_BANDS)]
        if block > 1:
            requests.append(
                {
                    "mergeCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": out_start - 1,
                            "endRowIndex": out_start,
                            "startColumnIndex": start_col,
                            "endColumnIndex": end_col,
                        },
                        "mergeType": "MERGE_ALL",
                    }
                }
            )
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": out_start - 1,
                        "endRowIndex": out_start + 3,
                        "startColumnIndex": start_col,
                        "endColumnIndex": end_col,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _rgb(strong),
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                            "textFormat": {
                                "bold": True,
                                "fontSize": 9,
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            },
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
                        "startRowIndex": out_start + 3,
                        "endRowIndex": last_row,
                        "startColumnIndex": start_col,
                        "endColumnIndex": end_col,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _rgb(pale),
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                            "textFormat": {
                                "fontSize": 9,
                                "foregroundColor": {"red": 0.12, "green": 0.16, "blue": 0.18},
                            },
                        }
                    },
                    "fields": "userEnteredFormat",
                }
            }
        )
        if index < len(names) - 1:
            requests.append(
                {
                    "updateBorders": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": out_start - 1,
                            "endRowIndex": last_row,
                            "startColumnIndex": end_col - 1,
                            "endColumnIndex": end_col,
                        },
                        "right": {
                            "style": "SOLID_MEDIUM",
                            "width": 2,
                            "color": {"red": 1, "green": 1, "blue": 1},
                        },
                    }
                }
            )
    requests.append(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": min(5, last_row), "frozenColumnCount": 1},
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        }
    )
    with_retry(lambda: ws.spreadsheet.batch_update({"requests": requests}), log=log, what="设置按人分色和分隔线")


def _parse_existing_custom_header(ws, out_start: int) -> dict[str, Any] | None:
    try:
        name_row = ws.row_values(out_start)
        type_row = ws.row_values(out_start + 2)
    except Exception:
        return None
    names: list[str] = []
    starts: list[int] = []
    for index in range(1, len(name_row)):
        name = str(name_row[index] or "").strip()
        if name:
            names.append(name)
            starts.append(index)
    if not names:
        return None
    block = starts[1] - starts[0] if len(starts) >= 2 else max(1, len(type_row) - starts[0])
    categories: list[str] = []
    for offset in range(block):
        idx = starts[0] + offset
        label = str(type_row[idx] if idx < len(type_row) else "").strip()
        if label:
            categories.append(_norm_type(label))
    return {"names": names, "types": categories}


def _write_custom_report_sheet(ws, out_start: int, unit: int, records, log: LogFn, types, count_mode: str) -> int:
    preferred_names: list[str] = []
    header = _parse_existing_custom_header(ws, out_start)
    header_locked = False
    configured = list(types or [])
    if header and header.get("names"):
        preferred_names = list(header["names"])
        if header.get("types"):
            configured = list(header["types"])
        header_locked = True
        missing = sorted(
            {str(rec.get("name") or "").strip() for rec in records} - set(preferred_names) - {""}
        )
        if missing:
            log("表头已固定，未自动加列的人：" + "、".join(missing[:12]) + ("…" if len(missing) > 12 else ""))
    else:
        try:
            preferred_names = [str(value or "").strip() for value in ws.row_values(out_start) if str(value or "").strip()]
        except Exception as exc:
            log(f"读取现有姓名顺序失败：{exc}")
    names, categories, payload = _custom_report_matrix(
        records,
        unit,
        configured,
        count_mode,
        preferred_names=preferred_names,
        value_mode="count",
        lock_names=header_locked,
    )
    width = max((len(row) for row in payload), default=1)
    old_width = max(int(getattr(ws, "col_count", 1) or 1), width)
    needed_rows = out_start - 1 + len(payload) + 8
    try:
        safe_resize_ws(ws, max(needed_rows, 40), max(width, 8), log=log)
    except RuntimeError as exc:
        log(str(exc))
        try:
            ws.resize(rows=1, cols=1)
            safe_resize_ws(ws, max(needed_rows, 40), max(width, 8), log=log)
        except Exception as inner:
            raise RuntimeError("数据表单元格超过上限，请换一个空的目标表或删掉空白列后再统计。") from inner
        header_locked = False
    if header_locked and len(payload) > 4:
        body = payload[4:]
        write_start = out_start + 4
        log("表头第 1-3 行已保留（姓名和分类列不动），只刷新汇总和各日期数量")
        try:
            last_data = write_start + len(body) - 1
            if ws.row_count > last_data:
                ws.batch_clear([f"A{last_data + 1}:{index_to_col_letter(old_width - 1)}{ws.row_count}"])
        except Exception:
            pass
    else:
        body = payload
        write_start = out_start
        try:
            ws.batch_clear([f"A{out_start}:{index_to_col_letter(old_width - 1)}{max(ws.row_count, out_start)}"])
        except Exception:
            pass
        if header:
            log("表头人数或分类列有变化，已连同第 1-3 行一起重写")
        else:
            log("首次写入数据表表头（第 1-3 行），之后只按表头姓名顺序和分类列刷新数量")
    with_retry(
        lambda: ws.update(
            range_name=f"A{write_start}",
            values=body,
            value_input_option="USER_ENTERED",
        ),
        log=log,
        what="写入自定义分类数据表",
    )
    if not header_locked:
        try:
            _format_custom_person_sheet(ws, out_start, names, categories, payload, log)
            log("已按人分色、合并姓名、收紧列宽并加上分隔线")
        except Exception as exc:
            log(f"设置分类汇总样式失败，数据已写入：{exc}")
    log(f"已写入「{ws.title}」：{len(names)} 人，{len(categories)} 个分类，{max(0, len(payload) - 7)} 个日期")
    return len(names)


def run_video_duration(cfg, log: LogFn = print, cancelled=None) -> dict[str, Any]:
    src_url = (getattr(cfg, "vd_source_url", "") or "").strip()
    dest_url = (getattr(cfg, "vd_dest_url", "") or "").strip()
    src_sheet = (getattr(cfg, "vd_source_sheet", "") or "").strip()
    source_sheets = [str(x).strip() for x in (getattr(cfg, "vd_source_sheets", []) or []) if str(x).strip()]
    if not source_sheets and src_sheet:
        source_sheets = [x.strip() for x in src_sheet.replace("，", "\n").replace(",", "\n").splitlines() if x.strip()]
    if not src_url:
        raise RuntimeError("请填写视频时长源表链接")
    write_log = bool(getattr(cfg, "vd_write_log", True))
    if not dest_url:
        raise RuntimeError("请填写写入目标表格链接" + ("（日志表 + 数据表）" if write_log else "（数据表）"))
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
    required_roles = ("date", "link", "name") if write_log else ("date", "name")
    missing_roles = [role for role in required_roles if not any(item["role"] == role for item in normalized_columns)]
    if missing_roles:
        raise RuntimeError("源表列映射必须保留：" + ("日期、视频链接、名字" if write_log else "日期、名字"))
    if not type_columns:
        raise RuntimeError("请至少保留一个类型/分类列映射")
    type_filters = _cfg_types(cfg)
    exclude_types = _cfg_type_list(getattr(cfg, "vd_exclude_types", None))
    other_category = _norm_type(getattr(cfg, "vd_other_category", "") or "")
    category_mode = str(getattr(cfg, "vd_category_mode", "columns_plus_other") or "columns_plus_other").strip()
    if category_mode not in ("columns_plus_other", "other_only"):
        category_mode = "columns_plus_other"
    empty_to_other = bool(getattr(cfg, "vd_empty_to_other", True))
    if not write_log:
        sep_set = {_norm_type(x) for x in type_filters}
        ex_set = {_norm_type(x) for x in exclude_types}
        if sep_set and ex_set and sep_set == ex_set:
            log("单独成列和排除填了同一批分类，已改成：这些分类各占一列，未分类和其他归入「其余」。")
            exclude_types = []
            empty_to_other = True
            category_mode = "columns_plus_other"
        if category_mode == "other_only":
            exclude_types = list(dict.fromkeys([*_cfg_type_list(type_filters), *exclude_types]))
            type_filters = []
            if not other_category:
                other_category = "其他"
            log("统计方式：上面这些分类不计入，未分类和其他全部归入「" + other_category + "」")
    log_sheet = (getattr(cfg, "vd_log_sheet", "日志表") or "日志表").strip()
    report_sheet = (getattr(cfg, "vd_report_sheet", "数据表") or "数据表").strip()
    report_categories = _cfg_type_list(getattr(cfg, "vd_report_categories", None))
    if write_log and not report_categories and type_filters:
        report_categories = list(type_filters)
        log("数据表未单独填额外分类列，已按第 4 节添加的分类自动建列：" + "、".join(report_categories))
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
    if write_log and log_sheet == report_sheet:
        raise RuntimeError("日志表和数据表请用两个不同的工作表名称")
    try:
        for item in normalized_columns:
            col_letter_to_index(item["column"])
    except ValueError:
        raise RuntimeError("日期/链接/制作人/类型列必须填字母，例如 A、B、H、E")

    gc = authorize_cfg(cfg, log=log)
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
        link_col: list[str] = []
        if write_log:
            log(f"读取 {col_link} 列单元格超链接（不是文件名）…")
            link_col = _read_link_column(session, src_ss.id, selected_sheet, col_link, start_row, last, log=log)
            if not sample:
                sample = next((url for url in link_col if url), "")
        total_source_rows += max(0, last - start_row + 1)
        row_count = max(last - start_row + 1, len(link_col))
        for offset in range(row_count):
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
                    "url": link_col[offset] if offset < len(link_col) else "",
                    "date": _at(date_values, row_num),
                    "name": _at(name_values, row_num),
                    "type": category_values[0] if category_values else "",
                    "types": category_values,
                }
            )
    n_url = sum(1 for item in source_items if item["url"])
    if write_log:
        log(f"共 {total_source_rows} 行，其中 {n_url} 条取到超链接" + (f"，示例：{sample}" if sample else "。若为 0，说明单元格不是超链接"))
        if n_url == 0:
            raise RuntimeError(
                f"{col_link} 列没有读到超链接。表格里看到的蓝字文件名本身不是网址，"
                "需要单元格插入的链接。请确认列字母填对，且服务账号对该表有权限。"
            )
    else:
        log(f"共 {total_source_rows} 行，按分类统计条数（不读取视频链接、不写日志）")

    dest = open_by_url_or_id(gc, dest_url, log=log)
    log_headers = ["日期", "视频链接", "名字", "时长(秒)", "类型", "备注"]
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
    used_rows = 0
    if log_ws is not None:
        try:
            used_rows = len(with_retry(lambda: log_ws.col_values(1), log=log, what="读取日志占用行") or [])
        except Exception:
            used_rows = len(existing)

    if log_ws is not None and not existing:
        if include_headers:
            with_retry(
                lambda: log_ws.update(range_name="A1", values=[log_headers], value_input_option="USER_ENTERED"),
                log=log,
                what="写日志表头",
            )
            next_row = max(2, used_rows + 1)
        else:
            next_row = max(1, out_start, used_rows + 1)
    elif log_ws is not None:
        next_row = max((2 if include_headers else 1) + len(existing), used_rows + 1)
    else:
        next_row = 1
    next_tracker = [next_row]

    pending: list[dict[str, Any]] = []
    seen_src: set[str] = set()
    skipped = 0
    empty_skip = 0
    out_of_range = 0
    type_skip = 0
    log_patches: list[dict[str, Any]] = []
    for source_item in source_items:
        url = extract_url(source_item["url"]) or source_item["url"]
        date_v = source_item["date"]
        date_s = _date_key(date_v)
        name = _norm_name(source_item["name"])
        type_values = source_item.get("types") or ([source_item.get("type")] if source_item.get("type") else [])
        typ = _norm_type(source_item.get("type"))
        if write_log and not url:
            empty_skip += 1
            continue
        if not write_log and not name:
            empty_skip += 1
            continue
        if write_log:
            hay = type_values or ([typ] if typ else [])
            hit = any(_type_equals(value, pattern) for value in hay for pattern in type_filters)
            type_rejected = (
                type_filter_mode == "include" and type_filters and not hit
            ) or (
                type_filter_mode == "exclude" and type_filters and hit
            )
            if type_rejected:
                type_skip += 1
                continue
        if not _in_date_range(date_s, start_d, end_d):
            out_of_range += 1
            continue
        if not write_log:
            buckets = _custom_buckets(
                type_values, type_filters, exclude_types, other_category, empty_to_other=empty_to_other
            )
            if not buckets:
                type_skip += 1
                continue
            for bucket in buckets:
                pending.append({"date": date_s, "url": url, "name": name, "sec": 0, "key": "", "type": bucket})
            continue
        key = link_key(url)
        if not key or key in seen_src:
            continue
        seen_src.add(key)
        prev = index.get(key)
        if prev is not None and prev.get("sec") is not None:
            changed: dict[str, Any] = {}
            if date_s and date_s != (prev.get("date") or ""):
                prev["date"] = date_s
                changed["date"] = date_s
            if typ and typ != (prev.get("type") or ""):
                prev["type"] = typ
                changed["type"] = typ
            if name and name != (prev.get("name") or ""):
                prev["name"] = name
                changed["name"] = name
            if changed and prev.get("row"):
                changed["row"] = prev["row"]
                log_patches.append(changed)
            skipped += 1
            continue
        pending.append({"date": date_s, "url": url, "name": name, "sec": None, "key": key, "type": typ})

    range_label = "全部日期"
    if start_d or end_d:
        range_label = f"{start_d or '…'} ~ {end_d or '…'}"
    if type_filters:
        range_label += " · " + "、".join(type_filters)
    if write_log:
        log(
            f"{range_label}：待查询 {len(pending)}，已有时长跳过 {skipped}，"
            f"空链接 {empty_skip}，类型不符 {type_skip}，日期外 {out_of_range}"
        )
        if log_ws is not None and log_patches:
            _patch_log_fields(log_ws, log_patches, log)
    else:
        log(
            f"{range_label}：待统计 {len(pending)} 条，空名字 {empty_skip}，"
            f"分类不符 {type_skip}，日期外 {out_of_range}"
        )

    cache: dict[str, float | None] = {}
    drive_cache: dict[str, float | None] = {}
    ok = 0
    miss = 0
    appended = 0
    reason_counts: dict[str, int] = {}

    def _flush_report():
        if not write_log:
            custom_types = list(type_filters)
            if other_category and other_category not in custom_types:
                custom_types.append(other_category)
            return _write_custom_report_sheet(
                report_ws,
                out_start,
                unit,
                records,
                log,
                custom_types,
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
            report_categories,
            count_mode,
        )
        return people

    if not pending:
        people = _flush_report()
        log(f"没有新链接需要查询。已按筛选条件重写数据表，{people} 人")
        return {
            "ok": True,
            "mode": "video" if write_log else "video_custom",
            "log_rows": len(records),
            "people": people,
            "ok_duration": 0,
            "miss_duration": 0,
            "skipped": skipped,
            "target_url": dest_url,
            "log_sheet": log_sheet,
            "report_sheet": report_sheet,
        }

    if not write_log:
        # 自定义模板统计同一日期、人员和分类的记录条数，不查询视频时长，
        # 也不创建日志表。原视频模板仍走下面的时长查询与日志追加流程。
        for item in pending:
            item["sec"] = 0
            records.append(item)
        people = _flush_report()
        log(f"自定义分类汇总完成：统计 {len(records)} 条，数据表 {people} 人")
        return {
            "ok": True,
            "mode": "video_custom",
            "log_rows": 0,
            "people": people,
            "ok_duration": 0,
            "miss_duration": 0,
            "skipped": skipped,
            "appended": len(pending),
            "target_url": dest_url,
            "log_sheet": "",
            "report_sheet": report_sheet,
        }

    for bi in range(0, len(pending), batch_size):
        if cancelled and cancelled():
            raise RuntimeError("已停止")
        chunk = pending[bi : bi + batch_size]
        ids = [drive_file_id(x["url"]) for x in chunk if drive_file_id(x["url"])]
        log(f"第 {bi // batch_size + 1} 批（{len(chunk)} 条）查询时长…")
        id_reasons: dict[str, str] = {}
        drive_cache.update(_prefetch_drive_durations(session, ids, log, reasons=id_reasons))
        rows_out: list[list[Any]] = []
        miss_reasons: dict[str, str] = {}
        for item in chunk:
            sec = duration_for_link(
                session, item["url"], cache, drive_cache, reasons=miss_reasons, id_reasons=id_reasons
            )
            item["sec"] = sec
            note = ""
            if sec is None:
                miss += 1
                note = miss_reasons.get(item["url"] or "", "未识别时长")
                reason_counts[note] = reason_counts.get(note, 0) + 1
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
                    note,
                ]
            )
        if log_ws is not None:
            _append_log_rows(log_ws, next_tracker, rows_out, log)
        appended += len(rows_out)
        batch_i = bi // batch_size + 1
        log(
            f"  本批完成：累计新增 {appended}/{len(pending)}，成功 {ok}，未识别 {miss}"
        )

    people = _flush_report()
    log(f"完成：新增日志 {appended}，跳过 {skipped}，成功 {ok}，未识别 {miss}，数据表 {people} 人")
    if miss:
        parts = [f"{name} {count}" for name, count in sorted(reason_counts.items(), key=lambda item: -item[1])[:8]]
        log("未识别原因（详见日志表「备注」列）：" + ("；".join(parts) if parts else "链接不是 Drive/YouTube，或文件没有视频时长"))
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
