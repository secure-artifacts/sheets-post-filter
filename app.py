# -*- coding: utf-8 -*-
"""本地网页界面：多数据源汇总，可写全部/高赞两张表，支持定时同步。"""

from __future__ import annotations

import copy
import os
import queue
import sys
import threading
import traceback
import webbrowser
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, render_template, request

from catalog_merge import run_catalog_merge
from roster_fill import run_roster_fill

from fetch_posts import (
    SCRIPT_DIR,
    RESOURCE_DIR,
    DEFAULT_FIELDS,
    Config,
    authorize,
    authorize_cfg,
    copy_default_fields,
    field_from_dict,
    load_config,
    load_sync_state,
    normalize_sources,
    open_by_url_or_id,
    parse_url_list,
    peek_source_headers,
    read_sheet_values,
    run,
    run_align_sync,
    save_config,
    save_sync_state,
    service_account_email,
    sources_have_changed,
    to_datetime,
    write_app_log,
    LOG_DIR,
)

app = Flask(
    __name__,
    template_folder=str(RESOURCE_DIR / "web"),
    static_folder=str(RESOURCE_DIR / "web" / "static"),
)

def _new_job_state() -> dict:
    return {
        "running": False,
        "logs": [],
        "result": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
        "skipped": False,
        "queued": False,
        "cancel": False,
        "kind": "",
    }


_job_lock = threading.Lock()
_job: dict = _new_job_state()  # 兼容桌面端旧引用，对应 default 任务。
_jobs: dict[str, dict] = {"default": _job}
_job_locks: dict[str, threading.Lock] = {"default": _job_lock}
_job_registry_lock = threading.Lock()

_sched_lock = threading.Lock()
_sched = {
    "enabled": False,
    "minutes": 60,
    "only_if_changed": True,
    "next_run": None,
    "last_msg": "",
}
_sched_stop = threading.Event()
_sched_thread: threading.Thread | None = None
_align_sched = {
    "enabled": False,
    "minutes": 60,
    "only_if_changed": True,
    "next_run": None,
    "last_msg": "",
}
_video_sched = {
    "enabled": False,
    "minutes": 180,
    "only_if_changed": False,
    "next_run": None,
    "last_msg": "",
}
_catalog_sched = {
    "enabled": False,
    "minutes": 180,
    "only_if_changed": False,
    "next_run": None,
    "last_msg": "",
}
# Per-menu timers so saving/switching another template cannot kill this one.
_menu_schedules: dict[str, dict] = {}


def _job_key(cfg=None, explicit: str = "") -> str:
    value = explicit or (getattr(cfg, "ui_active_menu", "") if cfg is not None else "") or "default"
    return str(value).strip()[:160] or "default"


def _job_parts(key: str) -> tuple[dict, threading.Lock]:
    key = _job_key(explicit=key)
    with _job_registry_lock:
        state = _jobs.setdefault(key, _new_job_state())
        lock = _job_locks.setdefault(key, threading.Lock())
    return state, lock


def job_snapshot(key: str = "default") -> dict:
    state, _lock = _job_parts(key)
    return {**state, "logs": list(state.get("logs") or [])}


def _reset_job(job: dict | None = None) -> None:
    job = job or _job
    job["running"] = True
    job["queued"] = False
    job["cancel"] = False
    job["logs"] = []
    job["result"] = None
    job["error"] = None
    job["skipped"] = False
    job["started_at"] = datetime.now().strftime("%H:%M:%S")
    job["finished_at"] = None


def _safe_print(text: str) -> None:
    """Never let Windows GBK consoles abort a job because a sheet title has emoji."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            print(str(text).encode(encoding, errors="replace").decode(encoding, errors="replace"))
        except Exception:
            pass
    except Exception:
        pass


def _log(msg: str, job: dict | None = None) -> None:
    job = job or _job
    line = {"t": datetime.now().strftime("%H:%M:%S"), "msg": str(msg)}
    logs = job.setdefault("logs", [])
    logs.append(line)
    extra = len(logs) - 800
    if extra > 0:
        del logs[:extra]
    _safe_print(f"[{line['t']}] {msg}")
    try:
        write_app_log(str(msg))
    except Exception:
        pass


def _record_job_failure(exc: Exception, context: str = "任务", job: dict | None = None) -> None:
    detail = str(exc).strip() or exc.__class__.__name__
    message = f"{context}失败：{detail}"
    job = job or _job
    job["error"] = message
    _log(message, job)
    tb = traceback.format_exc()
    _safe_print(tb)
    try:
        write_app_log(message, tb)
    except Exception:
        pass


def job_cancelled(job_key: str = "default") -> bool:
    if _stop_all.is_set():
        return True
    job, _lock = _job_parts(job_key)
    return bool(job.get("cancel"))


_run_queue: queue.Queue = queue.Queue()
_queue_worker: threading.Thread | None = None
_stop_all = threading.Event()


def _queue_worker_loop() -> None:
    while True:
        item = _run_queue.get()
        if item is None:
            continue
        kind, cfg, job_key, from_schedule = item
        job, lock = _job_parts(job_key)
        if job.get("cancel") or _stop_all.is_set():
            job["queued"] = False
            job["running"] = False
            _log("已跳过（已停止）", job)
            continue
        if not lock.acquire(blocking=False):
            _run_queue.put(item)
            threading.Event().wait(0.4)
            continue
        _reset_job(job)
        job["kind"] = kind
        try:
            if kind == "filter":
                _run_job(cfg, from_schedule=from_schedule, job_key=job_key)
            elif kind == "align":
                _run_align_job(cfg, from_schedule=from_schedule, job_key=job_key)
            elif kind == "video":
                _run_video_job(cfg, from_schedule=from_schedule, job_key=job_key)
            elif kind == "catalog":
                _run_catalog_job(cfg, job_key=job_key)
            elif kind == "roster":
                _run_roster_job(cfg, job_key=job_key)
            elif kind == "publish":
                start_publish_job(cfg)
                lock.release()
            else:
                _log(f"未知任务类型 {kind}", job)
                job["running"] = False
                if lock.locked():
                    lock.release()
        except Exception as exc:
            _record_job_failure(exc, "队列任务", job)
            job["running"] = False
            if lock.locked():
                lock.release()


def _ensure_queue_worker() -> None:
    global _queue_worker
    if _queue_worker is None or not _queue_worker.is_alive():
        _queue_worker = threading.Thread(target=_queue_worker_loop, daemon=True)
        _queue_worker.start()


def enqueue_job(kind: str, cfg: Config, job_key: str = "", from_schedule: bool = False) -> str | None:
    job_key = _job_key(cfg, job_key)
    job, _lock = _job_parts(job_key)
    if job.get("running") or job.get("queued"):
        return "这个配置已在队列中或正在运行"
    job["queued"] = True
    job["cancel"] = False
    job["kind"] = kind
    job["error"] = None
    _log(f"已加入执行队列：{kind}", job)
    _run_queue.put((kind, cfg, job_key, from_schedule))
    _ensure_queue_worker()
    return None


def stop_job(job_key: str = "default") -> None:
    job, _lock = _job_parts(job_key)
    job["cancel"] = True
    _log("已请求停止当前任务", job)


def stop_all_jobs() -> None:
    _stop_all.set()
    for key in list(_jobs):
        job, _lock = _job_parts(key)
        job["cancel"] = True
        job["queued"] = False
        _log("已请求全部停止", job)
    while True:
        try:
            item = _run_queue.get_nowait()
            if item:
                _log("已从队列移除待执行任务", _job_parts(item[2])[0])
        except queue.Empty:
            break
    threading.Timer(1.0, _stop_all.clear).start()


def _cfg_from_payload(data: dict, base: Config | None = None) -> Config:
    cfg = load_config() if base is None else copy.deepcopy(base)
    mapping = {
        "credentials_file": "credentials_file",
        "config_url": "config_url",
        "target_url": "target_url",
        "hot_target_url": "hot_target_url",
        "hot_output_sheet": "hot_output_sheet",
        "group_column": "group_column",
        "database_sheet": "database_sheet",
        "output_sheet": "output_sheet",
        "exclude_id_value": "exclude_id_value",
        "date_field": "date_field",
        "id_field": "id_field",
        "sort_field": "sort_field",
        "start_date": "start_date",
        "end_date": "end_date",
        "align_target_url": "align_target_url",
        "align_output_sheet": "align_output_sheet",
        "align_source_sheet": "align_source_sheet",
        "cf_publish_url": "cf_publish_url",
        "cf_publish_secret": "cf_publish_secret",
        "cf_publish_source": "cf_publish_source",
        "vd_source_url": "vd_source_url",
        "vd_source_sheet": "vd_source_sheet",
        "vd_col_date": "vd_col_date",
        "vd_col_link": "vd_col_link",
        "vd_col_name": "vd_col_name",
        "vd_col_type": "vd_col_type",
        "vd_dest_url": "vd_dest_url",
        "vd_log_sheet": "vd_log_sheet",
        "vd_report_sheet": "vd_report_sheet",
        "vd_count_mode": "vd_count_mode",
        "vd_start_date": "vd_start_date",
        "vd_end_date": "vd_end_date",
        "catalog_index_url": "catalog_index_url",
        "catalog_index_sheet": "catalog_index_sheet",
        "catalog_url_col": "catalog_url_col",
        "catalog_sheet_col": "catalog_sheet_col",
        "catalog_target_url": "catalog_target_url",
        "catalog_output_sheet": "catalog_output_sheet",
        "catalog_date_col": "catalog_date_col",
        "catalog_start_date": "catalog_start_date",
        "catalog_end_date": "catalog_end_date",
        "ui_active_menu": "ui_active_menu",
        "vd_type_filter_mode": "vd_type_filter_mode",
        "vd_other_category": "vd_other_category",
        "vd_category_mode": "vd_category_mode",
        "roster_config_url": "roster_config_url",
        "roster_config_sheet": "roster_config_sheet",
        "roster_target_url": "roster_target_url",
        "roster_traffic_sheet": "roster_traffic_sheet",
    }
    for src, dest in mapping.items():
        if src in data and data[src] is not None:
            setattr(cfg, dest, str(data[src]).strip())
    if "credentials_files" in data:
        raw_cf = data.get("credentials_files")
        if isinstance(raw_cf, str):
            cfg.credentials_files = [ln.strip() for ln in raw_cf.replace("，", "\n").splitlines() if ln.strip()]
        elif isinstance(raw_cf, list):
            cfg.credentials_files = [str(x).strip() for x in raw_cf if str(x).strip()]
        if cfg.credentials_files and not cfg.credentials_file:
            cfg.credentials_file = cfg.credentials_files[0]
    if cfg.vd_count_mode not in ("divide_total", "per_video_ceil"):
        cfg.vd_count_mode = "divide_total"
    if getattr(cfg, "vd_category_mode", "") not in ("columns_plus_other", "other_only"):
        cfg.vd_category_mode = "columns_plus_other"
    raw_sources = data.get("sources")
    if raw_sources is None:
        raw_sources = data.get("source_urls")
    if raw_sources is not None:
        refs = normalize_sources(raw_sources)
        cfg.sources = [{"name": s.name, "url": s.url, "sheet": s.sheet} for s in refs]
        cfg.source_urls = [s.url for s in refs]
    if "align_sources" in data:
        refs = normalize_sources(data.get("align_sources"))
        cfg.align_sources = [{"name": s.name, "url": s.url, "sheet": s.sheet} for s in refs]
    if "vd_exclude_types" in data:
        raw_ex = data.get("vd_exclude_types")
        if isinstance(raw_ex, str):
            parts = raw_ex.replace("，", "\n").replace(",", "\n").replace(";", "\n").splitlines()
            cfg.vd_exclude_types = [ln.strip() for ln in parts if ln.strip()]
        elif isinstance(raw_ex, list):
            cfg.vd_exclude_types = [str(x).strip() for x in raw_ex if str(x).strip()]
    if "vd_types" in data:
        raw_t = data.get("vd_types")
        if isinstance(raw_t, str):
            parts = raw_t.replace("，", "\n").replace(",", "\n").replace(";", "\n").splitlines()
            cfg.vd_types = [ln.strip() for ln in parts if ln.strip()]
        elif isinstance(raw_t, list):
            cleaned: list = []
            for item in raw_t:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("type") or "").strip()
                    if name:
                        cleaned.append(
                            {
                                "name": name,
                                "in_total": bool(item.get("in_total", True)),
                                "in_item": bool(item.get("in_item", True)),
                            }
                        )
                elif str(item).strip():
                    cleaned.append(str(item).strip())
            cfg.vd_types = cleaned
    if "vd_report_categories" in data:
        raw_rc = data.get("vd_report_categories")
        if isinstance(raw_rc, str):
            parts = raw_rc.replace("，", "\n").replace(",", "\n").replace(";", "\n").splitlines()
            cfg.vd_report_categories = [ln.strip() for ln in parts if ln.strip()]
        elif isinstance(raw_rc, list):
            cfg.vd_report_categories = [str(x).strip() for x in raw_rc if str(x).strip()]
    if "catalog_exclude_sheets" in data:
        raw_exs = data.get("catalog_exclude_sheets")
        if isinstance(raw_exs, str):
            parts = raw_exs.replace("，", "\n").replace(",", "\n").replace(";", "\n").splitlines()
            cfg.catalog_exclude_sheets = [ln.strip() for ln in parts if ln.strip()]
        elif isinstance(raw_exs, list):
            cfg.catalog_exclude_sheets = [str(x).strip() for x in raw_exs if str(x).strip()]
    if "align_headers" in data:
        raw_h = data.get("align_headers")
        if isinstance(raw_h, str):
            cfg.align_headers = [ln.strip() for ln in raw_h.splitlines() if ln.strip()]
        elif isinstance(raw_h, list):
            cfg.align_headers = [str(x).strip() for x in raw_h if str(x).strip()]
    if "align_mappings" in data and isinstance(data["align_mappings"], list):
        cfg.align_mappings = [
            {
                "target": str(item.get("target") or "").strip(),
                "source": str(item.get("source") or item.get("target") or "").strip(),
            }
            for item in data["align_mappings"]
            if isinstance(item, dict) and str(item.get("target") or "").strip()
        ]
        cfg.align_headers = [item["target"] for item in cfg.align_mappings]
    if "align_mapping_profiles" in data and isinstance(data["align_mapping_profiles"], dict):
        cfg.align_mapping_profiles = data["align_mapping_profiles"]
    if "ui_menus" in data and isinstance(data["ui_menus"], list):
        cfg.ui_menus = [item for item in data["ui_menus"] if isinstance(item, dict)]
    if "vd_source_sheets" in data:
        raw_sheets = data.get("vd_source_sheets")
        if isinstance(raw_sheets, str):
            cfg.vd_source_sheets = [x.strip() for x in raw_sheets.replace("，", "\n").replace(",", "\n").splitlines() if x.strip()]
        elif isinstance(raw_sheets, list):
            cfg.vd_source_sheets = [str(x).strip() for x in raw_sheets if str(x).strip()]
    if "vd_columns" in data and isinstance(data["vd_columns"], list):
        cfg.vd_columns = [
            {
                "field": str(item.get("field") or item.get("role") or "分类").strip(),
                "role": str(item.get("role") or "type").strip().lower(),
                "column": str(item.get("column") or "").strip().upper(),
            }
            for item in data["vd_columns"]
            if isinstance(item, dict) and str(item.get("column") or "").strip()
        ]
    if "roster_columns" in data and isinstance(data["roster_columns"], list):
        cfg.roster_columns = [
            {
                "field": str(item.get("field") or item.get("role") or "").strip(),
                "role": str(item.get("role") or "").strip().lower(),
                "column": str(item.get("column") or "").strip().upper(),
            }
            for item in data["roster_columns"]
            if isinstance(item, dict) and str(item.get("column") or "").strip() and str(item.get("role") or "").strip()
        ]
    if "fields" in data and isinstance(data["fields"], list):
        cleaned = []
        for item in data["fields"]:
            fm = field_from_dict(item)
            if fm:
                cleaned.append(fm.as_dict())
        cfg.fields = cleaned or copy_default_fields()
    for flag in (
        "add_source_column",
        "include_headers",
        "hot_include_headers",
        "align_include_headers",
        "sort_descending",
        "write_all",
        "write_hot",
        "upsert_by_id",
        "schedule_enabled",
        "schedule_only_if_changed",
        "align_schedule_enabled",
        "align_schedule_only_if_changed",
        "vd_schedule_enabled",
        "catalog_schedule_enabled",
        "cf_publish_after_sync",
        "vd_include_headers",
        "catalog_keep_each_header",
        "catalog_add_source",
        "catalog_skip_existing",
        "catalog_date_filter_enabled",
        "vd_date_filter_enabled",
        "vd_write_log",
        "vd_empty_to_other",
    ):
        if flag in data:
            setattr(cfg, flag, bool(data[flag]))
    for key in ("output_start_row", "hot_start_row", "align_start_row", "align_header_row", "vd_start_row", "vd_out_start_row", "catalog_start_row", "catalog_output_start_row", "roster_start_row", "roster_date_start_row"):
        if key in data and str(data[key]).strip():
            try:
                setattr(cfg, key, max(1, int(data[key])))
            except ValueError:
                pass
    if "likes_threshold" in data and str(data["likes_threshold"]).strip() != "":
        try:
            cfg.likes_threshold = max(0, int(float(data["likes_threshold"])))
        except ValueError:
            pass
    if "schedule_minutes" in data and str(data["schedule_minutes"]).strip() != "":
        try:
            cfg.schedule_minutes = max(5, int(data["schedule_minutes"]))
        except ValueError:
            pass
    if "align_schedule_minutes" in data and str(data["align_schedule_minutes"]).strip() != "":
        try:
            cfg.align_schedule_minutes = max(5, int(data["align_schedule_minutes"]))
        except ValueError:
            pass
    if "vd_schedule_minutes" in data and str(data["vd_schedule_minutes"]).strip() != "":
        try:
            cfg.vd_schedule_minutes = max(5, int(data["vd_schedule_minutes"]))
        except ValueError:
            pass
    if "catalog_schedule_minutes" in data and str(data["catalog_schedule_minutes"]).strip() != "":
        try:
            cfg.catalog_schedule_minutes = max(5, int(data["catalog_schedule_minutes"]))
        except ValueError:
            pass
    if "vd_unit_seconds" in data and str(data["vd_unit_seconds"]).strip() != "":
        try:
            cfg.vd_unit_seconds = max(1, int(data["vd_unit_seconds"]))
        except ValueError:
            pass
    if "vd_batch_size" in data and str(data["vd_batch_size"]).strip() != "":
        try:
            cfg.vd_batch_size = max(20, min(500, int(data["vd_batch_size"])))
        except ValueError:
            pass
    return cfg


def _run_job(cfg: Config, from_schedule: bool = False, job_key: str = "default") -> None:
    job, lock = _job_parts(job_key)
    job_log = lambda message: _log(message, job)
    try:
        if from_schedule and cfg.schedule_only_if_changed:
            cred = cfg.resolve_credentials()
            gc = authorize(cred)
            ids = [s.sid for s in normalize_sources(cfg.sources or cfg.source_urls)]
            if ids and not sources_have_changed(gc, ids, log=job_log):
                job["skipped"] = True
                job["result"] = {"ok": True, "skipped": True, "total_rows": 0, "hot_rows": 0}
                return
        result = run(
            cfg,
            sources=cfg.sources,
            source_urls=cfg.source_urls,
            target_url=cfg.target_url,
            config_url=cfg.config_url,
            start_date=to_datetime(cfg.start_date) if cfg.start_date else None,
            end_date=to_datetime(cfg.end_date) if cfg.end_date else None,
            log=job_log,
        )
        job["result"] = result
    except Exception as e:
        _record_job_failure(e, "筛选汇总", job)
    finally:
        job["running"] = False
        job["finished_at"] = datetime.now().strftime("%H:%M:%S")
        if lock.locked():
            lock.release()


def _schedule_state_key(kind: str) -> str:
    return f"{kind}_schedule_last_run"


def _schedule_last_run(kind: str) -> str:
    state = load_sync_state() or {}
    return str(state.get(_schedule_state_key(kind)) or (state.get("last_run") if kind == "filter" else "") or "")


def _remember_schedule_run(kind: str) -> None:
    state = load_sync_state() or {}
    state[_schedule_state_key(kind)] = datetime.now().isoformat(timespec="seconds")
    save_sync_state(state)


def _snap(st: dict, kind: str) -> dict:
    nxt = st.get("next_run")
    return {
        "enabled": bool(st.get("enabled")),
        "minutes": st.get("minutes"),
        "only_if_changed": st.get("only_if_changed"),
        "next_run": nxt.strftime("%Y-%m-%d %H:%M:%S") if isinstance(nxt, datetime) else "",
        "last_msg": st.get("last_msg") or "",
        "last_sync": _schedule_last_run(kind),
        "menu_id": st.get("menu_id") or "",
    }


def _schedule_snapshot() -> dict:
    return _snap(_sched, "filter")


def _align_schedule_snapshot() -> dict:
    return _snap(_align_sched, "align")


def _video_schedule_snapshot() -> dict:
    return _snap(_video_sched, "video")


def _catalog_schedule_snapshot() -> dict:
    return _snap(_catalog_sched, "catalog")


def menu_schedule_snapshot(menu_id: str) -> dict:
    with _sched_lock:
        st = _menu_schedules.get(str(menu_id) or "")
        if not st:
            return {"enabled": False, "minutes": 0, "only_if_changed": False, "next_run": "", "last_msg": "", "menu_id": menu_id}
        return _snap(st, f"menu:{menu_id}")


def _ensure_sched_thread() -> None:
    global _sched_thread
    if _sched_thread is None or not _sched_thread.is_alive():
        _sched_stop.clear()
        _sched_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _sched_thread.start()


def _template_schedule_kind(template: str) -> str | None:
    return {
        "filter": "filter",
        "align": "align",
        "video": "video",
        "custom": "video",
        "catalog": "catalog",
    }.get(str(template or ""))


def _menu_schedule_spec(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    template = str(item.get("template") or "")
    kind = _template_schedule_kind(template)
    settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
    if not kind:
        return None
    try:
        if kind == "filter":
            enabled = bool(settings.get("schedule_enabled"))
            minutes = int(settings.get("schedule_minutes") or 60)
            only = bool(settings.get("schedule_only_if_changed"))
        elif kind == "align":
            enabled = bool(settings.get("align_schedule_enabled"))
            minutes = int(settings.get("align_schedule_minutes") or 60)
            only = bool(settings.get("align_schedule_only_if_changed"))
        elif kind == "video":
            enabled = bool(settings.get("vd_schedule_enabled"))
            minutes = int(settings.get("vd_schedule_minutes") or 180)
            only = False
            if not str(settings.get("vd_source_url") or "").strip() or not str(settings.get("vd_dest_url") or "").strip():
                enabled = False
        else:
            enabled = bool(settings.get("catalog_schedule_enabled"))
            minutes = int(settings.get("catalog_schedule_minutes") or 180)
            only = False
            if not str(settings.get("catalog_index_url") or "").strip() or not str(settings.get("catalog_target_url") or "").strip():
                enabled = False
    except (TypeError, ValueError):
        return None
    if not enabled:
        return None
    return {
        "kind": kind,
        "minutes": max(5, minutes),
        "only_if_changed": only,
        "template": template,
        "menu_id": str(item.get("id") or ""),
    }


def _cfg_for_menu_id(menu_id: str) -> Config:
    stored = load_config()
    item = next((entry for entry in (stored.ui_menus or []) if str(entry.get("id") or "") == str(menu_id)), None)
    if not item:
        stored.ui_active_menu = menu_id
        return stored
    cfg = _cfg_from_payload(item.get("settings") or {}, base=Config())
    cfg.ui_menus = stored.ui_menus
    cfg.ui_active_menu = menu_id
    if not getattr(cfg, "credentials_file", ""):
        cfg.credentials_file = stored.credentials_file
    if not getattr(cfg, "credentials_files", None):
        cfg.credentials_files = list(stored.credentials_files or [])
    return cfg


def _mirror_kind_schedules_locked() -> None:
    first: dict[str, dict] = {}
    for st in _menu_schedules.values():
        kind = st.get("kind")
        if kind and kind not in first:
            first[kind] = st
    mapping = {
        "filter": _sched,
        "align": _align_sched,
        "video": _video_sched,
        "catalog": _catalog_sched,
    }
    for kind, slot in mapping.items():
        src = first.get(kind)
        if src:
            slot["enabled"] = True
            slot["minutes"] = src["minutes"]
            slot["only_if_changed"] = src.get("only_if_changed", False)
            slot["next_run"] = src.get("next_run")
            slot["last_msg"] = src.get("last_msg") or ""
            slot["menu_id"] = src.get("menu_id") or ""
        else:
            slot["enabled"] = False
            slot["next_run"] = None
            slot["last_msg"] = "已停止"
            slot["menu_id"] = ""


def sync_schedulers_from_menus(menus: list | None) -> None:
    """Start/keep timers from every menu that has them. Saving another menu will not stop these."""
    specs: dict[str, dict] = {}
    for item in menus or []:
        spec = _menu_schedule_spec(item)
        if spec and spec.get("menu_id"):
            specs[spec["menu_id"]] = spec
    now = datetime.now()
    with _sched_lock:
        for mid in list(_menu_schedules):
            if mid not in specs:
                _menu_schedules.pop(mid, None)
        for mid, spec in specs.items():
            old = _menu_schedules.get(mid)
            keep = (
                old
                and old.get("enabled")
                and old.get("minutes") == spec["minutes"]
                and isinstance(old.get("next_run"), datetime)
            )
            if keep:
                old.update(spec)
                old["enabled"] = True
                continue
            stale = _last_run_is_stale(spec["minutes"], f"menu:{mid}")
            _menu_schedules[mid] = {
                **spec,
                "enabled": True,
                "next_run": now + (timedelta(seconds=12) if stale else timedelta(minutes=spec["minutes"])),
                "last_msg": "已启动，即将执行一次" if stale else "已启动",
            }
        _mirror_kind_schedules_locked()
    if specs:
        _ensure_sched_thread()


def _try_fire(st: dict, kind: str, job_key: str = "", cfg_factory=None) -> None:
    with _sched_lock:
        enabled = st.get("enabled")
        nxt = st.get("next_run")
        minutes = st.get("minutes") or 60
        menu_id = st.get("menu_id") or ""
    if not enabled or not isinstance(nxt, datetime):
        return
    if datetime.now() < nxt:
        return
    job_key = job_key or menu_id or f"schedule:{kind}"
    labels = {"filter": "筛选汇总", "align": "表头对齐", "video": "视频时长", "catalog": "目录汇总"}
    queued = False
    try:
        cfg = cfg_factory() if cfg_factory else ( _cfg_for_menu_id(menu_id) if menu_id else load_config() )
        err = enqueue_job(kind, cfg, job_key=job_key, from_schedule=True)
        if err:
            with _sched_lock:
                st["last_msg"] = "到点时上一轮还在队列中，1 分钟后重试"
                st["next_run"] = datetime.now() + timedelta(minutes=1)
            return
        queued = True
        _log(f"{labels.get(kind, kind)}定时任务已排队（间隔 {minutes} 分钟）")
    except Exception as exc:
        _record_job_failure(exc, f"{labels.get(kind, kind)}定时调度")
    if queued:
        try:
            _remember_schedule_run(kind)
            if menu_id:
                _remember_schedule_run(f"menu:{menu_id}")
        except Exception:
            print(traceback.format_exc())
        with _sched_lock:
            if st.get("enabled"):
                st["next_run"] = datetime.now() + timedelta(minutes=max(5, int(st.get("minutes") or minutes)))
                st["last_msg"] = "已排队"
            else:
                st["next_run"] = None
            _mirror_kind_schedules_locked()


def _scheduler_loop() -> None:
    while not _sched_stop.wait(5):
        with _sched_lock:
            menu_items = list(_menu_schedules.items())
        if menu_items:
            for mid, st in menu_items:
                try:
                    _try_fire(st, st.get("kind") or "filter", job_key=mid)
                except Exception:
                    _log(f"定时器异常（{mid}），请查看本机运行日志")
                    print(traceback.format_exc())
                    with _sched_lock:
                        if st.get("enabled"):
                            st["next_run"] = datetime.now() + timedelta(minutes=1)
                            st["last_msg"] = "定时器异常，1 分钟后重试"
                            _mirror_kind_schedules_locked()
            continue
        for st, kind in (
            (_sched, "filter"),
            (_align_sched, "align"),
            (_video_sched, "video"),
            (_catalog_sched, "catalog"),
        ):
            try:
                _try_fire(st, kind)
            except Exception as exc:
                _log(f"定时器异常（{kind}），请查看本机运行日志")
                print(traceback.format_exc())
                with _sched_lock:
                    if st["enabled"]:
                        st["next_run"] = datetime.now() + timedelta(minutes=1)
                        st["last_msg"] = "定时器异常，1 分钟后重试"


def _last_run_is_stale(minutes: int, kind: str = "filter") -> bool:
    raw = _schedule_last_run(kind)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw).replace("Z", ""))
    except ValueError:
        return True
    return datetime.now() - last >= timedelta(minutes=max(5, int(minutes)))


def start_scheduler(minutes: int, only_if_changed: bool) -> None:
    minutes = max(5, int(minutes))
    with _sched_lock:
        preserve_next = (
            _sched["enabled"]
            and _sched["minutes"] == minutes
            and isinstance(_sched["next_run"], datetime)
        )
        _sched["enabled"] = True
        _sched["minutes"] = minutes
        _sched["only_if_changed"] = bool(only_if_changed)
        # 每天打开软件：若距上次已超过间隔，十几秒后先跑一轮，不必空等 60 分钟
        if preserve_next:
            pass
        elif _last_run_is_stale(minutes, "filter"):
            _sched["next_run"] = datetime.now() + timedelta(seconds=12)
            _sched["last_msg"] = "已启动，即将执行一次"
        else:
            _sched["next_run"] = datetime.now() + timedelta(minutes=minutes)
            _sched["last_msg"] = "已启动"
    _ensure_sched_thread()


def stop_scheduler() -> None:
    with _sched_lock:
        _sched["enabled"] = False
        _sched["next_run"] = None
        _sched["last_msg"] = "已停止"


def start_align_scheduler(minutes: int, only_if_changed: bool) -> None:
    minutes = max(5, int(minutes))
    with _sched_lock:
        preserve_next = (
            _align_sched["enabled"]
            and _align_sched["minutes"] == minutes
            and isinstance(_align_sched["next_run"], datetime)
        )
        _align_sched["enabled"] = True
        _align_sched["minutes"] = minutes
        _align_sched["only_if_changed"] = bool(only_if_changed)
        if not preserve_next:
            delay = timedelta(seconds=12) if _last_run_is_stale(minutes, "align") else timedelta(minutes=minutes)
            _align_sched["next_run"] = datetime.now() + delay
            _align_sched["last_msg"] = "已启动，即将执行一次" if delay.seconds == 12 else "已启动"
    _ensure_sched_thread()


def stop_align_scheduler() -> None:
    with _sched_lock:
        _align_sched["enabled"] = False
        _align_sched["next_run"] = None
        _align_sched["last_msg"] = "已停止"


def start_video_scheduler(minutes: int) -> None:
    minutes = max(5, int(minutes))
    with _sched_lock:
        preserve_next = (
            _video_sched["enabled"]
            and _video_sched["minutes"] == minutes
            and isinstance(_video_sched["next_run"], datetime)
        )
        _video_sched["enabled"] = True
        _video_sched["minutes"] = minutes
        if not preserve_next:
            delay = timedelta(seconds=12) if _last_run_is_stale(minutes, "video") else timedelta(minutes=minutes)
            _video_sched["next_run"] = datetime.now() + delay
            _video_sched["last_msg"] = "已启动，即将执行一次" if delay.seconds == 12 else "已启动"
    _ensure_sched_thread()


def stop_video_scheduler() -> None:
    with _sched_lock:
        _video_sched["enabled"] = False
        _video_sched["next_run"] = None
        _video_sched["last_msg"] = "已停止"


def start_catalog_scheduler(minutes: int) -> None:
    minutes = max(5, int(minutes))
    with _sched_lock:
        preserve_next = (
            _catalog_sched["enabled"]
            and _catalog_sched["minutes"] == minutes
            and isinstance(_catalog_sched["next_run"], datetime)
        )
        _catalog_sched["enabled"] = True
        _catalog_sched["minutes"] = minutes
        if not preserve_next:
            delay = timedelta(seconds=12) if _last_run_is_stale(minutes, "catalog") else timedelta(minutes=minutes)
            _catalog_sched["next_run"] = datetime.now() + delay
            _catalog_sched["last_msg"] = "已启动，即将执行一次" if delay.seconds == 12 else "已启动"
    _ensure_sched_thread()


def stop_catalog_scheduler() -> None:
    with _sched_lock:
        _catalog_sched["enabled"] = False
        _catalog_sched["next_run"] = None
        _catalog_sched["last_msg"] = "已停止"


def stop_all_menu_schedulers() -> None:
    with _sched_lock:
        _menu_schedules.clear()
        _mirror_kind_schedules_locked()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def api_config():
    cfg = load_config()
    email = ""
    cred_ok = False
    try:
        cred = cfg.resolve_credentials()
        cred_ok = cred.exists()
        email = service_account_email(cred)
        if not cfg.credentials_file:
            cfg.credentials_file = str(cred)
    except Exception:
        pass
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    return jsonify(
        {
            "config": {
                "credentials_file": cfg.credentials_file,
                "credentials_files": cfg.credentials_files or ([cfg.credentials_file] if cfg.credentials_file else []),
                "target_url": cfg.target_url,
                "hot_target_url": cfg.hot_target_url,
                "hot_output_sheet": cfg.hot_output_sheet,
                "sources": cfg.sources,
                "source_urls": cfg.source_urls,
                "fields": cfg.fields or copy_default_fields(),
                "output_sheet": cfg.output_sheet,
                "output_start_row": cfg.output_start_row,
                "include_headers": cfg.include_headers,
                "hot_start_row": cfg.hot_start_row,
                "hot_include_headers": cfg.hot_include_headers,
                "add_source_column": cfg.add_source_column,
                "exclude_id_value": cfg.exclude_id_value,
                "date_field": cfg.date_field,
                "id_field": cfg.id_field,
                "sort_field": cfg.sort_field,
                "sort_descending": cfg.sort_descending,
                "start_date": cfg.start_date or month_start,
                "end_date": cfg.end_date or today.isoformat(),
                "likes_threshold": cfg.likes_threshold,
                "write_all": cfg.write_all,
                "write_hot": cfg.write_hot,
                "upsert_by_id": cfg.upsert_by_id,
                "schedule_enabled": cfg.schedule_enabled,
                "schedule_minutes": cfg.schedule_minutes,
                "schedule_only_if_changed": cfg.schedule_only_if_changed,
                "align_sources": cfg.align_sources,
                "align_target_url": cfg.align_target_url,
                "align_output_sheet": cfg.align_output_sheet,
                "align_start_row": cfg.align_start_row,
                "align_include_headers": cfg.align_include_headers,
                "align_source_sheet": cfg.align_source_sheet,
                "align_header_row": cfg.align_header_row,
                "align_headers": cfg.align_headers,
                "align_mappings": cfg.align_mappings,
                "align_mapping_profiles": cfg.align_mapping_profiles,
                "align_schedule_enabled": cfg.align_schedule_enabled,
                "align_schedule_minutes": cfg.align_schedule_minutes,
                "align_schedule_only_if_changed": cfg.align_schedule_only_if_changed,
                "vd_schedule_enabled": cfg.vd_schedule_enabled,
                "vd_schedule_minutes": cfg.vd_schedule_minutes,
                "vd_source_url": cfg.vd_source_url,
                "vd_source_sheet": cfg.vd_source_sheet,
                "vd_source_sheets": cfg.vd_source_sheets,
                "vd_start_row": cfg.vd_start_row,
                "vd_col_date": cfg.vd_col_date,
                "vd_col_link": cfg.vd_col_link,
                "vd_col_name": cfg.vd_col_name,
                "vd_col_type": cfg.vd_col_type,
                "vd_types": cfg.vd_types,
                "vd_dest_url": cfg.vd_dest_url,
                "vd_log_sheet": cfg.vd_log_sheet,
                "vd_report_sheet": cfg.vd_report_sheet,
                "vd_out_start_row": cfg.vd_out_start_row,
                "vd_include_headers": cfg.vd_include_headers,
                "vd_unit_seconds": cfg.vd_unit_seconds,
                "vd_count_mode": cfg.vd_count_mode,
                "vd_start_date": cfg.vd_start_date,
                "vd_end_date": cfg.vd_end_date,
                "vd_batch_size": cfg.vd_batch_size,
                "vd_date_filter_enabled": cfg.vd_date_filter_enabled,
                "vd_type_filter_mode": cfg.vd_type_filter_mode,
                "vd_write_log": cfg.vd_write_log,
                "vd_columns": cfg.vd_columns,
                "catalog_index_url": cfg.catalog_index_url,
                "catalog_index_sheet": cfg.catalog_index_sheet,
                "catalog_start_row": cfg.catalog_start_row,
                "catalog_url_col": cfg.catalog_url_col,
                "catalog_sheet_col": cfg.catalog_sheet_col,
                "catalog_target_url": cfg.catalog_target_url,
                "catalog_output_sheet": cfg.catalog_output_sheet,
                "catalog_output_start_row": cfg.catalog_output_start_row,
                "catalog_keep_each_header": cfg.catalog_keep_each_header,
                "catalog_add_source": cfg.catalog_add_source,
                "catalog_skip_existing": cfg.catalog_skip_existing,
                "catalog_date_filter_enabled": cfg.catalog_date_filter_enabled,
                "catalog_date_col": cfg.catalog_date_col,
                "catalog_start_date": cfg.catalog_start_date,
                "catalog_end_date": cfg.catalog_end_date,
                "catalog_exclude_sheets": cfg.catalog_exclude_sheets,
                "ui_menus": cfg.ui_menus,
                "ui_active_menu": cfg.ui_active_menu,
                "cf_publish_url": cfg.cf_publish_url,
                "cf_publish_secret": cfg.cf_publish_secret,
                "cf_publish_after_sync": cfg.cf_publish_after_sync,
                "cf_publish_source": cfg.cf_publish_source,
            },
            "service_account_email": email,
            "credentials_ok": cred_ok,
            "default_fields": DEFAULT_FIELDS,
            "schedule": _schedule_snapshot(),
            "align_schedule": _align_schedule_snapshot(),
            "video_schedule": _video_schedule_snapshot(),
        }
    )


def start_filter_job(cfg: Config, from_schedule: bool = False) -> str | None:
    save_config(cfg)
    return enqueue_job("filter", cfg, from_schedule=from_schedule)


def start_publish_job(cfg: Config) -> str | None:
    job_key = _job_key(cfg)
    job, lock = _job_parts(job_key)
    if not lock.acquire(blocking=False):
        return "这个配置正在运行中，请等它结束"
    save_config(cfg)
    _reset_job(job)

    def _run_publish():
        try:
            from publish_cloudflare import (
                overlay_formula_urls,
                publish_assets_to_cloudflare,
                split_sheet_for_publish,
            )

            gc = authorize_cfg(cfg)
            src = str(cfg.cf_publish_source or "all").strip().lower()
            if src == "hot":
                url = cfg.hot_target_url or cfg.target_url
                sheet = cfg.hot_output_sheet or "点赞1000以上"
                start = int(cfg.hot_start_row or 1)
                include_headers = bool(cfg.hot_include_headers)
            else:
                url = cfg.target_url
                sheet = cfg.output_sheet
                start = int(cfg.output_start_row or 1)
                include_headers = bool(cfg.include_headers)
            if not url:
                raise RuntimeError("请先填写要发布的目标表链接")
            job_log = lambda message: _log(message, job)
            ss = open_by_url_or_id(gc, url, log=job_log)
            ws = ss.worksheet(sheet)
            values = read_sheet_values(ws, log=job_log)
            headers, rows, how, data_start = split_sheet_for_publish(
                values, start, include_headers
            )
            job_log(
                f"从「{ss.title}」/{sheet} 读取 {len(rows)} 行（{how}），开始发布 Cloudflare"
            )
            overlay_formula_urls(ws, rows, data_start, log=job_log)
            result = publish_assets_to_cloudflare(
                cfg, headers, rows, None, log=job_log, start_row=data_start
            )
            job["result"] = {"ok": True, "mode": "cloudflare", **result}
        except Exception as e:
            _record_job_failure(e, "发布", job)
        finally:
            job["running"] = False
            job["finished_at"] = datetime.now().strftime("%H:%M:%S")
            if lock.locked():
                lock.release()

    threading.Thread(target=_run_publish, daemon=True).start()
    return None


def start_align_job(cfg: Config, from_schedule: bool = False) -> str | None:
    if not cfg.align_sources:
        return "请至少填写一个数据源表格链接"
    if not cfg.align_target_url:
        return "请填写目标表链接"
    if not (cfg.align_mappings or cfg.align_headers):
        return "请配置字段映射"
    save_config(cfg)
    return enqueue_job("align", cfg, from_schedule=from_schedule)


def _run_video_job(cfg: Config, from_schedule: bool = False, job_key: str = "default") -> None:
    job, lock = _job_parts(job_key)
    try:
        from video_duration import run_video_duration

        result = run_video_duration(
            cfg,
            log=lambda message: _log(message, job),
            cancelled=lambda: job_cancelled(job_key),
        )
        job["result"] = result
    except Exception as e:
        _record_job_failure(e, "视频汇总", job)
    finally:
        job["running"] = False
        job["finished_at"] = datetime.now().strftime("%H:%M:%S")
        if lock.locked():
            lock.release()


def start_video_job(cfg: Config) -> str | None:
    if not (getattr(cfg, "vd_source_url", "") or "").strip():
        return "请填写视频时长源表链接"
    if not (getattr(cfg, "vd_dest_url", "") or "").strip():
        return "请填写写入目标表格链接"
    save_config(cfg)
    return enqueue_job("video", cfg)


def _run_catalog_job(cfg: Config, job_key: str = "default") -> None:
    job, lock = _job_parts(job_key)
    try:
        job["result"] = run_catalog_merge(
            cfg,
            log=lambda message: _log(message, job),
            cancelled=lambda: job_cancelled(job_key),
        )
    except Exception as e:
        _record_job_failure(e, "目录汇总", job)
    finally:
        job["running"] = False
        job["finished_at"] = datetime.now().strftime("%H:%M:%S")
        if lock.locked():
            lock.release()


def start_catalog_job(cfg: Config) -> str | None:
    if not cfg.catalog_index_url:
        return "请填写目录表链接"
    if not cfg.catalog_target_url:
        return "请填写目标表链接"
    save_config(cfg)
    return enqueue_job("catalog", cfg)


def _run_roster_job(cfg: Config, job_key: str = "default") -> None:
    job, lock = _job_parts(job_key)
    try:
        job["result"] = run_roster_fill(
            cfg,
            log=lambda message: _log(message, job),
            cancelled=lambda: job_cancelled(job_key),
        )
    except Exception as e:
        _record_job_failure(e, "队别专页汇总", job)
    finally:
        job["running"] = False
        job["finished_at"] = datetime.now().strftime("%H:%M:%S")
        if lock.locked():
            lock.release()


def start_roster_job(cfg: Config) -> str | None:
    if not (getattr(cfg, "roster_config_url", "") or "").strip():
        return "请填写配置表链接"
    if not (getattr(cfg, "roster_target_url", "") or "").strip():
        return "请填写写入目标表链接"
    save_config(cfg)
    return enqueue_job("roster", cfg)


@app.post("/api/run-catalog")
def api_run_catalog():
    cfg = _cfg_from_payload(request.get_json(force=True) or {})
    err = start_catalog_job(cfg)
    if err:
        return jsonify({"ok": False, "error": err}), 409 if "运行中" in err else 400
    return jsonify({"ok": True})


@app.post("/api/run-video")
def api_run_video():
    data = request.get_json(force=True) or {}
    cfg = _cfg_from_payload(data)
    err = start_video_job(cfg)
    if err:
        code = 409 if "运行中" in err else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify({"ok": True})


@app.post("/api/publish-cf")
def api_publish_cf():
    data = request.get_json(force=True) or {}
    cfg = _cfg_from_payload(data)
    err = start_publish_job(cfg)
    if err:
        return jsonify({"ok": False, "error": err}), 409
    return jsonify({"ok": True})


@app.post("/api/save")
def api_save():
    cfg = _cfg_from_payload(request.get_json(force=True) or {})
    path = save_config(cfg)
    return jsonify({"ok": True, "path": str(path)})


@app.post("/api/run")
def api_run():
    data = request.get_json(force=True) or {}
    cfg = _cfg_from_payload(data)
    if not parse_url_list(cfg.source_urls) and not cfg.sources:
        return jsonify({"ok": False, "error": "请至少填写一个数据源表格链接"}), 400
    if not cfg.start_date or not cfg.end_date:
        return jsonify({"ok": False, "error": "请填写开始和结束日期"}), 400
    if not cfg.write_all and not cfg.write_hot:
        return jsonify({"ok": False, "error": "请至少勾选：写入全部，或写入高赞"}), 400
    if cfg.write_all and not cfg.target_url:
        return jsonify({"ok": False, "error": "请填写「全部结果」目标表链接"}), 400
    if cfg.write_hot and not (cfg.hot_target_url or cfg.target_url):
        return jsonify({"ok": False, "error": "请填写高赞目标表链接（可与全部结果同一张表）"}), 400
    err = start_filter_job(cfg, from_schedule=False)
    if err:
        return jsonify({"ok": False, "error": err}), 409
    return jsonify({"ok": True})


def _run_align_job(cfg: Config, from_schedule: bool = False, job_key: str = "default") -> None:
    job, lock = _job_parts(job_key)
    job_log = lambda message: _log(message, job)
    try:
        if from_schedule and cfg.align_schedule_only_if_changed:
            cred = cfg.resolve_credentials()
            gc = authorize(cred)
            ids = [s.sid for s in normalize_sources(cfg.align_sources)]
            if ids and not sources_have_changed(gc, ids, log=job_log):
                job["skipped"] = True
                job["result"] = {"ok": True, "skipped": True, "mode": "align", "total_rows": 0}
                return
        result = run_align_sync(cfg, log=job_log)
        job["result"] = result
    except Exception as e:
        _record_job_failure(e, "字段映射", job)
    finally:
        job["running"] = False
        job["finished_at"] = datetime.now().strftime("%H:%M:%S")
        if lock.locked():
            lock.release()


@app.post("/api/run-align")
def api_run_align():
    data = request.get_json(force=True) or {}
    cfg = _cfg_from_payload(data)
    if not cfg.align_sources:
        return jsonify({"ok": False, "error": "请至少填写一个数据源表格链接"}), 400
    if not cfg.align_target_url:
        return jsonify({"ok": False, "error": "请填写目标表链接"}), 400
    if not (cfg.align_mappings or cfg.align_headers):
        return jsonify({"ok": False, "error": "请配置字段映射"}), 400
    err = start_align_job(cfg)
    if err:
        return jsonify({"ok": False, "error": err}), 409
    return jsonify({"ok": True})


@app.post("/api/peek-headers")
def api_peek_headers():
    data = request.get_json(force=True) or {}
    cfg = _cfg_from_payload(data)
    url = ""
    if data.get("url"):
        url = str(data["url"]).strip()
    elif cfg.align_sources:
        url = cfg.align_sources[0].get("url") or ""
    if not url:
        return jsonify({"ok": False, "error": "请先填写至少一个数据源链接"}), 400
    try:
        headers = peek_source_headers(
            cfg,
            url,
            sheet_name=str(
                data.get("sheet")
                or (cfg.align_sources[0].get("sheet") if cfg.align_sources else "")
                or cfg.align_source_sheet
                or ""
            ),
            header_row=int(data.get("header_row") or cfg.align_header_row or 1),
        )
    except Exception:
        # 详细异常只留在本机控制台，避免把路径、Google API 响应或内部
        # 实现信息通过 HTTP 接口返回给调用者（CWE-209 / CWE-497）。
        print(traceback.format_exc())
        return jsonify({"ok": False, "error": "读取表头失败，请查看本机运行日志"}), 500
    return jsonify({"ok": True, "headers": headers})


@app.post("/api/schedule")
def api_schedule():
    data = request.get_json(force=True) or {}
    cfg = _cfg_from_payload(data)
    enabled = bool(data.get("schedule_enabled"))
    cfg.schedule_enabled = enabled
    save_config(cfg)
    if enabled:
        if not cfg.sources and not cfg.source_urls:
            return jsonify({"ok": False, "error": "定时前请先填好数据源链接"}), 400
        start_scheduler(cfg.schedule_minutes, cfg.schedule_only_if_changed)
    else:
        stop_scheduler()
    return jsonify({"ok": True, "schedule": _schedule_snapshot()})


@app.post("/api/align-schedule")
def api_align_schedule():
    data = request.get_json(force=True) or {}
    cfg = _cfg_from_payload(data)
    enabled = bool(data.get("align_schedule_enabled"))
    cfg.align_schedule_enabled = enabled
    save_config(cfg)
    if enabled:
        if not cfg.align_sources:
            return jsonify({"ok": False, "error": "定时前请先填好数据源链接和工作表名称"}), 400
        if not cfg.align_target_url or not (cfg.align_mappings or cfg.align_headers):
            return jsonify({"ok": False, "error": "定时前请先填好目标表和字段映射"}), 400
        start_align_scheduler(cfg.align_schedule_minutes, cfg.align_schedule_only_if_changed)
    else:
        stop_align_scheduler()
    return jsonify({"ok": True, "align_schedule": _align_schedule_snapshot()})


@app.post("/api/video-schedule")
def api_video_schedule():
    data = request.get_json(force=True) or {}
    cfg = _cfg_from_payload(data)
    enabled = bool(data.get("vd_schedule_enabled"))
    cfg.vd_schedule_enabled = enabled
    save_config(cfg)
    if enabled:
        if not cfg.vd_source_url or not cfg.vd_dest_url:
            return jsonify({"ok": False, "error": "定时前请先填好源表和目标表链接"}), 400
        start_video_scheduler(cfg.vd_schedule_minutes)
    else:
        stop_video_scheduler()
    return jsonify({"ok": True, "video_schedule": _video_schedule_snapshot()})


@app.get("/api/status")
def api_status():
    job = job_snapshot(request.args.get("job_id", "default"))
    return jsonify(
        {
            "running": job["running"],
            "logs": job["logs"],
            "result": job["result"],
            "error": job["error"],
            "skipped": job.get("skipped"),
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "schedule": _schedule_snapshot(),
            "align_schedule": _align_schedule_snapshot(),
            "video_schedule": _video_schedule_snapshot(),
        }
    )


def main() -> None:
    import socket

    os.chdir(SCRIPT_DIR)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    port = 8765
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        already = probe.connect_ex(("127.0.0.1", port)) == 0
    if already:
        url = f"http://127.0.0.1:{port}"
        print(f"已经在运行，打开 {url}")
        webbrowser.open(url)
        return

    url = f"http://127.0.0.1:{port}"
    print(f"界面地址: {url}")
    print("请保持这个窗口开着。关掉后定时同步会停止。")
    cfg = load_config()
    if cfg.schedule_enabled:
        start_scheduler(cfg.schedule_minutes, cfg.schedule_only_if_changed)
        print(f"已按配置恢复筛选定时：每 {cfg.schedule_minutes} 分钟")
        if _last_run_is_stale(cfg.schedule_minutes):
            print("距上次同步已超过间隔，约 12 秒后自动跑一轮")
    if cfg.align_schedule_enabled:
        start_align_scheduler(cfg.align_schedule_minutes, cfg.align_schedule_only_if_changed)
        print(f"已按配置恢复对齐定时：每 {cfg.align_schedule_minutes} 分钟")
    if cfg.vd_schedule_enabled and cfg.vd_source_url and cfg.vd_dest_url:
        start_video_scheduler(cfg.vd_schedule_minutes)
        print(f"已按配置恢复视频时长定时：每 {cfg.vd_schedule_minutes} 分钟")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def _pause_on_error() -> None:
    try:
        input("出错了，按回车关闭窗口。")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        _pause_on_error()
        raise
