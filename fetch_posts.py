# -*- coding: utf-8 -*-
"""
把 Google 表格里的 LET + IMPORTRANGE + FILTER + SORT 改成 Python。

支持：
  - 多个数据源表格（字段配置相同，只是链接不同）
  - 汇总写入一张你单独指定的目标表
  - 命令行 或 本地网页界面（python app.py）
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, date, time as dt_time
from pathlib import Path
from typing import Any, Callable

_CONFIG_WRITE_LOCK = threading.Lock()

def resource_dir() -> Path:
    """PyInstaller 打包后的只读资源目录（网页界面）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """配置、状态、密钥所在目录（exe 旁边，可写）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


SCRIPT_DIR = data_dir()
RESOURCE_DIR = resource_dir()
STATE_FILE = SCRIPT_DIR / "sync_state.json"
LOG_DIR = SCRIPT_DIR / "logs"


def write_app_log(message: str, extra: str = "") -> Path:
    """Append a line to today's log file for later troubleshooting."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"app-{datetime.now().strftime('%Y-%m-%d')}.log"
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}"
    if extra:
        line += "\n" + extra.rstrip() + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return path
SHEETS_EPOCH = datetime(1899, 12, 30)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
URL_RE = re.compile(r"https?://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9-_]+)")
ID_RE = re.compile(r"^[a-zA-Z0-9-_]{20,}$")
COL_ROW_RE = re.compile(r"^([A-Za-z]+)(\d+)$")
WRITE_CHUNK = 4000
RETRY_TIMES = 5

LogFn = Callable[[str], None]


def is_transient_error(err: Exception) -> bool:
    text = str(err).lower()
    markers = (
        "connection aborted",
        "remotedisconnected",
        "remote end closed",
        "timed out",
        "timeout",
        "connectionreset",
        "connection reset",
        "temporarily unavailable",
        "ssl",
        "429",
        "502",
        "503",
        "504",
        "internal error",
        "backend error",
        "rate limit",
        "quota",
    )
    return any(m in text for m in markers)


def is_quota_error(err: Exception) -> bool:
    text = str(err).lower()
    return "429" in text or "quota" in text or "rate limit" in text


MAX_WORKBOOK_CELLS = 10_000_000
CELL_SAFETY = 200_000


def used_column_count(rows: list[list[Any]]) -> int:
    width = 0
    for row in rows or []:
        last = len(row)
        while last > 0 and str(row[last - 1] if last - 1 < len(row) else "").strip() == "":
            last -= 1
        if last > width:
            width = last
    return width


def drop_empty_rows(rows: list[list[Any]]) -> list[list[Any]]:
    """Skip completely blank rows."""
    out: list[list[Any]] = []
    for row in rows or []:
        if any(str(cell).strip() for cell in row):
            out.append(list(row))
    return out


def trim_trailing_empty_columns(rows: list[list[Any]]) -> list[list[Any]]:
    """Drop empty trailing columns so a 1000-column grid does not explode the 10M cell cap."""
    width = used_column_count(rows)
    if width <= 0:
        return []
    out: list[list[Any]] = []
    for row in rows:
        clipped = list(row[:width])
        if len(clipped) < width:
            clipped.extend([""] * (width - len(clipped)))
        out.append(clipped)
    return out


def compact_sheet_rows(rows: list[list[Any]]) -> list[list[Any]]:
    """Keep only rows/columns that actually have values; width follows the last used column."""
    return drop_empty_rows(trim_trailing_empty_columns(rows))


def workbook_cell_count(ss) -> int:
    total = 0
    for ws in ss.worksheets():
        total += max(1, int(getattr(ws, "row_count", 0) or 0)) * max(1, int(getattr(ws, "col_count", 0) or 0))
    return total


def safe_resize_ws(ws, rows: int, cols: int, log: LogFn = print) -> None:
    """Fit a sheet to rows×cols without crossing Google's 10 million cell workbook limit.

    Extra blank columns on an old target sheet are the usual cause of
    `This action would increase the number of cells... 10000000`.
    Shrink first, then grow to the exact needed size.
    """
    rows = max(1, int(rows))
    cols = max(1, int(cols))
    current_rows = max(1, int(getattr(ws, "row_count", 1) or 1))
    current_cols = max(1, int(getattr(ws, "col_count", 1) or 1))
    if current_rows == rows and current_cols == cols:
        return

    def _resize(next_rows: int, next_cols: int, what: str) -> None:
        with_retry(lambda r=next_rows, c=next_cols: ws.resize(rows=r, cols=c), log=log, what=what)

    # Free unused cells before any growth.
    if current_cols > cols or current_rows > rows:
        _resize(min(current_rows, max(rows, 1)), min(current_cols, max(cols, 1)), "缩小空白行列")
        current_rows = max(1, int(getattr(ws, "row_count", 1) or 1))
        current_cols = max(1, int(getattr(ws, "col_count", 1) or 1))

    if current_rows >= rows and current_cols >= cols:
        if current_rows != rows or current_cols != cols:
            _resize(rows, cols, "调整工作表大小")
        return

    ss = getattr(ws, "spreadsheet", None)
    other = 0
    if ss is not None:
        try:
            other = workbook_cell_count(ss) - current_rows * current_cols
        except Exception:
            other = 0
    room = MAX_WORKBOOK_CELLS - CELL_SAFETY - max(0, other)
    needed = rows * cols
    if needed > room:
        raise RuntimeError(
            f"目标表将超过 Google 表格 1000 万单元格上限（需要 {rows}×{cols}={needed}，"
            f"工作簿里其他工作表大约已占 {max(0, other)} 格）。"
            "请换一个空表格当目标，或删掉目标文件里不用的工作表和空白列后再汇总。"
        )
    try:
        _resize(rows, cols, "扩大工作表")
    except Exception as exc:
        text = str(exc)
        if "10000000" in text or "number of cells" in text.lower():
            raise RuntimeError(
                "目标表单元格超过 Google 1000 万上限。请换空表格，或先删掉目标文件里过大的工作表。"
            ) from exc
        raise


def with_retry(fn, log: LogFn = print, what: str = "请求", tries: int = RETRY_TIMES):
    last: Exception | None = None
    pool = current_credential_pool()
    n = max(tries, 8)
    if pool is not None and len(pool.paths) > 1:
        n = max(n, len(pool.paths) * 4)
    for i in range(n):
        try:
            result = fn()
            if pool is not None:
                pool.note_success()
            return result
        except Exception as e:
            last = e
            quota = is_quota_error(e)
            if (not is_transient_error(e) and not quota) or i >= n - 1:
                raise
            if quota and pool is not None and pool.rotate(f"{what}额度用完"):
                log(f"  {what}已换账号，立即重试（{i + 1}/{n}）…")
                continue
            if quota:
                wait = min(120.0, 65.0 + i * 10.0) + random.random() * 8
                log(f"  {what}额度用完，{wait:.0f} 秒后重试（{i + 1}/{n}）…")
            else:
                wait = min(40, (2 ** i) + random.random() * 2)
                log(f"  {what}连接中断，{wait:.0f} 秒后重试（{i + 1}/{n}）… {e}")
            time.sleep(wait)
    raise last  # pragma: no cover

# 截图「数据库」里的字段范围：所有源表表头相同，只换链接。
DEFAULT_SOURCE_SHEET = "当月贴文库"
DEFAULT_FIELDS: list[dict] = [
    {"name": "名字", "sheet": DEFAULT_SOURCE_SHEET, "range": "AB2:AB"},
    {"name": "帖文id", "sheet": DEFAULT_SOURCE_SHEET, "range": "A2:A"},
    {"name": "FB链接", "sheet": DEFAULT_SOURCE_SHEET, "range": "H2:H"},
    {"name": "引流", "sheet": DEFAULT_SOURCE_SHEET, "range": "C2:C"},
    {"name": "缩略图链接", "sheet": DEFAULT_SOURCE_SHEET, "range": "T2:T"},
    {"name": "帖文内容", "sheet": DEFAULT_SOURCE_SHEET, "range": "F2:F"},
    {"name": "改贴", "sheet": DEFAULT_SOURCE_SHEET, "range": "S2:S"},
    {"name": "发布日期", "sheet": DEFAULT_SOURCE_SHEET, "range": "AA2:AA"},
    {"name": "图片类型", "sheet": DEFAULT_SOURCE_SHEET, "range": "W2:W"},
    {"name": "点赞", "sheet": DEFAULT_SOURCE_SHEET, "range": "K2:K"},
    {"name": "评论", "sheet": DEFAULT_SOURCE_SHEET, "range": "L2:L"},
    {"name": "分享", "sheet": DEFAULT_SOURCE_SHEET, "range": "M2:M"},
    {"name": "帖文类型", "sheet": DEFAULT_SOURCE_SHEET, "range": "N2:N"},
    {"name": "OCR", "sheet": DEFAULT_SOURCE_SHEET, "range": "Q2:Q"},
    {"name": "OCR翻译", "sheet": DEFAULT_SOURCE_SHEET, "range": "R2:R"},
    {"name": "音频文本", "sheet": DEFAULT_SOURCE_SHEET, "range": "U2:U"},
    {"name": "音频文本翻译", "sheet": DEFAULT_SOURCE_SHEET, "range": "V2:V"},
    {"name": "专页id", "sheet": DEFAULT_SOURCE_SHEET, "range": "P2:P"},
]


def copy_default_fields() -> list[dict]:
    return [dict(x) for x in DEFAULT_FIELDS]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class Config:
    credentials_file: str = ""
    credentials_files: list[str] = field(default_factory=list)
    # 含「数据库」sheet 的表格（可选，一般不用）
    config_url: str = ""
    # 汇总写入的目标表
    target_url: str = ""
    # 多个数据源：[{name: 小组, url: 链接}]
    sources: list[dict] = field(default_factory=list)
    # 抓取字段（界面可改）。空则用截图默认。
    fields: list[dict] = field(default_factory=copy_default_fields)
    # 兼容旧字段
    source_urls: list[str] = field(default_factory=list)
    # 兼容旧字段：单表模式
    spreadsheet_id: str = ""
    database_sheet: str = "数据库"
    date_sheet: str = ""
    date_start_cell: str = "A1"
    date_end_cell: str = "B1"
    output_sheet: str = "筛选结果"
    output_start_row: int = 1
    include_headers: bool = True
    hot_start_row: int = 1
    hot_include_headers: bool = True
    add_source_column: bool = True
    group_column: str = "AC"
    upsert_by_id: bool = True
    exclude_id_value: str = "未找到"
    date_field: str = "发布日期"
    id_field: str = "帖文id"
    sort_field: str = "点赞"
    sort_descending: bool = True
    start_date: str = ""
    end_date: str = ""
    date_col_1based: int = 8
    id_col_1based: int = 2
    sort_col_1based: int = 10
    likes_threshold: int = 1000
    write_all: bool = True
    write_hot: bool = True
    hot_target_url: str = ""
    hot_output_sheet: str = "点赞1000以上"
    schedule_enabled: bool = False
    schedule_minutes: int = 60
    schedule_only_if_changed: bool = True
    cf_publish_url: str = "https://promo.zhixianglife.com/api/publish-cache"
    cf_publish_secret: str = ""
    cf_publish_after_sync: bool = False
    cf_publish_source: str = "all"  # all | hot
    cf_chunk_size: int = 800
    align_schedule_enabled: bool = False
    align_schedule_minutes: int = 60
    align_schedule_only_if_changed: bool = True
    # 表头对齐同步（第二个 Tab）
    align_sources: list[dict] = field(default_factory=list)
    align_target_url: str = ""
    align_output_sheet: str = "对齐结果"
    align_start_row: int = 1
    align_include_headers: bool = True
    align_source_sheet: str = ""
    align_header_row: int = 1
    align_headers: list[str] = field(default_factory=list)
    # 视频时长：源表 B 列链接 → 日志表 + 数据表
    vd_source_url: str = ""
    vd_source_sheet: str = ""
    vd_start_row: int = 2
    vd_col_date: str = "A"
    vd_col_link: str = "B"
    vd_col_name: str = "H"
    vd_col_type: str = "E"
    vd_types: list[str] = field(default_factory=list)
    vd_dest_url: str = ""
    vd_log_sheet: str = "日志表"
    vd_report_sheet: str = "数据表"
    vd_out_start_row: int = 1
    vd_include_headers: bool = True
    vd_unit_seconds: int = 30
    vd_count_mode: str = "divide_total"  # divide_total | per_video_ceil
    vd_start_date: str = ""
    vd_end_date: str = ""
    vd_batch_size: int = 100
    vd_schedule_enabled: bool = False
    vd_schedule_minutes: int = 180
    # 左侧工作区菜单。每个菜单实例保存独立模板配置，旧配置仍保留兼容。
    ui_menus: list[dict] = field(default_factory=list)
    ui_active_menu: str = "filter-default"
    # 目录表驱动汇总：索引表中一列是链接，另一列是工作表名称。
    catalog_index_url: str = ""
    catalog_index_sheet: str = ""
    catalog_start_row: int = 2
    catalog_url_col: str = "B"
    catalog_sheet_col: str = "D"
    catalog_target_url: str = ""
    catalog_output_sheet: str = "目录汇总"
    catalog_output_start_row: int = 1
    catalog_keep_each_header: bool = False
    catalog_schedule_enabled: bool = False
    catalog_schedule_minutes: int = 180
    # 队别专页 / 引流对照：配置表字段映射后，按队别分 sheet 写入。
    roster_config_url: str = ""
    roster_config_sheet: str = ""
    roster_start_row: int = 2
    roster_target_url: str = ""
    roster_traffic_sheet: str = "引流"
    roster_date_start_row: int = 24
    roster_columns: list[dict] = field(
        default_factory=lambda: [
            {"field": "队别", "role": "team", "column": "A"},
            {"field": "类型", "role": "type", "column": "B"},
            {"field": "名字", "role": "name", "column": "C"},
            {"field": "专页名字", "role": "page_name", "column": "G"},
            {"field": "专页编码", "role": "page_code", "column": "H"},
            {"field": "专页链接", "role": "page_link", "column": "I"},
            {"field": "chat", "role": "chat", "column": "K"},
            {"field": "数据表格", "role": "data_url", "column": "R"},
        ]
    )
    # 表头对齐支持“目标字段 <- 源字段”及按源链接覆盖。
    align_mappings: list[dict] = field(default_factory=list)
    align_mapping_profiles: dict[str, list[dict]] = field(default_factory=dict)
    # 原“视频提取时长”保留日志表；自定义数据汇总模板会显式设为 False。
    vd_source_sheets: list[str] = field(default_factory=list)
    vd_date_filter_enabled: bool = True
    vd_type_filter_mode: str = "include"  # include | exclude | all
    vd_write_log: bool = True
    vd_other_category: str = ""
    vd_exclude_types: list[str] = field(default_factory=list)
    # columns_plus_other: 单独成列 + 其余（含未分类）
    # other_only: 列出的分类不统计，其余含未分类全部归入
    vd_category_mode: str = "columns_plus_other"
    vd_empty_to_other: bool = True
    # 只影响数据表多出来的分类列，不影响第 4 节的筛选分类。
    vd_report_categories: list[str] = field(default_factory=list)
    vd_columns: list[dict] = field(
        default_factory=lambda: [
            {"field": "日期", "role": "date", "column": "A"},
            {"field": "视频链接", "role": "link", "column": "B"},
            {"field": "名字", "role": "name", "column": "H"},
            {"field": "类型", "role": "type", "column": "E"},
        ]
    )

    def resolve_credentials(self) -> Path:
        candidates = []
        if self.credentials_file:
            candidates.append(Path(self.credentials_file))
        candidates.extend(
            [
                SCRIPT_DIR / "credentials.json",
                Path.home() / "inspiration-finder" / "credentials.json",
            ]
        )
        for p in candidates:
            if p.exists():
                return p
        raise FileNotFoundError(
            "找不到服务账号 credentials.json。\n"
            "请在软件顶部点击「选择服务账号」，从电脑中选择 JSON 文件。"
        )


def config_to_dict(cfg: Config) -> dict[str, Any]:
    return asdict(cfg)


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    if path is None:
        for name in ("config.json", "config.example.json"):
            candidate = SCRIPT_DIR / name
            if candidate.exists():
                path = candidate
                break
    if path and path.exists():
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        for k, v in raw.items():
            if not hasattr(cfg, k) or v is None:
                continue
            setattr(cfg, k, v)
    if not cfg.fields:
        cfg.fields = copy_default_fields()
    return cfg


def save_config(cfg: Config, path: Path | None = None) -> Path:
    path = path or (SCRIPT_DIR / "config.json")
    with _CONFIG_WRITE_LOCK:
        path.write_text(
            json.dumps(config_to_dict(cfg), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return path


def service_account_email(cred_path: Path) -> str:
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8-sig"))
        return str(data.get("client_email") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def col_letter_to_index(letter: str) -> int:
    n = 0
    for ch in letter.strip().upper():
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"非法列字母: {letter}")
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def index_to_col_letter(idx: int) -> str:
    idx += 1
    out = []
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out.append(chr(65 + rem))
    return "".join(reversed(out))


def extract_spreadsheet_id(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    m = URL_RE.search(text)
    if m:
        return m.group(1)
    if ID_RE.match(text) and " " not in text:
        return text
    return None


def spreadsheet_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


@dataclass
class SourceRef:
    url: str
    name: str = ""
    sid: str = ""
    sheet: str = ""


def parse_url_list(text: str | list[str] | None) -> list[str]:
    if not text:
        return []
    if isinstance(text, list):
        lines = text
    else:
        lines = re.split(r"[\r\n]+", text)
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        sid = extract_spreadsheet_id(raw)
        if not sid:
            continue
        if sid in seen:
            continue
        seen.add(sid)
        out.append(spreadsheet_url(sid) if "http" not in raw else raw.split()[0])
    return out


def normalize_sources(raw: Any) -> list[SourceRef]:
    """把界面表格 / 旧版 url 列表 / 数据库 sheet 读到的内容统一成 SourceRef。"""
    items: list[Any]
    if raw is None or raw == "":
        items = []
    elif isinstance(raw, str):
        items = parse_url_list(raw)
    elif isinstance(raw, list):
        items = raw
    else:
        items = [raw]

    seen: set[tuple[str, str]] = set()
    out: list[SourceRef] = []
    for item in items:
        name = ""
        url = ""
        sheet = ""
        if isinstance(item, SourceRef):
            name, url, sheet = item.name, item.url, item.sheet
        elif isinstance(item, str):
            url = item
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("小组") or "").strip()
            url = str(item.get("url") or item.get("链接") or item.get("href") or "").strip()
            sheet = str(
                item.get("sheet")
                or item.get("工作表")
                or item.get("工作表名称")
                or ""
            ).strip()
        sid = extract_spreadsheet_id(url)
        if not sid:
            continue
        key = (sid, sheet)
        if key in seen:
            continue
        seen.add(key)
        pretty = url.split()[0] if "http" in url else spreadsheet_url(sid)
        out.append(SourceRef(url=pretty, name=name, sid=sid, sheet=sheet))
    return out


def parse_range_spec(spec: str, default_sheet: str | None = None) -> tuple[str, str, int]:
    text = (spec or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("空的范围")
    sheet = default_sheet
    if "!" in text:
        sheet_part, cell_part = text.split("!", 1)
        sheet = sheet_part.strip().strip("'")
    else:
        cell_part = text
    start = cell_part.split(":", 1)[0].strip()
    m = COL_ROW_RE.match(start)
    if not m:
        raise ValueError(f"无法解析范围: {spec}")
    if not sheet:
        raise ValueError(f"范围缺少工作表名: {spec}")
    return sheet, m.group(1).upper(), int(m.group(2))


# Google 表格日期序列大约在这个区间（约 1900–2173 年）。
# 帖文 id / 专页 id 会远大于此，绝不能拿去 timedelta。
_SHEETS_SERIAL_MIN = 1.0
_SHEETS_SERIAL_MAX = 100000.0
_JS_SAFE_INT = 2**53


def _parse_ymd(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def to_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, dt_time.min)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if _SHEETS_SERIAL_MIN <= n <= _SHEETS_SERIAL_MAX:
            try:
                return SHEETS_EPOCH + timedelta(days=n)
            except (OverflowError, ValueError, OSError):
                return None
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    if not text:
        return None
    if re.fullmatch(r"\d{4,5}(?:\.\d+)?", text):
        try:
            n = float(text)
        except ValueError:
            n = -1
        if _SHEETS_SERIAL_MIN <= n <= _SHEETS_SERIAL_MAX:
            try:
                return SHEETS_EPOCH + timedelta(days=n)
            except (OverflowError, ValueError, OSError):
                pass
    text = text.replace("年", "-").replace("月", "-").replace("日", "")
    text = text.replace(".", "-").replace("/", "-")
    text = re.sub(r"\s+", " ", text).strip(" -")
    time_part = r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
    m = re.match(rf"^(\d{{4}})-(\d{{1,2}})-(\d{{1,2}}){time_part}", text)
    if m:
        return _parse_ymd(
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4) or 0),
            int(m.group(5) or 0),
            int(m.group(6) or 0),
        )
    # 表格读回来经常是 8/23/2026、23/8/2026，转成 8-23-2026 后再拆。
    m = re.match(rf"^(\d{{1,2}})-(\d{{1,2}})-(\d{{4}}){time_part}", text)
    if m:
        first, second, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour, minute, second_n = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
        if first > 12:
            day, month = first, second
        elif second > 12:
            month, day = first, second
        else:
            month, day = first, second
        return _parse_ymd(year, month, day, hour, minute, second_n)
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    return None


def to_number(value: Any) -> float:
    if value is None or value == "":
        return float("-inf")
    if isinstance(value, bool):
        return float("-inf")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return float("-inf")
    try:
        return float(text)
    except ValueError:
        return float("-inf")


def cell_for_sheets(value: Any, is_date: bool = False) -> Any:
    """写回表格。大整数（帖文id）保持文本，只有日期列才按序列号转日期。"""
    if is_date:
        dt = to_datetime(value)
        if dt is not None:
            if dt.hour or dt.minute or dt.second:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d")
        return value if value is not None else ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) >= _JS_SAFE_INT:
        return str(value)
    if value is None:
        return ""
    return value


# ---------------------------------------------------------------------------
# 数据库 sheet
# ---------------------------------------------------------------------------

@dataclass
class FieldMap:
    name: str
    sheet: str
    col_letter: str
    start_row: int
    source_row: int = 0

    @property
    def range_spec(self) -> str:
        return f"{self.col_letter}{self.start_row}:{self.col_letter}"

    def as_dict(self) -> dict:
        return {"name": self.name, "sheet": self.sheet, "range": self.range_spec}


def field_from_dict(item: Any) -> FieldMap | None:
    if isinstance(item, FieldMap):
        return item
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    sheet = str(item.get("sheet") or DEFAULT_SOURCE_SHEET).strip() or DEFAULT_SOURCE_SHEET
    rng = str(item.get("range") or item.get("col_range") or "").strip()
    if not rng:
        col = str(item.get("col_letter") or item.get("col") or "").strip()
        start = item.get("start_row") or 2
        if not col:
            return None
        rng = f"{col}{start}:{col}"
    spec = rng if "!" in rng else f"{sheet}!{rng}"
    try:
        sheet, col, start_row = parse_range_spec(spec, default_sheet=sheet)
    except ValueError:
        return None
    return FieldMap(name=name, sheet=sheet, col_letter=col, start_row=start_row)


def resolve_fields(cfg: Config) -> list[FieldMap]:
    items = cfg.fields if cfg.fields else DEFAULT_FIELDS
    out: list[FieldMap] = []
    for item in items:
        fm = field_from_dict(item)
        if fm:
            out.append(fm)
    if not out:
        raise RuntimeError("字段配置为空，请在界面「抓取字段」里至少保留一列")
    return out


@dataclass
class DatabaseConfig:
    source_spreadsheet_id: str
    source_urls: list[str] = field(default_factory=list)
    sources: list[SourceRef] = field(default_factory=list)
    fields: list[FieldMap] = field(default_factory=list)


LINK_HEADERS = {"链接", "表格链接", "源表链接", "数据源链接", "数据源", "url", "URL"}


def collect_sources_from_sheet(rows: list[list[Any]]) -> list[SourceRef]:
    """
    从「数据库」收集数据源：
      - A2（以及任意格子）里的表格链接
      - 若有「小组」列，且右侧有链接列，则做成 小组+链接
    字段映射区 A–D 不会当成小组名。
    """
    group_col = None
    url_col = None
    header_row = 0
    for r_idx, r in enumerate(rows[:6]):
        for c_idx, cell in enumerate(r):
            t = str(cell or "").strip()
            if t == "小组":
                group_col = c_idx
                header_row = r_idx
            if t in LINK_HEADERS:
                url_col = c_idx
                header_row = r_idx

    if group_col is not None and url_col is None:
        for r in rows[header_row + 1 :]:
            for c in range(group_col + 1, len(r)):
                if extract_spreadsheet_id(str(r[c] if c < len(r) else "")):
                    url_col = c
                    break
            if url_col is not None:
                break

    tagged: list[SourceRef] = []
    if group_col is not None and url_col is not None:
        for r in rows[header_row + 1 :]:
            name = str(r[group_col] if group_col < len(r) else "").strip()
            url = str(r[url_col] if url_col < len(r) else "").strip()
            sid = extract_spreadsheet_id(url)
            if not sid:
                continue
            if name in LINK_HEADERS or name == "小组":
                continue
            tagged.append(SourceRef(url=spreadsheet_url(sid), name=name, sid=sid))

    leftovers: list[SourceRef] = []
    seen = {s.sid for s in tagged}
    for r in rows:
        for c_idx, cell in enumerate(r):
            sid = extract_spreadsheet_id(str(cell) if cell else "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            leftovers.append(SourceRef(url=spreadsheet_url(sid), name="", sid=sid))

    return tagged + leftovers


def collect_sheet_urls(rows: list[list[Any]]) -> list[str]:
    return [s.url for s in collect_sources_from_sheet(rows)]


def read_database_sheet(ws) -> DatabaseConfig:
    """
    读字段映射，并收集表里出现的全部数据源链接。
    截图布局：A2 = 源表链接，A 列字段名，B 列工作表，C 列范围，D 列完整引用。
    多个源可以把更多链接贴在同一张「数据库」任意空格（建议 F 列）。
    """
    rows = ws.get_all_values()
    if not rows:
        raise RuntimeError("「数据库」工作表是空的")

    collected = collect_sources_from_sheet(rows)
    source_urls = [s.url for s in collected]
    source_id = collected[0].sid if collected else ""

    fields: list[FieldMap] = []
    for i, r in enumerate(rows):
        row_num = i + 1
        name = (r[0] if len(r) > 0 else "").strip()
        sheet_name = (r[1] if len(r) > 1 else "").strip()
        col_range = (r[2] if len(r) > 2 else "").strip()
        full_range = (r[3] if len(r) > 3 else "").strip()
        if not name or extract_spreadsheet_id(name):
            continue
        spec = full_range or (
            f"{sheet_name}!{col_range}" if sheet_name and col_range else col_range
        )
        try:
            sheet, col, start_row = parse_range_spec(spec, default_sheet=sheet_name or None)
        except ValueError:
            continue
        fields.append(
            FieldMap(
                name=name,
                sheet=sheet,
                col_letter=col,
                start_row=start_row,
                source_row=row_num,
            )
        )

    if not fields:
        raise RuntimeError("「数据库」里没有解析到任何字段范围（请检查 B/C/D 列）")
    return DatabaseConfig(
        source_spreadsheet_id=source_id or "",
        source_urls=source_urls,
        sources=collected,
        fields=fields,
    )


def find_field_index(
    fields: list[FieldMap], name: str, fallback_1based: int, log: LogFn = print
) -> int:
    for i, f in enumerate(fields):
        if f.name.strip() == name:
            return i
    idx = fallback_1based - 1
    if idx < 0 or idx >= len(fields):
        raise RuntimeError(
            f"找不到字段「{name}」，且备用列号 {fallback_1based} 超出范围（共 {len(fields)} 列）"
        )
    log(f"未找到字段名「{name}」，改用第 {fallback_1based} 列「{fields[idx].name}」")
    return idx


# ---------------------------------------------------------------------------
# 拉数 / 过滤 / 排序
# ---------------------------------------------------------------------------

def authorize(cred_path: Path):
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(str(cred_path), scopes=SCOPES)
    return gspread.authorize(creds)


_pool_local = threading.local()


def current_credential_pool():
    return getattr(_pool_local, "pool", None)


def collect_credential_paths(cfg) -> list[Path]:
    """All configured service-account JSON files that exist on disk, first file first."""
    seen: set[str] = set()
    out: list[Path] = []
    raw: list[str] = []
    if getattr(cfg, "credentials_file", ""):
        raw.append(str(cfg.credentials_file).strip())
    for item in getattr(cfg, "credentials_files", None) or []:
        raw.append(str(item or "").strip())
    for text in raw:
        if not text:
            continue
        path = Path(text)
        if not path.is_absolute():
            path = SCRIPT_DIR / path
        try:
            key = str(path.resolve()) if path.exists() else str(path)
        except Exception:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            out.append(path)
    if out:
        return out
    return [cfg.resolve_credentials()]


class CredentialPool:
    """Round-robin multiple service accounts; swap immediately on 429 instead of waiting a minute."""

    def __init__(self, paths: list[Path], log: LogFn = print):
        self.paths = [Path(p) for p in paths]
        if not self.paths:
            raise FileNotFoundError("没有可用的服务账号 JSON")
        self.log = log
        self.index = 0
        self.gc = None
        self._hits = 0
        self._cooldown: dict[int, float] = {}

    def email(self, index: int | None = None) -> str:
        path = self.paths[self.index if index is None else index]
        return service_account_email(path) or path.name

    def _load_creds(self, path: Path):
        from google.oauth2.service_account import Credentials

        return Credentials.from_service_account_file(str(path), scopes=SCOPES)

    def _bind(self, gc, creds) -> None:
        gc.auth = creds
        hc = getattr(gc, "http_client", None)
        session = getattr(hc, "session", None) if hc is not None else None
        if session is not None and hasattr(session, "credentials"):
            session.credentials = creds
            return
        try:
            import google.auth.transport.requests as transport

            new_session = transport.AuthorizedSession(creds)
            if hc is not None:
                hc.session = new_session
                if hasattr(hc, "auth"):
                    hc.auth = creds
        except Exception:
            pass

    def activate(self):
        self.gc = authorize(self.paths[self.index])
        self._bind(self.gc, self._load_creds(self.paths[self.index]))
        _pool_local.pool = self
        extra = f"，共 {len(self.paths)} 个账号轮询" if len(self.paths) > 1 else ""
        self.log(f"服务账号: {self.email()}{extra}")
        return self.gc

    def rotate(self, reason: str = "额度用完") -> bool:
        if len(self.paths) < 2:
            return False
        self._cooldown[self.index] = time.time() + 55
        old = self.email()
        now = time.time()
        for step in range(1, len(self.paths) + 1):
            nxt = (self.index + step) % len(self.paths)
            if now < self._cooldown.get(nxt, 0) and step < len(self.paths):
                continue
            try:
                creds = self._load_creds(self.paths[nxt])
            except Exception as exc:
                self.log(f"服务账号 {self.paths[nxt].name} 无法加载：{exc}")
                continue
            self.index = nxt
            if self.gc is not None:
                self._bind(self.gc, creds)
            self._hits = 0
            self.log(f"{reason}，已切换服务账号 {old} → {self.email()}")
            return True
        return False

    def note_success(self) -> None:
        if len(self.paths) < 2:
            return
        self._hits += 1
        if self._hits >= 45:
            self.rotate("分散请求以免单账号额度用尽")


def authorize_cfg(cfg, log: LogFn = print):
    """Authorize using the credential pool so quota errors can rotate accounts."""
    return CredentialPool(collect_credential_paths(cfg), log=log).activate()


def load_sync_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"sources": {}, "last_run": ""}


def save_sync_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def remember_cf_fingerprint(fingerprint: str, total_rows: int = 0) -> None:
    state = load_sync_state()
    state["cf_fingerprint"] = fingerprint
    state["cf_total_rows"] = int(total_rows or 0)
    state["cf_published_at"] = datetime.now().isoformat(timespec="seconds")
    save_sync_state(state)


def fetch_modified_time(gc, spreadsheet_id: str) -> str:
    creds = getattr(gc, "auth", None)
    if creds is None:
        return ""
    try:
        import google.auth.transport.requests

        session = google.auth.transport.requests.AuthorizedSession(creds)
        r = session.get(
            f"https://www.googleapis.com/drive/v3/files/{spreadsheet_id}",
            params={"fields": "modifiedTime", "supportsAllDrives": "true"},
            timeout=30,
        )
        if r.status_code == 200:
            return str(r.json().get("modifiedTime") or "")
    except Exception:
        return ""
    return ""


def source_mtimes(gc, source_ids: list[str]) -> dict[str, str]:
    return {sid: fetch_modified_time(gc, sid) for sid in source_ids}


def sources_have_changed(gc, source_ids: list[str], log: LogFn = print) -> bool:
    prev = (load_sync_state().get("sources") or {})
    now = source_mtimes(gc, source_ids)
    if not prev:
        log("首次同步（无历史记录），将执行")
        return True
    for sid in source_ids:
        old, new = prev.get(sid) or "", now.get(sid) or ""
        # Drive modifiedTime 偶尔会因权限或网络问题取不到。此时不能把空值
        # 当成“没有变化”，否则定时任务会永久跳过实际需要同步的数据。
        if not new:
            log(f"无法确认源表更新时间，将安全执行: {sid}")
            return True
        if old != new:
            log(f"源表有变化: {sid}")
            return True
    log("源表未变化，跳过本次")
    return False


def remember_run_state(gc, source_ids: list[str]) -> None:
    state = load_sync_state()
    state["sources"] = source_mtimes(gc, source_ids)
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_sync_state(state)


def open_by_url_or_id(gc, url_or_id: str, log: LogFn = print):
    import gspread

    sid = extract_spreadsheet_id(url_or_id)
    if not sid:
        raise RuntimeError(f"无法从链接解析表格 ID：{url_or_id}")
    try:
        return with_retry(lambda: gc.open_by_key(sid), log=log, what=f"打开表格 {sid}")
    except gspread.exceptions.APIError as e:
        if is_transient_error(e):
            raise
        raise RuntimeError(
            f"打不开表格 {sid}。请确认链接正确，并且已把该表共享给服务账号（编辑者）。"
        ) from e
    except gspread.exceptions.SpreadsheetNotFound as e:
        raise RuntimeError(
            f"找不到表格 {sid}。请把该表共享给服务账号。"
        ) from e


def read_sheet_values(ws, log: LogFn = print) -> list[list[Any]]:
    return with_retry(
        lambda: ws.get_all_values(value_render_option="UNFORMATTED_VALUE"),
        log=log,
        what=f"读取「{getattr(ws, 'title', '工作表')}」",
    )


def build_datasource_from_ss(
    source_ss, fields: list[FieldMap], log: LogFn = print
) -> list[list[Any]]:
    import gspread

    sheet_cache: dict[str, list[list[Any]]] = {}
    columns: list[list[Any]] = []

    for f in fields:
        if f.sheet not in sheet_cache:
            try:
                src_ws = source_ss.worksheet(f.sheet)
            except gspread.exceptions.WorksheetNotFound as e:
                raise RuntimeError(
                    f"「{source_ss.title}」里找不到工作表「{f.sheet}」"
                ) from e
            log(f"  读取「{source_ss.title}」/「{f.sheet}」")
            sheet_cache[f.sheet] = read_sheet_values(src_ws, log=log)

        all_rows = sheet_cache[f.sheet]
        col_idx = col_letter_to_index(f.col_letter)
        start = f.start_row - 1
        col_vals: list[Any] = []
        for r in all_rows[start:]:
            col_vals.append(r[col_idx] if col_idx < len(r) else "")
        columns.append(col_vals)

    max_len = max((len(c) for c in columns), default=0)
    last_nonempty = -1
    for r in range(max_len):
        if any(
            (columns[c][r] not in ("", None) if r < len(columns[c]) else False)
            for c in range(len(columns))
        ):
            last_nonempty = r
    max_len = last_nonempty + 1
    aligned = []
    for col in columns:
        padded = list(col[:max_len]) + [""] * (max_len - len(col))
        aligned.append(padded)

    if not aligned or max_len == 0:
        return []
    return [list(row) for row in zip(*aligned)]


def filter_and_sort(
    rows: list[list[Any]],
    date_idx: int,
    id_idx: int,
    sort_idx: int,
    start: datetime,
    end: datetime,
    exclude_id: str,
    descending: bool,
    log: LogFn = print,
) -> tuple[list[list[Any]], dict[str, int]]:
    end_limit = end + timedelta(days=1)
    kept: list[list[Any]] = []
    stats = {"kept": 0, "skip_date": 0, "skip_empty_date": 0, "skip_id": 0}

    for row in rows:
        id_val = "" if id_idx >= len(row) else row[id_idx]
        if str(id_val).strip() == exclude_id:
            stats["skip_id"] += 1
            continue
        dt = to_datetime(row[date_idx] if date_idx < len(row) else None)
        if dt is None:
            stats["skip_empty_date"] += 1
            continue
        if dt < start or dt > end_limit:
            stats["skip_date"] += 1
            continue
        kept.append(row)

    sort_key_idx = sort_idx
    kept.sort(
        key=lambda r: to_number(r[sort_key_idx] if sort_key_idx < len(r) else None),
        reverse=descending,
    )
    stats["kept"] = len(kept)
    log(
        f"  过滤：保留 {stats['kept']} 行，"
        f"日期不符 {stats['skip_date']}，"
        f"日期为空 {stats['skip_empty_date']}，"
        f"id={exclude_id} {stats['skip_id']}"
    )
    return kept, stats


def read_date_cell(ws, a1: str, fallback: datetime | None) -> datetime:
    val = ws.acell(a1, value_render_option="UNFORMATTED_VALUE").value
    dt = to_datetime(val)
    if dt is None:
        if fallback is not None:
            return fallback
        raise RuntimeError(f"单元格 {ws.title}!{a1} 不是有效日期（当前值: {val!r}）")
    return dt


def _protect_error(ss_title: str, sheet_name: str, err: Exception) -> RuntimeError:
    text = str(err)
    if "protected" in text.lower() or "保护" in text:
        return RuntimeError(
            f"目标表「{ss_title}」工作表「{sheet_name}」有单元格保护，脚本写不进去。\n"
            "请打开该表 → 数据 → 保护的工作表和区域：取消保护，"
            "或把服务账号加进该保护区域的允许编辑名单。\n"
            "也可以把结果改写到一个没有保护的新工作表。"
        )
    return RuntimeError(f"写入「{ss_title}」/{sheet_name} 失败: {err}")


def _norm_id(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if text.endswith(".0"):
        core = text[:-2]
        if core.isdigit() or (core.startswith("-") and core[1:].isdigit()):
            return core
    return text


def _values_equal(old: Any, new: Any) -> bool:
    if old is None:
        old = ""
    if new is None:
        new = ""
    if old == new:
        return True
    od, nd = to_datetime(old), to_datetime(new)
    if od is not None and nd is not None:
        return od.replace(microsecond=0) == nd.replace(microsecond=0)
    so, sn = str(old).strip(), str(new).strip()
    if so == sn:
        return True
    try:
        return float(so.replace(",", "")) == float(sn.replace(",", ""))
    except (TypeError, ValueError):
        return False


def _push_cell_updates(updates: list[tuple[int, int, Any]], ws, log: LogFn) -> int:
    """updates: (row_1based, col_0based, value)。相邻格子合并成一段再批量提交。"""
    if not updates:
        return 0
    updates.sort()
    ranges: list[dict] = []
    i = 0
    n = len(updates)
    while i < n:
        row, col, val = updates[i]
        vals = [val]
        j = i + 1
        while j < n and updates[j][0] == row and updates[j][1] == col + (j - i):
            vals.append(updates[j][2])
            j += 1
        start = index_to_col_letter(col)
        end = index_to_col_letter(col + len(vals) - 1)
        a1 = f"{start}{row}" if start == end else f"{start}{row}:{end}{row}"
        ranges.append({"range": a1, "values": [vals]})
        i = j

    sent = 0
    chunk_size = 400
    for k in range(0, len(ranges), chunk_size):
        chunk = ranges[k : k + chunk_size]

        def _batch(ch=chunk):
            return ws.batch_update(ch, value_input_option="USER_ENTERED")

        with_retry(_batch, log=log, what=f"增量更新 {k + 1}-{k + len(chunk)}")
        sent += len(chunk)
        log(f"  已提交变更 {min(k + chunk_size, len(ranges))}/{len(ranges)} 段")
    return sent


def write_upsert(
    ss,
    sheet_name: str,
    start_row: int,
    headers: list[str],
    rows: list[list[Any]],
    include_headers: bool,
    date_col_idx: int | None,
    group_values: list[Any] | None,
    group_col: str,
    id_col_idx: int,
    log: LogFn,
) -> None:
    """按 B 列（帖文id）匹配：已有行只改有变动的格子，新 id 追加到表尾。"""
    import gspread

    try:
        ws = ss.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        log("目标工作表不存在，将新建后按追加写入")
        write_output(
            ss,
            sheet_name,
            start_row,
            headers,
            rows,
            include_headers,
            date_col_idx=date_col_idx,
            group_values=group_values,
            group_col=group_col,
            upsert_by_id=False,
            id_col_idx=id_col_idx,
            log=log,
        )
        return

    data_start = start_row + (1 if include_headers else 0)
    g_idx = col_letter_to_index(group_col) if group_values is not None else None
    need_cols = max(len(headers), (g_idx + 1 if g_idx is not None else 1))
    if ws.col_count < need_cols:
        ws.resize(rows=max(ws.row_count, data_start + 20), cols=need_cols)

    log("读取目标表已有行，按帖文id（B列）匹配…")
    existing = read_sheet_values(ws, log=log)

    if include_headers:
        header_row = existing[start_row - 1] if len(existing) >= start_row else []
        if not any(str(x).strip() for x in header_row[: len(headers)]):
            with_retry(
                lambda: ws.update(
                    range_name=f"A{start_row}",
                    values=[headers],
                    value_input_option="USER_ENTERED",
                ),
                log=log,
                what="写入表头",
            )
            if group_values is not None:
                with_retry(
                    lambda: ws.update(
                        range_name=f"{group_col}{start_row}",
                        values=[["小组"]],
                        value_input_option="USER_ENTERED",
                    ),
                    log=log,
                    what="写入小组表头",
                )

    id_to_row: dict[str, int] = {}
    last_data_row = data_start - 1
    for rnum, raw in enumerate(existing[data_start - 1 :], start=data_start):
        if any(str(x).strip() for x in raw):
            last_data_row = rnum
        pid = _norm_id(raw[id_col_idx] if id_col_idx < len(raw) else "")
        if pid and pid not in id_to_row:
            id_to_row[pid] = rnum

    cell_updates: list[tuple[int, int, Any]] = []
    new_rows: list[list[Any]] = []
    new_groups: list[Any] = []
    seen_in: set[str] = set()
    updated_rows = 0
    unchanged_rows = 0
    skipped_no_id = 0

    for i, row in enumerate(rows):
        new_row = [
            cell_for_sheets(v, is_date=(date_col_idx is not None and c == date_col_idx))
            for c, v in enumerate(row)
        ]
        if len(new_row) < len(headers):
            new_row = new_row + [""] * (len(headers) - len(new_row))
        gid = ""
        if group_values is not None:
            gid = group_values[i] if i < len(group_values) else ""
        pid = _norm_id(new_row[id_col_idx] if id_col_idx < len(new_row) else "")
        if not pid:
            skipped_no_id += 1
            continue
        if pid in seen_in:
            continue
        seen_in.add(pid)

        if pid in id_to_row:
            rnum = id_to_row[pid]
            old = existing[rnum - 1] if rnum - 1 < len(existing) else []
            row_changed = False
            for c, val in enumerate(new_row):
                old_val = old[c] if c < len(old) else ""
                if c == date_col_idx:
                    old_val = cell_for_sheets(old_val, is_date=True)
                if not _values_equal(old_val, val):
                    cell_updates.append((rnum, c, val))
                    row_changed = True
            if g_idx is not None:
                old_g = old[g_idx] if g_idx < len(old) else ""
                if not _values_equal(old_g, gid):
                    cell_updates.append((rnum, g_idx, gid))
                    row_changed = True
            if row_changed:
                updated_rows += 1
            else:
                unchanged_rows += 1
        else:
            new_rows.append(new_row)
            new_groups.append(gid)
            id_to_row[pid] = -1

    log(
        f"匹配结果：已有 {updated_rows} 行有变动，{unchanged_rows} 行无变化，"
        f"新增 {len(new_rows)} 行"
        + (f"，无id跳过 {skipped_no_id}" if skipped_no_id else "")
    )
    try:
        _push_cell_updates(cell_updates, ws, log)
        if new_rows:
            append_at = last_data_row + 1
            need_rows = append_at - 1 + len(new_rows)
            if ws.row_count < need_rows + 5:
                ws.resize(rows=need_rows + 50, cols=max(ws.col_count, need_cols))
            log(f"追加新行 {len(new_rows)} 行，从第 {append_at} 行起…")
            for k in range(0, len(new_rows), WRITE_CHUNK):
                chunk = new_rows[k : k + WRITE_CHUNK]
                row_at = append_at + k

                def _append(r=row_at, c=chunk):
                    return ws.update(
                        range_name=f"A{r}",
                        values=c,
                        value_input_option="USER_ENTERED",
                    )

                with_retry(_append, log=log, what=f"追加第 {row_at} 行起")
                if group_values is not None:
                    gchunk = [[g] for g in new_groups[k : k + WRITE_CHUNK]]

                    def _append_g(r=row_at, c=gchunk):
                        return ws.update(
                            range_name=f"{group_col}{r}",
                            values=c,
                            value_input_option="USER_ENTERED",
                        )

                    with_retry(_append_g, log=log, what=f"追加 {group_col}{row_at}")
                log(f"  已追加 {min(k + len(chunk), len(new_rows))}/{len(new_rows)}")
    except Exception as e:
        raise _protect_error(ss.title, sheet_name, e) from e
    log(
        f"增量写入完成「{ss.title}」/{sheet_name}："
        f"改 {updated_rows} 行、加 {len(new_rows)} 行、未动 {unchanged_rows} 行"
    )


def write_output(
    ss,
    sheet_name: str,
    start_row: int,
    headers: list[str],
    rows: list[list[Any]],
    include_headers: bool,
    date_col_idx: int | None = None,
    group_values: list[Any] | None = None,
    group_col: str = "AC",
    upsert_by_id: bool = False,
    id_col_idx: int = 1,
    log: LogFn = print,
) -> None:
    if upsert_by_id:
        write_upsert(
            ss,
            sheet_name,
            start_row,
            headers,
            rows,
            include_headers,
            date_col_idx,
            group_values,
            group_col,
            id_col_idx,
            log,
        )
        return

    import gspread

    try:
        ws = ss.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        cols = max(len(headers), 18, col_letter_to_index(group_col) + 1 if group_values is not None else 18)
        ws = ss.add_worksheet(
            title=sheet_name,
            rows=max(len(rows) + start_row + 20, 100),
            cols=cols,
        )
        log(f"已在目标表新建工作表「{sheet_name}」")

    end_col = index_to_col_letter(max(len(headers) - 1, 0))
    clear_ranges = [f"A{start_row}:{end_col}{ws.row_count}"]
    if group_values is not None:
        clear_ranges.append(f"{group_col}{start_row}:{group_col}{ws.row_count}")
    try:
        if ws.row_count >= start_row:
            ws.batch_clear(clear_ranges)
    except Exception as e:
        raise _protect_error(ss.title, sheet_name, e) from e

    payload: list[list[Any]] = []
    if include_headers:
        payload.append(headers)
    for row in rows:
        payload.append(
            [
                cell_for_sheets(v, is_date=(date_col_idx is not None and i == date_col_idx))
                for i, v in enumerate(row)
            ]
        )

    needed_rows = start_row - 1 + max(len(payload), 1)
    needed_cols = max(len(headers), 1)
    if group_values is not None:
        needed_cols = max(needed_cols, col_letter_to_index(group_col) + 1)
    if ws.row_count < needed_rows or ws.col_count < needed_cols:
        ws.resize(
            rows=max(ws.row_count, needed_rows + 20),
            cols=max(ws.col_count, needed_cols),
        )

    if not payload:
        log(f"没有数据可写，「{sheet_name}」输出区已清空")
        return

    total = len(payload)
    log(f"开始写入 {len(rows)} 行（分批 {WRITE_CHUNK} 行）…")
    try:
        for i in range(0, total, WRITE_CHUNK):
            chunk = payload[i : i + WRITE_CHUNK]
            row_at = start_row + i
            def _write_chunk(r=row_at, c=chunk):
                return ws.update(
                    range_name=f"A{r}",
                    values=c,
                    value_input_option="USER_ENTERED",
                )

            with_retry(_write_chunk, log=log, what=f"写入第 {row_at} 行起")
            log(f"  已写入 {min(i + len(chunk), total)}/{total}")
    except Exception as e:
        raise _protect_error(ss.title, sheet_name, e) from e

    if group_values is not None:
        group_payload: list[list[Any]] = []
        if include_headers:
            group_payload.append(["小组"])
        for g in group_values:
            group_payload.append([g if g is not None else ""])
        for i in range(0, len(group_payload), WRITE_CHUNK):
            chunk = group_payload[i : i + WRITE_CHUNK]
            row_at = start_row + i
            def _write_group(r=row_at, c=chunk):
                return ws.update(
                    range_name=f"{group_col}{r}",
                    values=c,
                    value_input_option="USER_ENTERED",
                )

            with_retry(_write_group, log=log, what=f"写入 {group_col}{row_at}")
        log(f"  小组已写入 {group_col} 列")
    log(f"已写入「{ss.title}」/「{sheet_name}」A{start_row} 起，共 {len(rows)} 行数据")


def write_csv(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    import csv

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for row in rows:
            out = []
            for v in row:
                cv = cell_for_sheets(v)
                if isinstance(cv, datetime):
                    out.append(cv.strftime("%Y-%m-%d %H:%M:%S"))
                else:
                    out.append(cv)
            w.writerow(out)


def resolve_dates(
    cfg: Config,
    ss,
    start_date: datetime | None,
    end_date: datetime | None,
) -> tuple[datetime, datetime]:
    if start_date is None and cfg.start_date:
        start_date = to_datetime(cfg.start_date)
    if end_date is None and cfg.end_date:
        end_date = to_datetime(cfg.end_date)
    if start_date is not None and end_date is not None:
        return start_date, end_date

    date_sheet_name = cfg.date_sheet or cfg.output_sheet
    date_ws = None
    try:
        date_ws = ss.worksheet(date_sheet_name)
    except Exception:
        date_ws = None
    if date_ws is None:
        raise RuntimeError("请在界面填写开始/结束日期，或在目标表 A1/B1 填日期")
    if start_date is None:
        start_date = read_date_cell(date_ws, cfg.date_start_cell, None)
    if end_date is None:
        end_date = read_date_cell(date_ws, cfg.date_end_cell, None)
    return start_date, end_date


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(
    cfg: Config,
    source_urls: list[str] | None = None,
    sources: list | None = None,
    target_url: str | None = None,
    config_url: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    dry_run: bool = False,
    csv_path: Path | None = None,
    log: LogFn = print,
) -> dict[str, Any]:
    gc = authorize_cfg(cfg, log=log)

    target_ref = target_url or cfg.target_url or cfg.spreadsheet_id
    source_refs = normalize_sources(
        sources if sources is not None else (cfg.sources or source_urls or cfg.source_urls)
    )
    field_maps = resolve_fields(cfg)

    if not source_refs:
        raise RuntimeError("请至少填写一个数据源表格链接（所有源表表头相同，只换链接）")
    write_all = bool(getattr(cfg, "write_all", True))
    write_hot = bool(getattr(cfg, "write_hot", True))
    if not write_all and not write_hot:
        raise RuntimeError("请至少勾选一种输出：全部，或点赞超过阈值")
    hot_ref = cfg.hot_target_url or target_ref
    if write_all and not target_ref:
        raise RuntimeError("请填写「全部结果」目标表链接")
    if write_hot and not hot_ref:
        raise RuntimeError("请填写「高赞」目标表链接")

    log("字段配置: " + "、".join(f"{f.name}({f.sheet}!{f.range_spec})" for f in field_maps))

    dest_ref = target_ref or hot_ref
    log("打开目标表…")
    target_ss = open_by_url_or_id(gc, dest_ref, log=log) if dest_ref else None
    if target_ss:
        log(f"目标表: {target_ss.title}")

    date_idx = find_field_index(field_maps, cfg.date_field, cfg.date_col_1based, log)
    id_idx = find_field_index(field_maps, cfg.id_field, cfg.id_col_1based, log)
    sort_idx = find_field_index(field_maps, cfg.sort_field, cfg.sort_col_1based, log)

    start_date, end_date = resolve_dates(cfg, target_ss, start_date, end_date)
    if start_date > end_date:
        raise RuntimeError("开始日期不能晚于结束日期")
    log(f"日期区间: {start_date.date()} ~ {end_date.date()}（含结束日）")
    log(
        f"排序字段: {field_maps[sort_idx].name}（"
        + ("降序" if cfg.sort_descending else "升序")
        + "）"
    )
    log(f"数据源 {len(source_refs)} 个")

    headers = [f.name for f in field_maps]
    group_col = (cfg.group_column or "AC").strip().upper() or "AC"
    paired: list[tuple[list[Any], str]] = []
    per_source: list[dict[str, Any]] = []

    for i, src in enumerate(source_refs, 1):
        label = src.name or src.sid
        item: dict[str, Any] = {
            "url": src.url,
            "id": src.sid,
            "name": src.name,
            "title": "",
            "kept": 0,
            "error": None,
        }
        try:
            log(f"[{i}/{len(source_refs)}] {label}  {src.sid}")
            source_ss = open_by_url_or_id(gc, src.url, log=log)
            item["title"] = source_ss.title
            log(f"  表名: {source_ss.title}")
            raw_rows = build_datasource_from_ss(source_ss, field_maps, log=log)
            log(f"  原始行数: {len(raw_rows)}")
            kept, stats = filter_and_sort(
                raw_rows,
                date_idx=date_idx,
                id_idx=id_idx,
                sort_idx=sort_idx,
                start=start_date,
                end=end_date,
                exclude_id=cfg.exclude_id_value,
                descending=cfg.sort_descending,
                log=log,
            )
            item["kept"] = stats["kept"]
            item["stats"] = stats
            source_label = src.name or source_ss.title
            for row in kept:
                paired.append((list(row), source_label))
        except Exception as e:
            item["error"] = str(e)
            log(f"  失败（跳过此源）: {e}")
        per_source.append(item)

    paired.sort(
        key=lambda item: to_number(item[0][sort_idx] if sort_idx < len(item[0]) else None),
        reverse=cfg.sort_descending,
    )
    merged = [item[0] for item in paired]
    group_values = [item[1] for item in paired] if cfg.add_source_column else None
    ok_sources = sum(1 for s in per_source if not s.get("error"))
    log(f"汇总: {ok_sources}/{len(source_refs)} 个源成功，合计 {len(merged)} 行")

    threshold = int(getattr(cfg, "likes_threshold", 1000) or 1000)
    likes_idx = sort_idx
    hot_pairs = [
        item
        for item in paired
        if to_number(item[0][likes_idx] if likes_idx < len(item[0]) else None) >= threshold
    ]
    hot_rows = [item[0] for item in hot_pairs]
    hot_groups = [item[1] for item in hot_pairs] if cfg.add_source_column else None
    log(f"点赞 ≥ {threshold}: {len(hot_rows)} 行 / 全部 {len(merged)} 行")

    if csv_path:
        write_csv(csv_path, headers, merged)
        log(f"已导出 CSV: {csv_path}")

    all_url = ""
    hot_url = ""
    date_col = date_idx
    opened: dict[str, Any] = {}

    def _ss(url_or_id: str):
        sid = extract_spreadsheet_id(url_or_id) or url_or_id
        if sid not in opened:
            opened[sid] = open_by_url_or_id(gc, url_or_id, log=log)
        return opened[sid]

    if target_ss is not None:
        opened[target_ss.id] = target_ss

    cf_info = None
    if dry_run:
        log("dry-run：不写回 Google 表格")
    else:
        if write_all:
            all_ss = _ss(target_ref)
            log(f"{'按帖文id增量更新' if cfg.upsert_by_id else '覆盖写入'}全部 → 「{all_ss.title}」/{cfg.output_sheet}")
            write_output(
                all_ss,
                sheet_name=cfg.output_sheet,
                start_row=cfg.output_start_row,
                headers=headers,
                rows=merged,
                include_headers=cfg.include_headers,
                date_col_idx=date_col,
                group_values=group_values,
                group_col=group_col,
                upsert_by_id=bool(getattr(cfg, "upsert_by_id", True)),
                id_col_idx=id_idx,
                log=log,
            )
            all_url = spreadsheet_url(all_ss.id)
        if write_hot:
            hot_sheet = cfg.hot_output_sheet or f"点赞{threshold}以上"
            hot_ss = _ss(hot_ref)
            log(f"{'按帖文id增量更新' if cfg.upsert_by_id else '覆盖写入'}高赞(≥{threshold}) → 「{hot_ss.title}」/{hot_sheet}")
            write_output(
                hot_ss,
                sheet_name=hot_sheet,
                start_row=int(getattr(cfg, "hot_start_row", 1) or 1),
                headers=headers,
                rows=hot_rows,
                include_headers=bool(getattr(cfg, "hot_include_headers", True)),
                date_col_idx=date_col,
                group_values=hot_groups,
                group_col=group_col,
                upsert_by_id=bool(getattr(cfg, "upsert_by_id", True)),
                id_col_idx=id_idx,
                log=log,
            )
            hot_url = spreadsheet_url(hot_ss.id)
        remember_run_state(gc, [s.sid for s in source_refs])
        if getattr(cfg, "cf_publish_after_sync", False):
            from publish_cloudflare import publish_assets_to_cloudflare

            src = str(getattr(cfg, "cf_publish_source", "all") or "all").strip().lower()
            pub_rows = hot_rows if src == "hot" else merged
            pub_groups = hot_groups if src == "hot" else group_values
            log("汇总完成，开始直推 Cloudflare…")
            cf_info = publish_assets_to_cloudflare(
                cfg, headers, pub_rows, pub_groups, log=log
            )

    result = {
        "ok": ok_sources > 0,
        "total_rows": len(merged),
        "hot_rows": len(hot_rows),
        "likes_threshold": threshold,
        "sources": per_source,
        "target_title": target_ss.title if target_ss else "",
        "target_url": all_url or (spreadsheet_url(target_ss.id) if target_ss else ""),
        "hot_url": hot_url,
        "sheet": cfg.output_sheet,
        "hot_sheet": cfg.hot_output_sheet or f"点赞{threshold}以上",
        "headers": headers,
        "skipped": False,
        "cloudflare": cf_info,
    }
    log("完成")
    return result


def _norm_header(name: Any) -> str:
    return str(name or "").strip()


def header_index_map(header_row: list[Any]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    lower: dict[str, int] = {}
    for i, cell in enumerate(header_row):
        name = _norm_header(cell)
        if not name:
            continue
        if name not in mapping:
            mapping[name] = i
        key = name.lower()
        if key not in lower:
            lower[key] = i
    mapping["_lower"] = lower  # type: ignore
    return mapping


def lookup_header(mapping: dict, name: str) -> int | None:
    name = _norm_header(name)
    if not name:
        return None
    if name in mapping and name != "_lower":
        return mapping[name]
    lower = mapping.get("_lower") or {}
    return lower.get(name.lower())


def pick_source_ws(ss, sheet_name: str):
    name = (sheet_name or "").strip()
    if name:
        import gspread

        try:
            return ss.worksheet(name)
        except gspread.exceptions.WorksheetNotFound as e:
            raise RuntimeError(f"「{ss.title}」里找不到工作表「{name}」") from e
    return ss.sheet1


def peek_source_headers(
    cfg: Config,
    source_url: str,
    sheet_name: str = "",
    header_row: int = 1,
) -> list[str]:
    gc = authorize_cfg(cfg)
    ss = open_by_url_or_id(gc, source_url)
    ws = pick_source_ws(ss, sheet_name or cfg.align_source_sheet)
    row_n = max(1, int(header_row or cfg.align_header_row or 1))
    values = with_retry(lambda: ws.row_values(row_n), what="读取表头")
    return [_norm_header(v) for v in values if _norm_header(v)]


def run_align_sync(cfg: Config, log: LogFn = print) -> dict[str, Any]:
    """按字段映射对齐拷贝；每个源链接可覆盖默认映射。"""
    gc = authorize_cfg(cfg, log=log)
    default_mappings: list[dict[str, str]] = []
    for item in getattr(cfg, "align_mappings", []) or []:
        if not isinstance(item, dict):
            continue
        target = _norm_header(item.get("target") or item.get("name") or "")
        source = _norm_header(item.get("source") or target)
        if target:
            default_mappings.append({"target": target, "source": source or target})
    if not default_mappings:
        default_mappings = [
            {"target": _norm_header(h), "source": _norm_header(h)}
            for h in (cfg.align_headers or [])
            if _norm_header(h)
        ]
    headers = [item["target"] for item in default_mappings]
    if not headers:
        raise RuntimeError("请先配置字段映射")
    source_refs = normalize_sources(cfg.align_sources)
    if not source_refs:
        raise RuntimeError("请至少填写一个数据源表格链接")
    target_ref = cfg.align_target_url
    if not target_ref:
        raise RuntimeError("请填写目标表链接")

    header_row_n = max(1, int(cfg.align_header_row or 1))
    log(f"字段映射 {len(headers)} 列: " + "、".join(headers))
    log(f"源表 {len(source_refs)} 个 · 表头在第 {header_row_n} 行")

    merged: list[list[Any]] = []
    per_source: list[dict[str, Any]] = []

    for i, src in enumerate(source_refs, 1):
        item: dict[str, Any] = {
            "url": src.url,
            "id": src.sid,
            "name": src.name,
            "title": "",
            "kept": 0,
            "missing": [],
            "error": None,
        }
        try:
            sheet_name = src.sheet or src.name or cfg.align_source_sheet
            log(f"[{i}/{len(source_refs)}] {sheet_name or src.sid}")
            ss = open_by_url_or_id(gc, src.url, log=log)
            item["title"] = ss.title
            item["sheet"] = sheet_name
            ws = pick_source_ws(ss, sheet_name)
            log(f"  读取「{ss.title}」/「{ws.title}」")
            values = read_sheet_values(ws, log=log)
            if len(values) < header_row_n:
                raise RuntimeError(f"工作表不足 {header_row_n} 行，读不到表头")
            hmap = header_index_map(values[header_row_n - 1])
            profiles = getattr(cfg, "align_mapping_profiles", {}) or {}
            raw_profile = profiles.get(src.url) or profiles.get(src.sid) or []
            profile_by_target = {
                _norm_header(x.get("target") or x.get("name") or ""): _norm_header(
                    x.get("source") or x.get("target") or x.get("name") or ""
                )
                for x in raw_profile
                if isinstance(x, dict) and _norm_header(x.get("target") or x.get("name") or "")
            }
            source_headers = [profile_by_target.get(item["target"], item["source"]) for item in default_mappings]
            missing = [
                headers[index]
                for index, source_header in enumerate(source_headers)
                if lookup_header(hmap, source_header) is None
            ]
            item["missing"] = missing
            if missing:
                log(f"  缺少表头（这些列留空）: " + "、".join(missing))
            else:
                log("  表头齐全")
            col_idx = [lookup_header(hmap, source_header) for source_header in source_headers]
            kept = 0
            for raw in values[header_row_n:]:
                if not any(str(c).strip() for c in raw):
                    continue
                out = []
                for idx in col_idx:
                    if idx is not None and idx < len(raw):
                        out.append(raw[idx])
                    else:
                        out.append("")
                merged.append(out)
                kept += 1
            item["kept"] = kept
            log(f"  写入 {kept} 行")
        except Exception as e:
            item["error"] = str(e)
            log(f"  失败（跳过此源）: {e}")
        per_source.append(item)

    ok_sources = sum(1 for s in per_source if not s.get("error"))
    log(f"对齐汇总: {ok_sources}/{len(source_refs)} 个源成功，合计 {len(merged)} 行")

    target_ss = open_by_url_or_id(gc, target_ref, log=log)
    log(f"覆盖写入 → 「{target_ss.title}」/{cfg.align_output_sheet}")
    write_output(
        target_ss,
        sheet_name=cfg.align_output_sheet or "对齐结果",
        start_row=int(cfg.align_start_row or 1),
        headers=headers,
        rows=merged,
        include_headers=bool(cfg.align_include_headers),
        date_col_idx=None,
        log=log,
    )
    remember_run_state(gc, [s.sid for s in source_refs])
    log("完成")
    return {
        "ok": ok_sources > 0,
        "mode": "align",
        "total_rows": len(merged),
        "sources": per_source,
        "target_url": spreadsheet_url(target_ss.id),
        "sheet": cfg.align_output_sheet,
        "headers": headers,
        "skipped": False,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="多源贴文库筛选汇总（替代 IMPORTRANGE 公式）")
    p.add_argument("--config", default="", help="config.json 路径")
    p.add_argument("--spreadsheet-id", default="", help="配置表 ID（旧参数）")
    p.add_argument("--config-url", default="", help="含「数据库」sheet 的表格链接")
    p.add_argument("--target-url", default="", help="汇总写入的目标表格链接")
    p.add_argument("--source-url", action="append", default=[], help="数据源链接，可重复")
    p.add_argument("--credentials", default="", help="服务账号 JSON 路径")
    p.add_argument("--start-date", default="", help="开始日期 YYYY-MM-DD")
    p.add_argument("--end-date", default="", help="结束日期 YYYY-MM-DD")
    p.add_argument("--output-sheet", default="", help="结果工作表名")
    p.add_argument("--csv", default="", help="同时导出本地 CSV")
    p.add_argument("--dry-run", action="store_true", help="只计算不写回表格")
    p.add_argument("--gui", action="store_true", help="打开网页界面")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.gui:
        from app import main as gui_main

        gui_main()
        return 0

    cfg_path = Path(args.config) if args.config else None
    cfg = load_config(cfg_path)
    if args.spreadsheet_id:
        cfg.spreadsheet_id = extract_spreadsheet_id(args.spreadsheet_id) or args.spreadsheet_id
    if args.config_url:
        cfg.config_url = args.config_url
    if args.target_url:
        cfg.target_url = args.target_url
    if args.source_url:
        cfg.source_urls = parse_url_list(args.source_url)
        cfg.sources = [{"name": "", "url": u} for u in cfg.source_urls]
    if args.credentials:
        cfg.credentials_file = args.credentials
    if args.output_sheet:
        cfg.output_sheet = args.output_sheet

    start = to_datetime(args.start_date) if args.start_date else None
    end = to_datetime(args.end_date) if args.end_date else None
    csv_path = Path(args.csv) if args.csv else None

    try:
        run(cfg, start_date=start, end_date=end, dry_run=args.dry_run, csv_path=csv_path)
    except Exception as e:
        print(f"失败: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
