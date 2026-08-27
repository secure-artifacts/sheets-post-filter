# -*- coding: utf-8 -*-
"""桌面工具：左侧配置菜单 + 右侧独立汇总模板。"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
import copy
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from fetch_posts import (
    LOG_DIR,
    RESOURCE_DIR,
    SCRIPT_DIR,
    authorize,
    copy_default_fields,
    load_config,
    peek_source_headers,
    save_config,
    service_account_email,
    write_app_log,
)

from app import (
    _align_schedule_snapshot,
    _catalog_schedule_snapshot,
    _cfg_from_payload,
    job_snapshot,
    _schedule_snapshot,
    _video_schedule_snapshot,
    start_align_job,
    start_catalog_job,
    start_catalog_scheduler,
    start_roster_job,
    start_align_scheduler,
    start_filter_job,
    start_publish_job,
    start_scheduler,
    start_video_job,
    start_video_scheduler,
    stop_align_scheduler,
    stop_all_jobs,
    stop_catalog_scheduler,
    stop_job,
    stop_scheduler,
    stop_video_scheduler,
)
from version import APP_VERSION, RELEASES_URL, UPDATE_API_URL, version_tuple

MUTEX_PORT = 18765
C = {
    "ink": "#134e4a",
    "muted": "#5b7a74",
    "paper": "#eef8f5",
    "card": "#ffffff",
    "line": "#cfe8e0",
    "head": "#0f766e",
    "accent": "#14b8a6",
    "ok": "#059669",
    "bad": "#e11d48",
    "log": "#0f2926",
    "cream": "#f0fdfa",
    "sa": "#115e59",
}
F = ("Microsoft YaHei UI", 10)
FS = ("Microsoft YaHei UI", 9)
FB = ("Microsoft YaHei UI", 11, "bold")
FH = ("Microsoft YaHei UI", 16, "bold")
MONO = ("Cascadia Mono", 9)


def _mutex_socket() -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", MUTEX_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def _bring_existing_window() -> bool:
    """Restore an already-running instance instead of silently exiting.

    Title-only matching is not enough: Explorer often has a folder named
    「数据汇总工具」(the dist directory). Restoring that folder made a second
    launch look like the app opened and immediately quit.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        GetWindowTextW = user32.GetWindowTextW
        GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        GetClassNameW = user32.GetClassNameW
        GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        found = []

        def cb(hwnd, _lparam):
            title = ctypes.create_unicode_buffer(256)
            GetWindowTextW(hwnd, title, 256)
            if title.value != "数据汇总工具":
                return True
            cls = ctypes.create_unicode_buffer(256)
            GetClassNameW(hwnd, cls, 256)
            # Tk windows are TkTopLevel; Explorer folders are CabinetWClass.
            if cls.value == "TkTopLevel":
                found.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_SHOWWINDOW = 0x0040
        for hwnd in found:
            user32.ShowWindow(hwnd, 9)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 80, 40, 1200, 840, SWP_SHOWWINDOW)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 80, 40, 1200, 840, SWP_SHOWWINDOW)
        return bool(found)
    except Exception:
        return False


def _pids_listening_on_mutex() -> list[int]:
    pids: list[int] = []
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return pids
    needle = f"127.0.0.1:{MUTEX_PORT}"
    for line in out.splitlines():
        if needle not in line:
            continue
        upper = line.upper()
        if "LISTENING" not in upper and "LISTEN" not in upper:
            continue
        try:
            pids.append(int(line.split()[-1]))
        except ValueError:
            continue
    return pids


def _process_name(pid: int) -> str:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return ""
    text = out.strip()
    if not text or text.lower().startswith("info:"):
        return ""
    return text.strip('"').split(",")[0].strip('"')


def _kill_stale_mutex() -> bool:
    """Kill a leftover instance that holds the single-instance port but has no window."""
    killed = False
    my_pid = os.getpid()
    for pid in _pids_listening_on_mutex():
        if pid == my_pid:
            continue
        name = _process_name(pid)
        if "数据汇总工具" not in name and name.lower() not in ("python.exe", "pythonw.exe"):
            continue
        try:
            subprocess.check_call(
                ["taskkill", "/PID", str(pid), "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            killed = True
        except Exception:
            continue
    return killed


class StyleBtn(tk.Button):
    def __init__(self, master, kind="ghost", **kw):
        if kind == "primary":
            defaults = dict(bg=C["accent"], fg="white", activebackground="#a84c1e", activeforeground="white", bd=0)
        elif kind == "head":
            defaults = dict(bg=C["head"], fg=C["cream"], activebackground="#163028", activeforeground=C["cream"], bd=0)
        else:
            defaults = dict(
                bg=C["card"],
                fg=C["ink"],
                activebackground="#efe8d8",
                activeforeground=C["ink"],
                bd=1,
                relief="solid",
                highlightthickness=0,
            )
        defaults.update(
            font=F,
            padx=14,
            pady=7,
            cursor="hand2",
            highlightbackground=C["line"],
        )
        defaults.update(kw)
        super().__init__(master, **defaults)


class DesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        os.chdir(SCRIPT_DIR)
        self.cfg = load_config()
        self._log_n = 0
        self._src_rows: list[tk.Frame] = []
        self._align_rows: list[tk.Frame] = []
        self._field_rows: list[tk.Frame] = []
        self._vd_type_rows: list[tk.Frame] = []
        self._vd_exclude_rows: list[tk.Frame] = []
        self._vd_report_rows: list[tk.Frame] = []
        self._vd_extra_col_rows: list[tk.Frame] = []
        self._alive = True
        self._tick_id = None
        self._vd_ui_mode = ""
        self._menus: list[dict] = []
        self._menu_buttons: dict[str, tk.Button] = {}
        self._active_menu_id = ""
        self._align_profiles: dict[str, list[dict]] = {}
        self._align_profile_key = "__default__"
        self._align_default_mappings: list[dict] = []
        self.var_credentials = tk.StringVar()
        self._tab = "filter"
        self.title("数据汇总工具")
        self.geometry("1180x820+80+40")
        self.minsize(980, 680)
        self.configure(bg=C["paper"])
        for ico in (RESOURCE_DIR / "logo.ico", SCRIPT_DIR / "logo.ico"):
            if ico.exists():
                try:
                    self.iconbitmap(str(ico))
                    break
                except Exception:
                    pass
        self._build()
        self._load_cfg(self.cfg)
        self._init_menus()
        if self.cfg.schedule_enabled:
            start_scheduler(self.cfg.schedule_minutes, self.cfg.schedule_only_if_changed)
        if self.cfg.align_schedule_enabled:
            start_align_scheduler(self.cfg.align_schedule_minutes, self.cfg.align_schedule_only_if_changed)
        if self.cfg.vd_schedule_enabled and self.cfg.vd_source_url and self.cfg.vd_dest_url:
            start_video_scheduler(self.cfg.vd_schedule_minutes)
        if getattr(self.cfg, "catalog_schedule_enabled", False) and self.cfg.catalog_index_url and self.cfg.catalog_target_url:
            start_catalog_scheduler(self.cfg.catalog_schedule_minutes)
        self._tick_id = self.after(300, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        def _tk_exc(exc, val, tb):
            err = "".join(traceback.format_exception(exc, val, tb))
            try:
                write_app_log("界面异常", err)
                (SCRIPT_DIR / "desktop_error.log").write_text(err, encoding="utf-8")
            except Exception:
                pass

        self.report_callback_exception = _tk_exc

    # ----- layout helpers -----
    def _card(self, parent, title, em="", collapsed=False):
        box = tk.Frame(parent, bg=C["card"], highlightbackground=C["line"], highlightthickness=1)
        box.pack(fill="x", padx=16, pady=(0, 12))
        head = tk.Frame(box, bg=C["card"], cursor="hand2")
        head.pack(fill="x", padx=12, pady=10)
        arrow = tk.StringVar(value="▸" if collapsed else "▾")
        tk.Label(head, textvariable=arrow, bg=C["card"], fg=C["muted"], font=FB, width=2).pack(side="left")
        tk.Label(head, text=title, bg=C["card"], fg=C["ink"], font=FB).pack(side="left")
        if em:
            tk.Label(head, text=em, bg=C["card"], fg=C["accent"], font=FS).pack(side="left", padx=8)
        tk.Label(head, text="点击展开/收起", bg=C["card"], fg="#b7ae9e", font=("Microsoft YaHei UI", 8)).pack(side="right")
        inner = tk.Frame(box, bg=C["card"])
        if not collapsed:
            inner.pack(fill="x", padx=16, pady=(0, 14))

        def toggle(_e=None):
            if inner.winfo_ismapped():
                inner.pack_forget()
                arrow.set("▸")
            else:
                inner.pack(fill="x", padx=16, pady=(0, 14))
                arrow.set("▾")

        for w in (head, *head.winfo_children()):
            w.bind("<Button-1>", toggle)
        return inner

    def _note(self, parent, text):
        tk.Label(parent, text=text, bg=C["card"], fg=C["muted"], font=FS, wraplength=820, justify="left").pack(anchor="w", pady=(0, 8))

    def _entry(self, parent, label, var, show=None, width=None):
        wrap = tk.Frame(parent, bg=C["card"])
        wrap.pack(fill="x", pady=4)
        tk.Label(wrap, text=label, bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
        e = tk.Entry(wrap, textvariable=var, font=MONO, show=show or "", relief="solid", bd=1)
        e.pack(fill="x", ipady=5, pady=(3, 0))
        return e

    def _check(self, parent, text, var):
        cb = tk.Checkbutton(
            parent,
            text=text,
            variable=var,
            bg=C["card"],
            fg=C["ink"],
            font=F,
            activebackground=C["card"],
            selectcolor="#fff",
            anchor="w",
        )
        cb.pack(anchor="w", pady=2)
        return cb

    def _row3(self, parent):
        f = tk.Frame(parent, bg=C["card"])
        f.pack(fill="x", pady=4)
        f.columnconfigure((0, 1, 2), weight=1)
        return f

    def _cell(self, grid, col, label, var, width=14):
        box = tk.Frame(grid, bg=C["card"])
        box.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0))
        tk.Label(box, text=label, bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
        tk.Entry(box, textvariable=var, font=MONO, relief="solid", bd=1).pack(fill="x", ipady=5, pady=(3, 0))
        return box

    def _scroll_tab(self, parent):
        wrap = tk.Frame(parent, bg=C["paper"])
        canvas = tk.Canvas(wrap, bg=C["paper"], highlightthickness=0)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=C["paper"])
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        def wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            return "break"

        canvas.bind("<MouseWheel>", wheel)
        inner.bind("<MouseWheel>", wheel)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return wrap, inner

    # ----- build -----
    def _build(self) -> None:
        self._build_header()
        self._build_tabs()
        self._build_log()

    def _build_header(self) -> None:
        head = tk.Frame(self, bg=C["head"])
        head.pack(fill="x")
        left = tk.Frame(head, bg=C["head"])
        left.pack(side="left", padx=18, pady=14)
        logo_path = RESOURCE_DIR / "web" / "static" / "logo.png"
        if logo_path.exists():
            try:
                self._logo = tk.PhotoImage(file=str(logo_path)).subsample(18, 18)
                tk.Label(left, image=self._logo, bg=C["head"]).pack(side="left", padx=(0, 12))
            except Exception:
                pass
        brand = tk.Frame(left, bg=C["head"])
        brand.pack(side="left")
        tk.Label(brand, text="数据汇总工具", fg=C["cream"], bg=C["head"], font=FH).pack(anchor="w")
        tk.Label(
            brand,
            text="左侧管理独立配置，右侧使用筛选、目录、字段映射、视频分类和队别专页模板。",
            fg="#c9d5cc",
            bg=C["head"],
            font=FS,
        ).pack(anchor="w")
        version_row = tk.Frame(brand, bg=C["head"])
        version_row.pack(anchor="w", pady=(5, 0))
        tk.Label(
            version_row,
            text=f"版本 {APP_VERSION}",
            fg="#9bb5ab",
            bg=C["head"],
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left")
        self.btn_update = StyleBtn(
            version_row,
            "ghost",
            text="检查更新",
            command=self._check_updates,
            bg=C["head"],
            fg=C["cream"],
            padx=8,
            pady=2,
        )
        self.btn_update.pack(side="left", padx=8)

        sa = tk.Frame(head, bg=C["sa"], highlightbackground="#2c5248", highlightthickness=1)
        sa.pack(side="right", padx=18, pady=12)
        self.sa_hint = tk.StringVar(value="源表和目标表都要共享给每一个账号 · 编辑者")
        tk.Label(sa, textvariable=self.sa_hint, bg=C["sa"], fg="#9bb5ab", font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=10, pady=(8, 2))
        self.var_sa = tk.StringVar(value="未找到服务账号")
        self._cred_files: list[str] = []
        tk.Label(sa, textvariable=self.var_sa, bg=C["sa"], fg="#e8d39a", font=FS, justify="left", wraplength=420).pack(anchor="w", padx=10)
        sa_actions = tk.Frame(sa, bg=C["sa"])
        sa_actions.pack(anchor="e", padx=10, pady=8)
        StyleBtn(sa_actions, "ghost", text="+ 添加服务账号", command=self._choose_credentials, bg=C["sa"], fg=C["cream"]).pack(side="left", padx=(0, 6))
        StyleBtn(sa_actions, "ghost", text="复制邮箱", command=self._copy_sa, bg=C["sa"], fg=C["cream"]).pack(side="left", padx=(0, 6))
        StyleBtn(sa_actions, "ghost", text="移除末个", command=self._remove_last_credential, bg=C["sa"], fg=C["cream"]).pack(side="left")

        self.status = tk.StringVar(value="待命")
        tk.Label(self, textvariable=self.status, bg=C["paper"], fg=C["head"], font=FB).pack(anchor="w", padx=18, pady=(10, 0))

        body = tk.Frame(self, bg=C["paper"])
        body.pack(fill="both", expand=True, pady=(6, 0))
        self.sidebar = tk.Frame(body, bg=C["sa"], width=220)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        side_head = tk.Frame(self.sidebar, bg=C["sa"])
        side_head.pack(fill="x", padx=12, pady=(14, 8))
        tk.Label(side_head, text="配置菜单", bg=C["sa"], fg=C["cream"], font=FB).pack(side="left")
        StyleBtn(side_head, "head", text="＋", command=self._add_menu, padx=9, pady=3).pack(side="right")
        self.menu_box = tk.Frame(self.sidebar, bg=C["sa"])
        self.menu_box.pack(fill="both", expand=True, padx=8)
        side_actions = tk.Frame(self.sidebar, bg=C["sa"])
        side_actions.pack(fill="x", padx=8, pady=10)
        StyleBtn(side_actions, "head", text="修改名称", command=self._rename_menu, padx=8, pady=4).pack(side="left")
        StyleBtn(side_actions, "head", text="删除", command=self._delete_menu, padx=8, pady=4).pack(side="right")

        right = tk.Frame(body, bg=C["paper"])
        right.pack(side="left", fill="both", expand=True)
        actions = tk.Frame(right, bg=C["paper"])
        actions.pack(fill="x", padx=16, pady=(2, 8))
        self.btn_run = StyleBtn(actions, "primary", text="开始汇总", command=self._run_now)
        self.btn_pub = StyleBtn(actions, "head", text="发布图库", command=self._publish)
        self.btn_align = StyleBtn(actions, "ghost", text="开始对齐同步", command=self._run_align)
        self.btn_video = StyleBtn(actions, "head", text="提取视频时长", command=self._run_video)
        self.btn_catalog = StyleBtn(actions, "primary", text="开始目录汇总", command=self._run_catalog)
        self.btn_roster = StyleBtn(actions, "primary", text="开始队别专页汇总", command=self._run_roster)
        self.btn_stop = StyleBtn(actions, "ghost", text="停止当前", command=self._stop_current)
        self.btn_run_selected = StyleBtn(actions, "head", text="执行所选", command=self._run_selected)
        self.btn_stop_all = StyleBtn(actions, "ghost", text="全部停止", command=self._stop_all)
        self.btn_save = StyleBtn(actions, "ghost", text="保存配置", command=self._save)

        self.pages = tk.Frame(right, bg=C["paper"])
        self.pages.pack(fill="both", expand=True)
        self.filter_page, self.filter_inner = self._scroll_tab(self.pages)
        self.catalog_page, self.catalog_inner = self._scroll_tab(self.pages)
        self.align_page, self.align_inner = self._scroll_tab(self.pages)
        self.video_page, self.video_inner = self._scroll_tab(self.pages)
        self.filter_page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.catalog_page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.align_page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.video_page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.roster_page, self.roster_inner = self._scroll_tab(self.pages)
        self.roster_page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._build_filter(self.filter_inner)
        self._build_catalog(self.catalog_inner)
        self._build_align(self.align_inner)
        self._build_video(self.video_inner)
        self._build_roster(self.roster_inner)

    def _build_tabs(self) -> None:
        pass

    def _show_tab(self, name: str) -> None:
        same = getattr(self, "_tab", None) == name
        self._tab = name
        if same:
            if name in ("video", "custom"):
                self._apply_vd_mode(name)
            return
        mapping = {
            "filter": self.filter_page,
            "catalog": self.catalog_page,
            "align": self.align_page,
            "video": self.video_page,
            "custom": self.video_page,
            "roster": self.roster_page,
        }
        mapping.get(name, self.filter_page).lift()
        for btn in (self.btn_run, self.btn_pub, self.btn_align, self.btn_video, self.btn_catalog, self.btn_roster, self.btn_save):
            btn.pack_forget()
        if name == "filter":
            self.btn_run.pack(side="left", padx=(0, 8))
            self.btn_pub.pack(side="left", padx=(0, 8))
        elif name == "catalog":
            self.btn_catalog.pack(side="left", padx=(0, 8))
        elif name == "align":
            self.btn_align.pack(side="left", padx=(0, 8))
        elif name == "roster":
            self.btn_roster.pack(side="left", padx=(0, 8))
        elif name in ("video", "custom"):
            self.btn_video.configure(text="提取视频时长" if name == "video" else "开始分类汇总")
            self.btn_video.pack(side="left", padx=(0, 8))
            self._apply_vd_mode(name)
        self.btn_stop.pack(side="left", padx=(0, 8))
        self.btn_run_selected.pack(side="left", padx=(0, 8))
        self.btn_stop_all.pack(side="left", padx=(0, 8))
        self.btn_save.pack(side="left")

    def _init_menus(self) -> None:
        template_names = {
            "filter": "贴文筛选汇总",
            "catalog": "目录表驱动汇总",
            "align": "字段映射 / 表头对齐",
            "video": "视频提取时长",
            "custom": "自定义数据汇总",
        }
        hidden_templates = {"roster"}
        base = self._payload()
        stored = getattr(self.cfg, "ui_menus", None) or []
        if stored:
            self._menus = [
                copy.deepcopy(item)
                for item in stored
                if isinstance(item, dict)
                and item.get("template") in template_names
                and item.get("template") not in hidden_templates
            ]
            for item in self._menus:
                name = str(item.get("name") or "")
                settings = item.setdefault("settings", {})
                if "自定义" in name:
                    item["template"] = "custom"
                    settings["vd_write_log"] = False
                elif item.get("template") == "video" or "视频提取" in name or "时长" in name:
                    item["template"] = "video"
                    settings["vd_write_log"] = True
                elif item.get("template") == "custom":
                    settings["vd_write_log"] = False
            if not any(item.get("template") == "video" for item in self._menus):
                video_settings = copy.deepcopy(base)
                video_settings["vd_write_log"] = True
                self._menus.insert(max(0, len(self._menus) - 1), {"id": "video-original-default", "name": template_names["video"], "template": "video", "settings": video_settings})
        if not self._menus:
            self._menus = []
            for key, label in template_names.items():
                settings = copy.deepcopy(base)
                if key in ("video", "custom"):
                    settings["vd_write_log"] = key == "video"
                self._menus.append({"id": f"{key}-default", "name": label, "template": key, "settings": settings})
        if not any(item.get("template") == "custom" for item in self._menus):
            settings = copy.deepcopy(base)
            settings["vd_write_log"] = False
            self._menus.append({"id": "custom-default", "name": template_names["custom"], "template": "custom", "settings": settings})
        for item in self._menus:
            item.setdefault("id", f"{item['template']}-{id(item)}")
            item.setdefault("name", template_names[item["template"]])
            item.setdefault("settings", copy.deepcopy(base))
        wanted = getattr(self.cfg, "ui_active_menu", "") or ""
        active = next((item["id"] for item in self._menus if item["id"] == wanted), self._menus[0]["id"])
        self._render_menu_buttons()
        self._select_menu(active, initial=True)

    def _render_menu_buttons(self) -> None:
        for child in self.menu_box.winfo_children():
            child.destroy()
        self._menu_buttons = {}
        if not hasattr(self, "_menu_checks"):
            self._menu_checks = {}
        for item in self._menus:
            wrap = tk.Frame(self.menu_box, bg=C["sa"])
            wrap.pack(fill="x", pady=2)
            var = self._menu_checks.get(item["id"])
            if var is None:
                var = tk.BooleanVar(value=True)
                self._menu_checks[item["id"]] = var
            tk.Checkbutton(
                wrap,
                variable=var,
                bg=C["sa"],
                activebackground=C["sa"],
                selectcolor=C["head"],
                fg=C["cream"],
                relief="flat",
                highlightthickness=0,
            ).pack(side="left", padx=(2, 0))
            text = f"{item['name']}\n  { {'filter':'贴文模板','catalog':'目录模板','align':'映射模板','video':'时长模板','custom':'自定义模板','roster':'专页模板'}[item['template']] }"
            btn = tk.Button(
                wrap,
                text=text,
                command=lambda menu_id=item["id"]: self._select_menu(menu_id),
                anchor="w",
                justify="left",
                bg="#0f766e" if item["id"] == self._active_menu_id else C["sa"],
                fg=C["cream"],
                activebackground="#0d9488",
                activeforeground=C["cream"],
                relief="flat",
                bd=0,
                padx=8,
                pady=8,
                font=F,
                cursor="hand2",
            )
            btn.pack(side="left", fill="x", expand=True)
            btn.bind("<Double-Button-1>", lambda _event, menu_id=item["id"]: self._rename_menu(menu_id))
            self._menu_buttons[item["id"]] = btn

    def _select_menu(self, menu_id: str, initial: bool = False) -> None:
        if menu_id == self._active_menu_id and not initial:
            return
        if self._active_menu_id and not initial:
            current = next((item for item in self._menus if item["id"] == self._active_menu_id), None)
            if current:
                current["settings"] = copy.deepcopy(self._payload())
        item = next((entry for entry in self._menus if entry["id"] == menu_id), None)
        if not item:
            return
        self._active_menu_id = menu_id
        settings = copy.deepcopy(item.get("settings") or {})
        self._vd_ui_mode = ""
        self._load_cfg(_cfg_from_payload(settings))
        self._show_tab(item["template"])
        job = job_snapshot(menu_id)
        self._log_n = len(job.get("logs") or [])
        self._highlight_menu_buttons()
        self.status.set(f"当前配置：{item['name']}")

    def _highlight_menu_buttons(self) -> None:
        for menu_id, btn in self._menu_buttons.items():
            try:
                btn.configure(bg="#0f766e" if menu_id == self._active_menu_id else C["sa"])
            except Exception:
                pass

    def _rename_menu(self, menu_id: str | None = None) -> None:
        menu_id = menu_id or self._active_menu_id
        item = next((entry for entry in self._menus if entry["id"] == menu_id), None)
        if not item:
            return
        name = simpledialog.askstring("修改菜单名称", "新名称：", initialvalue=item["name"], parent=self)
        if name and name.strip():
            item["name"] = name.strip()
            self._render_menu_buttons()

    def _add_menu(self) -> None:
        choice = simpledialog.askstring(
            "新增配置菜单",
            "选择模板：\n1 贴文筛选汇总\n2 目录表驱动汇总\n3 字段映射 / 表头对齐\n4 视频提取时长（保留日志）\n5 自定义数据汇总（不写日志）\n\n请输入 1-5：",
            parent=self,
        )
        template = {"1": "filter", "2": "catalog", "3": "align", "4": "video", "5": "custom"}.get((choice or "").strip())
        if not template:
            return
        labels = {"filter": "贴文筛选汇总", "catalog": "目录表驱动汇总", "align": "字段映射 / 表头对齐", "video": "视频提取时长", "custom": "自定义数据汇总"}
        name = simpledialog.askstring("新增配置菜单", "菜单名称：", initialvalue=f"{labels[template]}副本", parent=self)
        if not name or not name.strip():
            return
        source = next((item for item in self._menus if item["template"] == template), None)
        settings = copy.deepcopy(source.get("settings") if source else self._payload())
        if template in ("video", "custom"):
            settings["vd_write_log"] = template == "video"
        menu_id = f"{template}-{int(datetime.now().timestamp() * 1000)}"
        self._menus.append({"id": menu_id, "name": name.strip(), "template": template, "settings": settings})
        self._select_menu(menu_id)

    def _delete_menu(self) -> None:
        if len(self._menus) <= 1:
            messagebox.showwarning("数据汇总工具", "至少保留一个配置菜单")
            return
        item = next((entry for entry in self._menus if entry["id"] == self._active_menu_id), None)
        if not item or not messagebox.askyesno("删除配置", f"确定删除“{item['name']}”？"):
            return
        self._menus = [entry for entry in self._menus if entry["id"] != self._active_menu_id]
        self._active_menu_id = ""
        self._select_menu(self._menus[0]["id"], initial=True)

    def _src_table(self, parent, headers, add_text, rows_attr, name_ph):
        head = tk.Frame(parent, bg="#efe8d8")
        head.pack(fill="x")
        tk.Label(head, text=headers[0], bg="#efe8d8", font=FS, fg=C["muted"], width=16, anchor="w").pack(side="left", padx=6, pady=4)
        tk.Label(head, text=headers[1], bg="#efe8d8", font=FS, fg=C["muted"], anchor="w").pack(side="left", fill="x", expand=True)
        box = tk.Frame(parent, bg=C["card"])
        box.pack(fill="x")
        hint = tk.Frame(parent, bg=C["card"])
        hint.pack(fill="x", pady=6)
        count = tk.Label(hint, text="0 个链接", bg=C["card"], fg=C["muted"], font=FS)
        count.pack(side="left")
        StyleBtn(hint, "ghost", text=add_text, command=lambda: self._add_src_row(box, rows_attr, name_ph, count)).pack(side="right")
        StyleBtn(hint, "ghost", text="清空", command=lambda: self._clear_src(box, rows_attr, name_ph, count)).pack(side="right", padx=6)
        return box, count, name_ph

    def _add_src_row(self, box, rows_attr, name_ph, count, name="", url=""):
        row = tk.Frame(box, bg=C["card"])
        row.pack(fill="x", pady=3)
        n = tk.Entry(row, font=MONO, relief="solid", bd=1)
        n.insert(0, name)
        n.pack(side="left", ipady=4)
        n.configure(width=16)
        u = tk.Entry(row, font=MONO, relief="solid", bd=1)
        u.insert(0, url)
        u.pack(side="left", fill="x", expand=True, padx=6, ipady=4)
        StyleBtn(row, "ghost", text="删除", command=lambda: self._del_src_row(row, box, rows_attr, name_ph, count)).pack(side="left")
        row._name = n
        row._url = u
        getattr(self, rows_attr).append(row)
        self._upd_src_count(rows_attr, count)

    def _del_src_row(self, row, box, rows_attr, name_ph, count):
        lst = getattr(self, rows_attr)
        if row in lst:
            lst.remove(row)
        row.destroy()
        if not lst:
            self._add_src_row(box, rows_attr, name_ph, count)
        self._upd_src_count(rows_attr, count)

    def _clear_src(self, box, rows_attr, name_ph, count):
        for r in list(getattr(self, rows_attr)):
            r.destroy()
        setattr(self, rows_attr, [])
        self._add_src_row(box, rows_attr, name_ph, count)
        self._add_src_row(box, rows_attr, name_ph, count)

    def _upd_src_count(self, rows_attr, count):
        n = sum(1 for r in getattr(self, rows_attr) if r._url.get().strip())
        count.configure(text=f"{n} 个链接")

    def _read_src(self, rows_attr, align=False):
        out = []
        for r in getattr(self, rows_attr):
            label = r._name.get().strip()
            url = r._url.get().strip()
            if not url:
                continue
            out.append({"sheet": label, "url": url} if align else {"name": label, "url": url})
        return out

    def _set_src(self, box, rows_attr, name_ph, count, items, align=False):
        for r in list(getattr(self, rows_attr)):
            r.destroy()
        setattr(self, rows_attr, [])
        items = items or []
        if not items:
            self._add_src_row(box, rows_attr, name_ph, count)
            self._add_src_row(box, rows_attr, name_ph, count)
            return
        for s in items:
            if isinstance(s, str):
                self._add_src_row(box, rows_attr, name_ph, count, "", s)
            else:
                self._add_src_row(box, rows_attr, name_ph, count, s.get("sheet") or s.get("name") or "", s.get("url") or "")

    def _add_vd_type(self, value: str = "") -> None:
        row = tk.Frame(self.vd_type_box, bg=C["card"])
        row.pack(fill="x", pady=3)
        e = tk.Entry(row, font=MONO, relief="solid", bd=1)
        e.insert(0, value)
        e.pack(side="left", fill="x", expand=True, ipady=4)
        StyleBtn(row, "ghost", text="删除", command=lambda: self._del_vd_type(row)).pack(side="left", padx=(6, 0))
        row._val = e
        e.bind("<KeyRelease>", lambda _e: self._upd_vd_type_count())
        self._vd_type_rows.append(row)
        self._upd_vd_type_count()

    def _del_vd_type(self, row) -> None:
        if row in self._vd_type_rows:
            self._vd_type_rows.remove(row)
        row.destroy()
        if not self._vd_type_rows:
            self._add_vd_type()
        self._upd_vd_type_count()

    def _clear_vd_types(self) -> None:
        for r in list(self._vd_type_rows):
            r.destroy()
        self._vd_type_rows = []
        self._add_vd_type()
        self._add_vd_type()
        self._upd_vd_type_count()

    def _upd_vd_type_count(self) -> None:
        n = sum(1 for r in self._vd_type_rows if r._val.get().strip())
        self.vd_type_count.configure(text=f"{n} 个分类")

    def _read_vd_types(self) -> list[str]:
        out = []
        seen: set[str] = set()
        for r in self._vd_type_rows:
            t = r._val.get().strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _add_vd_report_cat(self, value: str = "") -> None:
        row = tk.Frame(self.vd_report_cat_box, bg=C["card"])
        row.pack(fill="x", pady=3)
        e = tk.Entry(row, font=MONO, relief="solid", bd=1)
        e.insert(0, value)
        e.pack(side="left", fill="x", expand=True, ipady=4)
        StyleBtn(row, "ghost", text="删除", command=lambda: self._del_vd_report_cat(row)).pack(side="left", padx=(6, 0))
        row._val = e
        e.bind("<KeyRelease>", lambda _e: self._upd_vd_report_cat_count())
        self._vd_report_rows.append(row)
        self._upd_vd_report_cat_count()

    def _del_vd_report_cat(self, row) -> None:
        if row in self._vd_report_rows:
            self._vd_report_rows.remove(row)
        row.destroy()
        if not self._vd_report_rows:
            self._add_vd_report_cat()
        self._upd_vd_report_cat_count()

    def _upd_vd_report_cat_count(self) -> None:
        n = sum(1 for r in self._vd_report_rows if r._val.get().strip())
        if hasattr(self, "vd_report_cat_count"):
            self.vd_report_cat_count.configure(text=f"{n} 个额外列")

    def _read_vd_report_cats(self) -> list[str]:
        out = []
        seen: set[str] = set()
        for r in self._vd_report_rows:
            t = r._val.get().strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _set_vd_report_cats(self, items) -> None:
        for r in list(self._vd_report_rows):
            r.destroy()
        self._vd_report_rows = []
        items = [str(x).strip() for x in (items or []) if str(x).strip()]
        if not items:
            self._add_vd_report_cat()
            return
        for t in items:
            self._add_vd_report_cat(t)

    def _add_vd_exclude(self, value: str = "") -> None:
        row = tk.Frame(self.vd_exclude_box, bg=C["card"])
        row.pack(fill="x", pady=3)
        e = tk.Entry(row, font=MONO, relief="solid", bd=1)
        e.insert(0, value)
        e.pack(side="left", fill="x", expand=True, ipady=4)
        StyleBtn(row, "ghost", text="删除", command=lambda: self._del_vd_exclude(row)).pack(side="left", padx=(6, 0))
        row._val = e
        e.bind("<KeyRelease>", lambda _e: self._upd_vd_exclude_count())
        self._vd_exclude_rows.append(row)
        self._upd_vd_exclude_count()

    def _del_vd_exclude(self, row) -> None:
        if row in self._vd_exclude_rows:
            self._vd_exclude_rows.remove(row)
        row.destroy()
        if not self._vd_exclude_rows:
            self._add_vd_exclude()
        self._upd_vd_exclude_count()

    def _upd_vd_exclude_count(self) -> None:
        n = sum(1 for r in self._vd_exclude_rows if r._val.get().strip())
        if hasattr(self, "vd_exclude_count"):
            self.vd_exclude_count.configure(text=f"{n} 个排除")

    def _read_vd_exclude_types(self) -> list[str]:
        out = []
        seen: set[str] = set()
        for r in self._vd_exclude_rows:
            t = r._val.get().strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _set_vd_exclude_types(self, items) -> None:
        for r in list(self._vd_exclude_rows):
            r.destroy()
        self._vd_exclude_rows = []
        items = [str(x).strip() for x in (items or []) if str(x).strip()]
        if not items:
            self._add_vd_exclude()
            return
        for t in items:
            self._add_vd_exclude(t)

    def _apply_vd_category_mode(self) -> None:
        other_only = "不统计" in (self.var_vd_category_mode.get() or "")
        if other_only:
            self.vd_exclude_wrap.pack_forget()
            self.vd_type_note.configure(
                text="这些分类不统计（同样支持包含匹配，如 wsp）。没填分类的、以及其他分类，全部计入下面的「其余」。"
            )
        else:
            if not self.vd_exclude_wrap.winfo_ismapped():
                self.vd_exclude_wrap.pack(fill="x", after=self.vd_mode_row)
            self.vd_type_note.configure(
                text="单独成列的分类：每个分类一列。填 wsp 会匹配所有包含 wsp 的类型；可用 * 通配符，例如 口播*。没填分类的算进「其余」。"
            )

    def _apply_vd_mode(self, name: str) -> None:
        if self._vd_ui_mode == name:
            return
        self._vd_ui_mode = name
        custom = name == "custom"
        if custom:
            self.vd_video_filter_row.pack_forget()
            self.vd_custom_box.pack(fill="x", pady=(8, 0))
            self.vd_link_cell.grid_remove()
            self.vd_log_cell.grid_remove()
            self.vd_video_opts.pack_forget()
            if hasattr(self, "vd_report_cat_wrap"):
                self.vd_report_cat_wrap.pack_forget()
            if hasattr(self, "vd_wildcard_note"):
                self.vd_wildcard_note.pack(anchor="w", pady=(0, 4))
            self._apply_vd_category_mode()
            self.vd_dest_note.configure(text="写入目标表：按制作人 × 日期 × 分类统计条数。不写日志、不提取时长、没有 30 秒算法。")
            self.vd_src_note.configure(text="读取源表的日期、制作人和类型，按下面的分类规则统计每天每个人每个分类有多少条。不读视频链接、不查时长。")
            self.vd_col_note.configure(text="列用字母。默认：A=日期，H=制作人，E=类型。")
            self.vd_sched_note.configure(text="到点后按分类规则重新统计并写入数据表。首次启用约 12 秒后执行一轮。")
            self.vd_sched_check.configure(text="启用分类汇总定时")
            self.vd_help_note.configure(
                text="数据表第 1 行是姓名（多分类会合并单元格），第 3 行是分类，第 5 行是每人每类合计，第 8 行起是每天条数。"
                "单独列出的分类各占一列；排除的不算；其余可归入「其他」。源表和写入表都要共享给服务账号。"
            )
        else:
            self.vd_custom_box.pack_forget()
            self.vd_video_filter_row.pack(fill="x", pady=4)
            self.vd_link_cell.grid()
            self.vd_log_cell.grid()
            self.vd_video_opts.pack(fill="x", pady=4)
            if hasattr(self, "vd_report_cat_wrap"):
                self.vd_report_cat_wrap.pack(fill="x", pady=(8, 0))
            if hasattr(self, "vd_wildcard_note"):
                self.vd_wildcard_note.pack_forget()
            self.vd_type_note.configure(text="只跑类型列等于下面这些分类的视频（精确匹配，不用通配符）。会提取时长并写入日志表。")
            self.vd_dest_note.configure(
                text="每人至少两列：总计数、逐条计数。第 4 节添加的分类会自动再加列（加 1 个分类就是 3 列）。也可在下面单独指定额外分类列。"
            )
            self.vd_src_note.configure(
                text="读取源表：A 列日期、B 列视频链接、H 列制作人（列字母可改）。B 列可以是蓝字文件名，程序会读取单元格里的超链接（Drive / YouTube），再写入另一张表的「日志表」和「数据表」。"
            )
            self.vd_col_note.configure(text="列用字母。默认：A=日期，B=视频链接，H=制作人，E=类型。")
            self.vd_sched_note.configure(text="到点后自动读取新链接、追加日志，并重新生成数据表。首次启用约 12 秒后执行一轮。")
            self.vd_sched_check.configure(text="启用视频时长定时")
            self.vd_help_note.configure(
                text="日志表：A 日期、B 链接、C 名字、D 时长(秒)、E 类型、F 备注（未识别原因）。已跑过的链接下次自动跳过。"
                "数据表：每人两列（总计数、逐条计数），添加分类再加列；一人一色，姓名合并，人与人之间有分隔线。"
                "未识别常见原因：不是视频、没有权限、Drive 还没生成时长。源表和 Drive 文件都要共享给服务账号。"
            )

    def _set_vd_types(self, items) -> None:
        for r in list(self._vd_type_rows):
            r.destroy()
        self._vd_type_rows = []
        items = [str(x).strip() for x in (items or []) if str(x).strip()]
        if not items:
            self._add_vd_type()
            self._add_vd_type()
            return
        for t in items:
            self._add_vd_type(t)

    def _add_vd_extra_col(self, field: str = "分类", column: str = "") -> None:
        row = tk.Frame(self.vd_extra_col_box, bg=C["card"])
        row.pack(fill="x", pady=3)
        name = tk.Entry(row, font=MONO, relief="solid", bd=1, width=18)
        name.insert(0, field)
        name.pack(side="left", ipady=4)
        col = tk.Entry(row, font=MONO, relief="solid", bd=1, width=10)
        col.insert(0, column)
        col.pack(side="left", padx=6, ipady=4)
        StyleBtn(row, "ghost", text="删除", command=lambda: self._del_vd_extra_col(row)).pack(side="left")
        row._field = name
        row._column = col
        self._vd_extra_col_rows.append(row)

    def _del_vd_extra_col(self, row) -> None:
        if row in self._vd_extra_col_rows:
            self._vd_extra_col_rows.remove(row)
        row.destroy()

    def _read_vd_columns(self) -> list[dict]:
        result = [
            {"field": "日期", "role": "date", "column": self.var_vd_col_date.get().strip().upper()},
            {"field": "名字", "role": "name", "column": self.var_vd_col_name.get().strip().upper()},
            {"field": "类型", "role": "type", "column": self.var_vd_col_type.get().strip().upper()},
        ]
        is_video = next(
            (item.get("template") == "video" for item in self._menus if item.get("id") == self._active_menu_id),
            True,
        )
        if is_video:
            result.insert(1, {"field": "视频链接", "role": "link", "column": self.var_vd_col_link.get().strip().upper()})
        for row in self._vd_extra_col_rows:
            column = row._column.get().strip().upper()
            if column:
                result.append({"field": row._field.get().strip() or "分类", "role": "type", "column": column})
        return [item for item in result if item["column"]]

    def _set_vd_columns(self, items) -> None:
        for row in list(self._vd_extra_col_rows):
            row.destroy()
        self._vd_extra_col_rows = []
        columns = [item for item in (items or []) if isinstance(item, dict)]
        role_vars = {"date": self.var_vd_col_date, "link": self.var_vd_col_link, "name": self.var_vd_col_name}
        type_seen = False
        for item in columns:
            role = str(item.get("role") or "type").lower()
            column = str(item.get("column") or "").upper()
            if role in role_vars:
                role_vars[role].set(column)
            elif role == "type" and not type_seen:
                self.var_vd_col_type.set(column)
                type_seen = True
            elif role == "type":
                self._add_vd_extra_col(str(item.get("field") or "分类"), column)

    def _build_filter(self, p) -> None:
        c1 = self._card(p, "1. 数据源表格链接", "只换链接，列范围相同", collapsed=True)
        self._note(c1, "每个源表都是同一套表头（当月贴文库）。小组名可选，用来写进汇总结果。")
        box, count, _ph = self._src_table(c1, ["小组（可选）", "表格链接"], "+ 添加数据源", "_src_rows", "例如：管理组")
        self.src_box, self.src_count = box, count
        self.var_add_source_column = tk.BooleanVar(value=True)
        self._check(c1, "把「小组」写到 AC 列（不占用 A 列）", self.var_add_source_column)

        c2 = self._card(p, "2. 日期与点赞过滤")
        self.var_start = tk.StringVar()
        self.var_end = tk.StringVar()
        self.var_likes = tk.StringVar(value="1000")
        g = self._row3(c2)
        self._cell(g, 0, "开始日期", self.var_start)
        self._cell(g, 1, "结束日期", self.var_end)
        self._cell(g, 2, "点赞阈值", self.var_likes)
        self._note(c2, "日期先筛一遍；高赞表再取点赞 ≥ 阈值的行。默认按 B 列帖文id 增量更新。")

        c3 = self._card(p, "3. 写入结果", "两套配置互不影响", collapsed=True)
        self.var_upsert = tk.BooleanVar(value=True)
        self._check(c3, "按 B 列帖文id 更新（已有行只改变化的列，新 id 追加；取消则整表覆盖）", self.var_upsert)
        self.var_write_all = tk.BooleanVar(value=True)
        self._check(c3, "全部结果（日期筛选后的所有行）", self.var_write_all)
        self.var_target_url = tk.StringVar()
        self._entry(c3, "表格链接", self.var_target_url)
        g = self._row3(c3)
        self.var_output_sheet = tk.StringVar(value="筛选结果")
        self.var_output_start_row = tk.StringVar(value="1")
        self.var_include_headers = tk.BooleanVar(value=True)
        self._cell(g, 0, "工作表名", self.var_output_sheet)
        self._cell(g, 1, "起始行", self.var_output_start_row)
        box = tk.Frame(g, bg=C["card"])
        box.grid(row=0, column=2, sticky="ew", padx=(8, 0))
        tk.Label(box, text=" ", bg=C["card"]).pack()
        self._check(box, "写入表头", self.var_include_headers)

        tk.Frame(c3, bg=C["line"], height=1).pack(fill="x", pady=10)
        self.var_write_hot = tk.BooleanVar(value=True)
        self._check(c3, "高赞结果（点赞 ≥ 阈值）", self.var_write_hot)
        self.var_hot_target_url = tk.StringVar()
        self._entry(c3, "表格链接（留空 = 与全部结果同一张表）", self.var_hot_target_url)
        g = self._row3(c3)
        self.var_hot_output_sheet = tk.StringVar(value="点赞1000以上")
        self.var_hot_start_row = tk.StringVar(value="1")
        self.var_hot_include_headers = tk.BooleanVar(value=True)
        self._cell(g, 0, "工作表名", self.var_hot_output_sheet)
        self._cell(g, 1, "起始行", self.var_hot_start_row)
        box = tk.Frame(g, bg=C["card"])
        box.grid(row=0, column=2, sticky="ew", padx=(8, 0))
        tk.Label(box, text=" ", bg=C["card"]).pack()
        self._check(box, "写入表头", self.var_hot_include_headers)

        c4 = self._card(p, "4. 发布到 Cloudflare", "密钥等少改，点标题展开", collapsed=True)
        self._note(c4, "发布请用顶部「发布图库」。这里只改地址和密钥。数据和上次相同会跳过。")
        self.var_cf_url = tk.StringVar(value="https://promo.zhixianglife.com/api/publish-cache")
        self.var_cf_secret = tk.StringVar()
        self._entry(c4, "发布地址", self.var_cf_url)
        self._entry(c4, "CACHE_PUBLISH_SECRET", self.var_cf_secret, show="•")
        g = self._row3(c4)
        cell = tk.Frame(g, bg=C["card"])
        cell.grid(row=0, column=0, sticky="ew")
        tk.Label(cell, text="发布哪份数据", bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
        self.var_cf_source = tk.StringVar(value="all")
        ttk.Combobox(cell, textvariable=self.var_cf_source, values=("all", "hot"), state="readonly", width=16).pack(anchor="w", pady=4)
        self.var_cf_after = tk.BooleanVar(value=False)
        box = tk.Frame(g, bg=C["card"])
        box.grid(row=0, column=1, sticky="ew", padx=8)
        tk.Label(box, text=" ", bg=C["card"]).pack()
        self._check(box, "汇总后自动发布", self.var_cf_after)

        c5 = self._card(p, "5. 定时同步", "关掉窗口就不再跑")
        self._note(c5, "可改成 1 小时(60)、3 小时(180) 或 1 天(1440)。打开软件后若已到期会先跑一轮。")
        g = self._row3(c5)
        self.var_minutes = tk.StringVar(value="1440")
        self._cell(g, 0, "间隔（分钟）", self.var_minutes)
        box = tk.Frame(g, bg=C["card"])
        box.grid(row=0, column=1, columnspan=2, sticky="ew", padx=8)
        tk.Label(box, text="快捷", bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
        pr = tk.Frame(box, bg=C["card"])
        pr.pack(anchor="w", pady=4)
        for label, m in (("1 小时", 60), ("3 小时", 180), ("6 小时", 360), ("1 天", 1440)):
            StyleBtn(pr, "ghost", text=label, command=lambda x=m: self._set_interval(x)).pack(side="left", padx=(0, 6))
        self.var_sched = tk.BooleanVar(value=False)
        self.var_changed = tk.BooleanVar(value=True)
        self._check(c5, "仅源表有变化时执行", self.var_changed)
        self._check(c5, "启用定时", self.var_sched)
        self.sched_info = tk.Label(c5, text="定时未启动", bg=C["card"], fg=C["muted"], font=FS)
        self.sched_info.pack(anchor="w", pady=4)

        c6 = self._card(p, "6. 抓取字段", "默认按截图，可改", collapsed=True)
        self._note(c6, "对应「数据库」里的 D 列范围。以后换列，改这里即可。")
        fh = tk.Frame(c6, bg="#efe8d8")
        fh.pack(fill="x")
        for t, w in (("字段名", 16), ("工作表", 16), ("范围", 16)):
            tk.Label(fh, text=t, bg="#efe8d8", font=FS, fg=C["muted"], width=w, anchor="w").pack(side="left", padx=4, pady=4)
        self.field_box = tk.Frame(c6, bg=C["card"])
        self.field_box.pack(fill="x")
        self.field_count = tk.Label(c6, text="0 列", bg=C["card"], fg=C["muted"], font=FS)
        self.field_count.pack(anchor="w", pady=4)
        fr = tk.Frame(c6, bg=C["card"])
        fr.pack(anchor="w")
        StyleBtn(fr, "ghost", text="+ 添加字段", command=lambda: self._add_field({})).pack(side="left")
        StyleBtn(fr, "ghost", text="恢复截图默认", command=self._reset_fields).pack(side="left", padx=6)

        c7 = self._card(p, "高级选项", collapsed=True)
        self.var_exclude = tk.StringVar(value="未找到")
        self.var_date_field = tk.StringVar(value="发布日期")
        self.var_sort_field = tk.StringVar(value="点赞")
        self.var_sort_desc = tk.BooleanVar(value=True)
        cred_wrap = tk.Frame(c7, bg=C["card"])
        cred_wrap.pack(fill="x", pady=4)
        tk.Label(cred_wrap, text="服务账号 JSON 文件", bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
        cred_line = tk.Frame(cred_wrap, bg=C["card"])
        cred_line.pack(fill="x", pady=(3, 0))
        tk.Entry(cred_line, textvariable=self.var_credentials, font=MONO, relief="solid", bd=1).pack(side="left", fill="x", expand=True, ipady=5)
        StyleBtn(cred_line, "ghost", text="选择文件…", command=self._choose_credentials).pack(side="left", padx=(8, 0))
        g = self._row3(c7)
        self._cell(g, 0, "排除帖文id", self.var_exclude)
        self._cell(g, 1, "日期字段", self.var_date_field)
        self._cell(g, 2, "排序字段", self.var_sort_field)
        self._check(c7, "降序排列", self.var_sort_desc)

    def _add_field(self, item):
        row = tk.Frame(self.field_box, bg=C["card"])
        row.pack(fill="x", pady=3)
        n = tk.Entry(row, font=MONO, relief="solid", bd=1, width=16)
        n.insert(0, (item or {}).get("name") or "")
        n.pack(side="left", ipady=4, padx=(0, 6))
        s = tk.Entry(row, font=MONO, relief="solid", bd=1, width=16)
        s.insert(0, (item or {}).get("sheet") or "当月贴文库")
        s.pack(side="left", ipady=4, padx=(0, 6))
        r = tk.Entry(row, font=MONO, relief="solid", bd=1, width=16)
        r.insert(0, (item or {}).get("range") or "")
        r.pack(side="left", ipady=4, padx=(0, 6))
        StyleBtn(row, "ghost", text="删除", command=lambda: self._del_field(row)).pack(side="left")
        row._name, row._sheet, row._range = n, s, r
        self._field_rows.append(row)
        self._upd_fields()

    def _del_field(self, row):
        if row in self._field_rows:
            self._field_rows.remove(row)
        row.destroy()
        if not self._field_rows:
            self._add_field({})
        self._upd_fields()

    def _reset_fields(self):
        for r in list(self._field_rows):
            r.destroy()
        self._field_rows = []
        for f in copy_default_fields():
            self._add_field(f)

    def _upd_fields(self):
        n = sum(1 for r in self._field_rows if r._name.get().strip() and r._range.get().strip())
        self.field_count.configure(text=f"{n} 列")

    def _read_fields(self):
        out = []
        for r in self._field_rows:
            name = r._name.get().strip()
            rng = r._range.get().strip()
            if name and rng:
                out.append({"name": name, "sheet": r._sheet.get().strip() or "当月贴文库", "range": rng})
        return out

    def _build_catalog(self, p) -> None:
        c1 = self._card(p, "1. 目录表", "遍历链接列和工作表名称列")
        self._note(c1, "目录表默认 B 列是表格链接、D 列是要查找的工作表名称；两列都可以修改。")
        self.var_catalog_index_url = tk.StringVar()
        self.var_catalog_index_sheet = tk.StringVar()
        self.var_catalog_start_row = tk.StringVar(value="2")
        self.var_catalog_url_col = tk.StringVar(value="B")
        self.var_catalog_sheet_col = tk.StringVar(value="D")
        self._entry(c1, "目录表链接", self.var_catalog_index_url)
        g = self._row3(c1)
        self._cell(g, 0, "目录工作表名", self.var_catalog_index_sheet)
        self._cell(g, 1, "数据起始行", self.var_catalog_start_row)
        g2 = self._row3(c1)
        self._cell(g2, 0, "链接所在列", self.var_catalog_url_col)
        self._cell(g2, 1, "工作表名称所在列", self.var_catalog_sheet_col)
        self.var_catalog_keep_header = tk.BooleanVar(value=False)
        self._check(c1, "每个工作表都保留首行（不勾选则只保留第一份表头）", self.var_catalog_keep_header)

        c2 = self._card(p, "2. 写入目标表")
        self.var_catalog_target_url = tk.StringVar()
        self.var_catalog_output_sheet = tk.StringVar(value="目录汇总")
        self.var_catalog_output_start_row = tk.StringVar(value="1")
        self._entry(c2, "目标表格链接", self.var_catalog_target_url)
        g = self._row3(c2)
        self._cell(g, 0, "工作表名", self.var_catalog_output_sheet)
        self._cell(g, 1, "写入起始行", self.var_catalog_output_start_row)
        self._note(
            c2,
            "只写入最后一列还有内容的范围，空列不写，整行为空的跳过。读一批写一批，每批 1000 行。"
            "下次如果源表列变多，按本轮实际最大列数扩展。目录 B 列可以是蓝字超链接。",
        )

        c3 = self._card(p, "3. 定时汇总", "关掉窗口就不再跑")
        self._note(c3, "目录汇总也支持定时。可改成 1 小时、3 小时或自定义分钟。")
        g = self._row3(c3)
        self.var_catalog_minutes = tk.StringVar(value="180")
        self._cell(g, 0, "间隔（分钟）", self.var_catalog_minutes)
        self.var_catalog_sched = tk.BooleanVar(value=False)
        self._check(c3, "启用目录汇总定时", self.var_catalog_sched)
        self.catalog_sched_info = tk.Label(c3, text="定时未启动", bg=C["card"], fg=C["muted"], font=FS)
        self.catalog_sched_info.pack(anchor="w", pady=4)

    def _build_align(self, p) -> None:
        c1 = self._card(p, "1. 数据源表格链接", collapsed=True)
        self._note(c1, "每行填一个数据源。工作表名称填源表里的 sheet 名（如 8月份）；留空则读第一个工作表。")
        box, count, ph = self._src_table(c1, ["工作表名称", "表格链接"], "+ 添加数据源", "_align_rows", "例如：8月份")
        self.align_box, self.align_count = box, count
        g = self._row3(c1)
        self.var_align_source_sheet = tk.StringVar()
        self.var_align_header_row = tk.StringVar(value="1")
        self._cell(g, 0, "默认工作表名", self.var_align_source_sheet)
        self._cell(g, 1, "表头所在行", self.var_align_header_row)

        c2 = self._card(p, "2. 字段映射", "和贴文筛选一样可增减字段，可按链接单独配置")
        self._note(c2, "每一行：目标字段（写入列名）← 源字段（源表表头）。上面选「某个链接」后，只改这个链接的映射，不影响其它链接。")
        profile_row = tk.Frame(c2, bg=C["card"])
        profile_row.pack(fill="x", pady=4)
        tk.Label(profile_row, text="配置范围", bg=C["card"], fg=C["muted"], font=FS).pack(side="left")
        self.var_align_profile = tk.StringVar(value="所有链接的默认映射")
        self.align_profile_combo = ttk.Combobox(profile_row, textvariable=self.var_align_profile, state="readonly", width=42)
        self.align_profile_combo.pack(side="left", padx=8)
        self.align_profile_combo.bind("<<ComboboxSelected>>", lambda _e: self._switch_align_profile())
        StyleBtn(profile_row, "ghost", text="刷新链接列表", command=self._refresh_align_profiles).pack(side="left")
        fh = tk.Frame(c2, bg="#ecfdf5")
        fh.pack(fill="x")
        tk.Label(fh, text="目标字段", bg="#ecfdf5", font=FS, fg=C["muted"], width=18, anchor="w").pack(side="left", padx=4, pady=4)
        tk.Label(fh, text="源字段", bg="#ecfdf5", font=FS, fg=C["muted"], width=18, anchor="w").pack(side="left", padx=4, pady=4)
        self.align_map_box = tk.Frame(c2, bg=C["card"])
        self.align_map_box.pack(fill="x")
        self._align_map_rows: list[tk.Frame] = []
        hr = tk.Frame(c2, bg=C["card"])
        hr.pack(fill="x", pady=4)
        self.align_header_count = tk.Label(hr, text="0 列", bg=C["card"], fg=C["muted"], font=FS)
        self.align_header_count.pack(side="left")
        StyleBtn(hr, "ghost", text="+ 添加字段", command=lambda: self._add_align_map_row("", "")).pack(side="right")
        StyleBtn(hr, "ghost", text="从源表读取表头", command=self._peek_headers).pack(side="right", padx=6)

        c3 = self._card(p, "3. 目标表", collapsed=True)
        self.var_align_target_url = tk.StringVar()
        self._entry(c3, "目标表格链接", self.var_align_target_url)
        g = self._row3(c3)
        self.var_align_output_sheet = tk.StringVar(value="对齐结果")
        self.var_align_start_row = tk.StringVar(value="1")
        self.var_align_include_headers = tk.BooleanVar(value=True)
        self._cell(g, 0, "工作表名", self.var_align_output_sheet)
        self._cell(g, 1, "起始行", self.var_align_start_row)
        box = tk.Frame(g, bg=C["card"])
        box.grid(row=0, column=2, sticky="ew", padx=8)
        tk.Label(box, text=" ", bg=C["card"]).pack()
        self._check(box, "写入表头", self.var_align_include_headers)

        c4 = self._card(p, "4. 定时同步", "需保持本程序开着", collapsed=True)
        self._note(c4, "到点后按上面的源表重新对齐写入。勾选「仅源表变化时执行」则没改动就跳过。")
        g = self._row3(c4)
        self.var_align_minutes = tk.StringVar(value="60")
        self._cell(g, 0, "间隔（分钟）", self.var_align_minutes)
        self.var_align_sched = tk.BooleanVar(value=False)
        self.var_align_changed = tk.BooleanVar(value=True)
        self._check(c4, "仅源表有变化时执行", self.var_align_changed)
        self._check(c4, "启用定时", self.var_align_sched)
        self.align_sched_info = tk.Label(c4, text="定时未启动", bg=C["card"], fg=C["muted"], font=FS)
        self.align_sched_info.pack(anchor="w", pady=4)

    def _build_video(self, p) -> None:
        c1 = self._card(p, "1. 源表", "B 列视频链接，H 列制作人")
        self.vd_src_note = tk.Label(
            c1,
            text="读取源表：A 列日期、B 列视频链接、H 列制作人（列字母可改）。B 列可以是蓝字文件名，程序会读取单元格里的超链接（Drive / YouTube），再写入另一张表的「日志表」和「数据表」。",
            bg=C["card"],
            fg=C["muted"],
            font=FS,
            wraplength=820,
            justify="left",
        )
        self.vd_src_note.pack(anchor="w", pady=(0, 8))
        self.var_vd_source_url = tk.StringVar()
        self.var_vd_source_sheet = tk.StringVar()
        self.var_vd_start_row = tk.StringVar(value="2")
        self._entry(c1, "源表格链接", self.var_vd_source_url)
        g = self._row3(c1)
        self._cell(g, 0, "工作表名称（多个用逗号分隔）", self.var_vd_source_sheet)
        self._cell(g, 1, "数据起始行", self.var_vd_start_row)

        c2 = self._card(p, "2. 源表列（默认可改）", "和贴文汇总一样，默认 A/B/H/E")
        self.vd_col_note = tk.Label(
            c2,
            text="列用字母。默认：A=日期，B=视频链接，H=制作人，E=类型。",
            bg=C["card"],
            fg=C["muted"],
            font=FS,
            wraplength=820,
            justify="left",
        )
        self.vd_col_note.pack(anchor="w", pady=(0, 8))
        g = self._row3(c2)
        self.var_vd_col_date = tk.StringVar(value="A")
        self.var_vd_col_link = tk.StringVar(value="B")
        self.var_vd_col_name = tk.StringVar(value="H")
        self._cell(g, 0, "日期列", self.var_vd_col_date)
        self.vd_link_cell = self._cell(g, 1, "视频链接列", self.var_vd_col_link)
        self._cell(g, 2, "制作人列", self.var_vd_col_name)
        g2 = self._row3(c2)
        self.var_vd_col_type = tk.StringVar(value="E")
        self._cell(g2, 0, "类型列", self.var_vd_col_type)
        self.vd_extra_col_box = tk.Frame(c2, bg=C["card"])
        self.vd_extra_col_box.pack(fill="x", pady=(6, 0))
        StyleBtn(c2, "ghost", text="+ 添加分类列", command=self._add_vd_extra_col).pack(anchor="e", pady=4)

        c_date = self._card(p, "3. 日期筛选", "和贴文汇总一样，按 A 列日期")
        self._note(
            c_date,
            "填写开始、结束日期后：只查询、只汇总这个范围内的视频。留空则不限日期。格式例如 2026-08-01。",
        )
        g = self._row3(c_date)
        self.var_vd_start = tk.StringVar()
        self.var_vd_end = tk.StringVar()
        self.var_vd_date_filter_enabled = tk.BooleanVar(value=True)
        self._check(c_date, "启用日期筛选（不勾选则处理全部日期）", self.var_vd_date_filter_enabled)
        self._cell(g, 0, "开始日期", self.var_vd_start)
        self._cell(g, 1, "结束日期", self.var_vd_end)

        c_type = self._card(p, "4. 类型 / 分类", "视频用来筛选；分类汇总用来统计")
        self.vd_type_note = tk.Label(
            c_type,
            text="视频时长：只跑下面这些类型。分类汇总：下面是单独统计的分类，每个分类一列。",
            bg=C["card"],
            fg=C["muted"],
            font=FS,
            wraplength=760,
            justify="left",
        )
        self.vd_type_note.pack(anchor="w", pady=(0, 6))
        self.vd_type_box = tk.Frame(c_type, bg=C["card"])
        self.vd_type_box.pack(fill="x")
        self.vd_video_filter_row = tk.Frame(c_type, bg=C["card"])
        self.vd_video_filter_row.pack(fill="x", pady=4)
        tk.Label(self.vd_video_filter_row, text="类型操作", bg=C["card"], fg=C["muted"], font=FS).pack(side="left")
        self.var_vd_type_filter_mode = tk.StringVar(value="只包含这些类型")
        ttk.Combobox(self.vd_video_filter_row, textvariable=self.var_vd_type_filter_mode, values=("只包含这些类型", "排除这些类型", "不筛选类型"), state="readonly", width=22).pack(side="left", padx=8)
        hint = tk.Frame(c_type, bg=C["card"])
        hint.pack(fill="x", pady=6)
        self.vd_type_count = tk.Label(hint, text="0 个分类", bg=C["card"], fg=C["muted"], font=FS)
        self.vd_type_count.pack(side="left")
        StyleBtn(hint, "ghost", text="+ 添加分类", command=self._add_vd_type).pack(side="right")
        StyleBtn(hint, "ghost", text="清空", command=self._clear_vd_types).pack(side="right", padx=6)
        self.vd_wildcard_note = tk.Label(
            c_type,
            text="自定义汇总支持包含匹配和通配符：wsp 会统计所有带 wsp 的分类；口播* 匹配以口播开头的。视频提取按精确分类筛选，不用通配符。",
            bg=C["card"],
            fg=C["muted"],
            font=FS,
            wraplength=760,
            justify="left",
        )
        self.vd_wildcard_note.pack(anchor="w", pady=(0, 4))
        self._add_vd_type()
        self._add_vd_type()

        self.vd_custom_box = tk.Frame(c_type, bg=C["card"])
        self.vd_mode_row = tk.Frame(self.vd_custom_box, bg=C["card"])
        self.vd_mode_row.pack(fill="x", pady=(8, 4))
        tk.Label(self.vd_mode_row, text="统计方式", bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
        self.var_vd_category_mode = tk.StringVar(value="分类单独成列，未分类和其他归入其余")
        self.vd_category_mode_combo = ttk.Combobox(
            self.vd_mode_row,
            textvariable=self.var_vd_category_mode,
            values=(
                "分类单独成列，未分类和其他归入其余",
                "这些分类不统计，未分类和其他全部归入其余",
            ),
            state="readonly",
        )
        self.vd_category_mode_combo.pack(fill="x", pady=(3, 0), ipady=3)
        self.vd_category_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_vd_category_mode())
        self.vd_exclude_wrap = tk.Frame(self.vd_custom_box, bg=C["card"])
        self.vd_exclude_wrap.pack(fill="x")
        tk.Label(self.vd_exclude_wrap, text="额外不统计的分类（不要和上面重复）", bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w", pady=(8, 2))
        self.vd_exclude_box = tk.Frame(self.vd_exclude_wrap, bg=C["card"])
        self.vd_exclude_box.pack(fill="x")
        ex_hint = tk.Frame(self.vd_exclude_wrap, bg=C["card"])
        ex_hint.pack(fill="x", pady=4)
        self.vd_exclude_count = tk.Label(ex_hint, text="0 个排除", bg=C["card"], fg=C["muted"], font=FS)
        self.vd_exclude_count.pack(side="left")
        StyleBtn(ex_hint, "ghost", text="+ 添加排除", command=self._add_vd_exclude).pack(side="right")
        g_other = self._row3(self.vd_custom_box)
        self.var_vd_other_category = tk.StringVar()
        self.vd_other_cell = self._cell(g_other, 0, "其余归入（含未填写分类，例如：图片）", self.var_vd_other_category)
        self.var_vd_empty_to_other = tk.BooleanVar(value=True)
        self.vd_empty_check = self._check(self.vd_custom_box, "没有分类的也归入「其余」", self.var_vd_empty_to_other)
        self.vd_custom_help = tk.Label(
            self.vd_custom_box,
            text="方式一：上面 FB/口播 等各占一列，没填分类的算进「其余」。方式二：上面这些分类完全不统计，剩下的（含空白）全部算进「其余」。不要把同一批分类同时填进两处。",
            bg=C["card"],
            fg=C["muted"],
            font=FS,
            wraplength=760,
            justify="left",
        )
        self.vd_custom_help.pack(anchor="w", pady=(4, 0))

        c3 = self._card(p, "5. 写入目标表", "视频写日志+数据；分类汇总只写数据")
        self.vd_dest_note = tk.Label(
            c3,
            text="视频提取时长会写日志表并按时长汇总；分类汇总不写日志，只按每天每个分类计条数。",
            bg=C["card"],
            fg=C["muted"],
            font=FS,
            wraplength=760,
            justify="left",
        )
        self.vd_dest_note.pack(anchor="w", pady=(0, 6))
        self.var_vd_dest_url = tk.StringVar()
        self._entry(c3, "写入表格链接", self.var_vd_dest_url)
        g = self._row3(c3)
        self.var_vd_log_sheet = tk.StringVar(value="日志表")
        self.var_vd_report_sheet = tk.StringVar(value="数据表")
        self.var_vd_out_start_row = tk.StringVar(value="1")
        self._cell(g, 0, "数据表名称", self.var_vd_report_sheet)
        self.vd_log_cell = self._cell(g, 1, "日志表名称（视频模板）", self.var_vd_log_sheet)
        self._cell(g, 2, "写入起始行", self.var_vd_out_start_row)
        self.vd_video_opts = self._row3(c3)
        self.var_vd_unit = tk.StringVar(value="30")
        self.var_vd_count_mode = tk.StringVar(value="汇总总秒数 ÷ 30")
        self.var_vd_include_headers = tk.BooleanVar(value=True)
        self._cell(self.vd_video_opts, 0, "汇总除数（仅视频模板）", self.var_vd_unit)
        mode_box = tk.Frame(self.vd_video_opts, bg=C["card"])
        mode_box.grid(row=0, column=1, sticky="ew", padx=8)
        tk.Label(mode_box, text="计数方式（统计模式）", bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
        ttk.Combobox(
            mode_box,
            textvariable=self.var_vd_count_mode,
            values=("汇总总秒数 ÷ 30", "逐条视频按30秒计数"),
            state="readonly",
        ).pack(fill="x", pady=(3, 0), ipady=4)
        box = tk.Frame(self.vd_video_opts, bg=C["card"])
        box.grid(row=0, column=2, sticky="ew", padx=8)
        tk.Label(box, text=" ", bg=C["card"]).pack()
        self._check(box, "写入表头", self.var_vd_include_headers)

        self.vd_report_cat_wrap = tk.Frame(c3, bg=C["card"])
        self.vd_report_cat_wrap.pack(fill="x", pady=(8, 0))
        tk.Label(
            self.vd_report_cat_wrap,
            text="数据表额外分类列（可留空：会按第 4 节添加的分类自动建列，总计数 + 逐条 + 每个分类。视频按精确分类名匹配）",
            bg=C["card"],
            fg=C["muted"],
            font=FS,
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        self.vd_report_cat_box = tk.Frame(self.vd_report_cat_wrap, bg=C["card"])
        self.vd_report_cat_box.pack(fill="x")
        rc_hint = tk.Frame(self.vd_report_cat_wrap, bg=C["card"])
        rc_hint.pack(fill="x", pady=4)
        self.vd_report_cat_count = tk.Label(rc_hint, text="0 个额外列", bg=C["card"], fg=C["muted"], font=FS)
        self.vd_report_cat_count.pack(side="left")
        StyleBtn(rc_hint, "ghost", text="+ 添加分类列", command=self._add_vd_report_cat).pack(side="right")
        self._add_vd_report_cat()

        c_sched = self._card(p, "6. 定时提取", "需保持本程序开着")
        self.vd_sched_note = tk.Label(
            c_sched,
            text="到点后自动读取新链接、追加日志，并重新生成数据表。首次启用约 12 秒后执行一轮。",
            bg=C["card"],
            fg=C["muted"],
            font=FS,
            wraplength=820,
            justify="left",
        )
        self.vd_sched_note.pack(anchor="w", pady=(0, 8))
        g3 = self._row3(c_sched)
        self.var_vd_schedule_minutes = tk.StringVar(value="180")
        self._cell(g3, 0, "间隔（分钟）", self.var_vd_schedule_minutes)
        self.var_vd_schedule_enabled = tk.BooleanVar(value=False)
        self.vd_sched_check = self._check(c_sched, "启用视频时长定时", self.var_vd_schedule_enabled)
        self.vd_sched_info = tk.Label(c_sched, text="定时未启动", bg=C["card"], fg=C["muted"], font=FS)
        self.vd_sched_info.pack(anchor="w", pady=4)

        c4 = self._card(p, "说明", collapsed=True)
        self.vd_help_note = tk.Label(
            c4,
            text="日志表：A 日期、B 链接、C 名字、D 时长(秒)、E 类型、F 备注（未识别原因）。已跑过的链接下次自动跳过。"
            "数据表：每人两列（总计数、逐条计数），添加分类再加列；一人一色，姓名合并，人与人之间有分隔线。"
            "未识别常见原因：不是视频、没有权限、Drive 还没生成时长。源表和 Drive 文件都要共享给服务账号。",
            bg=C["card"],
            fg=C["muted"],
            font=FS,
            wraplength=820,
            justify="left",
        )
        self.vd_help_note.pack(anchor="w", pady=(0, 8))

    def _build_roster(self, p) -> None:
        from roster_fill import DEFAULT_COLUMNS

        c1 = self._card(p, "1. 配置表", "提供人员、专页和数据表链接")
        self._note(
            c1,
            "配置表默认：A 队别、B 类型、C 名字、G 专页名字、H 专页编码、I 专页链接、K chat、Q 是否上表、R 数据表格。"
            "Q 列打勾 / true 的才汇总。R 列可以是蓝字超链接，程序会打开里面的「引流」工作表。",
        )
        self.var_roster_config_url = tk.StringVar()
        self.var_roster_config_sheet = tk.StringVar()
        self.var_roster_start_row = tk.StringVar(value="2")
        self._entry(c1, "配置表链接", self.var_roster_config_url)
        g = self._row3(c1)
        self._cell(g, 0, "工作表名（留空=第一个）", self.var_roster_config_sheet)
        self._cell(g, 1, "数据起始行", self.var_roster_start_row)

        c2 = self._card(p, "2. 字段映射", "列字母可改")
        self._note(c2, "每一行：字段名、用途、列字母。默认已按你给的 A/B/C/G/H/I/K/R 填好。")
        fh = tk.Frame(c2, bg="#ecfdf5")
        fh.pack(fill="x")
        tk.Label(fh, text="字段", bg="#ecfdf5", font=FS, fg=C["muted"], width=16, anchor="w").pack(side="left", padx=4, pady=4)
        tk.Label(fh, text="用途", bg="#ecfdf5", font=FS, fg=C["muted"], width=14, anchor="w").pack(side="left", padx=4, pady=4)
        tk.Label(fh, text="列", bg="#ecfdf5", font=FS, fg=C["muted"], width=8, anchor="w").pack(side="left", padx=4, pady=4)
        self.roster_map_box = tk.Frame(c2, bg=C["card"])
        self.roster_map_box.pack(fill="x")
        self._roster_map_rows: list[tk.Frame] = []
        hr = tk.Frame(c2, bg=C["card"])
        hr.pack(fill="x", pady=4)
        StyleBtn(hr, "ghost", text="+ 添加字段", command=lambda: self._add_roster_map_row("", "name", "")).pack(side="right")
        StyleBtn(hr, "ghost", text="恢复默认列", command=self._reset_roster_map).pack(side="right", padx=6)
        for item in DEFAULT_COLUMNS:
            self._add_roster_map_row(item["field"], item["role"], item["column"])

        c3 = self._card(p, "3. 写入目标表", "按队别分工作表")
        self._note(
            c3,
            "目标表没有固定 sheet 名：按 A 列队别自动建工作表，同一队别写进同名 sheet。"
            "一列一个专页编码，同一个名字的专页排在一起。"
            "第 1 行 chat，第 2 行数据表链接，第 3 行专页链接，第 4 行空，第 5 行专页编码，第 6 行名字，第 7 行空，第 8 行类型，9–23 行空。"
            "A24 起按引流表结构：一行日期，下面 24 个小时段（00:00-01:00 … 23:00-00:00），再下一日期，日期新的在前。"
            "用第 5 行专页编码去引流表找对应列，按 A 列日期/时段对齐写入。",
        )
        self.var_roster_target_url = tk.StringVar()
        self.var_roster_traffic_sheet = tk.StringVar(value="引流")
        self.var_roster_date_start = tk.StringVar(value="24")
        self._entry(c3, "目标表格链接", self.var_roster_target_url)
        g = self._row3(c3)
        self._cell(g, 0, "引流工作表名", self.var_roster_traffic_sheet)
        self._cell(g, 1, "日期起始行", self.var_roster_date_start)

    def _add_roster_map_row(self, field: str, role: str, column: str) -> None:
        row = tk.Frame(self.roster_map_box, bg=C["card"])
        row.pack(fill="x", pady=3)
        f = tk.Entry(row, font=MONO, relief="solid", bd=1, width=16)
        f.insert(0, field)
        f.pack(side="left", ipady=4, padx=(0, 6))
        r = tk.Entry(row, font=MONO, relief="solid", bd=1, width=14)
        r.insert(0, role)
        r.pack(side="left", ipady=4, padx=(0, 6))
        c = tk.Entry(row, font=MONO, relief="solid", bd=1, width=8)
        c.insert(0, column)
        c.pack(side="left", ipady=4, padx=(0, 6))
        StyleBtn(row, "ghost", text="删除", command=lambda: self._del_roster_map_row(row)).pack(side="left")
        row._field, row._role, row._column = f, r, c
        self._roster_map_rows.append(row)

    def _del_roster_map_row(self, row) -> None:
        if row in self._roster_map_rows:
            self._roster_map_rows.remove(row)
        row.destroy()
        if not self._roster_map_rows:
            self._reset_roster_map()

    def _reset_roster_map(self) -> None:
        from roster_fill import DEFAULT_COLUMNS

        for row in list(self._roster_map_rows):
            row.destroy()
        self._roster_map_rows = []
        for item in DEFAULT_COLUMNS:
            self._add_roster_map_row(item["field"], item["role"], item["column"])

    def _read_roster_columns(self) -> list[dict]:
        out = []
        for row in self._roster_map_rows:
            role = row._role.get().strip().lower()
            column = row._column.get().strip().upper()
            if role and column:
                out.append({"field": row._field.get().strip() or role, "role": role, "column": column})
        return out

    def _set_roster_columns(self, items) -> None:
        from roster_fill import DEFAULT_COLUMNS

        for row in list(self._roster_map_rows):
            row.destroy()
        self._roster_map_rows = []
        columns = [item for item in (items or []) if isinstance(item, dict) and item.get("column")]
        if not columns:
            columns = list(DEFAULT_COLUMNS)
        else:
            seen = {str(item.get("role") or "").strip().lower() for item in columns}
            for item in DEFAULT_COLUMNS:
                if item["role"] not in seen:
                    columns.append(item)
        for item in columns:
            self._add_roster_map_row(str(item.get("field") or ""), str(item.get("role") or ""), str(item.get("column") or ""))

    def _build_log(self) -> None:
        logf = tk.Frame(self, bg=C["log"])
        logf.pack(fill="x", side="bottom")
        top = tk.Frame(logf, bg=C["log"])
        top.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(top, text="运行日志", bg=C["log"], fg=C["cream"], font=FB).pack(side="left")
        self.badge = tk.Label(top, text="待命", bg="#3a463c", fg=C["cream"], font=FS, padx=8, pady=2)
        self.badge.pack(side="right")
        self.log = tk.Text(logf, bg=C["log"], fg="#d7e3d4", insertbackground="#d7e3d4", font=MONO, wrap="word", height=8, bd=0)
        self.log.pack(fill="x", padx=10, pady=8)
        self.log.insert("end", "软件已启动。左侧可切换、重命名或新增配置菜单；改完点保存。\n")
        self.log.configure(state="disabled")

    def _copy_sa(self):
        self.clipboard_clear()
        self.clipboard_append(self.var_sa.get())
        self._append_log("已复制服务账号邮箱")

    def _refresh_sa_display(self) -> None:
        files = [str(p).strip() for p in (self._cred_files or []) if str(p).strip()]
        self._cred_files = files
        self.var_credentials.set(files[0] if files else "")
        emails: list[str] = []
        for path in files:
            emails.append(service_account_email(Path(path)) or Path(path).name)
        self.var_sa.set("\n".join(emails) if emails else "未找到服务账号")
        if len(files) > 1:
            self.sa_hint.set(f"{len(files)} 个服务账号轮询 · 每个都要共享编辑者")
        else:
            self.sa_hint.set("源表和目标表都要共享给它 · 编辑者")

    def _remove_last_credential(self) -> None:
        if len(self._cred_files) <= 1:
            messagebox.showinfo("数据汇总工具", "至少保留一个服务账号。")
            return
        removed = self._cred_files.pop()
        self._refresh_sa_display()
        self._append_log(f"已移除服务账号：{Path(removed).name}")

    def _choose_credentials(self) -> None:
        current = Path(self.var_credentials.get().strip()) if self.var_credentials.get().strip() else None
        initial = current.parent if current and current.parent.exists() else Path.home()
        selected = filedialog.askopenfilenames(
            title="选择 Google 服务账号 JSON（可多选）",
            initialdir=str(initial),
            filetypes=(("JSON 文件", "*.json"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        added = []
        for item in selected:
            src = Path(item)
            email = service_account_email(src)
            if not email:
                messagebox.showerror("数据汇总工具", f"{src.name} 不是有效的 Google 服务账号 JSON（未找到 client_email）。")
                continue
            slug = email.split("@")[0].replace(".", "-")
            dest = SCRIPT_DIR / f"credentials-{slug}.json"
            try:
                shutil.copy2(src, dest)
                stored = str(dest)
            except Exception:
                stored = str(src)
            if stored not in self._cred_files:
                self._cred_files.append(stored)
                added.append(email)
        self._refresh_sa_display()
        if added:
            self._append_log("已添加服务账号：" + "、".join(added) + "。额度满了会自动换下一个，不必长时间等待。")

    def _check_updates(self) -> None:
        self.btn_update.configure(state="disabled", text="检查中…")

        def work():
            try:
                import json
                from urllib.request import Request, urlopen

                req = Request(
                    UPDATE_API_URL,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": f"sheets-post-filter/{APP_VERSION}",
                    },
                )
                with urlopen(req, timeout=15) as response:
                    data = json.loads(response.read().decode("utf-8"))
                latest = str(data.get("tag_name") or data.get("name") or "").strip()
                url = str(data.get("html_url") or RELEASES_URL)
                if not latest:
                    raise RuntimeError("更新服务器没有返回版本号")
                self.after(0, lambda: self._show_update_result(latest, url))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._show_update_error(e))

        threading.Thread(target=work, daemon=True).start()

    def _show_update_result(self, latest: str, url: str) -> None:
        self.btn_update.configure(state="normal", text="检查更新")
        extra = "\n\n点「打开日志」可查看报错记录。"
        if version_tuple(latest) > version_tuple(APP_VERSION):
            if messagebox.askyesno(
                "发现新版本",
                f"当前版本：{APP_VERSION}\n最新版本：{latest}\n\n是否打开下载页面？{extra}",
            ):
                import webbrowser

                webbrowser.open(url)
        else:
            if messagebox.askyesno("检查更新", f"当前版本 {APP_VERSION} 已是最新。\n\n是否打开运行日志？"):
                self._open_logs()

    def _show_update_error(self, error: str) -> None:
        self.btn_update.configure(state="normal", text="检查更新")
        if messagebox.askyesno(
            "检查更新失败",
            "无法读取 GitHub Release。\n\n" + error + "\n\n是否打开运行日志排查？",
        ):
            self._open_logs()

    def _open_logs(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(LOG_DIR.glob("app-*.log"), reverse=True)
        latest = files[0] if files else None
        win = tk.Toplevel(self)
        win.title("运行日志")
        win.geometry("780x480")
        win.configure(bg=C["paper"])
        tk.Label(win, text=str(latest or LOG_DIR), bg=C["paper"], fg=C["muted"], font=FS).pack(anchor="w", padx=12, pady=8)
        text = tk.Text(win, bg=C["log"], fg="#d7e3d4", font=MONO, wrap="word")
        text.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        if latest and latest.exists():
            content = latest.read_text(encoding="utf-8", errors="replace")
            text.insert("end", content[-40000:])
        else:
            text.insert("end", "还没有日志文件。任务报错后会写到这个文件夹。")
        text.configure(state="disabled")
        btns = tk.Frame(win, bg=C["paper"])
        btns.pack(fill="x", padx=12, pady=8)
        StyleBtn(btns, "ghost", text="打开日志文件夹", command=lambda: os.startfile(str(LOG_DIR))).pack(side="left")
        StyleBtn(btns, "primary", text="关闭", command=win.destroy).pack(side="right")

    def _set_interval(self, minutes: int) -> None:
        self.var_minutes.set(str(minutes))
        self.var_sched.set(True)
        self._save(quiet=True)
        self._append_log(f"定时间隔已改为 {minutes} 分钟，并已启用")

    def _upd_align_headers(self):
        n = sum(1 for row in getattr(self, "_align_map_rows", []) if row._target.get().strip())
        if hasattr(self, "align_header_count"):
            self.align_header_count.configure(text=f"{n} 列")

    def _add_align_map_row(self, target: str = "", source: str = "") -> None:
        row = tk.Frame(self.align_map_box, bg=C["card"])
        row.pack(fill="x", pady=3)
        t = tk.Entry(row, font=MONO, relief="solid", bd=1, width=18)
        t.insert(0, target)
        t.pack(side="left", ipady=4, padx=(0, 6))
        s = tk.Entry(row, font=MONO, relief="solid", bd=1, width=18)
        s.insert(0, source or target)
        s.pack(side="left", ipady=4, padx=(0, 6))
        StyleBtn(row, "ghost", text="删除", command=lambda: self._del_align_map_row(row)).pack(side="left")
        row._target, row._source = t, s
        t.bind("<KeyRelease>", lambda _e: self._upd_align_headers())
        self._align_map_rows.append(row)
        self._upd_align_headers()

    def _del_align_map_row(self, row) -> None:
        if row in self._align_map_rows:
            self._align_map_rows.remove(row)
        row.destroy()
        if not self._align_map_rows:
            self._add_align_map_row("", "")
        self._upd_align_headers()

    def _read_align_mappings(self) -> list[dict]:
        out = []
        for row in getattr(self, "_align_map_rows", []):
            target = row._target.get().strip()
            if not target:
                continue
            source = row._source.get().strip() or target
            out.append({"target": target, "source": source})
        return out

    def _write_align_mappings(self, mappings) -> None:
        for row in list(getattr(self, "_align_map_rows", [])):
            row.destroy()
        self._align_map_rows = []
        cleaned = [item for item in (mappings or []) if isinstance(item, dict) and str(item.get("target") or "").strip()]
        if not cleaned:
            self._add_align_map_row("", "")
            return
        for item in cleaned:
            self._add_align_map_row(str(item.get("target") or "").strip(), str(item.get("source") or item.get("target") or "").strip())

    def _refresh_align_profiles(self) -> None:
        self._save_align_profile()
        values = ["所有链接的默认映射"]
        self._align_profile_urls = {"所有链接的默认映射": "__default__"}
        for index, source in enumerate(self._read_src("_align_rows", align=True), 1):
            label = f"单独配置：{source.get('sheet') or f'链接 {index}'}"
            values.append(label)
            self._align_profile_urls[label] = source["url"]
        self.align_profile_combo.configure(values=values)
        selected = next((label for label, key in self._align_profile_urls.items() if key == self._align_profile_key), values[0])
        self.var_align_profile.set(selected)

    def _save_align_profile(self) -> None:
        if not hasattr(self, "align_map_box"):
            return
        mappings = self._read_align_mappings()
        if self._align_profile_key == "__default__":
            self._align_default_mappings = mappings
        else:
            self._align_profiles[self._align_profile_key] = mappings

    def _switch_align_profile(self) -> None:
        self._save_align_profile()
        key = getattr(self, "_align_profile_urls", {}).get(self.var_align_profile.get(), "__default__")
        self._align_profile_key = key
        mappings = self._align_default_mappings if key == "__default__" else self._align_profiles.get(key, self._align_default_mappings)
        self._write_align_mappings(mappings)

    def _payload(self) -> dict:
        self._save_align_profile()
        return {
            "target_url": self.var_target_url.get().strip(),
            "hot_target_url": self.var_hot_target_url.get().strip(),
            "output_sheet": self.var_output_sheet.get().strip(),
            "hot_output_sheet": self.var_hot_output_sheet.get().strip(),
            "output_start_row": self.var_output_start_row.get().strip(),
            "hot_start_row": self.var_hot_start_row.get().strip(),
            "start_date": self.var_start.get().strip(),
            "end_date": self.var_end.get().strip(),
            "likes_threshold": self.var_likes.get().strip(),
            "schedule_minutes": self.var_minutes.get().strip(),
            "credentials_file": (self._cred_files[0] if self._cred_files else self.var_credentials.get().strip()),
            "credentials_files": list(self._cred_files or ([self.var_credentials.get().strip()] if self.var_credentials.get().strip() else [])),
            "exclude_id_value": self.var_exclude.get().strip(),
            "date_field": self.var_date_field.get().strip(),
            "sort_field": self.var_sort_field.get().strip(),
            "align_target_url": self.var_align_target_url.get().strip(),
            "align_output_sheet": self.var_align_output_sheet.get().strip(),
            "align_start_row": self.var_align_start_row.get().strip(),
            "align_source_sheet": self.var_align_source_sheet.get().strip(),
            "align_header_row": self.var_align_header_row.get().strip(),
            "align_schedule_minutes": self.var_align_minutes.get().strip(),
            "cf_publish_url": self.var_cf_url.get().strip(),
            "cf_publish_secret": self.var_cf_secret.get().strip(),
            "cf_publish_source": self.var_cf_source.get().strip() or "all",
            "catalog_index_url": self.var_catalog_index_url.get().strip(),
            "catalog_index_sheet": self.var_catalog_index_sheet.get().strip(),
            "catalog_start_row": self.var_catalog_start_row.get().strip(),
            "catalog_url_col": self.var_catalog_url_col.get().strip(),
            "catalog_sheet_col": self.var_catalog_sheet_col.get().strip(),
            "catalog_target_url": self.var_catalog_target_url.get().strip(),
            "catalog_output_sheet": self.var_catalog_output_sheet.get().strip(),
            "catalog_output_start_row": self.var_catalog_output_start_row.get().strip(),
            "catalog_keep_each_header": self.var_catalog_keep_header.get(),
            "catalog_schedule_enabled": self.var_catalog_sched.get(),
            "catalog_schedule_minutes": self.var_catalog_minutes.get().strip(),
            "include_headers": self.var_include_headers.get(),
            "hot_include_headers": self.var_hot_include_headers.get(),
            "add_source_column": self.var_add_source_column.get(),
            "sort_descending": self.var_sort_desc.get(),
            "write_all": self.var_write_all.get(),
            "write_hot": self.var_write_hot.get(),
            "upsert_by_id": self.var_upsert.get(),
            "schedule_enabled": self.var_sched.get(),
            "schedule_only_if_changed": self.var_changed.get(),
            "align_include_headers": self.var_align_include_headers.get(),
            "align_schedule_enabled": self.var_align_sched.get(),
            "align_schedule_only_if_changed": self.var_align_changed.get(),
            "cf_publish_after_sync": self.var_cf_after.get(),
            "vd_source_url": self.var_vd_source_url.get().strip(),
            "vd_source_sheet": self.var_vd_source_sheet.get().strip(),
            "vd_start_row": self.var_vd_start_row.get().strip(),
            "vd_col_date": self.var_vd_col_date.get().strip(),
            "vd_col_link": self.var_vd_col_link.get().strip(),
            "vd_col_name": self.var_vd_col_name.get().strip(),
            "vd_col_type": self.var_vd_col_type.get().strip(),
            "vd_types": self._read_vd_types(),
            "vd_report_categories": self._read_vd_report_cats(),
            "vd_dest_url": self.var_vd_dest_url.get().strip(),
            "vd_log_sheet": self.var_vd_log_sheet.get().strip(),
            "vd_report_sheet": self.var_vd_report_sheet.get().strip(),
            "vd_out_start_row": self.var_vd_out_start_row.get().strip(),
            "vd_unit_seconds": self.var_vd_unit.get().strip(),
            "vd_count_mode": (
                "per_video_ceil"
                if self.var_vd_count_mode.get() == "逐条视频按30秒计数"
                else "divide_total"
            ),
            "vd_include_headers": self.var_vd_include_headers.get(),
            "vd_schedule_enabled": self.var_vd_schedule_enabled.get(),
            "vd_schedule_minutes": self.var_vd_schedule_minutes.get().strip(),
            "vd_start_date": self.var_vd_start.get().strip(),
            "vd_end_date": self.var_vd_end.get().strip(),
            "vd_source_sheets": [value.strip() for value in self.var_vd_source_sheet.get().replace("，", ",").split(",") if value.strip()],
            "vd_date_filter_enabled": self.var_vd_date_filter_enabled.get(),
            "vd_type_filter_mode": {"只包含这些类型": "include", "排除这些类型": "exclude", "不筛选类型": "all"}.get(self.var_vd_type_filter_mode.get(), "include"),
            "vd_write_log": next((item.get("template") == "video" for item in self._menus if item.get("id") == self._active_menu_id), True),
            "vd_other_category": self.var_vd_other_category.get().strip(),
            "vd_exclude_types": self._read_vd_exclude_types(),
            "vd_category_mode": (
                "other_only"
                if "不统计" in (self.var_vd_category_mode.get() or "")
                else "columns_plus_other"
            ),
            "vd_empty_to_other": self.var_vd_empty_to_other.get(),
            "vd_columns": self._read_vd_columns(),
            "roster_config_url": self.var_roster_config_url.get().strip(),
            "roster_config_sheet": self.var_roster_config_sheet.get().strip(),
            "roster_start_row": self.var_roster_start_row.get().strip(),
            "roster_target_url": self.var_roster_target_url.get().strip(),
            "roster_traffic_sheet": self.var_roster_traffic_sheet.get().strip(),
            "roster_date_start_row": self.var_roster_date_start.get().strip(),
            "roster_columns": self._read_roster_columns(),
            "sources": self._read_src("_src_rows"),
            "fields": self._read_fields(),
            "align_sources": self._read_src("_align_rows", align=True),
            "align_headers": [item["target"] for item in self._align_default_mappings],
            "align_mappings": self._align_default_mappings,
            "align_mapping_profiles": self._align_profiles,
        }

    def _load_cfg(self, cfg) -> None:
        self.var_start.set(cfg.start_date or "")
        self.var_end.set(cfg.end_date or "")
        self.var_likes.set(str(cfg.likes_threshold or 1000))
        self.var_add_source_column.set(bool(cfg.add_source_column))
        self.var_upsert.set(bool(cfg.upsert_by_id))
        self.var_write_all.set(bool(cfg.write_all))
        self.var_write_hot.set(bool(cfg.write_hot))
        self.var_target_url.set(cfg.target_url or "")
        self.var_output_sheet.set(cfg.output_sheet or "筛选结果")
        self.var_output_start_row.set(str(cfg.output_start_row or 1))
        self.var_include_headers.set(bool(cfg.include_headers))
        self.var_hot_target_url.set(cfg.hot_target_url or "")
        self.var_hot_output_sheet.set(cfg.hot_output_sheet or "点赞1000以上")
        self.var_hot_start_row.set(str(cfg.hot_start_row or 1))
        self.var_hot_include_headers.set(bool(cfg.hot_include_headers))
        self.var_cf_url.set(cfg.cf_publish_url or "")
        self.var_cf_secret.set(cfg.cf_publish_secret or "")
        self.var_cf_source.set(cfg.cf_publish_source or "all")
        self.var_cf_after.set(bool(cfg.cf_publish_after_sync))
        self.var_minutes.set(str(cfg.schedule_minutes or 1440))
        self.var_sched.set(bool(cfg.schedule_enabled))
        self.var_changed.set(bool(cfg.schedule_only_if_changed))
        creds = [str(x).strip() for x in (getattr(cfg, "credentials_files", None) or []) if str(x).strip()]
        if cfg.credentials_file and cfg.credentials_file not in creds:
            creds.insert(0, cfg.credentials_file)
        self._cred_files = creds
        self.var_credentials.set(creds[0] if creds else "")
        self._refresh_sa_display()
        self.var_exclude.set(cfg.exclude_id_value or "未找到")
        self.var_date_field.set(cfg.date_field or "发布日期")
        self.var_sort_field.set(cfg.sort_field or "点赞")
        self.var_sort_desc.set(bool(cfg.sort_descending))
        self.var_align_source_sheet.set(cfg.align_source_sheet or "")
        self.var_align_header_row.set(str(cfg.align_header_row or 1))
        self.var_align_target_url.set(cfg.align_target_url or "")
        self.var_align_output_sheet.set(cfg.align_output_sheet or "对齐结果")
        self.var_align_start_row.set(str(cfg.align_start_row or 1))
        self.var_align_include_headers.set(bool(cfg.align_include_headers))
        self.var_align_minutes.set(str(cfg.align_schedule_minutes or 60))
        self.var_align_sched.set(bool(cfg.align_schedule_enabled))
        self.var_align_changed.set(bool(cfg.align_schedule_only_if_changed))
        self.var_catalog_index_url.set(getattr(cfg, "catalog_index_url", "") or "")
        self.var_catalog_index_sheet.set(getattr(cfg, "catalog_index_sheet", "") or "")
        self.var_catalog_start_row.set(str(getattr(cfg, "catalog_start_row", 2) or 2))
        self.var_catalog_url_col.set(getattr(cfg, "catalog_url_col", "B") or "B")
        self.var_catalog_sheet_col.set(getattr(cfg, "catalog_sheet_col", "D") or "D")
        self.var_catalog_target_url.set(getattr(cfg, "catalog_target_url", "") or "")
        self.var_catalog_output_sheet.set(getattr(cfg, "catalog_output_sheet", "目录汇总") or "目录汇总")
        self.var_catalog_output_start_row.set(str(getattr(cfg, "catalog_output_start_row", 1) or 1))
        self.var_catalog_keep_header.set(bool(getattr(cfg, "catalog_keep_each_header", False)))
        self.var_catalog_sched.set(bool(getattr(cfg, "catalog_schedule_enabled", False)))
        self.var_catalog_minutes.set(str(getattr(cfg, "catalog_schedule_minutes", 180) or 180))
        self.var_vd_source_url.set(getattr(cfg, "vd_source_url", "") or "")
        source_sheets = getattr(cfg, "vd_source_sheets", None) or []
        self.var_vd_source_sheet.set(", ".join(source_sheets) if source_sheets else (getattr(cfg, "vd_source_sheet", "") or ""))
        self.var_vd_start_row.set(str(getattr(cfg, "vd_start_row", 2) or 2))
        self.var_vd_col_date.set(getattr(cfg, "vd_col_date", "A") or "A")
        self.var_vd_col_link.set(getattr(cfg, "vd_col_link", "B") or "B")
        self.var_vd_col_name.set(getattr(cfg, "vd_col_name", "H") or "H")
        self.var_vd_col_type.set(getattr(cfg, "vd_col_type", "E") or "E")
        self._set_vd_types(getattr(cfg, "vd_types", None) or [])
        self._set_vd_report_cats(getattr(cfg, "vd_report_categories", None) or [])
        self.var_vd_dest_url.set(getattr(cfg, "vd_dest_url", "") or "")
        self.var_vd_log_sheet.set(getattr(cfg, "vd_log_sheet", "日志表") or "日志表")
        self.var_vd_report_sheet.set(getattr(cfg, "vd_report_sheet", "数据表") or "数据表")
        self.var_vd_out_start_row.set(str(getattr(cfg, "vd_out_start_row", 1) or 1))
        self.var_vd_unit.set(str(getattr(cfg, "vd_unit_seconds", 30) or 30))
        self.var_vd_count_mode.set(
            "逐条视频按30秒计数"
            if getattr(cfg, "vd_count_mode", "divide_total") == "per_video_ceil"
            else "汇总总秒数 ÷ 30"
        )
        self.var_vd_include_headers.set(bool(getattr(cfg, "vd_include_headers", True)))
        self.var_vd_start.set(getattr(cfg, "vd_start_date", "") or "")
        self.var_vd_end.set(getattr(cfg, "vd_end_date", "") or "")
        self.var_vd_schedule_enabled.set(bool(getattr(cfg, "vd_schedule_enabled", False)))
        self.var_vd_schedule_minutes.set(str(getattr(cfg, "vd_schedule_minutes", 180) or 180))
        self.var_vd_date_filter_enabled.set(bool(getattr(cfg, "vd_date_filter_enabled", True)))
        self.var_vd_type_filter_mode.set({"include": "只包含这些类型", "exclude": "排除这些类型", "all": "不筛选类型"}.get(getattr(cfg, "vd_type_filter_mode", "include"), "只包含这些类型"))
        self.var_vd_other_category.set(getattr(cfg, "vd_other_category", "") or "")
        self.var_roster_config_url.set(getattr(cfg, "roster_config_url", "") or "")
        self.var_roster_config_sheet.set(getattr(cfg, "roster_config_sheet", "") or "")
        self.var_roster_start_row.set(str(getattr(cfg, "roster_start_row", 2) or 2))
        self.var_roster_target_url.set(getattr(cfg, "roster_target_url", "") or "")
        self.var_roster_traffic_sheet.set(getattr(cfg, "roster_traffic_sheet", "引流") or "引流")
        self.var_roster_date_start.set(str(getattr(cfg, "roster_date_start_row", 24) or 24))
        self._set_roster_columns(getattr(cfg, "roster_columns", None) or [])
        exclude = list(getattr(cfg, "vd_exclude_types", None) or [])
        types = list(getattr(cfg, "vd_types", None) or [])
        if types and exclude and {str(x).strip() for x in types} == {str(x).strip() for x in exclude}:
            exclude = []
        self._set_vd_exclude_types(exclude)
        self.var_vd_category_mode.set(
            "这些分类不统计，未分类和其他全部归入其余"
            if getattr(cfg, "vd_category_mode", "columns_plus_other") == "other_only"
            else "分类单独成列，未分类和其他归入其余"
        )
        self.var_vd_empty_to_other.set(bool(getattr(cfg, "vd_empty_to_other", True)))
        if hasattr(self, "vd_category_mode_combo"):
            self._apply_vd_category_mode()
        self._set_vd_columns(getattr(cfg, "vd_columns", None) or [])
        self._set_src(self.src_box, "_src_rows", "例如：管理组", self.src_count, cfg.sources or cfg.source_urls)
        self._set_src(self.align_box, "_align_rows", "例如：8月份", self.align_count, cfg.align_sources, align=True)
        for r in list(self._field_rows):
            r.destroy()
        self._field_rows = []
        for f in cfg.fields or copy_default_fields():
            self._add_field(f)
        mappings = getattr(cfg, "align_mappings", None) or [
            {"target": header, "source": header} for header in (cfg.align_headers or [])
        ]
        self._align_default_mappings = copy.deepcopy(mappings)
        self._align_profiles = copy.deepcopy(getattr(cfg, "align_mapping_profiles", None) or {})
        self._align_profile_key = "__default__"
        self._write_align_mappings(self._align_default_mappings)
        self._refresh_align_profiles()

    def _save(self, quiet: bool = False) -> None:
        cfg = self._cfg_for_action()
        payload = self._payload()
        current = next((item for item in self._menus if item["id"] == self._active_menu_id), None)
        if current:
            current["settings"] = copy.deepcopy(payload)
        cfg.ui_menus = copy.deepcopy(self._menus)
        cfg.ui_active_menu = self._active_menu_id
        save_config(cfg)
        self.cfg = cfg
        if cfg.schedule_enabled:
            start_scheduler(cfg.schedule_minutes, cfg.schedule_only_if_changed)
        else:
            stop_scheduler()
        if cfg.align_schedule_enabled:
            start_align_scheduler(cfg.align_schedule_minutes, cfg.align_schedule_only_if_changed)
        else:
            stop_align_scheduler()
        if cfg.vd_schedule_enabled:
            if not cfg.vd_source_url or not cfg.vd_dest_url:
                self.var_vd_schedule_enabled.set(False)
                cfg.vd_schedule_enabled = False
                save_config(cfg)
                stop_video_scheduler()
                if not quiet:
                    messagebox.showwarning("数据汇总工具", "视频时长定时需要先填写源表和目标表链接")
            else:
                start_video_scheduler(cfg.vd_schedule_minutes)
        else:
            stop_video_scheduler()
        if getattr(cfg, "catalog_schedule_enabled", False):
            if not cfg.catalog_index_url or not cfg.catalog_target_url:
                self.var_catalog_sched.set(False)
                cfg.catalog_schedule_enabled = False
                save_config(cfg)
                stop_catalog_scheduler()
                if not quiet:
                    messagebox.showwarning("数据汇总工具", "目录汇总定时需要先填写目录表和目标表链接")
            else:
                start_catalog_scheduler(cfg.catalog_schedule_minutes)
        else:
            stop_catalog_scheduler()
        if not quiet:
            self._append_log("已保存配置")

    def _cfg_for_action(self):
        """Build a complete config while retaining the editable menu definitions."""
        payload = self._payload()
        current = next((item for item in self._menus if item["id"] == self._active_menu_id), None)
        if current:
            current["settings"] = copy.deepcopy(payload)
        cfg = _cfg_from_payload(payload)
        cfg.ui_menus = copy.deepcopy(self._menus)
        cfg.ui_active_menu = self._active_menu_id
        return cfg

    def _run_now(self) -> None:
        cfg = self._cfg_for_action()
        err = start_filter_job(cfg, from_schedule=False)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._append_log("筛选汇总已加入队列")
        self._set_badge("排队中", C["accent"])

    def _run_align(self) -> None:
        cfg = self._cfg_for_action()
        err = start_align_job(cfg)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._append_log("表头对齐已加入队列")
        self._set_badge("排队中", C["accent"])

    def _run_catalog(self) -> None:
        cfg = self._cfg_for_action()
        err = start_catalog_job(cfg)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._append_log("目录汇总已加入队列")
        self._set_badge("排队中", C["accent"])

    def _run_roster(self) -> None:
        cfg = self._cfg_for_action()
        err = start_roster_job(cfg)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._append_log("队别专页汇总已加入队列")
        self._set_badge("排队中", C["accent"])

    def _publish(self) -> None:
        cfg = self._cfg_for_action()
        err = start_publish_job(cfg)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._set_badge("发布中", C["accent"])

    def _run_video(self) -> None:
        cfg = self._cfg_for_action()
        err = start_video_job(cfg)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._append_log("视频任务已加入队列")
        self._set_badge("排队中", C["accent"])

    def _stop_current(self) -> None:
        stop_job(self._active_menu_id or "default")
        self._append_log("已请求停止当前配置")

    def _stop_all(self) -> None:
        stop_all_jobs()
        self._append_log("已请求停止全部任务")

    def _run_selected(self) -> None:
        starters = {
            "filter": start_filter_job,
            "catalog": start_catalog_job,
            "align": start_align_job,
            "video": start_video_job,
            "custom": start_video_job,
            "roster": start_roster_job,
        }
        queued = 0
        for item in self._menus:
            if not self._menu_checks.get(item["id"], tk.BooleanVar(value=False)).get():
                continue
            settings = copy.deepcopy(item.get("settings") or {})
            if item["id"] == self._active_menu_id:
                settings = copy.deepcopy(self._payload())
                item["settings"] = settings
            cfg = _cfg_from_payload(settings)
            cfg.ui_active_menu = item["id"]
            cfg.ui_menus = copy.deepcopy(self._menus)
            fn = starters.get(item["template"])
            if not fn:
                continue
            err = fn(cfg)
            if err:
                self._append_log(f"{item['name']} 未排队：{err}")
            else:
                queued += 1
                self._append_log(f"{item['name']} 已加入队列")
        if queued:
            self._log_n = 0
            self._set_badge("排队中", C["accent"])
        else:
            messagebox.showinfo("数据汇总工具", "没有可执行的配置。请在左侧勾选要跑的菜单。")

    def _peek_headers(self) -> None:
        cfg = self._cfg_for_action()
        srcs = cfg.align_sources or []
        source = next((item for item in srcs if item.get("url") == self._align_profile_key), srcs[0] if srcs else None)
        if not source or not source.get("url"):
            messagebox.showwarning("数据汇总工具", "请先填写至少一个数据源链接")
            return

        def work():
            try:
                headers = peek_source_headers(
                    cfg,
                    source["url"],
                    sheet_name=source.get("sheet") or cfg.align_source_sheet or "",
                    header_row=int(cfg.align_header_row or 1),
                )
                self.after(0, lambda: self._fill_headers(headers))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("数据汇总工具", str(e)))

        threading.Thread(target=work, daemon=True).start()
        self._append_log("正在读取源表表头…")

    def _fill_headers(self, headers):
        mappings = [{"target": header, "source": header} for header in (headers or [])]
        self._write_align_mappings(mappings)
        self._save_align_profile()
        self._append_log(f"读到 {len(headers or [])} 个表头")

    def _set_badge(self, text, bg):
        if not self._alive:
            return
        try:
            self.badge.configure(text=text, bg=bg)
        except Exception:
            pass

    def _append_log(self, msg: str) -> None:
        if not self._alive:
            return
        try:
            self.log.configure(state="normal")
            self.log.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        except Exception:
            return
        try:
            write_app_log(msg)
        except Exception:
            pass

    def _tick(self) -> None:
        if not self._alive:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            job = job_snapshot(self._active_menu_id or "default")
            logs = job.get("logs") or []
            if self._log_n > len(logs):
                self._log_n = 0
            if len(logs) > self._log_n:
                chunk = logs[self._log_n :]
                if len(chunk) > 150:
                    chunk = chunk[-150:]
                self.log.configure(state="normal")
                for line in chunk:
                    self.log.insert("end", f"[{line.get('t','')}] {line.get('msg','')}\n")
                self._log_n = len(logs)
                try:
                    if float(self.log.index("end-1c").split(".")[0]) > 900:
                        self.log.delete("1.0", "end-600l")
                except Exception:
                    pass
                self.log.see("end")
                self.log.configure(state="disabled")
            running = bool(job.get("running"))
            queued = bool(job.get("queued"))
            snap = _schedule_snapshot()
            asnap = _align_schedule_snapshot()
            vsnap = _video_schedule_snapshot()
            csnap = _catalog_schedule_snapshot()
            nxt = snap.get("next_run") or ""
            if running:
                self.status.set("运行中…  " + (logs[-1]["msg"] if logs else ""))
                self._set_badge("运行中", C["accent"])
            elif queued:
                self.status.set("已在队列中等待…")
                self._set_badge("排队中", C["accent"])
            elif job.get("error"):
                self.status.set("失败：" + str(job.get("error")))
                self._set_badge("失败", C["bad"])
            else:
                self._set_badge("待命", C["sa"])
                if snap.get("enabled"):
                    extra = f"  下次 {nxt}" if nxt else ""
                    last = snap.get("last_sync") or ""
                    if last:
                        extra += f"  上次 {last.replace('T', ' ')}"
                    self.status.set(f"定时已开 · 每 {snap.get('minutes')} 分钟" + extra)
                else:
                    self.status.set("待命")
            if hasattr(self, "sched_info"):
                if snap.get("enabled"):
                    self.sched_info.configure(text=f"已启动 · 每 {snap.get('minutes')} 分钟 · 下次 {nxt or '-'}")
                else:
                    self.sched_info.configure(text="定时未启动")
            if hasattr(self, "align_sched_info"):
                if asnap.get("enabled"):
                    self.align_sched_info.configure(text=f"已启动 · 每 {asnap.get('minutes')} 分钟 · 下次 {asnap.get('next_run') or '-'}")
                else:
                    self.align_sched_info.configure(text="定时未启动")
            if hasattr(self, "vd_sched_info"):
                if vsnap.get("enabled"):
                    self.vd_sched_info.configure(
                        text=f"已启动 · 每 {vsnap.get('minutes')} 分钟 · 下次 {vsnap.get('next_run') or '-'}"
                    )
                else:
                    self.vd_sched_info.configure(text="定时未启动")
            if hasattr(self, "catalog_sched_info"):
                if csnap.get("enabled"):
                    self.catalog_sched_info.configure(
                        text=f"已启动 · 每 {csnap.get('minutes')} 分钟 · 下次 {csnap.get('next_run') or '-'}"
                    )
                else:
                    self.catalog_sched_info.configure(text="定时未启动")
        except Exception as exc:
            try:
                write_app_log(f"界面刷新失败: {exc}")
            except Exception:
                pass
        if self._alive:
            try:
                self._tick_id = self.after(800, self._tick)
            except Exception:
                pass

    def _on_close(self) -> None:
        self._alive = False
        if self._tick_id is not None:
            try:
                self.after_cancel(self._tick_id)
            except Exception:
                pass
        try:
            stop_scheduler()
            stop_align_scheduler()
            stop_video_scheduler()
            stop_catalog_scheduler()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    os.chdir(SCRIPT_DIR)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    try:
        write_app_log("程序启动")
    except Exception:
        pass
    mutex = _mutex_socket()
    if mutex is None:
        if _bring_existing_window():
            return
        if _kill_stale_mutex():
            time.sleep(0.5)
            mutex = _mutex_socket()
        if mutex is None:
            if _bring_existing_window():
                return
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("数据汇总工具", "已经在运行，请看任务栏里的「数据汇总工具」。")
            root.destroy()
            return
    app = DesktopApp()
    app._mutex = mutex
    app.lift()
    app.attributes("-topmost", True)
    app.after(800, lambda: app.attributes("-topmost", False))
    try:
        app.mainloop()
    except Exception:
        err = traceback.format_exc()
        try:
            write_app_log("主循环异常", err)
            (SCRIPT_DIR / "desktop_error.log").write_text(err, encoding="utf-8")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        err = traceback.format_exc()
        try:
            (SCRIPT_DIR / "desktop_error.log").write_text(err, encoding="utf-8")
        except Exception:
            pass
        raise
