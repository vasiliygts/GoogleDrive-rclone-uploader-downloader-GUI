#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RcloneSync GUI - візуальна оболонка над rclone для Google Drive (та будь-яких інших remote).

Можливості:
  * дерево локальних папок/файлів + дерево віддаленого сховища;
  * черга завдань: кожен елемент має власне призначення;
  * режими: copy / sync (дзеркало) / move;
  * контроль перезапису: перезаписувати / тільки новіші / не чіпати існуючі;
  * прогрес у реальному часі (байти, швидкість, ETA, поточні файли);
  * зворотній напрямок - завантаження з хмари на диск;
  * dry-run для безпечної перевірки перед реальним запуском.

Потрібен встановлений rclone з налаштованим remote (rclone config).
"""

import json
import os
import queue
import shlex
import shutil
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

APP_NAME = "RcloneSync GUI"
APP_DIR = Path(os.getenv("APPDATA") or Path.home()) / "RcloneSyncGUI"
CFG_PATH = APP_DIR / "settings.json"

NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

MODES = {
    "copy": "Копіювати (нічого не видаляє)",
    "sync": "Синхронізувати (ДЗЕРКАЛО — видаляє зайве)",
    "move": "Перемістити (видаляє джерело)",
}

OVERWRITE = {
    "always": "Перезаписувати змінені файли",
    "newer": "Тільки якщо джерело новіше (--update)",
    "never": "Не чіпати існуючі (--ignore-existing)",
}


# --------------------------------------------------------------------------- #
#  Утиліти
# --------------------------------------------------------------------------- #

def find_rclone() -> str:
    # портативний rclone поруч зі скриптом має пріоритет над системним
    here = Path(__file__).resolve().parent / ("rclone.exe" if os.name == "nt" else "rclone")
    if here.exists():
        return str(here)
    p = shutil.which("rclone")
    if p:
        return p
    candidates = [
        r"C:\Program Files\rclone\rclone.exe",
        r"C:\rclone\rclone.exe",
        str(Path.home() / "rclone" / "rclone.exe"),
        str(Path.home() / "scoop" / "shims" / "rclone.exe"),
        "/usr/bin/rclone",
        "/usr/local/bin/rclone",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return ""


def split_flags(s: str) -> list:
    """Розбиває рядок прапорців із повагою до лапок.

    posix=False, щоб не з'їдало зворотні слеші у Windows-шляхах
    (--exclude "D:\\Temp\\**"); зайві лапки знімаємо вручну.
    """
    if not s or not s.strip():
        return []
    try:
        parts = shlex.split(s, posix=False)
    except ValueError:
        return s.split()
    out = []
    for p in parts:
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
            p = p[1:-1]
        out.append(p)
    return out


def human(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024.0:
            return f"{n:,.1f} {unit}".replace(",", " ")
        n /= 1024.0
    return f"{n:.1f} ПБ"


def human_time(sec) -> str:
    if sec is None:
        return "—"
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "—"
    if sec < 0:
        return "—"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}г {m:02d}хв"
    if m:
        return f"{m}хв {s:02d}с"
    return f"{s}с"


def load_cfg() -> dict:
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cfg(data: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def run_capture(args, timeout=90):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NOWIN,
        timeout=timeout,
    )


@dataclass
class Job:
    """Одне завдання в черзі."""
    src: str          # локальний шлях або "remote:path"
    dst_dir: str      # тека призначення
    is_dir: bool
    direction: str    # "up" | "down"
    name: str

    def dst_full(self, mode: str) -> str:
        """rclone copy DIR DST кладе ВМІСТ теки в DST, тому дописуємо ім'я теки."""
        if not self.is_dir:
            return self.dst_dir
        sep = "\\" if (self.direction == "down" and os.name == "nt") else "/"
        base = self.dst_dir.rstrip("/\\")
        return f"{base}{sep}{self.name}"


# --------------------------------------------------------------------------- #
#  Виконавець rclone у фоновому потоці
# --------------------------------------------------------------------------- #

class RcloneWorker(threading.Thread):
    def __init__(self, rclone, jobs, mode, overwrite, dry_run, extra_flags, transfers, out_q):
        super().__init__(daemon=True)
        self.rclone = rclone
        self.jobs = jobs
        self.mode = mode
        self.overwrite = overwrite
        self.dry_run = dry_run
        self.extra_flags = extra_flags
        self.transfers = transfers
        self.q = out_q
        self.proc = None
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def emit(self, kind, payload):
        self.q.put((kind, payload))

    def build_cmd(self, job: Job):
        mode = self.mode
        # sync для одиничного файлу небезпечний/некоректний -> зводимо до copy
        if mode == "sync" and not job.is_dir:
            mode = "copy"

        cmd = [self.rclone, mode, job.src, job.dst_full(self.mode)]
        cmd += [
            "--use-json-log",
            "--stats", "1s",
            "--stats-log-level", "NOTICE",
            "--transfers", str(self.transfers),
            "--checkers", str(max(4, self.transfers * 2)),
            "--retries", "3",
            "--low-level-retries", "10",
            "-v",
        ]
        if self.overwrite == "newer":
            cmd.append("--update")
        elif self.overwrite == "never":
            cmd.append("--ignore-existing")
        if self.dry_run:
            cmd.append("--dry-run")
        cmd += split_flags(self.extra_flags)
        return cmd

    def run(self):
        total = len(self.jobs)
        errors = 0
        started = time.time()

        for idx, job in enumerate(self.jobs, 1):
            if self._stop.is_set():
                break

            cmd = self.build_cmd(job)
            self.emit("job", {"index": idx, "total": total, "job": job,
                              "dst": job.dst_full(self.mode)})
            self.emit("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=NOWIN,
                )
            except Exception as e:
                errors += 1
                self.emit("log", f"[ПОМИЛКА ЗАПУСКУ] {e}")
                continue

            for line in self.proc.stderr:
                if self._stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        self.emit("log", line)
                        continue
                    stats = obj.get("stats")
                    if stats:
                        self.emit("stats", stats)
                    else:
                        msg = obj.get("msg", "").strip()
                        lvl = obj.get("level", "")
                        if msg and lvl in ("error", "warning", "notice"):
                            self.emit("log", f"[{lvl}] {msg}")
                        elif msg:
                            self.emit("log", msg)
                else:
                    self.emit("log", line)

            rc = self.proc.wait()
            if self._stop.is_set():
                self.emit("log", f"[×] Перервано: {job.name}")
            elif rc != 0:
                errors += 1
                self.emit("log", f"[!] Завдання «{job.name}» завершилось з кодом {rc}")
            else:
                self.emit("log", f"[✓] Готово: {job.name}")

        self.emit("done", {"errors": errors,
                           "cancelled": self._stop.is_set(),
                           "elapsed": time.time() - started})


# --------------------------------------------------------------------------- #
#  Головне вікно
# --------------------------------------------------------------------------- #

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.cfg = load_cfg()

        # розмір вікна: збережений з минулого разу, інакше підганяємо під екран
        # (на 1536x864 стандартні 780 не влазили — кнопки «Старт/Стоп» виїжджали)
        sh = self.winfo_screenheight()
        self.geometry(self.cfg.get("geometry")
                      or f"1180x{min(820, max(520, sh - 90))}")
        self.minsize(900, 480)
        saved = (self.cfg.get("rclone") or "").strip()
        # збережений шлях міг протухнути (перенесли теку) — тоді шукаємо заново
        self.rclone = saved if (saved and Path(saved).exists()) else find_rclone()
        self.remotes = []
        self.q = queue.Queue()
        self.worker = None
        self.jobs = []
        self._job_pos = (0, 0)   # (поточне завдання, усього) — для відсотка в заголовку

        # мапа iid -> метадані вузлів дерев
        self.lmeta = {}
        self.rmeta = {}

        self._build_ui()
        self._check_rclone()
        self._pump_id = self.after(120, self._pump)
        # позиції роздільників — коли вікно вже промальоване
        self._sash_id = self.after(250, self._restore_sash)
        self.bind("<F5>", self._refresh_focused)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI ---------------- #

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=22)

        # --- закріплена нижня смуга: прогрес і Старт/Стоп поза прокруткою ---
        bottom_fixed = ttk.Frame(self)
        bottom_fixed.pack(fill="x", side="bottom")

        # --- прокручувана область для всієї решти форми ---
        scroll_wrap = ttk.Frame(self)
        scroll_wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(scroll_wrap, highlightthickness=0)
        vsb = ttk.Scrollbar(scroll_wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(self.canvas)
        self._inner_id = self.canvas.create_window((0, 0), window=inner, anchor="nw")
        # висота вмісту змінилась -> перерахувати діапазон прокрутки
        inner.bind("<Configure>",
                   lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        # ширину вмісту тримаємо рівною ширині полотна, щоб не було горизонтальної прокрутки
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._inner_id, width=e.width))
        self.bind_all("<MouseWheel>", self._on_wheel)

        # --- верхня панель ---
        top = ttk.Frame(inner, padding=(8, 6))
        top.pack(fill="x")

        ttk.Label(top, text="rclone:").pack(side="left")
        self.var_rclone = tk.StringVar(value=self.rclone)
        ttk.Entry(top, textvariable=self.var_rclone, width=46).pack(side="left", padx=4)
        ttk.Button(top, text="…", width=3, command=self._pick_rclone).pack(side="left")

        ttk.Label(top, text="   Remote:").pack(side="left")
        self.var_remote = tk.StringVar()
        self.cb_remote = ttk.Combobox(top, textvariable=self.var_remote, width=22, state="readonly")
        self.cb_remote.pack(side="left", padx=4)
        self.cb_remote.bind("<<ComboboxSelected>>", lambda e: self._load_remote_root())

        ttk.Button(top, text="⟳ Оновити remotes", command=self._check_rclone).pack(side="left", padx=4)
        ttk.Button(top, text="⚙ rclone config", command=self._open_config).pack(side="left")

        # --- дерева: власна висота, яка тягнеться смужкою під ними ---
        self.tree_area = ttk.Frame(inner, height=int(self.cfg.get("tree_h", 340)))
        self.tree_area.pack(fill="x", padx=8, pady=(4, 0))
        self.tree_area.pack_propagate(False)   # тримати задану висоту, а не підганятись під вміст

        # --- два дерева ---
        panes = ttk.PanedWindow(self.tree_area, orient="horizontal")
        self.hpanes = panes

        # локальне
        lf = ttk.LabelFrame(panes, text="Локальний диск", padding=4)
        panes.add(lf, weight=1)

        lbar = ttk.Frame(lf)
        lbar.pack(fill="x", pady=(0, 4))
        ttk.Button(lbar, text="⟳", width=3, command=self._refresh_local_node).pack(side="left")
        ttk.Button(lbar, text="Додати теку…", command=self._add_local_root).pack(side="left", padx=4)
        ttk.Button(lbar, text="Σ Розмір теки", command=self._calc_local_size).pack(side="left")
        self.var_showfiles_l = tk.BooleanVar(value=True)
        ttk.Checkbutton(lbar, text="файли", variable=self.var_showfiles_l,
                        command=self._load_local_roots).pack(side="left", padx=6)

        # обгортка на grid: так вертикальна і горизонтальна смуги не б'ються між собою
        lwrap = ttk.Frame(lf)
        lwrap.pack(fill="both", expand=True)
        self.ltree = ttk.Treeview(lwrap, selectmode="extended", show="tree")
        lsb = ttk.Scrollbar(lwrap, orient="vertical", command=self.ltree.yview)
        lhb = ttk.Scrollbar(lwrap, orient="horizontal", command=self.ltree.xview)
        self.ltree.configure(yscrollcommand=lsb.set, xscrollcommand=lhb.set)
        self.ltree.grid(row=0, column=0, sticky="nsew")
        lsb.grid(row=0, column=1, sticky="ns")
        lhb.grid(row=1, column=0, sticky="ew")
        lwrap.rowconfigure(0, weight=1)
        lwrap.columnconfigure(0, weight=1)
        self.ltree.column("#0", width=420, minwidth=200, stretch=True)
        self.ltree.bind("<<TreeviewOpen>>", self._expand_local)

        # віддалене
        rf = ttk.LabelFrame(panes, text="Google Drive / remote", padding=4)
        panes.add(rf, weight=1)

        rbar = ttk.Frame(rf)
        rbar.pack(fill="x", pady=(0, 4))
        ttk.Button(rbar, text="⟳", width=3, command=self._refresh_remote_node).pack(side="left")
        ttk.Button(rbar, text="＋ Нова тека", command=self._mkdir_remote).pack(side="left", padx=4)
        ttk.Button(rbar, text="Σ Розмір теки", command=self._calc_remote_size).pack(side="left")
        self.var_showfiles_r = tk.BooleanVar(value=True)
        ttk.Checkbutton(rbar, text="файли", variable=self.var_showfiles_r,
                        command=self._load_remote_root).pack(side="left", padx=6)

        rwrap = ttk.Frame(rf)
        rwrap.pack(fill="both", expand=True)
        self.rtree = ttk.Treeview(rwrap, selectmode="extended", show="tree")
        rsb = ttk.Scrollbar(rwrap, orient="vertical", command=self.rtree.yview)
        rhb = ttk.Scrollbar(rwrap, orient="horizontal", command=self.rtree.xview)
        self.rtree.configure(yscrollcommand=rsb.set, xscrollcommand=rhb.set)
        self.rtree.grid(row=0, column=0, sticky="nsew")
        rsb.grid(row=0, column=1, sticky="ns")
        rhb.grid(row=1, column=0, sticky="ew")
        rwrap.rowconfigure(0, weight=1)
        rwrap.columnconfigure(0, weight=1)
        self.rtree.column("#0", width=420, minwidth=200, stretch=True)
        self.rtree.bind("<<TreeviewOpen>>", self._expand_remote)

        # --- смужка зміни висоти дерев (тягнути мишею вгору/вниз) ---
        grip = tk.Frame(inner, height=7, bg="#b0b0b0", cursor="sb_v_double_arrow")
        grip.pack(fill="x", padx=8, pady=3)
        grip.bind("<ButtonPress-1>", self._grip_press)
        grip.bind("<B1-Motion>", self._grip_drag)

        # --- кнопки напрямку ---
        mid = ttk.Frame(inner, padding=(8, 2))
        ttk.Button(mid, text="⬆  Вивантажити виділене  →  у виділену теку хмари",
                   command=self._queue_upload).pack(side="left", padx=2)
        ttk.Button(mid, text="⬇  Завантажити виділене  →  у виділену локальну теку",
                   command=self._queue_download).pack(side="left", padx=2)

        # --- черга ---
        qf = ttk.LabelFrame(inner, text="Черга завдань", padding=4)

        cols = ("dir", "src", "dst")
        self.qtree = ttk.Treeview(qf, columns=cols, show="headings", height=4, selectmode="extended")
        self.qtree.heading("dir", text="")
        self.qtree.heading("src", text="Джерело")
        self.qtree.heading("dst", text="Призначення")
        self.qtree.column("dir", width=40, anchor="center", stretch=False)
        self.qtree.column("src", width=460)
        self.qtree.column("dst", width=460)
        qsb = ttk.Scrollbar(qf, orient="vertical", command=self.qtree.yview)
        self.qtree.configure(yscrollcommand=qsb.set)
        qsb.pack(side="right", fill="y")
        self.qtree.pack(fill="both", expand=True)

        qb = ttk.Frame(inner, padding=(8, 0))
        ttk.Button(qb, text="Видалити з черги", command=self._remove_job).pack(side="left")
        ttk.Button(qb, text="Очистити чергу", command=self._clear_jobs).pack(side="left", padx=4)
        ttk.Button(qb, text="Порахувати обсяг черги", command=self._calc_size).pack(side="left")

        # --- опції ---
        of = ttk.Frame(inner, padding=(8, 6))

        ttk.Label(of, text="Режим:").grid(row=0, column=0, sticky="w")
        self.var_mode = tk.StringVar(value=self.cfg.get("mode", "copy"))
        cb_mode = ttk.Combobox(of, width=42, state="readonly",
                               values=[MODES[k] for k in MODES])
        cb_mode.grid(row=0, column=1, sticky="w", padx=4)
        cb_mode.set(MODES[self.var_mode.get()])
        cb_mode.bind("<<ComboboxSelected>>",
                     lambda e: self.var_mode.set(
                         [k for k, v in MODES.items() if v == cb_mode.get()][0]))
        self.cb_mode = cb_mode

        ttk.Label(of, text="Перезапис:").grid(row=0, column=2, sticky="w", padx=(14, 0))
        self.var_ow = tk.StringVar(value=self.cfg.get("overwrite", "always"))
        cb_ow = ttk.Combobox(of, width=38, state="readonly",
                             values=[OVERWRITE[k] for k in OVERWRITE])
        cb_ow.grid(row=0, column=3, sticky="w", padx=4)
        cb_ow.set(OVERWRITE[self.var_ow.get()])
        cb_ow.bind("<<ComboboxSelected>>",
                   lambda e: self.var_ow.set(
                       [k for k, v in OVERWRITE.items() if v == cb_ow.get()][0]))

        self.var_dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="Dry-run (лише перевірка)", variable=self.var_dry)\
            .grid(row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(of, text="Потоків:").grid(row=1, column=2, sticky="w", padx=(14, 0), pady=(6, 0))
        self.var_tr = tk.IntVar(value=self.cfg.get("transfers", 4))
        ttk.Spinbox(of, from_=1, to=16, width=5, textvariable=self.var_tr)\
            .grid(row=1, column=3, sticky="w", padx=4, pady=(6, 0))

        ttk.Label(of, text="Дод. прапорці:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.var_flags = tk.StringVar(value=self.cfg.get("flags", "--drive-chunk-size 32M"))
        ttk.Entry(of, textvariable=self.var_flags, width=60)\
            .grid(row=2, column=1, columnspan=3, sticky="we", padx=4, pady=(6, 0))
        of.columnconfigure(3, weight=1)

        # --- прогрес (у закріпленій смузі) ---
        pf = ttk.LabelFrame(bottom_fixed, text="Прогрес", padding=6)

        self.lbl_job = ttk.Label(pf, text="Очікування…", anchor="w")
        self.lbl_job.pack(fill="x")
        self.pb = ttk.Progressbar(pf, maximum=1000)
        self.pb.pack(fill="x", pady=3)
        self.lbl_stats = ttk.Label(pf, text="", anchor="w", foreground="#444")
        self.lbl_stats.pack(fill="x")
        self.lbl_cur = ttk.Label(pf, text="", anchor="w", foreground="#666")
        self.lbl_cur.pack(fill="x")

        # --- лог ---
        lg = ttk.LabelFrame(inner, text="Журнал", padding=4)
        self.log = tk.Text(lg, height=5, wrap="none", font=("Consolas", 9))
        gsb = ttk.Scrollbar(lg, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=gsb.set, state="disabled")
        gsb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

        # --- нижня панель (у закріпленій смузі) ---
        bf = ttk.Frame(bottom_fixed, padding=(8, 6))
        self.btn_start = ttk.Button(bf, text="▶  Старт", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bf, text="■  Стоп", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(bf, text="Очистити журнал", command=self._clear_log).pack(side="right")

        # --- РОЗКЛАДКА ---
        # Закріплена смуга внизу: її не видно в прокрутці, тому «Старт»
        # доступний завжди, хоч як розтягнуті дерева.
        pf.pack(fill="x", padx=8, pady=4)
        bf.pack(fill="x")

        # Прокручувана частина, звичайним порядком згори вниз.
        # top і self.tree_area зі смужкою вже запаковані вище.
        panes.pack(fill="both", expand=True)     # усередині self.tree_area
        mid.pack(fill="x")
        qf.pack(fill="x", padx=8, pady=4)
        qb.pack(fill="x")
        of.pack(fill="x")
        lg.pack(fill="x", padx=8, pady=(4, 8))

    # ---------------- rclone ---------------- #

    def _pick_rclone(self):
        p = filedialog.askopenfilename(title="Вкажіть rclone",
                                       filetypes=[("rclone", "rclone.exe rclone"), ("Усі", "*.*")])
        if p:
            self.var_rclone.set(p)
            self._check_rclone()

    def _open_config(self):
        exe = self.var_rclone.get().strip()
        if not exe:
            return
        try:
            if os.name == "nt":
                subprocess.Popen(f'start "" cmd /k "{exe}" config', shell=True)
            else:
                subprocess.Popen(["x-terminal-emulator", "-e", exe, "config"])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Не вдалося відкрити консоль:\n{e}")

    def _check_rclone(self):
        exe = self.var_rclone.get().strip()
        if not exe or not (Path(exe).exists() or shutil.which(exe)):
            self._log("rclone не знайдено. Встанови його (winget install Rclone.Rclone), "
                      "поклади rclone.exe поруч зі скриптом або вкажи шлях кнопкою «…».")
            self._load_local_roots()  # локальне дерево від rclone не залежить
            return
        self.rclone = exe
        try:
            r = run_capture([exe, "version"], timeout=20)
            self._log(r.stdout.splitlines()[0] if r.stdout else "rclone OK")
            r = run_capture([exe, "listremotes"], timeout=20)
            self.remotes = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        except Exception as e:
            self._log(f"Помилка запуску rclone: {e}")
            return

        self.cb_remote["values"] = self.remotes
        if self.remotes:
            saved = self.cfg.get("remote")
            self.var_remote.set(saved if saved in self.remotes else self.remotes[0])
            self._load_remote_root()
        else:
            self._log("Немає жодного налаштованого remote. Натисни «⚙ rclone config» "
                      "→ n → ім'я (напр. gdrive) → drive → далі за майстром.")
        self._load_local_roots()

    # ---------------- локальне дерево ---------------- #

    def _load_local_roots(self):
        self.ltree.delete(*self.ltree.get_children())
        self.lmeta.clear()
        roots = []
        if os.name == "nt":
            for letter in string.ascii_uppercase:
                d = f"{letter}:\\"
                if Path(d).exists():
                    roots.append(d)
        else:
            roots = ["/", str(Path.home())]
        roots += [r for r in self.cfg.get("extra_roots", []) if Path(r).exists()]

        for r in roots:
            iid = self.ltree.insert("", "end", text=r, open=False)
            self.lmeta[iid] = {"path": r, "dir": True, "loaded": False}
            self.ltree.insert(iid, "end", text="…")

    def _add_local_root(self):
        p = filedialog.askdirectory(title="Додати теку в дерево")
        if not p:
            return
        extra = self.cfg.setdefault("extra_roots", [])
        if p not in extra:
            extra.append(p)
            save_cfg(self.cfg)
        self._load_local_roots()

    def _expand_local(self, _evt):
        self._expand_local_node(self.ltree.focus())

    def _expand_local_node(self, iid):
        meta = self.lmeta.get(iid)
        if not meta or meta["loaded"]:
            return
        meta["loaded"] = True
        self.ltree.delete(*self.ltree.get_children(iid))
        base = Path(meta["path"])
        try:
            entries = sorted(base.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception as e:
            self.ltree.insert(iid, "end", text=f"[немає доступу: {e.__class__.__name__}]")
            return

        shown = 0
        for p in entries:
            try:
                isdir = p.is_dir()
            except OSError:
                continue
            if not isdir and not self.var_showfiles_l.get():
                continue
            label = ("📁 " if isdir else "📄 ") + p.name
            child = self.ltree.insert(iid, "end", text=label)
            self.lmeta[child] = {"path": str(p), "dir": isdir, "loaded": not isdir}
            if isdir:
                self.ltree.insert(child, "end", text="…")
            shown += 1
            if shown >= 3000:
                self.ltree.insert(iid, "end", text="[…забагато елементів, показано 3000]")
                break

    # ---------------- віддалене дерево ---------------- #

    def _remote_prefix(self) -> str:
        r = self.var_remote.get().strip()
        return r if r.endswith(":") else r + ":"

    def _load_remote_root(self):
        self.rtree.delete(*self.rtree.get_children())
        self.rmeta.clear()
        if not self.var_remote.get():
            return
        self.cfg["remote"] = self.var_remote.get()
        save_cfg(self.cfg)
        root = self._remote_prefix()
        iid = self.rtree.insert("", "end", text=root, open=False)
        self.rmeta[iid] = {"path": "", "dir": True, "loaded": False}
        self.rtree.insert(iid, "end", text="…")

    def _lsjson(self, rpath: str, prefix: str, show_files: bool):
        # prefix/show_files приходять з головного потоку: tkinter не потокобезпечний
        cmd = [self.rclone, "lsjson", prefix + rpath, "--no-modtime"]
        if not show_files:
            cmd.append("--dirs-only")
        r = run_capture(cmd, timeout=120)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "").strip()[:400])
        return json.loads(r.stdout or "[]")

    def _expand_remote(self, _evt):
        self._expand_remote_node(self.rtree.focus())

    def _expand_remote_node(self, iid):
        meta = self.rmeta.get(iid)
        if not meta or meta["loaded"]:
            return
        meta["loaded"] = True
        self.rtree.delete(*self.rtree.get_children(iid))
        self.rtree.insert(iid, "end", text="⏳ завантаження…")

        # читаємо tkinter-змінні тут, у головному потоці, і передаємо значеннями
        prefix = self._remote_prefix()
        show_files = self.var_showfiles_r.get()

        def work():
            try:
                items = self._lsjson(meta["path"], prefix, show_files)
                self.q.put(("rls", (iid, meta["path"], items, None)))
            except Exception as e:
                self.q.put(("rls", (iid, meta["path"], None, str(e))))

        threading.Thread(target=work, daemon=True).start()

    def _fill_remote(self, iid, base, items, err):
        if not self.rtree.exists(iid):
            return
        self.rtree.delete(*self.rtree.get_children(iid))
        if err:
            self.rtree.insert(iid, "end", text=f"[помилка] {err[:120]}")
            self._log(f"lsjson: {err}")
            # даємо шанс на повтор: згорни й розгорни гілку ще раз
            m = self.rmeta.get(iid)
            if m:
                m["loaded"] = False
            self.rtree.insert(iid, "end", text="…")
            return
        items.sort(key=lambda d: (not d.get("IsDir"), d.get("Name", "").lower()))
        for it in items:
            isdir = bool(it.get("IsDir"))
            name = it.get("Name", "")
            size = "" if isdir else f"   ({human(it.get('Size'))})"
            label = ("📁 " if isdir else "📄 ") + name + size
            child = self.rtree.insert(iid, "end", text=label)
            full = f"{base}/{name}" if base else name
            self.rmeta[child] = {"path": full, "dir": isdir, "loaded": not isdir, "name": name}
            if isdir:
                self.rtree.insert(child, "end", text="…")

    def _mkdir_remote(self):
        sel = self.rtree.selection()
        base = ""
        if sel:
            m = self.rmeta.get(sel[0])
            if m:
                base = m["path"] if m["dir"] else m["path"].rsplit("/", 1)[0]
        name = simpledialog.askstring(APP_NAME, f"Назва нової теки в «{self._remote_prefix()}{base}»:")
        if not name:
            return
        target = self._remote_prefix() + (f"{base}/{name}" if base else name)
        r = run_capture([self.rclone, "mkdir", target], timeout=60)
        if r.returncode == 0:
            self._log(f"Створено: {target}")
            self._load_remote_root()
        else:
            messagebox.showerror(APP_NAME, r.stderr[:500])

    # ---------------- оновлення дерев ---------------- #

    def _refresh_focused(self, _evt=None):
        """F5 — оновити те дерево, у якому зараз курсор."""
        if self.focus_get() is self.rtree:
            self._refresh_remote_node()
        else:
            self._refresh_local_node()

    def _refresh_node(self, which, iid):
        """Перечитати вміст вузла, лишивши решту дерева розгорнутою."""
        tree = self.rtree if which == "r" else self.ltree
        meta = (self.rmeta if which == "r" else self.lmeta)[iid]
        meta["loaded"] = False
        tree.delete(*tree.get_children(iid))
        if tree.item(iid, "open"):
            if which == "r":
                self._expand_remote_node(iid)
            else:
                self._expand_local_node(iid)
        else:
            tree.insert(iid, "end", text="…")   # підвантажиться при розгортанні

    def _refresh_local_node(self):
        sel = [i for i in self.ltree.selection()
               if i in self.lmeta and self.lmeta[i].get("dir")]
        if not sel:
            self._load_local_roots()            # нічого не виділено — перебудова з нуля
            return
        for iid in sel:
            self._refresh_node("l", iid)

    def _refresh_remote_node(self):
        sel = [i for i in self.rtree.selection()
               if i in self.rmeta and self.rmeta[i].get("dir")]
        if not sel:
            self._load_remote_root()
            return
        for iid in sel:
            self._refresh_node("r", iid)

    def _grip_press(self, e):
        self._grip_y0 = e.y_root
        self._grip_h0 = self.tree_area.winfo_height()

    def _grip_drag(self, e):
        """Тягнення смужки під деревами змінює їхню висоту."""
        self.tree_area.configure(
            height=max(160, self._grip_h0 + (e.y_root - self._grip_y0)))

    def _on_wheel(self, e):
        """Колесо гортає форму; дерева і журнал прокручують себе самі."""
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if isinstance(w, (ttk.Treeview, tk.Text)):
                return
            w = w.master
        self.canvas.yview_scroll(-2 if e.delta > 0 else 2, "units")

    def _restore_sash(self):
        """Роздільник між деревами — коли вікно вже промальоване."""
        p = self.cfg.get("hsash")
        if p:
            try:
                self.hpanes.sashpos(0, int(p))
            except Exception:
                pass

    # ---------------- розмір тек (на вимогу, для обох дерев) ---------------- #

    def _base_label(self, which: str, iid) -> str:
        """Підпис вузла без дописаного розміру ("r" — хмара, "l" — локальне)."""
        if which == "r":
            meta = self.rmeta.get(iid) or {}
            name = meta.get("name")
            return f"📁 {name}" if name else self._remote_prefix()
        path = (self.lmeta.get(iid) or {}).get("path", "")
        name = Path(path).name
        return f"📁 {name}" if name else path      # корінь диска — просто «C:\»

    def _calc_remote_size(self):
        """Drive не віддає розмір теки у списку — рахуємо рекурсивно, лише на вимогу."""
        prefix = self._remote_prefix()
        sel = [i for i in self.rtree.selection()
               if i in self.rmeta and self.rmeta[i].get("dir")]
        self._start_size("r", sel, {i: prefix + self.rmeta[i]["path"] for i in sel},
                         "Виділи справа одну або кілька тек.")

    def _calc_local_size(self):
        sel = [i for i in self.ltree.selection()
               if i in self.lmeta and self.lmeta[i].get("dir")]
        self._start_size("l", sel, {i: self.lmeta[i]["path"] for i in sel},
                         "Виділи зліва одну або кілька тек.")

    def _start_size(self, which, sel, targets, empty_msg):
        if not sel:
            messagebox.showinfo(APP_NAME, empty_msg)
            return
        tree = self.rtree if which == "r" else self.ltree
        for iid in sel:
            tree.item(iid, text=f"{self._base_label(which, iid)}   (рахую…)")
            threading.Thread(target=self._size_worker,
                             args=(which, iid, targets[iid]), daemon=True).start()

    def _size_worker(self, which, iid, target):
        try:
            # --fast-list різко зменшує кількість запитів до Drive на великих теках
            r = run_capture([self.rclone, "size", target, "--json", "--fast-list"], timeout=900)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "").strip()[:300])
            d = json.loads(r.stdout or "{}")
            self.q.put(("rsize", (which, iid, d.get("bytes"), d.get("count"), None)))
        except Exception as e:
            self.q.put(("rsize", (which, iid, None, None, str(e))))

    def _apply_size(self, which, iid, nbytes, count, err):
        tree = self.rtree if which == "r" else self.ltree
        if not tree.exists(iid):
            return
        base = self._base_label(which, iid)
        if err:
            tree.item(iid, text=f"{base}   (не порахувалось)")
            self._log(f"size: {err}")
        else:
            tree.item(iid, text=f"{base}   [{human(nbytes)} · {count} файлів]")

    # ---------------- черга ---------------- #

    def _sel_dest_remote(self):
        sel = self.rtree.selection()
        if not sel:
            return None
        m = self.rmeta.get(sel[0])
        if not m:
            return None
        p = m["path"] if m["dir"] else m["path"].rsplit("/", 1)[0]
        return self._remote_prefix() + p

    def _sel_dest_local(self):
        sel = self.ltree.selection()
        if not sel:
            return None
        m = self.lmeta.get(sel[0])
        if not m:
            return None
        return m["path"] if m["dir"] else str(Path(m["path"]).parent)

    def _queue_upload(self):
        srcs = [self.lmeta[i] for i in self.ltree.selection() if i in self.lmeta]
        if not srcs:
            messagebox.showinfo(APP_NAME, "Виділи зліва папки/файли для вивантаження.")
            return
        dst = self._sel_dest_remote()
        if dst is None:
            messagebox.showinfo(APP_NAME, "Виділи справа теку призначення в хмарі.")
            return
        for m in srcs:
            name = Path(m["path"]).name or m["path"].replace("\\", "").replace(":", "")
            self._add_job(Job(m["path"], dst, m["dir"], "up", name))

    def _queue_download(self):
        srcs = [self.rmeta[i] for i in self.rtree.selection() if i in self.rmeta]
        srcs = [m for m in srcs if m.get("path")]
        if not srcs:
            messagebox.showinfo(APP_NAME, "Виділи справа папки/файли для завантаження.")
            return
        dst = self._sel_dest_local()
        if dst is None:
            messagebox.showinfo(APP_NAME, "Виділи зліва локальну теку призначення.")
            return
        for m in srcs:
            self._add_job(Job(self._remote_prefix() + m["path"], dst,
                              m["dir"], "down", m.get("name") or m["path"].split("/")[-1]))

    def _add_job(self, job: Job):
        self.jobs.append(job)
        arrow = "⬆" if job.direction == "up" else "⬇"
        self.qtree.insert("", "end", values=(arrow, job.src, job.dst_full(self.var_mode.get())))

    def _remove_job(self):
        for iid in self.qtree.selection():
            idx = self.qtree.index(iid)
            self.qtree.delete(iid)
            if 0 <= idx < len(self.jobs):
                self.jobs.pop(idx)

    def _clear_jobs(self):
        self.qtree.delete(*self.qtree.get_children())
        self.jobs.clear()

    def _calc_size(self):
        if not self.jobs:
            messagebox.showinfo(
                APP_NAME,
                "Черга порожня — рахувати нічого.\n\n"
                "Ця кнопка рахує обсяг ЧЕРГИ. Щоб дізнатися розмір окремої теки, "
                "виділи її в дереві й натисни «Σ Розмір теки» над цим деревом.")
            return
        self._log("Рахую обсяг черги…")

        def work():
            total_b, total_f = 0, 0
            for j in self.jobs:
                try:
                    r = run_capture([self.rclone, "size", j.src, "--json"], timeout=300)
                    d = json.loads(r.stdout or "{}")
                    total_b += d.get("bytes", 0) or 0
                    total_f += d.get("count", 0) or 0
                except Exception as e:
                    self.q.put(("log", f"size {j.src}: {e}"))
            self.q.put(("log", f"Разом у черзі: {total_f} файлів, {human(total_b)}"))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- запуск ---------------- #

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.jobs:
            messagebox.showinfo(APP_NAME, "Черга порожня.")
            return
        mode = self.var_mode.get()
        if mode == "sync" and not self.var_dry.get():
            ok = messagebox.askyesno(
                APP_NAME,
                "Режим SYNC робить призначення точною копією джерела:\n"
                "усе зайве в теці призначення БУДЕ ВИДАЛЕНО.\n\nПродовжити?",
                icon="warning")
            if not ok:
                return
        if mode == "move" and not self.var_dry.get():
            if not messagebox.askyesno(APP_NAME, "Режим MOVE видалить файли з джерела після передачі.\nПродовжити?",
                                       icon="warning"):
                return

        self.cfg.update({"rclone": self.rclone, "mode": mode, "overwrite": self.var_ow.get(),
                         "transfers": int(self.var_tr.get()), "flags": self.var_flags.get(),
                         "remote": self.var_remote.get()})
        save_cfg(self.cfg)

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.pb["value"] = 0
        self._log("=" * 70)
        self._log(f"СТАРТ · режим={mode} · перезапис={self.var_ow.get()} · "
                  f"dry-run={'ТАК' if self.var_dry.get() else 'ні'}")

        self.worker = RcloneWorker(self.rclone, list(self.jobs), mode, self.var_ow.get(),
                                   self.var_dry.get(), self.var_flags.get().strip(),
                                   int(self.var_tr.get()), self.q)
        self.worker.start()

    def _stop(self):
        if self.worker:
            self._log("Скасування…")
            self.worker.cancel()

    # ---------------- події з потоків ---------------- #

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "rls":
                    self._fill_remote(*payload)
                elif kind == "rsize":
                    self._apply_size(*payload)
                elif kind == "job":
                    j = payload["job"]
                    self._job_pos = (payload["index"], payload["total"])
                    self.lbl_job.configure(
                        text=f"[{payload['index']}/{payload['total']}] "
                             f"{'⬆' if j.direction == 'up' else '⬇'} {j.src}  →  {payload['dst']}")
                    self.pb["value"] = 0
                elif kind == "stats":
                    self._update_stats(payload)
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self._pump_id = self.after(120, self._pump)

    def _update_stats(self, st):
        done = st.get("bytes", 0) or 0
        total = st.get("totalBytes", 0) or 0
        frac = (done / total) if total else 0.0
        self.pb["value"] = min(1000, frac * 1000)
        pct_txt = f"{frac * 100:.1f}%" if total else "—"
        # відсоток дублюємо в заголовок: видно на панелі задач, коли вікно згорнуте
        if total:
            self.title(f"{APP_NAME} — {frac * 100:.0f}%  "
                       f"[{self._job_pos[0]}/{self._job_pos[1]}]")
        self.lbl_stats.configure(
            text=f"{pct_txt}   ·   {human(done)} / {human(total)}   ·   "
                 f"{human(st.get('speed'))}/с   ·   "
                 f"ETA {human_time(st.get('eta'))}   ·   "
                 f"файлів {st.get('transfers', 0)}/{st.get('totalTransfers', 0)}   ·   "
                 f"перевірено {st.get('checks', 0)}   ·   "
                 f"помилок {st.get('errors', 0)}")
        cur = st.get("transferring") or []
        if cur:
            parts = []
            for t in cur[:3]:
                p = t.get("percentage")
                parts.append(f"{t.get('name', '')[:48]} {p}%" if p is not None else t.get("name", ""))
            more = f"  (+{len(cur) - 3})" if len(cur) > 3 else ""
            self.lbl_cur.configure(text="→ " + "   |   ".join(parts) + more)
        else:
            self.lbl_cur.configure(text="")

    def _finish(self, payload):
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.title(APP_NAME)
        if payload["cancelled"]:
            msg = "Скасовано користувачем."
        elif payload["errors"]:
            msg = f"Завершено з помилками: {payload['errors']}"
        else:
            msg = f"Усе успішно за {human_time(payload['elapsed'])}."
            self.pb["value"] = 1000
        self.lbl_job.configure(text=msg)
        self._log(msg)
        if self.var_dry.get():
            self._log("Це був DRY-RUN — реальних змін не зроблено. "
                      "Зніми галочку і натисни «Старт» ще раз.")

    # ---------------- дрібниці ---------------- #

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + str(text) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "Триває передача. Перервати і вийти?"):
                return
            self.worker.cancel()
        for aid in (getattr(self, "_pump_id", None), getattr(self, "_sash_id", None)):
            try:                               # щоб нічого не смикалось після знищення вікна
                self.after_cancel(aid)
            except Exception:
                pass
        # запам'ятовуємо розмір вікна і положення роздільників на наступний запуск
        try:
            self.cfg["geometry"] = self.geometry()
            self.cfg["tree_h"] = self.tree_area.winfo_height()
            self.cfg["hsash"] = self.hpanes.sashpos(0)
            save_cfg(self.cfg)
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()