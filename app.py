# -*- coding: utf-8 -*-
"""本地网页界面：多数据源汇总，可写全部/高赞两张表，支持定时同步。"""

from __future__ import annotations

import threading
import webbrowser
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, render_template, request

from fetch_posts import (
    SCRIPT_DIR,
    DEFAULT_FIELDS,
    Config,
    authorize,
    copy_default_fields,
    field_from_dict,
    load_config,
    load_sync_state,
    normalize_sources,
    parse_url_list,
    peek_source_headers,
    run,
    run_align_sync,
    save_config,
    service_account_email,
    sources_have_changed,
    to_datetime,
)

app = Flask(
    __name__,
    template_folder=str(SCRIPT_DIR / "web"),
    static_folder=str(SCRIPT_DIR / "web" / "static"),
)

_job_lock = threading.Lock()
_job: dict = {
    "running": False,
    "logs": [],
    "result": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
    "skipped": False,
}

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


def _reset_job() -> None:
    _job["running"] = True
    _job["logs"] = []
    _job["result"] = None
    _job["error"] = None
    _job["skipped"] = False
    _job["started_at"] = datetime.now().strftime("%H:%M:%S")
    _job["finished_at"] = None


def _log(msg: str) -> None:
    line = {"t": datetime.now().strftime("%H:%M:%S"), "msg": str(msg)}
    _job["logs"].append(line)
    print(f"[{line['t']}] {msg}")


def _cfg_from_payload(data: dict) -> Config:
    cfg = load_config()
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
    }
    for src, dest in mapping.items():
        if src in data and data[src] is not None:
            setattr(cfg, dest, str(data[src]).strip())
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
    if "align_headers" in data:
        raw_h = data.get("align_headers")
        if isinstance(raw_h, str):
            cfg.align_headers = [ln.strip() for ln in raw_h.splitlines() if ln.strip()]
        elif isinstance(raw_h, list):
            cfg.align_headers = [str(x).strip() for x in raw_h if str(x).strip()]
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
    ):
        if flag in data:
            setattr(cfg, flag, bool(data[flag]))
    for key in ("output_start_row", "hot_start_row", "align_start_row", "align_header_row"):
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
    return cfg


def _run_job(cfg: Config, from_schedule: bool = False) -> None:
    try:
        if from_schedule and cfg.schedule_only_if_changed:
            cred = cfg.resolve_credentials()
            gc = authorize(cred)
            ids = [s.sid for s in normalize_sources(cfg.sources or cfg.source_urls)]
            if ids and not sources_have_changed(gc, ids, log=_log):
                _job["skipped"] = True
                _job["result"] = {"ok": True, "skipped": True, "total_rows": 0, "hot_rows": 0}
                return
        result = run(
            cfg,
            sources=cfg.sources,
            source_urls=cfg.source_urls,
            target_url=cfg.target_url,
            config_url=cfg.config_url,
            start_date=to_datetime(cfg.start_date) if cfg.start_date else None,
            end_date=to_datetime(cfg.end_date) if cfg.end_date else None,
            log=_log,
        )
        _job["result"] = result
    except Exception as e:
        _job["error"] = str(e)
        _log(f"失败: {e}")
    finally:
        _job["running"] = False
        _job["finished_at"] = datetime.now().strftime("%H:%M:%S")
        _job_lock.release()


def _snap(st: dict) -> dict:
    nxt = st["next_run"]
    return {
        "enabled": st["enabled"],
        "minutes": st["minutes"],
        "only_if_changed": st["only_if_changed"],
        "next_run": nxt.strftime("%Y-%m-%d %H:%M:%S") if isinstance(nxt, datetime) else "",
        "last_msg": st["last_msg"],
        "last_sync": (load_sync_state() or {}).get("last_run") or "",
    }


def _schedule_snapshot() -> dict:
    return _snap(_sched)


def _align_schedule_snapshot() -> dict:
    return _snap(_align_sched)


def _ensure_sched_thread() -> None:
    global _sched_thread
    if _sched_thread is None or not _sched_thread.is_alive():
        _sched_stop.clear()
        _sched_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _sched_thread.start()


def _try_fire(st: dict, kind: str) -> None:
    with _sched_lock:
        enabled = st["enabled"]
        nxt = st["next_run"]
        minutes = st["minutes"]
    if not enabled or not isinstance(nxt, datetime):
        return
    if datetime.now() < nxt:
        return
    if not _job_lock.acquire(blocking=False):
        st["last_msg"] = "到点时上一轮还在跑，稍后重试"
        with _sched_lock:
            st["next_run"] = datetime.now() + timedelta(minutes=1)
        return
    cfg = load_config()
    _reset_job()
    _log(f"{'表头对齐' if kind == 'align' else '筛选汇总'}定时任务开始（间隔 {minutes} 分钟）")
    if kind == "align":
        _run_align_job(cfg, from_schedule=True)
    else:
        _run_job(cfg, from_schedule=True)
    with _sched_lock:
        if st["enabled"]:
            st["next_run"] = datetime.now() + timedelta(minutes=max(5, st["minutes"]))
            st["last_msg"] = "已执行"
        else:
            st["next_run"] = None


def _scheduler_loop() -> None:
    while not _sched_stop.wait(5):
        _try_fire(_sched, "filter")
        _try_fire(_align_sched, "align")


def start_scheduler(minutes: int, only_if_changed: bool) -> None:
    with _sched_lock:
        _sched["enabled"] = True
        _sched["minutes"] = max(5, int(minutes))
        _sched["only_if_changed"] = bool(only_if_changed)
        _sched["next_run"] = datetime.now() + timedelta(minutes=_sched["minutes"])
        _sched["last_msg"] = "已启动"
    _ensure_sched_thread()


def stop_scheduler() -> None:
    with _sched_lock:
        _sched["enabled"] = False
        _sched["next_run"] = None
        _sched["last_msg"] = "已停止"


def start_align_scheduler(minutes: int, only_if_changed: bool) -> None:
    with _sched_lock:
        _align_sched["enabled"] = True
        _align_sched["minutes"] = max(5, int(minutes))
        _align_sched["only_if_changed"] = bool(only_if_changed)
        _align_sched["next_run"] = datetime.now() + timedelta(minutes=_align_sched["minutes"])
        _align_sched["last_msg"] = "已启动"
    _ensure_sched_thread()


def stop_align_scheduler() -> None:
    with _sched_lock:
        _align_sched["enabled"] = False
        _align_sched["next_run"] = None
        _align_sched["last_msg"] = "已停止"


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
                "align_schedule_enabled": cfg.align_schedule_enabled,
                "align_schedule_minutes": cfg.align_schedule_minutes,
                "align_schedule_only_if_changed": cfg.align_schedule_only_if_changed,
            },
            "service_account_email": email,
            "credentials_ok": cred_ok,
            "default_fields": DEFAULT_FIELDS,
            "schedule": _schedule_snapshot(),
            "align_schedule": _align_schedule_snapshot(),
        }
    )


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
    if not _job_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "正在运行中，请等当前任务结束"}), 409
    save_config(cfg)
    _reset_job()
    t = threading.Thread(target=_run_job, args=(cfg, False), daemon=True)
    t.start()
    return jsonify({"ok": True})


def _run_align_job(cfg: Config, from_schedule: bool = False) -> None:
    try:
        if from_schedule and cfg.align_schedule_only_if_changed:
            cred = cfg.resolve_credentials()
            gc = authorize(cred)
            ids = [s.sid for s in normalize_sources(cfg.align_sources)]
            if ids and not sources_have_changed(gc, ids, log=_log):
                _job["skipped"] = True
                _job["result"] = {"ok": True, "skipped": True, "mode": "align", "total_rows": 0}
                return
        result = run_align_sync(cfg, log=_log)
        _job["result"] = result
    except Exception as e:
        _job["error"] = str(e)
        _log(f"失败: {e}")
    finally:
        _job["running"] = False
        _job["finished_at"] = datetime.now().strftime("%H:%M:%S")
        _job_lock.release()


@app.post("/api/run-align")
def api_run_align():
    data = request.get_json(force=True) or {}
    cfg = _cfg_from_payload(data)
    if not cfg.align_sources:
        return jsonify({"ok": False, "error": "请至少填写一个数据源表格链接"}), 400
    if not cfg.align_target_url:
        return jsonify({"ok": False, "error": "请填写目标表链接"}), 400
    if not cfg.align_headers:
        return jsonify({"ok": False, "error": "请配置规范表头（一行一个）"}), 400
    if not _job_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "正在运行中，请等当前任务结束"}), 409
    save_config(cfg)
    _reset_job()
    t = threading.Thread(target=_run_align_job, args=(cfg,), daemon=True)
    t.start()
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
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
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
        if not cfg.align_target_url or not cfg.align_headers:
            return jsonify({"ok": False, "error": "定时前请先填好目标表和规范表头"}), 400
        start_align_scheduler(cfg.align_schedule_minutes, cfg.align_schedule_only_if_changed)
    else:
        stop_align_scheduler()
    return jsonify({"ok": True, "align_schedule": _align_schedule_snapshot()})


@app.get("/api/status")
def api_status():
    return jsonify(
        {
            "running": _job["running"],
            "logs": _job["logs"],
            "result": _job["result"],
            "error": _job["error"],
            "skipped": _job.get("skipped"),
            "started_at": _job["started_at"],
            "finished_at": _job["finished_at"],
            "schedule": _schedule_snapshot(),
            "align_schedule": _align_schedule_snapshot(),
        }
    )


def main() -> None:
    import socket

    port = 8765
    for p in range(8765, 8785):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                port = p
                break
    url = f"http://127.0.0.1:{port}"
    print(f"界面地址: {url}")
    cfg = load_config()
    if cfg.schedule_enabled:
        start_scheduler(cfg.schedule_minutes, cfg.schedule_only_if_changed)
        print(f"已按配置恢复筛选定时：每 {cfg.schedule_minutes} 分钟")
    if cfg.align_schedule_enabled:
        start_align_scheduler(cfg.align_schedule_minutes, cfg.align_schedule_only_if_changed)
        print(f"已按配置恢复对齐定时：每 {cfg.align_schedule_minutes} 分钟")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
