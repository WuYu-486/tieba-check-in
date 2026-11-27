#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tieba_manager.py - 贴吧管理中心(图形界面, tkinter + paramiko, 无第三方 GUI 依赖)
用法: python scripts/tieba_manager.py   (Windows 可双击 scripts/tieba_manager.bat)
说明:
  默认通过 deploy.ini 管理服务器 /opt/tieba-sign 上运行的项目;
  覆盖全部现有脚本能力: 立即签到、水贴目标增删/开关/立即执行、
  自动发贴目标增删/开关/立即执行(含预览)、按吧清理、关注吧经验查询、
  周报预览/发送、当日日志邮件、测试告警、服务器日志查看与概览。
  所有写操作均复用 scripts/ 下现有 CLI, 行为与命令行完全一致;
  发送邮件在服务器端执行(读取服务器 notify.ini)。
不做: 关注/取关吧、发主题贴、好友/群组等与帖子无关的功能。
"""

import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import webbrowser

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

import add_water
import tieba_exp
from add_water import BASE, load_deploy, ssh_connect
from tieba_water import PER_POST_COUNT

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_PY = "/opt/python/bin/python3"
GUI_LOG = os.path.join(BASE, "tieba-manager-gui.log")

FAMILY_UI = "Microsoft YaHei UI"
FAMILY_MONO = "Consolas"
FONT_SIZE_DEFAULT = 13
FONT_SIZE_CHOICES = [("小", 11), ("标准", 13), ("大", 15), ("特大", 17)]
FONT = (FAMILY_UI, FONT_SIZE_DEFAULT)
FONT_BOLD = (FAMILY_UI, FONT_SIZE_DEFAULT, "bold")
TITLE_FONT = (FAMILY_UI, FONT_SIZE_DEFAULT + 4, "bold")
MONO = (FAMILY_MONO, FONT_SIZE_DEFAULT - 1)

LOG_FILES = [
    "tieba-run.log", "tieba-run.log.prev",
    "tieba-water.log", "tieba-water.log.prev",
    "tieba-autopost.log", "tieba-autopost.log.prev",
    "mail.log", "cron-run.log", "water-cron.log", "autopost-cron.log",
]

CACHE_FILE = os.path.join(BASE, "tieba-manager-cache.json")
SETTINGS_FILE = os.path.join(BASE, "tieba-manager-settings.json")
CACHE_TTL = {"overview": 120, "sign": 60, "exp": 600}


def _atomic_write(path, text):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        pass


def fmt_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return "%d 分钟前" % (seconds // 60)
    if seconds < 86400:
        return "%d 小时前" % (seconds // 3600)
    return "%d 天前" % (seconds // 86400)


class Cache:
    """内存 + 磁盘 JSON 两级缓存, 线程安全"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self.data = {k: v for k, v in raw.items()
                             if isinstance(v, dict)
                             and "ts" in v and "value" in v}
        except (OSError, ValueError):
            pass

    def get(self, key, ttl):
        with self.lock:
            entry = self.data.get(key)
        if not entry:
            return None
        try:
            age = time.time() - float(entry.get("ts", 0))
        except (TypeError, ValueError):
            return None
        return {"value": entry.get("value"), "age": age, "fresh": age <= ttl}

    def put(self, key, value):
        with self.lock:
            self.data[key] = {"ts": time.time(), "value": value}
            raw = json.dumps(self.data, ensure_ascii=False)
        _atomic_write(self.path, raw)

    def invalidate(self, *keys):
        with self.lock:
            changed = False
            for key in keys:
                if self.data.pop(key, None) is not None:
                    changed = True
            raw = json.dumps(self.data, ensure_ascii=False) if changed else None
        if raw is not None:
            _atomic_write(self.path, raw)

    def clear(self):
        with self.lock:
            self.data = {}
            raw = "{}"
        _atomic_write(self.path, raw)


def load_font_size():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            size = int(json.load(f).get("font_size", 0))
        if size in [s for _, s in FONT_SIZE_CHOICES]:
            return size
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return FONT_SIZE_DEFAULT


def save_font_size(size):
    _atomic_write(SETTINGS_FILE,
                  json.dumps({"font_size": size}, ensure_ascii=False))

SNAPSHOT_SCRIPT = r"""
import json
import os
import time
import tieba_water as tw

posts = tw.load_posts()
cache = tw.load_json(tw.THREAD_CACHE_FILE, {})
state = tw.load_json(tw.STATE_FILE, {})
today = time.strftime("%Y-%m-%d")
if state.get("date") != today:
    state = {"posts": {}}
progress = {tid: (state.get("posts") or {}).get(tid, {}).get("sent", 0)
            for tid in posts}

targets = tw.load_json(os.path.join(tw.BASE, "autopost_targets.json"), [])
if not isinstance(targets, list):
    targets = []
astate = tw.load_json(os.path.join(tw.BASE, "autopost_state.json"), {})
if astate.get("date") != today:
    astate = {"posts": {}}
ap_progress = {}
for t in targets:
    tid = str(t.get("tid"))
    ap_progress[tid] = (astate.get("posts") or {}).get(tid, {}).get("sent", 0)

print("RESULT:" + json.dumps({
    "date": today,
    "posts": posts,
    "map": {tid: (cache.get(tid) or {}).get("kw") for tid in posts},
    "disabled": sorted(tw.load_disabled()),
    "progress": progress,
    "targets": targets,
    "ap_progress": ap_progress,
}, ensure_ascii=True))
"""

EXP_SCRIPT = r"""
import json
import tieba_sign as ts

bduss, stoken = ts.load_cookies()
if not bduss:
    print("RESULT:" + json.dumps({"error": "未找到 BDUSS"}))
else:
    cookie = ts.cookie_header(bduss, stoken)
    status, text = ts.http(ts.MOINDEX_URL, cookie=cookie)
    try:
        data = json.loads(text).get("data", {})
    except ValueError:
        data = {}
    if not data.get("uid"):
        print("RESULT:" + json.dumps({"error": "凭证失效(未登录)"}))
    else:
        forums = []
        for item in data.get("like_forum", []):
            if item.get("is_like") == 0:
                continue
            forums.append({
                "kw": item.get("forum_name", ""),
                "level": int(item.get("user_level") or 0),
                "exp": int(item.get("user_exp") or 0),
                "signed": int(item.get("is_sign") or 0),
                "fid": str(item.get("forum_id") or ""),
            })
        print("RESULT:" + json.dumps({"forums": forums}, ensure_ascii=True))
"""


class AppError(Exception):
    pass


def split_targets(text):
    return [p for p in re.split(r"[\s,，;；]+", text.strip()) if p]


def clip(text, limit):
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def set_text(widget, text):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.configure(state="disabled")


def make_text(parent, height=10, mono=True):
    frame = ttk.Frame(parent)
    text = tk.Text(frame, height=height, wrap="none",
                   font=MONO if mono else FONT, spacing1=1, spacing3=1)
    ys = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    text.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    text.configure(state="disabled")
    return frame, text


def make_tree(parent, columns, widths, height=12, selectmode="extended"):
    frame = ttk.Frame(parent)
    tv = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                      height=height, selectmode=selectmode)
    for key, title, anchor in columns:
        tv.heading(key, text=title)
        tv.column(key, width=widths[key], anchor=anchor, stretch=(key in ("kw", "content")))
    ys = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=ys.set)
    tv.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    tv.tag_configure("off", foreground="#b03a2e")
    return frame, tv


def ask_water_add(parent, ui_font):
    dlg = tk.Toplevel(parent)
    dlg.title("新增水贴目标")
    dlg.transient(parent)
    dlg.resizable(False, False)
    body = ttk.Frame(dlg, padding=12)
    body.pack(fill="both", expand=True)

    ttk.Label(body, text="帖子链接或 tid(可多个, 用空格/逗号/换行分隔)").grid(
        row=0, column=0, sticky="w")
    txt = tk.Text(body, width=56, height=5, font=ui_font)
    txt.grid(row=1, column=0, sticky="we", pady=(4, 8))

    mode = tk.StringVar(value="water")
    ttk.Radiobutton(body, text="添加后立即补齐今日水贴(默认)",
                    variable=mode, value="water").grid(row=2, column=0, sticky="w")
    ttk.Radiobutton(body, text="仅添加, 次日 09:05 自动生效",
                    variable=mode, value="none").grid(row=3, column=0, sticky="w")
    dry = tk.BooleanVar(value=False)
    ttk.Checkbutton(body, text="仅预览(dry-run, 不修改任何文件)",
                    variable=dry).grid(row=4, column=0, sticky="w", pady=(4, 0))

    result = {}

    def ok():
        tokens = split_targets(txt.get("1.0", "end"))
        if not tokens:
            messagebox.showwarning("新增水贴", "请填写帖子链接或 tid", parent=dlg)
            return
        result.update(targets=tokens, no_water=(mode.get() == "none"),
                      dry=dry.get())
        dlg.destroy()

    btns = ttk.Frame(body)
    btns.grid(row=5, column=0, sticky="e", pady=(12, 0))
    ttk.Button(btns, text="确定", command=ok).pack(side="right")
    ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=(0, 8))

    dlg.grab_set()
    dlg.geometry("+%d+%d" % (parent.winfo_rootx() + 180, parent.winfo_rooty() + 140))
    parent.wait_window(dlg)
    return result or None


def ask_autopost_add(parent, ui_font):
    dlg = tk.Toplevel(parent)
    dlg.title("新增自动发贴目标")
    dlg.transient(parent)
    dlg.resizable(False, False)
    body = ttk.Frame(dlg, padding=12)
    body.pack(fill="both", expand=True)

    ttk.Label(body, text="帖子链接或 tid(可多个, 用空格/逗号/换行分隔)").grid(
        row=0, column=0, sticky="w")
    txt = tk.Text(body, width=56, height=4, font=ui_font)
    txt.grid(row=1, column=0, sticky="we", pady=(4, 8))

    ttk.Label(body, text="发贴内容(单行)").grid(row=2, column=0, sticky="w")
    content = ttk.Entry(body, width=56)
    content.grid(row=3, column=0, sticky="we", pady=(4, 8))

    row = ttk.Frame(body)
    row.grid(row=4, column=0, sticky="w")
    ttk.Label(row, text="每目标每日条数").pack(side="left")
    count = tk.IntVar(value=1)
    ttk.Spinbox(row, from_=1, to=50, width=6, textvariable=count).pack(
        side="left", padx=(8, 0))

    result = {}

    def ok():
        tokens = split_targets(txt.get("1.0", "end"))
        if not tokens:
            messagebox.showwarning("新增自动发贴", "请填写帖子链接或 tid", parent=dlg)
            return
        text = content.get().strip()
        if not text:
            messagebox.showwarning("新增自动发贴", "请填写发贴内容", parent=dlg)
            return
        try:
            n = int(count.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning("新增自动发贴", "条数需为 1~50 的整数", parent=dlg)
            return
        if not 1 <= n <= 50:
            messagebox.showwarning("新增自动发贴", "条数需为 1~50 的整数", parent=dlg)
            return
        result.update(targets=tokens, content=text, count=n)
        dlg.destroy()

    btns = ttk.Frame(body)
    btns.grid(row=5, column=0, sticky="e", pady=(12, 0))
    ttk.Button(btns, text="确定", command=ok).pack(side="right")
    ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=(0, 8))

    dlg.grab_set()
    dlg.geometry("+%d+%d" % (parent.winfo_rootx() + 180, parent.winfo_rooty() + 140))
    parent.wait_window(dlg)
    return result or None


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("贴吧管理中心")
        self.root.geometry("1180x860")
        self.root.minsize(1000, 720)

        self._q = queue.Queue()
        self._busy = False
        self._exp_rows = []
        self._lists_loaded = False
        self._text_widgets = []
        self._tree_registry = []
        self.font_size = load_font_size()
        self.cache = Cache(CACHE_FILE)
        try:
            self.deploy_info = load_deploy()
        except SystemExit:
            self.deploy_info = None

        self._setup_style()
        self._build()
        self._apply_font(self.font_size)
        self.root.report_callback_exception = self._report_exception
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)
        self.root.after(400, lambda: self._overview_flow(False))

    # ---------- 基础设施 ----------

    def _setup_style(self):
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        self.root.option_add("*Font", FONT)
        self.style = ttk.Style()
        style = self.style
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=FONT)
        style.configure("Treeview", font=FONT, rowheight=int(FONT_SIZE_DEFAULT * 2.3))
        style.configure("Treeview.Heading", font=FONT_BOLD)
        style.configure("Title.TLabel", font=TITLE_FONT)
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Warn.TLabel", foreground="#b03a2e")
        style.configure("Section.TLabelframe.Label", font=FONT_BOLD)

    def _apply_font(self, size):
        self.font_size = size
        ui = (FAMILY_UI, size)
        self.style.configure(".", font=ui)
        self.style.configure("Treeview", font=ui, rowheight=int(size * 2.3))
        self.style.configure("Treeview.Heading", font=(FAMILY_UI, size, "bold"))
        self.style.configure("Title.TLabel", font=(FAMILY_UI, size + 4, "bold"))
        self.style.configure("Section.TLabelframe.Label",
                             font=(FAMILY_UI, size, "bold"))
        self.root.option_add("*Font", ui)
        self.root.option_add("*TCombobox*Listbox.font", ui)
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont",
                     "TkMenuFont", "TkCaptionFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(family=FAMILY_UI, size=size)
            except tk.TclError:
                pass
        mono = (FAMILY_MONO, max(9, size - 1))
        for widget in self._text_widgets:
            widget.configure(font=mono)
        factor = size / float(FONT_SIZE_DEFAULT)
        for tv, widths in self._tree_registry:
            for key, base in widths.items():
                tv.column(key, width=int(base * factor))

    def _on_font_choice(self, _event=None):
        mapping = dict(FONT_SIZE_CHOICES)
        size = mapping.get(self.font_choice.get(), FONT_SIZE_DEFAULT)
        self._apply_font(size)
        save_font_size(size)

    def _build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(14, 10, 14, 2))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="贴吧管理中心", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="默认管理服务器上运行的项目", style="Hint.TLabel").pack(
            side="left", padx=(12, 0), pady=(5, 0))
        self.deploy_label = ttk.Label(header, style="Hint.TLabel")
        self.deploy_label.pack(side="right", pady=(5, 0))
        fontbar = ttk.Frame(header)
        fontbar.pack(side="right", padx=(0, 18), pady=(5, 0))
        ttk.Label(fontbar, text="字号").pack(side="left")
        self.font_choice = tk.StringVar(
            value=next((name for name, s in FONT_SIZE_CHOICES
                        if s == self.font_size), FONT_SIZE_CHOICES[1][0]))
        self.font_combo = ttk.Combobox(
            fontbar, textvariable=self.font_choice, state="readonly", width=5,
            values=[name for name, _ in FONT_SIZE_CHOICES])
        self.font_combo.pack(side="left", padx=(6, 0))
        self.font_combo.bind("<<ComboboxSelected>>", self._on_font_choice)
        self._update_deploy_label()

        self.nb = ttk.Notebook(self.root)
        self.nb.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        self._build_overview()
        self._build_sign()
        self._build_water()
        self._build_autopost()
        self._build_forum()
        self._build_mail()
        self._build_logs()

        out = ttk.LabelFrame(self.root, text="运行输出", padding=6)
        out.grid(row=2, column=0, sticky="nsew", padx=12, pady=(2, 4))
        out.columnconfigure(0, weight=1)
        out.rowconfigure(0, weight=1)
        self.out_frame, self.out_text = make_text(out, height=9, mono=True)
        self._text_widgets.append(self.out_text)
        self.out_frame.grid(row=0, column=0, sticky="nsew")
        ttk.Button(out, text="清空", width=6,
                   command=lambda: set_text(self.out_text, "")).grid(
            row=0, column=1, sticky="ne", padx=(6, 0))

        status = ttk.Frame(self.root, padding=(14, 2, 14, 6))
        status.grid(row=3, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(status, text="就绪")
        self.status_label.grid(row=0, column=0, sticky="w")
        ttk.Label(status, text="配置文件: deploy.ini", style="Hint.TLabel").grid(
            row=0, column=1, sticky="e")

    def _update_deploy_label(self):
        if self.deploy_info:
            self.deploy_label.configure(
                text="服务器: %s : %s" % (self.deploy_info["host"],
                                          self.deploy_info["remote_dir"]),
                style="Hint.TLabel")
        else:
            self.deploy_label.configure(
                text="未找到 deploy.ini, 无法连接服务器", style="Warn.TLabel")

    def _ensure_deploy(self):
        if self.deploy_info:
            return self.deploy_info
        try:
            self.deploy_info = load_deploy()
        except SystemExit:
            raise AppError("未找到 deploy.ini(SSH 连接配置), 请先在仓库根目录创建")
        if threading.current_thread() is threading.main_thread():
            self._update_deploy_label()
        return self.deploy_info

    def _dir(self):
        return self._ensure_deploy()["remote_dir"]

    def _ssh(self):
        self._ensure_deploy()
        try:
            return ssh_connect(self.deploy_info)
        except SystemExit:
            raise AppError("SSH 连接/认证失败, 请检查 deploy.ini、host key 与网络")
        except Exception as e:
            raise AppError("SSH 连接失败: %s" % e)

    def _ssh_exec(self, cmd, timeout=120, log=None):
        t, sock = self._ssh()
        try:
            chan = t.open_session(timeout=timeout)
            chan.exec_command(cmd)
            if log is not None:
                lines = []
                for raw in chan.makefile("rb"):
                    line = raw.decode("utf-8", "replace").rstrip()
                    lines.append(line)
                    log(line)
                out = "\n".join(lines)
            else:
                out = chan.makefile("rb").read().decode("utf-8", "replace")
            err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
            code = chan.recv_exit_status()
        finally:
            t.close()
            sock.close()
        return code, out, err

    def _remote_py(self, script, timeout=120):
        cmd = "cd %s && %s - <<'PYEOF'\n%s\nPYEOF" % (
            shlex.quote(self._dir()), REMOTE_PY, script)
        code, out, err = self._ssh_exec(cmd, timeout=timeout)
        if code != 0:
            raise AppError("远程脚本失败(退出码 %d): %s"
                           % (code, (err.strip() or out[-300:] or "无输出")))
        for line in out.splitlines():
            if line.startswith("RESULT:"):
                return json.loads(line[7:])
        raise AppError("远程输出异常: %s" % (out[-300:] or "空"))

    def _run_cli(self, args, log):
        args = [str(a) for a in args]
        cmd = [sys.executable, "-u", os.path.join(SCRIPT_DIR, args[0])] + args[1:]
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        log("$ python %s" % " ".join(args))
        try:
            proc = subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace",
                                    env=env, **kwargs)
        except OSError as e:
            raise AppError("无法启动脚本: %s" % e)
        for line in proc.stdout:
            log(line.rstrip())
        return proc.wait()

    def _bg(self, label, work, done=None):
        if self._busy:
            messagebox.showinfo("请稍候", "已有任务正在执行, 请等待其完成")
            return
        self._busy = True
        self.status_label.configure(text="执行中: %s" % label)
        self._append("\n>>> %s" % label)

        def logger(text):
            self._q.put(("line", text))

        def runner():
            try:
                payload = work(logger)
                self._q.put(("done", label, payload, done))
            except AppError as e:
                self._q.put(("error", label, str(e)))
            except SystemExit:
                self._q.put(("error", label, "脚本异常退出, 请检查运行输出"))
            except Exception as e:
                self._q.put(("error", label, "%s: %s" % (type(e).__name__, e)))

        threading.Thread(target=runner, daemon=True).start()

    def _poll(self):
        try:
            while True:
                msg = self._q.get_nowait()
                kind = msg[0]
                if kind == "line":
                    self._append(msg[1])
                elif kind == "done":
                    _, label, payload, cb = msg
                    self._busy = False
                    self.status_label.configure(text="就绪")
                    self._append("<<< %s 完成\n" % label)
                    if cb:
                        try:
                            cb(payload)
                        except Exception as e:
                            messagebox.showerror("界面更新失败", str(e))
                elif kind == "error":
                    _, label, err = msg
                    self._busy = False
                    self.status_label.configure(text="失败")
                    self._append("!!! %s 失败: %s\n" % (label, err))
                    messagebox.showerror(label, err)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _append(self, line):
        text = self.out_text
        text.configure(state="normal")
        text.insert("end", line + "\n")
        if int(text.index("end-1c").split(".")[0]) > 6000:
            text.delete("1.0", "1000.0")
        text.see("end")
        text.configure(state="disabled")
        try:
            with open(GUI_LOG, "a", encoding="utf-8") as f:
                f.write("[" + time.strftime("%Y-%m-%d %H:%M:%S") + "] "
                        + line.rstrip() + "\n")
        except OSError:
            pass

    def _on_close(self):
        if self._busy and not messagebox.askyesno(
                "退出", "仍有任务在执行, 确定退出吗?"):
            return
        self.root.destroy()

    def _report_exception(self, exc_type, exc_value, tb):
        import traceback
        text = "".join(traceback.format_exception(exc_type, exc_value, tb))
        self._append("!!! 界面异常:\n" + text)
        log_path = os.path.join(BASE, "tieba-manager-error.log")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass
        messagebox.showerror(
            "界面异常", "%s: %s\n详情已写入 %s"
            % (exc_type.__name__, exc_value, log_path))

    def _notice(self, title, code, ok_msg):
        if code == 0:
            messagebox.showinfo(title, ok_msg)
        else:
            messagebox.showwarning(
                title, "命令退出码 %d, 存在失败或警告, 详见运行输出" % code)

    def _selected(self, tv):
        return list(tv.selection())

    def _invalidate(self, *keys):
        self.cache.invalidate(*keys)
        labels = {"overview": getattr(self, "ov_cache_label", None),
                  "sign": getattr(self, "sign_cache_label", None),
                  "exp": getattr(self, "exp_cache_label", None)}
        for key in keys:
            label = labels.get(key)
            if label is not None:
                label.configure(text="数据已过期, 建议刷新")

    def _on_tab_changed(self, _event=None):
        try:
            name = self.nb.tab(self.nb.select(), "text").strip()
        except tk.TclError:
            return
        if name == "概览":
            self._overview_flow(force=False)
        elif name == "签到":
            self._sign_flow(force=False)
        elif name == "吧管理":
            self._exp_flow(force=False)
        elif name in ("水贴", "自动发贴"):
            self._lists_flow()

    def _lists_flow(self):
        """首次进入水贴/自动发贴页且列表尚未加载时, 实时拉取一次"""
        if self._lists_loaded or self._busy:
            return

        def work(log):
            return self._remote_py(SNAPSHOT_SCRIPT)

        def done(snap):
            self._render_water(snap)
            self._render_autopost(snap)

        self._bg("加载水贴/自动发贴列表", work, done)

    # ---------- 概览 ----------

    def _build_overview(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  概览  ")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)
        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="刷新概览", command=self.act_overview).pack(side="left")
        ttk.Button(bar, text="清除缓存", command=self.act_cache_clear).pack(
            side="left", padx=(8, 0))
        ttk.Label(bar, text="读取服务器状态、定时任务与今日进度",
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))
        self.ov_cache_label = ttk.Label(bar, text="尚未加载", style="Hint.TLabel")
        self.ov_cache_label.pack(side="right")
        frame, self.overview_text = make_text(tab, height=20)
        self._text_widgets.append(self.overview_text)
        frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        set_text(self.overview_text, "正在加载概览...")

    def _overview_cmd(self):
        d = shlex.quote(self._dir())
        return (
            "cd %s && ("
            "echo '#TIME'; date '+%%F %%T %%Z'; "
            "echo '#CHECK'; for f in cookies notify.ini cacert.pem; do "
            "if [ -f \"$f\" ]; then echo \"$f=OK\"; else echo \"$f=MISSING\"; fi; done; "
            "echo '#CRON'; crontab -l 2>/dev/null | grep -E 'tieba|push_mail' "
            "|| echo '(无相关定时任务)'; "
            "echo '#LAST'; for f in tieba-run.log tieba-water.log tieba-autopost.log; do "
            "echo \"--$f\"; if [ -f \"$f\" ]; then "
            "grep -a '完成' \"$f\" | tail -n 1; else echo '(无日志)'; fi; done"
            ")" % d)

    def act_overview(self):
        self._overview_flow(force=True)

    def _overview_flow(self, force):
        if not force:
            cached = self.cache.get("overview", CACHE_TTL["overview"])
            if cached and isinstance(cached["value"], dict) \
                    and "snap" in cached["value"]:
                self._render_overview(cached["value"], age=cached["age"])
                if cached["fresh"] or self._busy:
                    return
        if self._busy:
            if force:
                messagebox.showinfo("请稍候", "已有任务正在执行, 请等待其完成")
            return

        def work(log):
            snap = self._remote_py(SNAPSHOT_SCRIPT)
            _, out, _ = self._ssh_exec(self._overview_cmd(), timeout=60)
            payload = {"snap": snap, "info": out}
            self.cache.put("overview", payload)
            return payload

        self._bg("刷新概览", work,
                 lambda payload: self._render_overview(payload, age=None))

    def act_cache_clear(self):
        if not messagebox.askyesno(
                "清除缓存",
                "清除概览/签到/经验的本地缓存(不影响服务器数据), 继续?"):
            return
        self.cache.clear()
        for label in (self.ov_cache_label, self.sign_cache_label,
                      self.exp_cache_label):
            label.configure(text="缓存已清除")
        self._append("已清除本地缓存")

    def _render_overview(self, payload, age=0):
        snap = payload["snap"]
        d = self._ensure_deploy()
        posts = snap.get("posts") or []
        disabled = set(snap.get("disabled") or [])
        on_n = len(posts) - sum(1 for t in posts if t in disabled)
        sent = sum(int(v) for v in (snap.get("progress") or {}).values())
        targets = snap.get("targets") or []
        ap_on = sum(1 for t in targets if t.get("enabled", True))
        ap_sent = sum(int(v) for v in (snap.get("ap_progress") or {}).values())
        ap_total = sum(int(t.get("count", 1)) for t in targets
                       if t.get("enabled", True))

        lines = [
            "【服务器】%s   目录: %s" % (d["host"], d["remote_dir"]),
            "【日期】%s" % snap.get("date", ""),
            "",
            "【今日目标】",
            "  水贴:      %d 个帖子(开启 %d, 关闭 %d), 今日已发 %d/%d 条"
            % (len(posts), on_n, len(posts) - on_n, sent, on_n * PER_POST_COUNT),
            "  自动发贴:  %d 个目标(开启 %d), 今日已发 %d/%d 条"
            % (len(targets), ap_on, ap_sent, ap_total),
            "",
            "【服务器信息】",
        ]
        for line in (payload["info"] or "").splitlines():
            if line == "#TIME":
                lines.append("  服务器时间:")
            elif line == "#CHECK":
                lines.append("  关键文件:")
            elif line == "#CRON":
                lines.append("  定时任务(crontab):")
            elif line == "#LAST":
                lines.append("  最近运行汇总:")
            else:
                lines.append("    " + line)
        set_text(self.overview_text, "\n".join(lines))
        if age is None:
            self.ov_cache_label.configure(text="实时数据 · 刚刚更新")
        else:
            self.ov_cache_label.configure(text="缓存数据 · %s" % fmt_age(age))
        self._render_water(snap)
        self._render_autopost(snap)

    # ---------- 签到 ----------

    def _build_sign(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  签到  ")
        tab.rowconfigure(2, weight=1)
        tab.columnconfigure(0, weight=1)
        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="立即签到", command=self.act_sign).pack(side="left")
        ttk.Button(bar, text="刷新签到日志", command=self.act_sign_log).pack(
            side="left", padx=(8, 0))
        ttk.Label(bar, text="在服务器执行, 结果写入 tieba-run.log 并计入周报",
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))
        self.sign_cache_label = ttk.Label(bar, text="尚未加载", style="Hint.TLabel")
        self.sign_cache_label.pack(side="right")
        ttk.Label(tab, text="tieba-run.log 最近 80 行:", style="Hint.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 2))
        frame, self.sign_text = make_text(tab, height=18)
        self._text_widgets.append(self.sign_text)
        frame.grid(row=2, column=0, sticky="nsew")

    def act_sign(self):
        def work(log):
            log("在服务器执行 tieba_sign.py, 预计 1~2 分钟...")
            code = self._run_cli(["tieba_sign_now.py"], log)
            _, tail, _ = self._ssh_exec(
                "cd %s && tail -n 80 tieba-run.log 2>/dev/null"
                % shlex.quote(self._dir()), timeout=60)
            self.cache.put("sign", tail)
            return {"code": code, "tail": tail}

        def done(p):
            self._invalidate("overview", "exp")
            set_text(self.sign_text, p["tail"])
            self.sign_cache_label.configure(text="实时数据 · 刚刚更新")
            if p["code"] == 0:
                messagebox.showinfo("签到完成", "全部关注吧签到成功")
            elif p["code"] == 2:
                messagebox.showwarning("签到完成", "凭证失效, 请更新服务器 cookies 文件")
            else:
                messagebox.showwarning(
                    "签到完成", "存在失败项(退出码 %d), 详见日志" % p["code"])

        self._bg("立即签到", work, done)

    def act_sign_log(self):
        self._sign_flow(force=True)

    def _sign_flow(self, force):
        if not force:
            cached = self.cache.get("sign", CACHE_TTL["sign"])
            if cached and isinstance(cached["value"], str):
                set_text(self.sign_text, cached["value"])
                self.sign_cache_label.configure(
                    text="缓存数据 · %s" % fmt_age(cached["age"]))
                if cached["fresh"] or self._busy:
                    return
        if self._busy:
            if force:
                messagebox.showinfo("请稍候", "已有任务正在执行, 请等待其完成")
            return

        def work(log):
            _, out, _ = self._ssh_exec(
                "cd %s && tail -n 80 tieba-run.log 2>/dev/null"
                % shlex.quote(self._dir()), timeout=60)
            self.cache.put("sign", out)
            return out

        def done(text):
            set_text(self.sign_text, text)
            self.sign_cache_label.configure(text="实时数据 · 刚刚更新")

        self._bg("刷新签到日志", work, done)

    # ---------- 水贴 ----------

    def _build_water(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  水贴  ")
        tab.rowconfigure(2, weight=1)
        tab.columnconfigure(0, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="刷新", command=self.act_water_refresh).pack(side="left")
        ttk.Button(bar, text="新增目标", command=self.act_water_add).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="开启", command=lambda: self.act_water_switch("on")).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="关闭", command=lambda: self.act_water_switch("off")).pack(
            side="left", padx=(4, 0))
        ttk.Button(bar, text="删除", command=self.act_water_delete).pack(
            side="left", padx=(4, 0))
        ttk.Button(bar, text="立即执行水贴", command=self.act_water_now).pack(
            side="left", padx=(8, 0))
        self.water_summary = ttk.Label(bar, text="尚未加载", style="Hint.TLabel")
        self.water_summary.pack(side="right")

        ttk.Label(tab, text="双击行打开帖子; 关闭的帖子不参与每日 09:05 自动水贴, 当日进度保留",
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 2))
        cols = [("idx", "#", "center"), ("kw", "吧名", "w"), ("tid", "帖子 tid", "center"),
                ("switch", "自动水贴", "center"), ("progress", "今日进度", "center"),
                ("url", "链接", "w")]
        widths = {"idx": 46, "kw": 170, "tid": 120, "switch": 90,
                  "progress": 90, "url": 290}
        frame, self.water_tv = make_tree(tab, cols, widths, height=14)
        self._tree_registry.append((self.water_tv, widths))
        frame.grid(row=2, column=0, sticky="nsew")
        self.water_tv.bind("<Double-1>",
                           lambda e: self._open_thread(self.water_tv))

    def _render_water(self, snap):
        self._lists_loaded = True
        tv = self.water_tv
        tv.delete(*tv.get_children())
        posts = snap.get("posts") or []
        disabled = set(snap.get("disabled") or [])
        mapping = snap.get("map") or {}
        progress = snap.get("progress") or {}
        for i, tid in enumerate(posts, 1):
            off = tid in disabled
            tv.insert("", "end", iid=tid, values=(
                i, mapping.get(tid) or "?", tid,
                "关闭" if off else "开启",
                "%d/%d" % (int(progress.get(tid, 0)), PER_POST_COUNT),
                "https://tieba.baidu.com/p/%s" % tid,
            ), tags=("off",) if off else ())
        on_n = len(posts) - sum(1 for t in posts if t in disabled)
        self.water_summary.configure(
            text="共 %d 个帖子: 开启 %d, 关闭 %d"
                 % (len(posts), on_n, len(posts) - on_n))

    def _open_thread(self, tv):
        sel = tv.selection()
        if sel:
            webbrowser.open("https://tieba.baidu.com/p/%s" % sel[0])

    def act_water_refresh(self):
        self._bg("刷新水贴目标",
                 lambda log: self._remote_py(SNAPSHOT_SCRIPT),
                 self._render_water)

    def act_water_add(self):
        data = ask_water_add(self.root, (FAMILY_UI, self.font_size))
        if not data:
            return
        args = ["add_water.py"] + data["targets"]
        if data["no_water"]:
            args.append("--no-water")
        if data["dry"]:
            args.append("--dry-run")

        def work(log):
            code = self._run_cli(args, log)
            snap = None if data["dry"] else self._remote_py(SNAPSHOT_SCRIPT)
            return {"code": code, "snap": snap}

        def done(p):
            if p["snap"]:
                self._render_water(p["snap"])
            if not data["dry"]:
                if data["no_water"]:
                    self._invalidate("overview")
                else:
                    self._invalidate("overview", "exp")
            if p["code"] != 0:
                messagebox.showwarning(
                    "新增水贴", "部分目标未处理成功(退出码 %d), 详见运行输出" % p["code"])
            elif data["dry"]:
                messagebox.showinfo("新增水贴", "dry-run 完成, 未修改任何文件")

        self._bg("新增水贴目标", work, done)

    def act_water_switch(self, action):
        tids = self._selected(self.water_tv)
        if not tids:
            messagebox.showinfo("水贴开关", "请先在列表中选择帖子")
            return

        def work(log):
            code = self._run_cli(["water_switch.py", action, *tids], log)
            return {"code": code, "snap": self._remote_py(SNAPSHOT_SCRIPT)}

        def done(p):
            self._render_water(p["snap"])
            self._invalidate("overview")
            self._notice("水贴开关", p["code"], "开关已更新, 切换状态不会立即发贴")

        self._bg("水贴%s" % ("开启" if action == "on" else "关闭"), work, done)

    def act_water_delete(self):
        tids = self._selected(self.water_tv)
        if not tids:
            messagebox.showinfo("删除水贴", "请先在列表中选择帖子")
            return
        preview = "、".join(tids[:6]) + (" 等" if len(tids) > 6 else "")
        if not messagebox.askyesno(
                "删除水贴目标",
                "将从服务器删除 %d 个帖子(%s)的水贴目标,\n并清除当日进度与开关状态。\n"
                "已发出的回复不会撤回, 确认删除?" % (len(tids), preview)):
            return

        def work(log):
            code = self._run_cli(["water_delete.py", *tids], log)
            return {"code": code, "snap": self._remote_py(SNAPSHOT_SCRIPT)}

        def done(p):
            self._render_water(p["snap"])
            self._invalidate("overview")
            self._notice("删除水贴", p["code"], "已删除所选水贴目标")

        self._bg("删除水贴目标", work, done)

    def act_water_now(self):
        if not messagebox.askyesno(
                "立即执行水贴",
                "将在服务器立即执行 tieba_water.py --now,\n"
                "每帖最多补 4 条, 耗时可能 5~10 分钟。\n继续?"):
            return

        def work(log):
            cmd = "cd %s && %s tieba_water.py --now" % (
                shlex.quote(self._dir()), REMOTE_PY)
            code, _, err = self._ssh_exec(cmd, timeout=3600, log=log)
            if code != 0:
                raise AppError("水贴任务退出码 %d: %s"
                               % (code, (err.strip() or "详见运行输出")))
            return self._remote_py(SNAPSHOT_SCRIPT)

        def done(snap):
            self._render_water(snap)
            self._invalidate("overview", "exp")

        self._bg("立即执行水贴", work, done)

    # ---------- 自动发贴 ----------

    def _build_autopost(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  自动发贴  ")
        tab.rowconfigure(2, weight=1)
        tab.columnconfigure(0, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="刷新", command=self.act_ap_refresh).pack(side="left")
        ttk.Button(bar, text="新增目标", command=self.act_ap_add).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="开启", command=lambda: self.act_ap_switch("on")).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="关闭", command=lambda: self.act_ap_switch("off")).pack(
            side="left", padx=(4, 0))
        ttk.Button(bar, text="删除", command=self.act_ap_delete).pack(
            side="left", padx=(4, 0))
        ttk.Button(bar, text="执行预览", command=lambda: self.act_ap_now(True)).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="立即执行", command=lambda: self.act_ap_now(False)).pack(
            side="left", padx=(4, 0))
        self.ap_summary = ttk.Label(bar, text="尚未加载", style="Hint.TLabel")
        self.ap_summary.pack(side="right")

        ttk.Label(tab, text="双击行打开帖子; 每日 13:05 由服务器自动执行, 当天重复运行不会超发",
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 2))
        cols = [("idx", "#", "center"), ("kw", "吧名", "w"), ("tid", "帖子 tid", "center"),
                ("content", "内容", "w"), ("count", "条数", "center"),
                ("switch", "自动发贴", "center"), ("progress", "今日进度", "center"),
                ("url", "链接", "w")]
        widths = {"idx": 42, "kw": 130, "tid": 112, "content": 210, "count": 58,
                  "switch": 88, "progress": 88, "url": 240}
        frame, self.ap_tv = make_tree(tab, cols, widths, height=14)
        self._tree_registry.append((self.ap_tv, widths))
        frame.grid(row=2, column=0, sticky="nsew")
        self.ap_tv.bind("<Double-1>", lambda e: self._open_thread(self.ap_tv))

    def _render_autopost(self, snap):
        self._lists_loaded = True
        tv = self.ap_tv
        tv.delete(*tv.get_children())
        targets = snap.get("targets") or []
        prog = snap.get("ap_progress") or {}
        for i, t in enumerate(targets, 1):
            tid = str(t.get("tid"))
            on = t.get("enabled", True)
            count = int(t.get("count", 1))
            tv.insert("", "end", iid=tid, values=(
                i, t.get("kw") or "?", tid, clip(t.get("content", ""), 40), count,
                "开启" if on else "关闭",
                "%d/%d" % (int(prog.get(tid, 0)), count),
                "https://tieba.baidu.com/p/%s" % tid,
            ), tags=() if on else ("off",))
        on_n = sum(1 for t in targets if t.get("enabled", True))
        self.ap_summary.configure(
            text="共 %d 个目标: 开启 %d, 关闭 %d"
                 % (len(targets), on_n, len(targets) - on_n))

    def act_ap_refresh(self):
        self._bg("刷新自动发贴目标",
                 lambda log: self._remote_py(SNAPSHOT_SCRIPT),
                 self._render_autopost)

    def act_ap_add(self):
        data = ask_autopost_add(self.root, (FAMILY_UI, self.font_size))
        if not data:
            return
        args = ["autopost_manage.py", "add", *data["targets"],
                "--content", data["content"], "--count", data["count"]]

        def work(log):
            code = self._run_cli(args, log)
            return {"code": code, "snap": self._remote_py(SNAPSHOT_SCRIPT)}

        def done(p):
            self._render_autopost(p["snap"])
            self._invalidate("overview")
            self._notice("新增自动发贴", p["code"],
                         "目标已添加(不会立即发贴, 次日 13:05 生效)")

        self._bg("新增自动发贴目标", work, done)

    def act_ap_switch(self, action):
        tids = self._selected(self.ap_tv)
        if not tids:
            messagebox.showinfo("自动发贴开关", "请先在列表中选择目标")
            return

        def work(log):
            code = self._run_cli(["autopost_manage.py", action, *tids], log)
            return {"code": code, "snap": self._remote_py(SNAPSHOT_SCRIPT)}

        def done(p):
            self._render_autopost(p["snap"])
            self._invalidate("overview")
            self._notice("自动发贴开关", p["code"], "开关已更新(不会立即发贴)")

        self._bg("自动发贴%s" % ("开启" if action == "on" else "关闭"), work, done)

    def act_ap_delete(self):
        tids = self._selected(self.ap_tv)
        if not tids:
            messagebox.showinfo("删除自动发贴", "请先在列表中选择目标")
            return
        preview = "、".join(tids[:6]) + (" 等" if len(tids) > 6 else "")
        if not messagebox.askyesno(
                "删除自动发贴目标",
                "将删除 %d 个目标(%s)及其当日进度, 确认?" % (len(tids), preview)):
            return

        def work(log):
            code = self._run_cli(["autopost_manage.py", "del", *tids], log)
            return {"code": code, "snap": self._remote_py(SNAPSHOT_SCRIPT)}

        def done(p):
            self._render_autopost(p["snap"])
            self._invalidate("overview")
            self._notice("删除自动发贴", p["code"], "已删除所选目标")

        self._bg("删除自动发贴目标", work, done)

    def act_ap_now(self, dry):
        if not dry and not messagebox.askyesno(
                "立即执行自动发贴",
                "将按服务器配置真实发送评论(每天不会超发),\n继续?"):
            return

        def work(log):
            cmd = "cd %s && %s tieba_autopost.py --now%s" % (
                shlex.quote(self._dir()), REMOTE_PY, " --dry-run" if dry else "")
            code, _, err = self._ssh_exec(cmd, timeout=1800, log=log)
            if code != 0:
                raise AppError("自动发贴任务退出码 %d: %s"
                               % (code, (err.strip() or "详见运行输出")))
            return self._remote_py(SNAPSHOT_SCRIPT)

        def done(snap):
            self._render_autopost(snap)
            if not dry:
                self._invalidate("overview", "exp")

        self._bg("自动发贴预览" if dry else "立即执行自动发贴", work, done)

    # ---------- 吧管理(清理/经验) ----------

    def _build_forum(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  吧管理  ")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)

        cleanup = ttk.LabelFrame(tab, text="清理某个吧的全部目标", padding=10,
                                 style="Section.TLabelframe")
        cleanup.grid(row=0, column=0, sticky="ew")
        cleanup.columnconfigure(1, weight=1)
        ttk.Label(cleanup, text="吧名").grid(row=0, column=0, sticky="w")
        self.cleanup_entry = ttk.Entry(cleanup)
        self.cleanup_entry.grid(row=0, column=1, sticky="we", padx=(8, 8))
        ttk.Button(cleanup, text="执行清理", command=self.act_cleanup).grid(
            row=0, column=2, sticky="e")
        ttk.Label(cleanup,
                  text="删除该吧的水贴目标与自动发贴目标(可多个吧名, 逗号分隔), "
                       "执行前会先检查影响范围; 不涉及关注/取关",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3,
                                            sticky="w", pady=(6, 0))

        exp = ttk.LabelFrame(tab, text="关注吧经验", padding=10,
                             style="Section.TLabelframe")
        exp.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        exp.rowconfigure(1, weight=1)
        exp.columnconfigure(0, weight=1)

        bar = ttk.Frame(exp)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="刷新", command=self.act_exp).pack(side="left")
        ttk.Label(bar, text="排序").pack(side="left", padx=(10, 4))
        self.exp_sort = tk.StringVar(value="exp")
        combo = ttk.Combobox(bar, textvariable=self.exp_sort, state="readonly",
                             width=8, values=["exp", "need", "level", "name"])
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda e: self._render_exp())
        ttk.Label(bar, text="exp=经验降序  need=最快升级  level=等级降序  name=吧名",
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))
        self.exp_cache_label = ttk.Label(bar, text="尚未加载", style="Hint.TLabel")
        self.exp_cache_label.pack(side="right", padx=(0, 14))
        self.exp_summary = ttk.Label(bar, text="尚未加载", style="Hint.TLabel")
        self.exp_summary.pack(side="right")

        cols = [("idx", "#", "center"), ("kw", "吧名", "w"), ("level", "等级", "center"),
                ("exp", "当前经验", "center"), ("next", "下一级", "center"),
                ("need", "还差", "center"), ("signed", "今日签到", "center")]
        widths = {"idx": 46, "kw": 200, "level": 70, "exp": 100,
                  "next": 70, "need": 90, "signed": 90}
        frame, self.exp_tv = make_tree(exp, cols, widths, height=10,
                                       selectmode="browse")
        self._tree_registry.append((self.exp_tv, widths))
        frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def act_cleanup(self):
        raw = self.cleanup_entry.get().strip()
        kws = split_targets(raw)
        if not kws:
            messagebox.showinfo("清理", "请先输入吧名")
            return

        def work(log):
            return {"kws": kws, "snap": self._remote_py(SNAPSHOT_SCRIPT)}

        def done(p):
            snap = p["snap"]
            wanted = set(p["kws"])
            w_hits = [tid for tid in (snap.get("posts") or [])
                      if (snap.get("map") or {}).get(tid) in wanted]
            a_hits = [str(t.get("tid")) for t in (snap.get("targets") or [])
                      if str(t.get("kw")) in wanted]
            if not w_hits and not a_hits:
                messagebox.showinfo("清理", "这些吧没有水贴/自动发贴目标")
                return
            if not messagebox.askyesno(
                    "确认清理",
                    "将删除: 水贴目标 %d 个, 自动发贴目标 %d 个。\n"
                    "已发出的回复不会撤回, 确认清理?"
                    % (len(w_hits), len(a_hits))):
                return

            def work2(log):
                code = self._run_cli(["cleanup_forum.py", *p["kws"]], log)
                if code != 0:
                    raise AppError("cleanup_forum 退出码 %d, 详见运行输出" % code)
                return self._remote_py(SNAPSHOT_SCRIPT)

            def done2(snap2):
                self._render_water(snap2)
                self._render_autopost(snap2)
                self._invalidate("overview")
                self.cleanup_entry.delete(0, "end")
                messagebox.showinfo("清理完成", "该吧的水贴/自动发贴目标已删除并同步")

            self._bg("清理吧目标", work2, done2)

        self._bg("检查清理影响", work, done)

    def act_exp(self):
        self._exp_flow(force=True)

    def _exp_flow(self, force):
        if not force:
            cached = self.cache.get("exp", CACHE_TTL["exp"])
            if cached and isinstance(cached["value"], list):
                self._exp_rows = cached["value"]
                self._render_exp()
                self.exp_cache_label.configure(
                    text="缓存数据 · %s" % fmt_age(cached["age"]))
                if cached["fresh"] or self._busy:
                    return
        if self._busy:
            if force:
                messagebox.showinfo("请稍候", "已有任务正在执行, 请等待其完成")
            return

        def work(log):
            try:
                thresholds = tieba_exp.load_thresholds()
            except SystemExit:
                raise AppError("无法解析 docs/贴吧各等级经验值.md")
            data = self._remote_py(EXP_SCRIPT, timeout=90)
            if data.get("error"):
                raise AppError("经验查询失败: %s" % data["error"])
            rows = tieba_exp.compute(data["forums"], thresholds)
            self.cache.put("exp", rows)
            return rows

        def done(rows):
            self._exp_rows = rows
            self._render_exp()
            self.exp_cache_label.configure(text="实时数据 · 刚刚更新")

        self._bg("查询关注吧经验", work, done)

    def _render_exp(self):
        if not self._exp_rows:
            return
        rows = list(self._exp_rows)
        sort = self.exp_sort.get()
        if sort == "level":
            rows.sort(key=lambda r: (-r["level"], r["kw"]))
        elif sort == "name":
            rows.sort(key=lambda r: r["kw"])
        elif sort == "need":
            rows.sort(key=lambda r: (r["need"] is None, r["need"] or 0, r["kw"]))
        else:
            rows.sort(key=lambda r: (-r["exp"], r["kw"]))
        tv = self.exp_tv
        tv.delete(*tv.get_children())
        for i, r in enumerate(rows, 1):
            tv.insert("", "end", values=(
                i, r["kw"], r["level"], r["exp"],
                r["next_level"] if r["next_level"] else "满级",
                r["need"] if r["need"] is not None else "—",
                "已签" if r["signed"] else "未签",
            ))
        full = sum(1 for r in rows if not r["next_level"])
        signed = sum(1 for r in rows if r["signed"])
        self.exp_summary.configure(
            text="共 %d 个吧: 满级 %d, 今日已签到 %d" % (len(rows), full, signed))

    # ---------- 邮件/周报 ----------

    def _build_mail(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  周报与邮件  ")
        tab.columnconfigure(0, weight=1)

        weekly = ttk.LabelFrame(tab, text="周报", padding=10,
                                style="Section.TLabelframe")
        weekly.grid(row=0, column=0, sticky="ew")
        ttk.Button(weekly, text="预览周报(不发送)",
                   command=lambda: self.act_mail_weekly(True)).pack(side="left")
        ttk.Button(weekly, text="发送周报...",
                   command=lambda: self.act_mail_weekly(False)).pack(
            side="left", padx=(8, 0))
        ttk.Label(weekly,
                  text="预览内容输出到下方运行输出; 发送成功后服务器三份日志会轮转(.prev)",
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))

        daily = ttk.LabelFrame(tab, text="当日日志邮件", padding=10,
                               style="Section.TLabelframe")
        daily.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(daily, text="日志").pack(side="left")
        self.mail_log = tk.StringVar(value="tieba-run.log")
        ttk.Combobox(daily, textvariable=self.mail_log, state="readonly", width=22,
                     values=["tieba-run.log", "tieba-water.log",
                             "tieba-autopost.log"]).pack(side="left", padx=(6, 8))
        ttk.Button(daily, text="发送当日日志邮件",
                   command=self.act_mail_daily).pack(side="left")
        ttk.Label(daily, text="取该日志今日内容作为正文发送",
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))

        alert = ttk.LabelFrame(tab, text="测试告警邮件", padding=10,
                               style="Section.TLabelframe")
        alert.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        alert.columnconfigure(0, weight=1)
        self.alert_text = tk.StringVar(value="这是一封来自贴吧管理中心的测试告警邮件")
        ttk.Entry(alert, textvariable=self.alert_text).grid(
            row=0, column=0, sticky="we")
        ttk.Button(alert, text="发送测试告警", command=self.act_mail_alert).grid(
            row=0, column=1, padx=(8, 0))

        ttk.Label(tab, text="邮件在服务器端通过 push_mail.py 发送(读取服务器 notify.ini)",
                  style="Hint.TLabel").grid(row=3, column=0, sticky="w", pady=(12, 0))

    def _mail_cmd(self, args):
        return "cd %s && %s push_mail.py %s" % (
            shlex.quote(self._dir()), REMOTE_PY, args)

    def _run_mail(self, label, argline):
        def work(log):
            code, _, err = self._ssh_exec(self._mail_cmd(argline),
                                         timeout=180, log=log)
            if code != 0:
                raise AppError("邮件任务退出码 %d: %s"
                               % (code, (err.strip() or "详见运行输出")))
            return None

        self._bg(label, work)

    def act_mail_weekly(self, dry):
        if not dry and not messagebox.askyesno(
                "发送周报",
                "将向 notify.ini 配置的收件人发送周报,\n"
                "发送成功后服务器三份日志会轮转为 .prev(下一期重新累积)。\n继续?"):
            return
        argline = ("--weekly tieba-run.log --water-log tieba-water.log "
                   "--autopost-log tieba-autopost.log --subject %s%s"
                   % (shlex.quote("贴吧签到周报"), " --dry-run" if dry else ""))
        self._run_mail("周报预览" if dry else "发送周报", argline)
        if dry:
            self._append("提示: 预览内容为服务器上当前三份日志生成的正文/附件")

    def act_mail_daily(self):
        log_file = self.mail_log.get()
        if not messagebox.askyesno(
                "发送日志邮件", "将发送 %s 今日内容到 notify.ini 配置的收件人, 继续?"
                % log_file):
            return
        argline = "--log %s --subject %s" % (
            shlex.quote(log_file), shlex.quote("贴吧运行日志日报"))
        self._run_mail("发送当日日志邮件", argline)

    def act_mail_alert(self):
        text = self.alert_text.get().strip()
        if not text:
            messagebox.showinfo("测试告警", "请填写告警正文")
            return
        if not messagebox.askyesno("发送测试告警", "将立即发送一封测试告警邮件, 继续?"):
            return
        argline = "--subject %s --alert %s" % (
            shlex.quote("测试: 贴吧管理中心告警"), shlex.quote(text))
        self._run_mail("发送测试告警", argline)

    # ---------- 日志 ----------

    def _build_logs(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  日志  ")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="日志文件").pack(side="left")
        self.log_file = tk.StringVar(value="tieba-run.log")
        ttk.Combobox(bar, textvariable=self.log_file, state="readonly", width=24,
                     values=LOG_FILES).pack(side="left", padx=(6, 10))
        ttk.Label(bar, text="行数").pack(side="left")
        self.log_lines = tk.IntVar(value=200)
        ttk.Spinbox(bar, from_=50, to=2000, increment=50, width=7,
                    textvariable=self.log_lines).pack(side="left", padx=(6, 10))
        ttk.Button(bar, text="刷新", command=self.act_logs).pack(side="left")
        ttk.Label(bar, text="读取服务器 /opt/tieba-sign 下的日志尾部",
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))

        frame, self.log_text = make_text(tab, height=20)
        self._text_widgets.append(self.log_text)
        frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def act_logs(self):
        name = self.log_file.get()
        try:
            n = max(10, int(self.log_lines.get()))
        except (tk.TclError, ValueError):
            n = 200

        def work(log):
            cmd = "cd %s && tail -n %d %s 2>/dev/null || echo '(无此日志)'" % (
                shlex.quote(self._dir()), n, shlex.quote(name))
            _, out, _ = self._ssh_exec(cmd, timeout=60)
            return out

        self._bg("读取日志", work, lambda text: set_text(self.log_text, text))


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        app = App()
        app.root.mainloop()
    except Exception:
        import traceback
        log_path = os.path.join(BASE, "tieba-manager-error.log")
        text = traceback.format_exc()
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        try:
            messagebox.showerror("贴吧管理中心启动失败",
                                 "启动异常, 详情见 %s\n\n%s"
                                 % (log_path, text[-800:]))
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())