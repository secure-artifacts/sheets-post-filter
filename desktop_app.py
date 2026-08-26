# -*- coding: utf-8 -*-
"""桌面工具：左侧配置菜单 + 右侧独立汇总模板。"""

from __future__ import annotations

import os
import socket
import threading
import tkinter as tk
import copy
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from fetch_posts import (
    RESOURCE_DIR,
    SCRIPT_DIR,
    authorize,
    copy_default_fields,
    load_config,
    peek_source_headers,
    save_config,
    service_account_email,
)

from app import (
    _align_schedule_snapshot,
    _cfg_from_payload,
    job_snapshot,
    _schedule_snapshot,
    _video_schedule_snapshot,
    start_align_job,
    start_catalog_job,
    start_align_scheduler,
    start_filter_job,
    start_publish_job,
    start_scheduler,
    start_video_job,
    start_video_scheduler,
    stop_align_scheduler,
    stop_scheduler,
    stop_video_scheduler,
)
from version import APP_VERSION, RELEASES_URL, UPDATE_API_URL, version_tuple

MUTEX_PORT = 18765
C = {
    "ink": "#1b241c",
    "muted": "#5d6a5e",
    "paper": "#f3efe4",
    "card": "#fffdf8",
    "line": "#d7d0c2",
    "head": "#1e3a32",
    "accent": "#c45c26",
    "ok": "#2f7d4a",
    "bad": "#a33b2b",
    "log": "#141a16",
    "cream": "#f4efe4",
    "sa": "#163028",
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
        self._vd_extra_col_rows: list[tk.Frame] = []
        self._menus: list[dict] = []
        self._menu_buttons: dict[str, tk.Button] = {}
        self._active_menu_id = ""
        self._align_profiles: dict[str, list[dict]] = {}
        self._align_profile_key = "__default__"
        self._align_default_mappings: list[dict] = []
        self.var_credentials = tk.StringVar()
        self._tab = "filter"
        self.title("数据汇总工具")
        self.geometry("1180x820")
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
        self.after(300, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        tk.Checkbutton(
            parent,
            text=text,
            variable=var,
            bg=C["card"],
            fg=C["ink"],
            font=F,
            activebackground=C["card"],
            selectcolor="#fff",
            anchor="w",
        ).pack(anchor="w", pady=2)

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

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
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
            text="左侧管理独立配置，右侧使用筛选、目录、字段映射和视频分类模板。",
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
        tk.Label(sa, text="服务账号（源表和目标表都要共享给它 · 编辑者）", bg=C["sa"], fg="#9bb5ab", font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=10, pady=(8, 2))
        try:
            email = service_account_email(self.cfg.resolve_credentials()) or "未找到"
        except Exception:
            email = "未找到服务账号"
        self.var_sa = tk.StringVar(value=email)
        tk.Label(sa, textvariable=self.var_sa, bg=C["sa"], fg="#e8d39a", font=FS).pack(anchor="w", padx=10)
        sa_actions = tk.Frame(sa, bg=C["sa"])
        sa_actions.pack(anchor="e", padx=10, pady=8)
        StyleBtn(sa_actions, "ghost", text="选择服务账号", command=self._choose_credentials, bg=C["sa"], fg=C["cream"]).pack(side="left", padx=(0, 6))
        StyleBtn(sa_actions, "ghost", text="复制邮箱", command=self._copy_sa, bg=C["sa"], fg=C["cream"]).pack(side="left")

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
        self._build_filter(self.filter_inner)
        self._build_catalog(self.catalog_inner)
        self._build_align(self.align_inner)
        self._build_video(self.video_inner)

    def _build_tabs(self) -> None:
        pass

    def _show_tab(self, name: str) -> None:
        self._tab = name
        mapping = {
            "filter": self.filter_page,
            "catalog": self.catalog_page,
            "align": self.align_page,
            "video": self.video_page,
            "custom": self.video_page,
        }
        mapping.get(name, self.filter_page).lift()
        for btn in (self.btn_run, self.btn_pub, self.btn_align, self.btn_video, self.btn_catalog, self.btn_save):
            btn.pack_forget()
        if name == "filter":
            self.btn_run.pack(side="left", padx=(0, 8))
            self.btn_pub.pack(side="left", padx=(0, 8))
        elif name == "catalog":
            self.btn_catalog.pack(side="left", padx=(0, 8))
        elif name == "align":
            self.btn_align.pack(side="left", padx=(0, 8))
        elif name in ("video", "custom"):
            self.btn_video.configure(text="提取视频时长" if name == "video" else "开始自定义汇总")
            self.btn_video.pack(side="left", padx=(0, 8))
        self.btn_save.pack(side="left")

    def _init_menus(self) -> None:
        template_names = {
            "filter": "贴文筛选汇总",
            "catalog": "目录表驱动汇总",
            "align": "字段映射 / 表头对齐",
            "video": "视频提取时长",
            "custom": "自定义数据汇总",
        }
        base = self._payload()
        stored = getattr(self.cfg, "ui_menus", None) or []
        if stored:
            self._menus = [copy.deepcopy(item) for item in stored if isinstance(item, dict) and item.get("template") in template_names]
            migrated = False
            for item in self._menus:
                if item.get("template") == "video" and (
                    (item.get("settings") or {}).get("vd_write_log") is False or "自定义" in str(item.get("name") or "")
                ):
                    item["template"] = "custom"
                    item.setdefault("settings", {})["vd_write_log"] = False
                    migrated = True
            if migrated and not any(item.get("template") == "video" for item in self._menus):
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
        for item in self._menus:
            text = f"{item['name']}\n  { {'filter':'贴文模板','catalog':'目录模板','align':'映射模板','video':'时长模板','custom':'自定义模板'}[item['template']] }"
            btn = tk.Button(
                self.menu_box,
                text=text,
                command=lambda menu_id=item["id"]: self._select_menu(menu_id),
                anchor="w",
                justify="left",
                bg="#2c5144" if item["id"] == self._active_menu_id else C["sa"],
                fg=C["cream"],
                activebackground="#2c5144",
                activeforeground=C["cream"],
                relief="flat",
                bd=0,
                padx=10,
                pady=9,
                font=F,
                cursor="hand2",
            )
            btn.pack(fill="x", pady=2)
            btn.bind("<Double-Button-1>", lambda _event, menu_id=item["id"]: self._rename_menu(menu_id))
            self._menu_buttons[item["id"]] = btn

    def _select_menu(self, menu_id: str, initial: bool = False) -> None:
        if self._active_menu_id and not initial:
            current = next((item for item in self._menus if item["id"] == self._active_menu_id), None)
            if current:
                current["settings"] = copy.deepcopy(self._payload())
        item = next((entry for entry in self._menus if entry["id"] == menu_id), None)
        if not item:
            return
        self._active_menu_id = menu_id
        settings = copy.deepcopy(item.get("settings") or {})
        self._load_cfg(_cfg_from_payload(settings))
        self._show_tab(item["template"])
        self._log_n = 0
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._render_menu_buttons()
        self.status.set(f"当前配置：{item['name']}")

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
            {"field": "视频链接", "role": "link", "column": self.var_vd_col_link.get().strip().upper()},
            {"field": "名字", "role": "name", "column": self.var_vd_col_name.get().strip().upper()},
            {"field": "类型", "role": "type", "column": self.var_vd_col_type.get().strip().upper()},
        ]
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
        self._note(c2, "源工作表里有什么就汇总什么；目标表会按最大列数自动扩展。")

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

        c2 = self._card(p, "2. 字段映射", "目标字段 ← 源字段，可按链接覆盖")
        self._note(c2, "左右一行对应一个字段。默认目标字段和源字段相同；选择某个链接后可单独配置。")
        profile_row = tk.Frame(c2, bg=C["card"])
        profile_row.pack(fill="x", pady=4)
        tk.Label(profile_row, text="配置范围", bg=C["card"], fg=C["muted"], font=FS).pack(side="left")
        self.var_align_profile = tk.StringVar(value="所有链接的默认映射")
        self.align_profile_combo = ttk.Combobox(profile_row, textvariable=self.var_align_profile, state="readonly", width=42)
        self.align_profile_combo.pack(side="left", padx=8)
        self.align_profile_combo.bind("<<ComboboxSelected>>", lambda _e: self._switch_align_profile())
        StyleBtn(profile_row, "ghost", text="刷新链接列表", command=self._refresh_align_profiles).pack(side="left")
        maps = tk.Frame(c2, bg=C["card"])
        maps.pack(fill="x", pady=4)
        left = tk.Frame(maps, bg=C["card"])
        right = tk.Frame(maps, bg=C["card"])
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))
        tk.Label(left, text="目标字段（写入列名）", bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
        tk.Label(right, text="源字段（源表表头）", bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
        self.align_headers = tk.Text(left, height=10, font=MONO, relief="solid", bd=1)
        self.align_source_headers = tk.Text(right, height=10, font=MONO, relief="solid", bd=1)
        self.align_headers.pack(fill="both", expand=True, pady=4)
        self.align_source_headers.pack(fill="both", expand=True, pady=4)
        hr = tk.Frame(c2, bg=C["card"])
        hr.pack(fill="x")
        self.align_header_count = tk.Label(hr, text="0 列", bg=C["card"], fg=C["muted"], font=FS)
        self.align_header_count.pack(side="left")
        StyleBtn(hr, "ghost", text="从第一个源表读取表头", command=self._peek_headers).pack(side="right")
        self.align_headers.bind("<KeyRelease>", lambda e: self._upd_align_headers())

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
        self._note(
            c1,
            "读取源表：A 列日期、B 列视频链接、H 列制作人（列字母可改）。B 列可以是蓝字文件名，程序会读取单元格里的超链接（Drive / YouTube），再写入另一张表的「日志表」和「数据表」。",
        )
        self.var_vd_source_url = tk.StringVar()
        self.var_vd_source_sheet = tk.StringVar()
        self.var_vd_start_row = tk.StringVar(value="2")
        self._entry(c1, "源表格链接", self.var_vd_source_url)
        g = self._row3(c1)
        self._cell(g, 0, "工作表名称（多个用逗号分隔）", self.var_vd_source_sheet)
        self._cell(g, 1, "数据起始行", self.var_vd_start_row)

        c2 = self._card(p, "2. 源表列（默认可改）", "和贴文汇总一样，默认 A/B/H/E")
        self._note(c2, "列用字母。默认：A=日期，B=视频链接，H=制作人，E=类型。")
        g = self._row3(c2)
        self.var_vd_col_date = tk.StringVar(value="A")
        self.var_vd_col_link = tk.StringVar(value="B")
        self.var_vd_col_name = tk.StringVar(value="H")
        self._cell(g, 0, "日期列", self.var_vd_col_date)
        self._cell(g, 1, "视频链接列", self.var_vd_col_link)
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

        c_type = self._card(p, "4. 类型筛选", "按类型列，可加多个分类")
        self._note(
            c_type,
            "只跑类型列等于下面任一分类的行。可添加多个，例如「横版」「竖版」。留空则不限类型。分类名和列字母都可以改。",
        )
        self.vd_type_box = tk.Frame(c_type, bg=C["card"])
        self.vd_type_box.pack(fill="x")
        mode_row = tk.Frame(c_type, bg=C["card"])
        mode_row.pack(fill="x", pady=4)
        tk.Label(mode_row, text="类型操作", bg=C["card"], fg=C["muted"], font=FS).pack(side="left")
        self.var_vd_type_filter_mode = tk.StringVar(value="只包含这些类型")
        ttk.Combobox(mode_row, textvariable=self.var_vd_type_filter_mode, values=("只包含这些类型", "排除这些类型", "不筛选类型"), state="readonly", width=22).pack(side="left", padx=8)
        hint = tk.Frame(c_type, bg=C["card"])
        hint.pack(fill="x", pady=6)
        self.vd_type_count = tk.Label(hint, text="0 个分类", bg=C["card"], fg=C["muted"], font=FS)
        self.vd_type_count.pack(side="left")
        StyleBtn(hint, "ghost", text="+ 添加分类", command=self._add_vd_type).pack(side="right")
        StyleBtn(hint, "ghost", text="清空", command=self._clear_vd_types).pack(side="right", padx=6)
        self._add_vd_type()
        self._add_vd_type()

        c3 = self._card(p, "5. 写入目标表", "视频模板写日志和数据；自定义模板只写数据")
        self._note(
            c3,
            "视频提取时长保留日志表并支持断点续跑；自定义数据汇总不写日志，按日期和分类统计条数，并保留姓名顺序和用户样式。",
        )
        self.var_vd_dest_url = tk.StringVar()
        self._entry(c3, "写入表格链接", self.var_vd_dest_url)
        g = self._row3(c3)
        self.var_vd_log_sheet = tk.StringVar(value="日志表")
        self.var_vd_report_sheet = tk.StringVar(value="数据表")
        self.var_vd_out_start_row = tk.StringVar(value="1")
        self._cell(g, 0, "数据表名称", self.var_vd_report_sheet)
        self._cell(g, 1, "日志表名称（视频模板）", self.var_vd_log_sheet)
        self._cell(g, 2, "写入起始行", self.var_vd_out_start_row)
        g2 = self._row3(c3)
        self.var_vd_unit = tk.StringVar(value="30")
        self.var_vd_count_mode = tk.StringVar(value="汇总总秒数 ÷ 30")
        self.var_vd_include_headers = tk.BooleanVar(value=True)
        self._cell(g2, 0, "汇总除数（仅视频模板）", self.var_vd_unit)
        mode_box = tk.Frame(g2, bg=C["card"])
        mode_box.grid(row=0, column=1, sticky="ew", padx=8)
        tk.Label(mode_box, text="计数方式（仅视频模板）", bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
        ttk.Combobox(
            mode_box,
            textvariable=self.var_vd_count_mode,
            values=("汇总总秒数 ÷ 30", "逐条视频按30秒计数"),
            state="readonly",
        ).pack(fill="x", pady=(3, 0), ipady=4)
        box = tk.Frame(g2, bg=C["card"])
        box.grid(row=0, column=2, sticky="ew", padx=8)
        tk.Label(box, text=" ", bg=C["card"]).pack()
        self._check(box, "写入表头", self.var_vd_include_headers)

        c_sched = self._card(p, "6. 定时提取", "需保持本程序开着")
        self._note(c_sched, "到点后自动读取新链接、追加日志，并重新生成数据表。首次启用约 12 秒后执行一轮。")
        g3 = self._row3(c_sched)
        self.var_vd_schedule_minutes = tk.StringVar(value="180")
        self._cell(g3, 0, "间隔（分钟）", self.var_vd_schedule_minutes)
        self.var_vd_schedule_enabled = tk.BooleanVar(value=False)
        self._check(c_sched, "启用视频时长定时", self.var_vd_schedule_enabled)
        self.vd_sched_info = tk.Label(c_sched, text="定时未启动", bg=C["card"], fg=C["muted"], font=FS)
        self.vd_sched_info.pack(anchor="w", pady=4)

        c4 = self._card(p, "说明", collapsed=True)
        self._note(
            c4,
            "日志表：A 日期、B 链接、C 名字、D 时长(秒)。已跑过的链接下次自动跳过，中断后可继续。"
            "数据表：A5 起按日期升序，每人占两列；每个数值列按高绿低浅显示渐变色。"
            "源表、写入表、Drive 视频都要共享给服务账号。工作表名留空则用源表第一张。",
        )

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

    def _choose_credentials(self) -> None:
        current = Path(self.var_credentials.get().strip()) if self.var_credentials.get().strip() else None
        initial = current.parent if current and current.parent.exists() else Path.home()
        selected = filedialog.askopenfilename(
            title="选择 Google 服务账号 JSON",
            initialdir=str(initial),
            filetypes=(("JSON 文件", "*.json"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        email = service_account_email(Path(selected))
        if not email:
            messagebox.showerror("数据汇总工具", "所选文件不是有效的 Google 服务账号 JSON（未找到 client_email）。")
            return
        self.var_credentials.set(selected)
        self.var_sa.set(email)
        self._append_log(f"已选择服务账号：{email}；运行任务或点击保存配置后会保存此路径")

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
        if version_tuple(latest) > version_tuple(APP_VERSION):
            if messagebox.askyesno(
                "发现新版本",
                f"当前版本：{APP_VERSION}\n最新版本：{latest}\n\n是否打开下载页面？",
            ):
                import webbrowser

                webbrowser.open(url)
        else:
            messagebox.showinfo("检查更新", f"当前版本 {APP_VERSION} 已是最新版本。")

    def _show_update_error(self, error: str) -> None:
        self.btn_update.configure(state="normal", text="检查更新")
        messagebox.showerror(
            "检查更新失败",
            "无法读取 GitHub Release。请检查网络，或确认项目已经发布 Release。\n\n" + error,
        )

    def _set_interval(self, minutes: int) -> None:
        self.var_minutes.set(str(minutes))
        self.var_sched.set(True)
        self._save(quiet=True)
        self._append_log(f"定时间隔已改为 {minutes} 分钟，并已启用")

    def _upd_align_headers(self):
        n = sum(1 for ln in self.align_headers.get("1.0", "end").splitlines() if ln.strip())
        self.align_header_count.configure(text=f"{n} 列")

    def _read_align_mappings(self) -> list[dict]:
        targets = [line.strip() for line in self.align_headers.get("1.0", "end").splitlines() if line.strip()]
        sources = [line.strip() for line in self.align_source_headers.get("1.0", "end").splitlines()]
        return [
            {"target": target, "source": (sources[index] if index < len(sources) and sources[index] else target)}
            for index, target in enumerate(targets)
        ]

    def _write_align_mappings(self, mappings) -> None:
        cleaned = [item for item in (mappings or []) if isinstance(item, dict) and str(item.get("target") or "").strip()]
        self.align_headers.delete("1.0", "end")
        self.align_source_headers.delete("1.0", "end")
        if cleaned:
            self.align_headers.insert("1.0", "\n".join(str(item.get("target") or "").strip() for item in cleaned))
            self.align_source_headers.insert("1.0", "\n".join(str(item.get("source") or item.get("target") or "").strip() for item in cleaned))
        self._upd_align_headers()

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
        if not hasattr(self, "align_source_headers"):
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
            "credentials_file": self.var_credentials.get().strip(),
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
            "vd_write_log": next((item.get("template") != "custom" for item in self._menus if item.get("id") == self._active_menu_id), True),
            "vd_columns": self._read_vd_columns(),
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
        self.var_credentials.set(cfg.credentials_file or "")
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
        self.var_vd_source_url.set(getattr(cfg, "vd_source_url", "") or "")
        source_sheets = getattr(cfg, "vd_source_sheets", None) or []
        self.var_vd_source_sheet.set(", ".join(source_sheets) if source_sheets else (getattr(cfg, "vd_source_sheet", "") or ""))
        self.var_vd_start_row.set(str(getattr(cfg, "vd_start_row", 2) or 2))
        self.var_vd_col_date.set(getattr(cfg, "vd_col_date", "A") or "A")
        self.var_vd_col_link.set(getattr(cfg, "vd_col_link", "B") or "B")
        self.var_vd_col_name.set(getattr(cfg, "vd_col_name", "H") or "H")
        self.var_vd_col_type.set(getattr(cfg, "vd_col_type", "E") or "E")
        self._set_vd_types(getattr(cfg, "vd_types", None) or [])
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
        self._set_badge("运行中", C["accent"])

    def _run_align(self) -> None:
        cfg = self._cfg_for_action()
        err = start_align_job(cfg)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._set_badge("运行中", C["accent"])

    def _run_catalog(self) -> None:
        cfg = self._cfg_for_action()
        err = start_catalog_job(cfg)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._set_badge("目录汇总中", C["accent"])

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
        self._set_badge("提取时长", C["accent"])

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
        self.badge.configure(text=text, bg=bg)

    def _append_log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _tick(self) -> None:
        job = job_snapshot(self._active_menu_id or "default")
        logs = job.get("logs") or []
        if len(logs) > self._log_n:
            self.log.configure(state="normal")
            for line in logs[self._log_n :]:
                self.log.insert("end", f"[{line.get('t','')}] {line.get('msg','')}\n")
            self._log_n = len(logs)
            self.log.see("end")
            self.log.configure(state="disabled")
        running = bool(job.get("running"))
        for b in (self.btn_run, self.btn_pub, self.btn_align, self.btn_video, self.btn_catalog):
            b.configure(state="disabled" if running else "normal")
        snap = _schedule_snapshot()
        asnap = _align_schedule_snapshot()
        vsnap = _video_schedule_snapshot()
        nxt = snap.get("next_run") or ""
        if running:
            self.status.set("运行中…  " + (logs[-1]["msg"] if logs else ""))
            self._set_badge("运行中", C["accent"])
        elif job.get("error"):
            self.status.set("失败：" + str(job.get("error")))
            self._set_badge("失败", C["bad"])
        else:
            self._set_badge("待命", "#3a463c")
            if snap.get("enabled"):
                extra = f"  下次 {nxt}" if nxt else ""
                last = snap.get("last_sync") or ""
                if last:
                    extra += f"  上次 {last.replace('T', ' ')}"
                self.status.set(f"定时已开 · 每 {snap.get('minutes')} 分钟" + extra)
            else:
                self.status.set("待命（贴文库定时未启用）")
        if snap.get("enabled"):
            self.sched_info.configure(text=f"已启动 · 每 {snap.get('minutes')} 分钟 · 下次 {nxt or '-'}")
        else:
            self.sched_info.configure(text="定时未启动")
        if asnap.get("enabled"):
            self.align_sched_info.configure(text=f"已启动 · 每 {asnap.get('minutes')} 分钟 · 下次 {asnap.get('next_run') or '-'}")
        else:
            self.align_sched_info.configure(text="定时未启动")
        if vsnap.get("enabled"):
            self.vd_sched_info.configure(
                text=f"已启动 · 每 {vsnap.get('minutes')} 分钟 · 下次 {vsnap.get('next_run') or '-'}"
            )
        else:
            self.vd_sched_info.configure(text="定时未启动")
        self.after(400, self._tick)

    def _on_close(self) -> None:
        stop_scheduler()
        stop_align_scheduler()
        stop_video_scheduler()
        self.destroy()


def main() -> None:
    os.chdir(SCRIPT_DIR)
    mutex = _mutex_socket()
    if mutex is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("数据汇总工具", "已经在运行，请看任务栏里的窗口。")
        root.destroy()
        return
    app = DesktopApp()
    app._mutex = mutex
    app.mainloop()


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
