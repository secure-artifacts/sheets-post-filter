# -*- coding: utf-8 -*-
"""桌面工具：网页两套 Tab 的完整配置，不弹浏览器。"""

from __future__ import annotations

import os
import socket
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
    _job,
    _schedule_snapshot,
    _video_schedule_snapshot,
    start_align_job,
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
        self.var_credentials = tk.StringVar()
        self._tab = "filter"
        self.title("数据汇总工具")
        self.geometry("980x780")
        self.minsize(860, 640)
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
            text="筛选汇总，或按表头对齐原样拷贝。两个功能分开，互不影响。",
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

        actions = tk.Frame(self, bg=C["paper"])
        actions.pack(fill="x", padx=16, pady=(8, 4))
        self.btn_run = StyleBtn(actions, "primary", text="开始汇总", command=self._run_now)
        self.btn_run.pack(side="left", padx=(0, 8))
        self.btn_pub = StyleBtn(actions, "head", text="发布图库", command=self._publish)
        self.btn_pub.pack(side="left", padx=(0, 8))
        self.btn_align = StyleBtn(actions, "ghost", text="开始对齐同步", command=self._run_align)
        self.btn_align.pack(side="left", padx=(0, 8))
        self.btn_video = StyleBtn(actions, "head", text="提取视频时长", command=self._run_video)
        self.btn_video.pack(side="left", padx=(0, 8))
        StyleBtn(actions, "ghost", text="保存配置", command=self._save).pack(side="left")

        nav = tk.Frame(self, bg=C["paper"])
        nav.pack(fill="x", padx=16, pady=(8, 6))
        self.tab_filter_btn = StyleBtn(nav, "head", text="贴文筛选汇总", command=lambda: self._show_tab("filter"))
        self.tab_align_btn = StyleBtn(nav, "ghost", text="表头对齐同步", command=lambda: self._show_tab("align"))
        self.tab_video_btn = StyleBtn(nav, "ghost", text="视频时长", command=lambda: self._show_tab("video"))
        self.tab_filter_btn.pack(side="left", padx=(0, 8))
        self.tab_align_btn.pack(side="left", padx=(0, 8))
        self.tab_video_btn.pack(side="left")

        self.pages = tk.Frame(self, bg=C["paper"])
        self.pages.pack(fill="both", expand=True)
        self.filter_page, self.filter_inner = self._scroll_tab(self.pages)
        self.align_page, self.align_inner = self._scroll_tab(self.pages)
        self.video_page, self.video_inner = self._scroll_tab(self.pages)
        self.filter_page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.align_page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.video_page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._build_filter(self.filter_inner)
        self._build_align(self.align_inner)
        self._build_video(self.video_inner)
        self._show_tab("filter")

    def _build_tabs(self) -> None:
        pass

    def _show_tab(self, name: str) -> None:
        self._tab = name
        mapping = {
            "filter": (self.filter_page, self.tab_filter_btn),
            "align": (self.align_page, self.tab_align_btn),
            "video": (self.video_page, self.tab_video_btn),
        }
        for key, (page, btn) in mapping.items():
            if key == name:
                page.lift()
                btn.configure(bg=C["head"], fg=C["cream"], bd=0, relief="flat")
            else:
                btn.configure(bg=C["card"], fg=C["muted"], bd=1, relief="solid")

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

        c2 = self._card(p, "2. 规范表头", "手动配置，按列名对齐")
        self._note(c2, "一行一个表头，顺序就是写入目标表的列顺序。旧表缺某列就跳过留空。")
        self.align_headers = tk.Text(c2, height=10, font=MONO, relief="solid", bd=1)
        self.align_headers.pack(fill="x", pady=4)
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
        self._cell(g, 0, "工作表名称", self.var_vd_source_sheet)
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

        c_date = self._card(p, "3. 日期筛选", "和贴文汇总一样，按 A 列日期")
        self._note(
            c_date,
            "填写开始、结束日期后：只查询、只汇总这个范围内的视频。留空则不限日期。格式例如 2026-08-01。",
        )
        g = self._row3(c_date)
        self.var_vd_start = tk.StringVar()
        self.var_vd_end = tk.StringVar()
        self._cell(g, 0, "开始日期", self.var_vd_start)
        self._cell(g, 1, "结束日期", self.var_vd_end)

        c_type = self._card(p, "4. 类型筛选", "按类型列，可加多个分类")
        self._note(
            c_type,
            "只跑类型列等于下面任一分类的行。可添加多个，例如「横版」「竖版」。留空则不限类型。分类名和列字母都可以改。",
        )
        self.vd_type_box = tk.Frame(c_type, bg=C["card"])
        self.vd_type_box.pack(fill="x")
        hint = tk.Frame(c_type, bg=C["card"])
        hint.pack(fill="x", pady=6)
        self.vd_type_count = tk.Label(hint, text="0 个分类", bg=C["card"], fg=C["muted"], font=FS)
        self.vd_type_count.pack(side="left")
        StyleBtn(hint, "ghost", text="+ 添加分类", command=self._add_vd_type).pack(side="right")
        StyleBtn(hint, "ghost", text="清空", command=self._clear_vd_types).pack(side="right", padx=6)
        self._add_vd_type()
        self._add_vd_type()

        c3 = self._card(p, "5. 写入目标表", "另外一张表：日志表 + 数据表")
        self._note(
            c3,
            "按链接去重：日志里已有时长的跳过，新的每 100 条追加写入。"
            "数据表按日期重算：A 列为日期，每人占两列（总秒数、总秒数÷30），第 4 行显示本月汇总。",
        )
        self.var_vd_dest_url = tk.StringVar()
        self._entry(c3, "写入表格链接", self.var_vd_dest_url)
        g = self._row3(c3)
        self.var_vd_log_sheet = tk.StringVar(value="日志表")
        self.var_vd_report_sheet = tk.StringVar(value="数据表")
        self.var_vd_out_start_row = tk.StringVar(value="1")
        self._cell(g, 0, "日志表名称", self.var_vd_log_sheet)
        self._cell(g, 1, "数据表名称", self.var_vd_report_sheet)
        self._cell(g, 2, "写入起始行", self.var_vd_out_start_row)
        g2 = self._row3(c3)
        self.var_vd_unit = tk.StringVar(value="30")
        self.var_vd_count_mode = tk.StringVar(value="汇总总秒数 ÷ 30")
        self.var_vd_include_headers = tk.BooleanVar(value=True)
        self._cell(g2, 0, "汇总除数（秒）", self.var_vd_unit)
        mode_box = tk.Frame(g2, bg=C["card"])
        mode_box.grid(row=0, column=1, sticky="ew", padx=8)
        tk.Label(mode_box, text="计数方式", bg=C["card"], fg=C["muted"], font=FS).pack(anchor="w")
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
        self.log.insert("end", "软件已启动。三个板块：贴文筛选汇总 / 表头对齐同步 / 视频时长。改完点保存。\n")
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

    def _payload(self) -> dict:
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
            "sources": self._read_src("_src_rows"),
            "fields": self._read_fields(),
            "align_sources": self._read_src("_align_rows", align=True),
            "align_headers": self.align_headers.get("1.0", "end"),
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
        self.var_vd_source_url.set(getattr(cfg, "vd_source_url", "") or "")
        self.var_vd_source_sheet.set(getattr(cfg, "vd_source_sheet", "") or "")
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
        self._set_src(self.src_box, "_src_rows", "例如：管理组", self.src_count, cfg.sources or cfg.source_urls)
        self._set_src(self.align_box, "_align_rows", "例如：8月份", self.align_count, cfg.align_sources, align=True)
        for r in list(self._field_rows):
            r.destroy()
        self._field_rows = []
        for f in cfg.fields or copy_default_fields():
            self._add_field(f)
        headers = cfg.align_headers or []
        self.align_headers.delete("1.0", "end")
        if headers:
            self.align_headers.insert("1.0", "\n".join(headers))
        self._upd_align_headers()

    def _save(self, quiet: bool = False) -> None:
        cfg = _cfg_from_payload(self._payload())
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

    def _run_now(self) -> None:
        cfg = _cfg_from_payload(self._payload())
        err = start_filter_job(cfg, from_schedule=False)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._set_badge("运行中", C["accent"])

    def _run_align(self) -> None:
        cfg = _cfg_from_payload(self._payload())
        err = start_align_job(cfg)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._set_badge("运行中", C["accent"])

    def _publish(self) -> None:
        cfg = _cfg_from_payload(self._payload())
        err = start_publish_job(cfg)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._set_badge("发布中", C["accent"])

    def _run_video(self) -> None:
        cfg = _cfg_from_payload(self._payload())
        err = start_video_job(cfg)
        if err:
            messagebox.showwarning("数据汇总工具", err)
            return
        self._log_n = 0
        self._set_badge("提取时长", C["accent"])

    def _peek_headers(self) -> None:
        cfg = _cfg_from_payload(self._payload())
        srcs = cfg.align_sources or []
        if not srcs or not srcs[0].get("url"):
            messagebox.showwarning("数据汇总工具", "请先填写至少一个数据源链接")
            return

        def work():
            try:
                headers = peek_source_headers(
                    cfg,
                    srcs[0]["url"],
                    sheet_name=srcs[0].get("sheet") or cfg.align_source_sheet or "",
                    header_row=int(cfg.align_header_row or 1),
                )
                self.after(0, lambda: self._fill_headers(headers))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("数据汇总工具", str(e)))

        threading.Thread(target=work, daemon=True).start()
        self._append_log("正在读取源表表头…")

    def _fill_headers(self, headers):
        self.align_headers.delete("1.0", "end")
        self.align_headers.insert("1.0", "\n".join(headers or []))
        self._upd_align_headers()
        self._append_log(f"读到 {len(headers or [])} 个表头")

    def _set_badge(self, text, bg):
        self.badge.configure(text=text, bg=bg)

    def _append_log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _tick(self) -> None:
        logs = _job.get("logs") or []
        if len(logs) > self._log_n:
            self.log.configure(state="normal")
            for line in logs[self._log_n :]:
                self.log.insert("end", f"[{line.get('t','')}] {line.get('msg','')}\n")
            self._log_n = len(logs)
            self.log.see("end")
            self.log.configure(state="disabled")
        running = bool(_job.get("running"))
        for b in (self.btn_run, self.btn_pub, self.btn_align, self.btn_video):
            b.configure(state="disabled" if running else "normal")
        snap = _schedule_snapshot()
        asnap = _align_schedule_snapshot()
        vsnap = _video_schedule_snapshot()
        nxt = snap.get("next_run") or ""
        if running:
            self.status.set("运行中…  " + (logs[-1]["msg"] if logs else ""))
            self._set_badge("运行中", C["accent"])
        elif _job.get("error"):
            self.status.set("失败：" + str(_job.get("error")))
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
