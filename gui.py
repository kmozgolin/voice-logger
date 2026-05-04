"""
gui.py — Native desktop GUI for Voice Logger.
Starts dashboard_server.py in a background thread and polls the Flask API.
"""

import json
import os
import subprocess
import sys
import threading
import time
import datetime
from pathlib import Path

import requests
import customtkinter as ctk

API = "http://127.0.0.1:7331/api"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

ACCENT   = "#7c6af7"
ACCENT2  = "#3de8a0"
DANGER   = "#f76c6c"
WARN     = "#f7c86c"
BG       = "#0a0a0f"
SURFACE  = "#12121a"
BORDER   = "#1e1e2e"
TEXT     = "#d4d4e8"
MUTED    = "#5a5a7a"


def _api(path, method="GET", json_body=None, timeout=3):
    try:
        if method == "POST":
            r = requests.post(f"{API}{path}", json=json_body, timeout=timeout)
        else:
            r = requests.get(f"{API}{path}", timeout=timeout)
        return r.json()
    except Exception:
        return None


# ── Main window ────────────────────────────────────────────────────────────

class VoiceLoggerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Voice Logger")
        self.geometry("1260x760")
        self.minsize(900, 600)
        self.configure(fg_color=BG)

        self._transcripts      = []
        self._active_idx       = None
        self._recorder_running = False
        self._chunk_start      = None
        self._chunk_seconds    = 300
        self._active_meetings  = []
        self._forced_meeting   = None
        self._pending_labels   = []
        self._label_idx        = 0
        self._cfg              = {}

        self._build_ui()
        self._start_server()
        self._start_polling()

    # ── Server ─────────────────────────────────────────────────────────────

    def _start_server(self):
        def run():
            py = sys.executable
            cfg_dir = Path(__file__).parent
            py_txt = cfg_dir / "python_path.txt"
            if py_txt.exists():
                py = py_txt.read_text().strip()
            self._server_proc = subprocess.Popen(
                [py, "dashboard_server.py"],
                cwd=str(cfg_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self._server_proc = None
        threading.Thread(target=run, daemon=True).start()

    # ── UI layout ──────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_sidebar()
        self._build_main()
        self._build_right_panel()

    # ── Header ─────────────────────────────────────────────────────────────

    def _build_header(self):
        hdr = ctk.CTkFrame(self, height=52, fg_color=SURFACE, corner_radius=0)
        hdr.grid(row=0, column=0, columnspan=3, sticky="ew")
        hdr.grid_columnconfigure(3, weight=1)
        hdr.grid_propagate(False)

        ctk.CTkLabel(hdr, text="VOICE / LOGGER", font=("Courier New", 13, "bold"),
                     text_color=ACCENT).grid(row=0, column=0, padx=20, pady=14)

        self._lbl_today = ctk.CTkLabel(hdr, text="Сегодня: — расшифровок",
                                       font=("Courier New", 10), text_color=MUTED)
        self._lbl_today.grid(row=0, column=1, padx=12)

        self._lbl_last = ctk.CTkLabel(hdr, text="Последняя: —",
                                      font=("Courier New", 10), text_color=MUTED)
        self._lbl_last.grid(row=0, column=2, padx=12)

        # spacer
        ctk.CTkFrame(hdr, fg_color="transparent").grid(row=0, column=3, sticky="ew")

        self._loopback_badge = ctk.CTkLabel(hdr, text="Loopback off",
            font=("Courier New", 10), text_color=MUTED,
            fg_color=BORDER, corner_radius=10, padx=10, pady=3)
        self._loopback_badge.grid(row=0, column=4, padx=6)

        self._device_badge = ctk.CTkLabel(hdr, text="—",
            font=("Courier New", 10), text_color=MUTED,
            fg_color=BORDER, corner_radius=10, padx=10, pady=3)
        self._device_badge.grid(row=0, column=5, padx=4)

        self._start_btn = ctk.CTkButton(hdr, text="● Старт", width=80, height=30,
            fg_color="#1a3a1a", hover_color="#1f4f1f",
            text_color=ACCENT2, border_width=1, border_color=ACCENT2,
            font=("Courier New", 11), command=self._start_recorder)
        self._start_btn.grid(row=0, column=6, padx=(8, 2))

        self._stop_btn = ctk.CTkButton(hdr, text="■ Стоп", width=80, height=30,
            fg_color="#3a1a1a", hover_color="#5a2020",
            text_color=DANGER, border_width=1, border_color=DANGER,
            font=("Courier New", 11), command=self._stop_recorder,
            state="disabled")
        self._stop_btn.grid(row=0, column=7, padx=(2, 8))

        self._status_lbl = ctk.CTkLabel(hdr, text="● IDLE",
            font=("Courier New", 11, "bold"), text_color=MUTED,
            fg_color=BORDER, corner_radius=12, padx=14, pady=4)
        self._status_lbl.grid(row=0, column=8, padx=(0, 8))

        ctk.CTkButton(hdr, text="✕ Выход", width=80, height=30,
            fg_color="transparent", hover_color="#2a1a1a",
            text_color=MUTED, border_width=1, border_color=BORDER,
            font=("Courier New", 11), command=self.on_close).grid(row=0, column=9, padx=(0, 16))

    # ── Left sidebar ────────────────────────────────────────────────────────

    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=260, fg_color=SURFACE, corner_radius=0)
        sb.grid(row=1, column=0, sticky="nsew")
        sb.grid_rowconfigure(1, weight=2)
        sb.grid_rowconfigure(3, weight=1)
        sb.grid_propagate(False)

        hdr1 = ctk.CTkFrame(sb, fg_color=BORDER, height=32, corner_radius=0)
        hdr1.grid(row=0, column=0, sticky="ew")
        hdr1.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr1, text="ТРАНСКРИПТЫ", font=("Courier New", 9),
                     text_color=MUTED).grid(row=0, column=0, sticky="w", padx=12, pady=6)
        self._lbl_count = ctk.CTkLabel(hdr1, text="0", font=("Courier New", 9),
                                        text_color=TEXT, fg_color=BG,
                                        corner_radius=8, padx=6, pady=1)
        self._lbl_count.grid(row=0, column=1, padx=8)

        self._transcript_list = ctk.CTkScrollableFrame(sb, fg_color=BG, corner_radius=0)
        self._transcript_list.grid(row=1, column=0, sticky="nsew")
        self._transcript_list.grid_columnconfigure(0, weight=1)

        hdr2 = ctk.CTkFrame(sb, fg_color=BORDER, height=32, corner_radius=0)
        hdr2.grid(row=2, column=0, sticky="ew")
        hdr2.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr2, text="СЕССИИ", font=("Courier New", 9),
                     text_color=MUTED).grid(row=0, column=0, sticky="w", padx=12, pady=6)
        self._lbl_sessions = ctk.CTkLabel(hdr2, text="0", font=("Courier New", 9),
                                           text_color=TEXT, fg_color=BG,
                                           corner_radius=8, padx=6, pady=1)
        self._lbl_sessions.grid(row=0, column=1, padx=8)

        self._sessions_list = ctk.CTkScrollableFrame(sb, fg_color=BG, corner_radius=0, height=130)
        self._sessions_list.grid(row=3, column=0, sticky="nsew")
        self._sessions_list.grid_columnconfigure(0, weight=1)

    # ── Main content ─────────────────────────────────────────────────────────

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        main.grid(row=1, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        # Toolbar
        tb = ctk.CTkFrame(main, height=42, fg_color=SURFACE, corner_radius=0)
        tb.grid(row=0, column=0, sticky="ew")
        tb.grid_columnconfigure(0, weight=1)
        tb.grid_propagate(False)

        self._viewer_title = ctk.CTkLabel(tb, text="Выберите транскрипт",
            font=("Courier New", 11), text_color=MUTED, anchor="w")
        self._viewer_title.grid(row=0, column=0, sticky="ew", padx=16, pady=10)

        self._pipeline_btn = ctk.CTkButton(tb, text="⚙ Pipeline", width=90, height=26,
            fg_color=BORDER, hover_color="#2a2a3a", text_color=MUTED,
            font=("Courier New", 10), command=self._toggle_pipeline)
        self._pipeline_btn.grid(row=0, column=1, padx=4)

        ctk.CTkButton(tb, text="↻ Обновить", width=90, height=26,
            fg_color=BORDER, hover_color="#2a2a3a", text_color=MUTED,
            font=("Courier New", 10), command=self._refresh_all).grid(row=0, column=2, padx=(0, 12))

        # Transcript viewer
        self._viewer = ctk.CTkTextbox(main, font=("Courier New", 12),
            fg_color=BG, text_color=TEXT, wrap="word", state="disabled",
            border_width=0, corner_radius=0)
        self._viewer.grid(row=1, column=0, sticky="nsew")

        # Pipeline frame (hidden by default)
        self._pipeline_frame = ctk.CTkScrollableFrame(main, fg_color=BG, corner_radius=0)
        self._pipeline_frame.grid(row=1, column=0, sticky="nsew")
        self._pipeline_frame.grid_remove()
        self._build_pipeline_view(self._pipeline_frame)

        self._pipeline_open = False

    def _build_pipeline_view(self, parent):
        parent.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(parent, text="ЗАГРУЗКА СИСТЕМЫ", font=("Courier New", 9),
                     text_color=MUTED).grid(row=0, column=0, sticky="w", padx=20, pady=(20, 6))

        load_f = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=8)
        load_f.grid(row=1, column=0, sticky="ew", padx=16, pady=4)
        load_f.grid_columnconfigure(1, weight=1)

        rows = [("CPU", ACCENT), ("RAM", ACCENT2), ("GPU", DANGER), ("VRAM", WARN)]
        self._load_bars = {}
        self._load_vals = {}
        for i, (name, color) in enumerate(rows):
            ctk.CTkLabel(load_f, text=name, font=("Courier New", 10),
                         text_color=MUTED, width=40, anchor="e").grid(row=i, column=0, padx=(12,6), pady=5)
            bar = ctk.CTkProgressBar(load_f, height=8, fg_color=BORDER,
                                      progress_color=color, corner_radius=4)
            bar.set(0)
            bar.grid(row=i, column=1, sticky="ew", padx=6, pady=5)
            val = ctk.CTkLabel(load_f, text="—", font=("Courier New", 10),
                                text_color=TEXT, width=90, anchor="w")
            val.grid(row=i, column=2, padx=(4, 12), pady=5)
            self._load_bars[name] = bar
            self._load_vals[name] = val

        ctk.CTkLabel(parent, text="СТАТИСТИКА", font=("Courier New", 9),
                     text_color=MUTED).grid(row=2, column=0, sticky="w", padx=20, pady=(18, 6))

        stat_f = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=8)
        stat_f.grid(row=3, column=0, sticky="ew", padx=16, pady=4)
        stats = [
            ("ps-total",   "чанков\nобработано", TEXT),
            ("ps-skipped", "пропущено\n(тишина)",  MUTED),
            ("ps-sessions","сессий\nсклеено",      ACCENT2),
            ("ps-avg-t",   "ср. транскр.\nсек",    WARN),
            ("ps-avg-d",   "ср. диар.\nсек",       ACCENT2),
        ]
        self._pipe_vals = {}
        for i, (key, lbl, color) in enumerate(stats):
            f = ctk.CTkFrame(stat_f, fg_color=BORDER, corner_radius=6)
            f.grid(row=0, column=i, padx=6, pady=10, sticky="ew")
            stat_f.grid_columnconfigure(i, weight=1)
            v = ctk.CTkLabel(f, text="—", font=("Courier New", 22, "bold"),
                              text_color=color)
            v.pack(pady=(10, 2))
            ctk.CTkLabel(f, text=lbl, font=("Courier New", 9),
                         text_color=MUTED, justify="center").pack(pady=(0, 10))
            self._pipe_vals[key] = v

    # ── Right panel ───────────────────────────────────────────────────────

    def _build_right_panel(self):
        panel = ctk.CTkScrollableFrame(self, width=300, fg_color=SURFACE, corner_radius=0)
        panel.grid(row=1, column=2, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)

        row = 0

        # ── Stats ──────────────────────────────────────────────────────────
        ctk.CTkLabel(panel, text="СТАТИСТИКА", font=("Courier New", 9),
                     text_color=MUTED).grid(row=row, column=0, sticky="w", padx=16, pady=(16,4)); row+=1

        stats_f = ctk.CTkFrame(panel, fg_color=BORDER, corner_radius=8)
        stats_f.grid(row=row, column=0, sticky="ew", padx=12, pady=(0,4)); row+=1
        stats_f.grid_columnconfigure((0,1), weight=1)

        self._stat_today = ctk.CTkLabel(stats_f, text="0",
            font=("Courier New", 26, "bold"), text_color=ACCENT)
        self._stat_today.grid(row=0, column=0, pady=(10,2))
        ctk.CTkLabel(stats_f, text="сегодня", font=("Courier New", 9),
                     text_color=MUTED).grid(row=1, column=0, pady=(0,10))

        self._stat_total = ctk.CTkLabel(stats_f, text="0",
            font=("Courier New", 26, "bold"), text_color=ACCENT)
        self._stat_total.grid(row=0, column=1, pady=(10,2))
        ctk.CTkLabel(stats_f, text="всего файлов", font=("Courier New", 9),
                     text_color=MUTED).grid(row=1, column=1, pady=(0,10))

        # Progress bar
        self._chunk_bar = ctk.CTkProgressBar(panel, height=4,
            fg_color=BORDER, progress_color=ACCENT, corner_radius=2)
        self._chunk_bar.set(0)
        self._chunk_bar.grid(row=row, column=0, sticky="ew", padx=12, pady=(2,2)); row+=1
        self._chunk_lbl = ctk.CTkLabel(panel, text="—", font=("Courier New", 9),
                                        text_color=MUTED, anchor="e")
        self._chunk_lbl.grid(row=row, column=0, sticky="ew", padx=14, pady=(0,8)); row+=1

        # ── Current meeting ────────────────────────────────────────────────
        ctk.CTkLabel(panel, text="ТЕКУЩАЯ ВСТРЕЧА", font=("Courier New", 9),
                     text_color=MUTED).grid(row=row, column=0, sticky="w", padx=16, pady=(12,4)); row+=1

        self._cal_frame = ctk.CTkFrame(panel, fg_color=BORDER, corner_radius=8)
        self._cal_frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(0,8)); row+=1
        self._cal_frame.grid_columnconfigure(0, weight=1)
        self._cal_lbl = ctk.CTkLabel(self._cal_frame, text="Нет активных событий",
            font=("Courier New", 10), text_color=MUTED, wraplength=240, justify="left", anchor="w")
        self._cal_lbl.grid(row=0, column=0, padx=12, pady=10, sticky="ew")

        # ── Speakers ───────────────────────────────────────────────────────
        ctk.CTkLabel(panel, text="СПИКЕРЫ", font=("Courier New", 9),
                     text_color=MUTED).grid(row=row, column=0, sticky="w", padx=16, pady=(12,4)); row+=1

        self._speakers_frame = ctk.CTkFrame(panel, fg_color="transparent", corner_radius=0)
        self._speakers_frame.grid(row=row, column=0, sticky="ew", padx=12); row+=1

        # ── Settings ───────────────────────────────────────────────────────
        ctk.CTkLabel(panel, text="НАСТРОЙКИ", font=("Courier New", 9),
                     text_color=MUTED).grid(row=row, column=0, sticky="w", padx=16, pady=(16,4)); row+=1

        self._settings_frame = ctk.CTkFrame(panel, fg_color=BORDER, corner_radius=8)
        self._settings_frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(0,8)); row+=1
        self._settings_frame.grid_columnconfigure(0, weight=1)
        self._build_settings(self._settings_frame)

        # ── Errors ─────────────────────────────────────────────────────────
        self._errors_frame = ctk.CTkFrame(panel, fg_color="transparent", corner_radius=0)
        self._errors_frame.grid(row=row, column=0, sticky="ew", padx=12); row+=1

    def _build_settings(self, parent):
        self._cfg_vars = {}

        def row(label, sub, widget_fn, key, r):
            ctk.CTkLabel(parent, text=label, font=("sans-serif", 12),
                         text_color=TEXT, anchor="w").grid(
                row=r*2, column=0, sticky="w", padx=12, pady=(10,0))
            if sub:
                ctk.CTkLabel(parent, text=sub, font=("Courier New", 9),
                             text_color=MUTED, anchor="w").grid(
                    row=r*2+1, column=0, sticky="w", padx=12)
            w = widget_fn()
            w.grid(row=r*2, column=1, rowspan=2, padx=(4,12), pady=(8,2), sticky="e")
            self._cfg_vars[key] = w

        # Whisper model
        r = 0
        def mk_model():
            v = ctk.CTkOptionMenu(parent, values=["tiny","base","small","medium","large"],
                                   width=100, font=("Courier New", 11))
            v.set("large"); return v
        row("Модель Whisper", "tiny → large", mk_model, "whisper_model", r); r+=1

        # Device
        def mk_device():
            v = ctk.CTkOptionMenu(parent, values=["auto","cpu","cuda"],
                                   width=100, font=("Courier New", 11))
            v.set("auto"); return v
        row("Устройство", "GPU быстрее в 10x", mk_device, "whisper_device", r); r+=1

        # Language
        def mk_lang():
            v = ctk.CTkOptionMenu(parent, values=["auto","ru","en","ka"],
                                   width=100, font=("Courier New", 11))
            v.set("auto"); return v
        row("Язык", "auto = определять", mk_lang, "language", r); r+=1

        # Chunk
        def mk_chunk():
            v = ctk.CTkEntry(parent, width=80, font=("Courier New", 11))
            v.insert(0, "300"); return v
        row("Чанк (сек)", "длина записи", mk_chunk, "chunk_seconds", r); r+=1

        # CPU threads
        def mk_cpu():
            v = ctk.CTkEntry(parent, width=80, font=("Courier New", 11))
            v.insert(0, "4"); return v
        row("CPU потоки", "меньше = легче", mk_cpu, "cpu_threads", r); r+=1

        # GPU fraction
        def mk_gpu():
            v = ctk.CTkEntry(parent, width=80, font=("Courier New", 11))
            v.insert(0, "0.5"); return v
        row("GPU память", "доля VRAM (0.1–1.0)", mk_gpu, "gpu_memory_fraction", r); r+=1

        # Max parallel
        def mk_par():
            v = ctk.CTkEntry(parent, width=80, font=("Courier New", 11))
            v.insert(0, "1"); return v
        row("Парал. чанки", "1 = не перегружать", mk_par, "max_parallel_chunks", r); r+=1

        # Diarization toggle
        ctk.CTkLabel(parent, text="Диаризация", font=("sans-serif", 12),
                     text_color=TEXT, anchor="w").grid(
            row=r*2, column=0, sticky="w", padx=12, pady=(10,8))
        self._cfg_vars["diarization_enabled"] = ctk.CTkSwitch(
            parent, text="", onvalue=True, offvalue=False)
        self._cfg_vars["diarization_enabled"].select()
        self._cfg_vars["diarization_enabled"].grid(
            row=r*2, column=1, padx=(4,12), pady=(8,8), sticky="e"); r+=1

        # Save button
        self._save_msg = ctk.CTkLabel(parent, text="", font=("Courier New", 10),
                                       text_color=ACCENT2)
        self._save_msg.grid(row=r*2, column=0, columnspan=2, pady=4)
        ctk.CTkButton(parent, text="Сохранить", fg_color=ACCENT,
                       hover_color="#6b59e6", font=("Courier New", 11),
                       command=self._save_config).grid(
            row=r*2+1, column=0, columnspan=2, sticky="ew",
            padx=12, pady=(0,12))

    # ── Polling ────────────────────────────────────────────────────────────

    def _start_polling(self):
        def poll():
            ticks = {"status": 0, "transcripts": 0, "speakers": 0,
                     "meetings": 0, "labels": 0, "pipeline": 0, "load": 0}
            while True:
                now = time.time()
                self._safe(_api, "/status")
                if now - ticks["status"] > 3:
                    data = _api("/status")
                    if data:
                        self.after(0, self._update_status, data)
                    ticks["status"] = now

                if now - ticks["transcripts"] > 15:
                    data = _api("/transcripts")
                    if data:
                        self.after(0, self._update_transcripts, data)
                    ticks["transcripts"] = now

                if now - ticks["speakers"] > 20:
                    data = _api("/speakers")
                    if data is not None:
                        self.after(0, self._update_speakers, data)
                    ticks["speakers"] = now

                if now - ticks["meetings"] > 15:
                    data = _api("/active_meetings")
                    if data:
                        self.after(0, self._update_meetings, data)
                    ticks["meetings"] = now

                if now - ticks["labels"] > 5:
                    data = _api("/pending_labels")
                    if data is not None:
                        self.after(0, self._update_labels, data)
                    ticks["labels"] = now

                if now - ticks["pipeline"] > 4 and self._pipeline_open:
                    data = _api("/pipeline_stats")
                    if data:
                        self.after(0, self._update_pipeline, data)
                    ticks["pipeline"] = now

                if now - ticks["load"] > 4 and self._pipeline_open:
                    data = _api("/system_load")
                    if data:
                        self.after(0, self._update_load, data)
                    ticks["load"] = now

                self.after(0, self._update_progress)
                time.sleep(1)

        threading.Thread(target=poll, daemon=True).start()
        # Fetch config once after server starts
        self.after(2000, self._fetch_config)

    def _safe(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:
            return None

    # ── Update handlers ────────────────────────────────────────────────────

    def _update_status(self, s):
        state = s.get("state", "idle")
        colors = {"recording": DANGER, "transcribing": WARN,
                  "diarizing": ACCENT, "uploading": ACCENT, "idle": ACCENT2}
        color = colors.get(state, MUTED)
        self._status_lbl.configure(text=f"● {state.upper()}", text_color=color)

        today = s.get("transcripts_today", 0)
        self._lbl_today.configure(text=f"Сегодня: {today} расшифровок")

        if s.get("last_transcript"):
            name = Path(s["last_transcript"]).name
            self._lbl_last.configure(text=f"Последняя: {name}")

        lb = s.get("loopback_active", False)
        self._loopback_badge.configure(
            text="⊕ Loopback on" if lb else "Loopback off",
            text_color=ACCENT2 if lb else MUTED)

        dev = s.get("whisper_device", "")
        if dev == "cuda":
            self._device_badge.configure(text="⚡ GPU CUDA", text_color=ACCENT2)
        elif dev == "cpu":
            self._device_badge.configure(text="▣ CPU", text_color=WARN)

        if s.get("current_chunk_start"):
            try:
                self._chunk_start = datetime.datetime.fromisoformat(s["current_chunk_start"])
            except Exception:
                pass

        self._stat_today.configure(text=str(today))

        # Calendar (single meeting — picker handles multiple)
        cal = s.get("last_calendar_event")
        if cal and len(self._active_meetings) <= 1:
            self._render_single_meeting(cal)

        # Recorder button
        rec_data = _api("/recorder/status")
        if rec_data:
            running = rec_data.get("running", False)
            if running != self._recorder_running:
                self._recorder_running = running
                self._update_rec_btn()

        # Errors
        errs = s.get("errors") or []
        for w in self._errors_frame.winfo_children():
            w.destroy()
        for e in errs[-3:]:
            ctk.CTkLabel(self._errors_frame, text=e[:80], font=("Courier New", 9),
                         text_color=DANGER, wraplength=260, justify="left").pack(
                anchor="w", padx=4, pady=2)

    def _update_transcripts(self, data):
        self._transcripts = data
        chunks   = [t for t in data if "sessions" not in t["path"] and "permanent" not in t["path"]]
        sessions = [t for t in data if "sessions" in t["path"]]

        self._lbl_count.configure(text=str(len(chunks)))
        self._lbl_sessions.configure(text=str(len(sessions)))
        self._stat_total.configure(text=str(len(data)))

        self._render_list(self._transcript_list, chunks)
        self._render_list(self._sessions_list, sessions, compact=True)

    def _render_list(self, parent, items, compact=False):
        for w in parent.winfo_children():
            w.destroy()
        for i, t in enumerate(items):
            idx = self._transcripts.index(t) if t in self._transcripts else -1
            bg  = "#1a1a2e" if idx == self._active_idx else "transparent"
            f = ctk.CTkFrame(parent, fg_color=bg, corner_radius=4, cursor="hand2")
            f.grid(row=i, column=0, sticky="ew", pady=1, padx=4)
            f.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(f, text=t["name"][:38], font=("Courier New", 10),
                         text_color=TEXT if idx == self._active_idx else TEXT,
                         anchor="w").grid(row=0, column=0, sticky="ew", padx=8, pady=(5,1))
            if not compact:
                meta = f'{t["date"]}  {t["size_kb"]} KB'
                ctk.CTkLabel(f, text=meta, font=("Courier New", 9),
                             text_color=MUTED, anchor="w").grid(
                    row=1, column=0, sticky="ew", padx=8, pady=(0,5))
            f.bind("<Button-1>", lambda e, ix=idx: self._open_transcript(ix))
            for child in f.winfo_children():
                child.bind("<Button-1>", lambda e, ix=idx: self._open_transcript(ix))

    def _open_transcript(self, idx):
        if idx < 0 or idx >= len(self._transcripts):
            return
        self._active_idx = idx
        t = self._transcripts[idx]
        self._viewer_title.configure(text=f"{t['name']}  —  {t['date']}", text_color=TEXT)

        def load():
            data = _api(f"/transcript?path={t['path']}")
            if data and "content" in data:
                self.after(0, self._show_text, data["content"])
        threading.Thread(target=load, daemon=True).start()

        # Refresh list highlighting
        data = _api("/transcripts")
        if data:
            self._update_transcripts(data)

    def _show_text(self, content):
        self._viewer.configure(state="normal")
        self._viewer.delete("1.0", "end")
        self._viewer.insert("1.0", content)
        self._viewer.configure(state="disabled")

    def _update_speakers(self, speakers):
        for w in self._speakers_frame.winfo_children():
            w.destroy()
        for s in speakers:
            f = ctk.CTkFrame(self._speakers_frame, fg_color=BORDER, corner_radius=6)
            f.pack(fill="x", pady=3)
            f.grid_columnconfigure(1, weight=1)

            dot_color = s.get("color") or "#888"
            dot = ctk.CTkLabel(f, text="●", font=("sans-serif", 10),
                                text_color=dot_color, width=20)
            dot.grid(row=0, column=0, padx=(10,4), pady=8)

            name_lbl = ctk.CTkLabel(f, text=s["name"], font=("sans-serif", 12),
                                     text_color=ACCENT2 if s.get("is_owner") else TEXT,
                                     anchor="w")
            name_lbl.grid(row=0, column=1, sticky="ew", pady=8)

            edit_btn = ctk.CTkButton(f, text="✎", width=26, height=22,
                fg_color="transparent", hover_color=BORDER,
                text_color=MUTED, font=("sans-serif", 11),
                command=lambda sid=s["id"], sn=s["name"]: self._rename_speaker(sid, sn))
            edit_btn.grid(row=0, column=2, padx=2)

            if not s.get("is_owner"):
                del_btn = ctk.CTkButton(f, text="✕", width=26, height=22,
                    fg_color="transparent", hover_color=BORDER,
                    text_color=DANGER, font=("sans-serif", 11),
                    command=lambda sid=s["id"]: self._delete_speaker(sid))
                del_btn.grid(row=0, column=3, padx=(0, 6))

    def _rename_speaker(self, sid, current_name):
        dlg = ctk.CTkInputDialog(text=f"Новое имя для {current_name}:", title="Переименовать спикера")
        name = dlg.get_input()
        if name and name.strip():
            _api(f"/speakers/{sid}/rename", "POST", {"name": name.strip()})
            data = _api("/speakers")
            if data is not None:
                self._update_speakers(data)

    def _delete_speaker(self, sid):
        # Simple confirm via dialog
        dlg = ctk.CTkInputDialog(text="Введите 'да' для подтверждения удаления:", title="Удалить спикера?")
        ans = dlg.get_input()
        if ans and ans.strip().lower() in ("да", "yes", "y"):
            _api(f"/speakers/{sid}", "DELETE")
            data = _api("/speakers")
            if data is not None:
                self._update_speakers(data)

    def _update_meetings(self, data):
        self._active_meetings = data.get("meetings", [])
        self._forced_meeting  = data.get("forced")
        self._render_meetings()

    def _render_single_meeting(self, cal):
        for w in self._cal_frame.winfo_children():
            w.destroy()
        self._cal_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self._cal_frame, text=cal.get("subject", "—"),
                     font=("sans-serif", 12, "bold"), text_color=ACCENT2,
                     wraplength=240, justify="left", anchor="w").grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10,4))
        detail = f'{cal.get("start","")[:16]} → {cal.get("end","")[:16]}'
        if cal.get("organizer"):
            detail += f'\n👤 {cal["organizer"]}'
        if cal.get("location"):
            detail += f'\n📍 {cal["location"]}'
        ctk.CTkLabel(self._cal_frame, text=detail,
                     font=("Courier New", 9), text_color=MUTED,
                     wraplength=240, justify="left", anchor="w").grid(
            row=1, column=0, sticky="ew", padx=12, pady=(0,10))

    def _render_meetings(self):
        for w in self._cal_frame.winfo_children():
            w.destroy()
        self._cal_frame.grid_columnconfigure(0, weight=1)

        meetings = self._active_meetings
        forced   = self._forced_meeting

        if not meetings:
            ctk.CTkLabel(self._cal_frame, text="Нет активных событий",
                         font=("Courier New", 10), text_color=MUTED).grid(
                row=0, column=0, padx=12, pady=10)
            return

        if len(meetings) > 1:
            ctk.CTkLabel(self._cal_frame,
                         text=f"⚠ {len(meetings)} встречи одновременно",
                         font=("Courier New", 9), text_color=WARN).grid(
                row=0, column=0, sticky="w", padx=12, pady=(8,4))

        for i, m in enumerate(meetings):
            is_sel = (forced and forced.get("subject") == m["subject"]
                      and forced.get("start") == m["start"]) or \
                     (not forced and i == 0 and len(meetings) > 1) or \
                     len(meetings) == 1

            bg = "#0d2a1e" if is_sel else BORDER
            border = ACCENT2 if is_sel else BORDER

            card = ctk.CTkFrame(self._cal_frame, fg_color=bg, corner_radius=6,
                                 border_width=1, border_color=border, cursor="hand2")
            card.grid(row=i+1, column=0, sticky="ew", padx=8, pady=3)
            card.grid_columnconfigure(0, weight=1)

            subj_color = ACCENT2 if is_sel else TEXT
            ctk.CTkLabel(card, text=m["subject"], font=("sans-serif", 11, "bold"),
                         text_color=subj_color, wraplength=220, justify="left",
                         anchor="w").grid(row=0, column=0, sticky="ew", padx=10, pady=(8,2))

            t_start = str(m.get("start",""))
            t_end   = str(m.get("end",""))
            detail  = f'{t_start[11:16]} → {t_end[11:16]}'
            if m.get("organizer"):
                detail += f'  👤 {m["organizer"]}'
            ctk.CTkLabel(card, text=detail, font=("Courier New", 9),
                         text_color=MUTED, anchor="w").grid(
                row=1, column=0, sticky="ew", padx=10, pady=(0,8))

            if len(meetings) > 1:
                badge_text = "✓ выбрано" if (forced and is_sel) else ("✓ авто" if is_sel else "выбрать")
                badge_color = ACCENT2 if is_sel else MUTED
                ctk.CTkLabel(card, text=badge_text, font=("Courier New", 9),
                             text_color=badge_color, anchor="e").grid(
                    row=0, column=1, padx=(0,10), pady=(8,2))
                card.bind("<Button-1>",
                          lambda e, idx=i: self._select_meeting(idx))
                for child in card.winfo_children():
                    child.bind("<Button-1>",
                               lambda e, idx=i: self._select_meeting(idx))

        if forced and len(meetings) > 1:
            clear = ctk.CTkLabel(self._cal_frame, text="сбросить выбор →",
                                  font=("Courier New", 9), text_color=MUTED, cursor="hand2")
            clear.grid(row=len(meetings)+1, column=0, sticky="e", padx=12, pady=(2,8))
            clear.bind("<Button-1>", lambda e: self._clear_meeting())

    def _select_meeting(self, idx):
        m = self._active_meetings[idx]
        self._forced_meeting = m
        _api("/select_meeting", "POST", {"event": m})
        self._render_meetings()

    def _clear_meeting(self):
        self._forced_meeting = None
        _api("/select_meeting", "POST", {"event": None})
        self._render_meetings()

    def _update_labels(self, labels):
        self._pending_labels = labels
        if labels:
            self._show_label_popup(0)

    def _show_label_popup(self, idx):
        if not self._pending_labels:
            return
        lbl = self._pending_labels[min(idx, len(self._pending_labels)-1)]
        self._label_idx = min(idx, len(self._pending_labels)-1)

        if hasattr(self, "_label_win") and self._label_win.winfo_exists():
            self._label_win.destroy()

        win = ctk.CTkToplevel(self)
        win.title("Новый спикер")
        win.geometry("400x280")
        win.configure(fg_color=SURFACE)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        self._label_win = win

        ctk.CTkLabel(win, text="🎙 НОВЫЙ СПИКЕР", font=("Courier New", 11, "bold"),
                     text_color=ACCENT).pack(pady=(16,4), padx=20, anchor="w")

        ctk.CTkLabel(win, text=lbl.get("speaker_name","?"),
                     font=("sans-serif", 16, "bold"), text_color=TEXT).pack(pady=4, padx=20, anchor="w")

        sample = "\n".join(f'"{l}"' for l in lbl.get("sample_lines",[])[:2]) or "(нет реплик)"
        ctk.CTkLabel(win, text=sample, font=("sans-serif", 11), text_color=MUTED,
                     wraplength=360, justify="left").pack(pady=6, padx=20, anchor="w")

        entry = ctk.CTkEntry(win, placeholder_text="Введите имя…",
                              font=("sans-serif", 13), height=36)
        entry.pack(fill="x", padx=20, pady=(8,4))
        entry.focus()

        remaining = len(self._pending_labels) - 1
        if remaining > 0:
            ctk.CTkLabel(win, text=f"Ещё {remaining} незнакомых спикера",
                         font=("Courier New", 9), text_color=MUTED).pack(pady=2)

        btn_f = ctk.CTkFrame(win, fg_color="transparent")
        btn_f.pack(fill="x", padx=20, pady=(4,16))

        def submit():
            name = entry.get().strip()
            if not name:
                return
            _api("/label_speaker", "POST", {"request_id": lbl["request_id"], "name": name})
            self._pending_labels.pop(self._label_idx)
            win.destroy()
            if self._pending_labels:
                self.after(200, self._show_label_popup, 0)

        def dismiss():
            _api("/label_speaker", "POST", {"request_id": lbl["request_id"], "dismiss": True})
            self._pending_labels.pop(self._label_idx)
            win.destroy()

        entry.bind("<Return>", lambda e: submit())
        ctk.CTkButton(btn_f, text="OK", fg_color=ACCENT, hover_color="#6b59e6",
                       command=submit).pack(side="left", expand=True, fill="x", padx=(0,4))
        ctk.CTkButton(btn_f, text="Пропустить", fg_color=BORDER,
                       command=dismiss).pack(side="left", expand=True, fill="x", padx=(4,0))

    def _update_progress(self):
        if self._chunk_start:
            elapsed = (datetime.datetime.now() - self._chunk_start).total_seconds()
            pct = min(1.0, elapsed / max(1, self._chunk_seconds))
            self._chunk_bar.set(pct)
            rem = max(0, self._chunk_seconds - elapsed)
            self._chunk_lbl.configure(
                text=f"чанк: {int(rem//60)}:{int(rem%60):02d} осталось")

    def _update_pipeline(self, s):
        self._pipe_vals["ps-total"].configure(text=str(s.get("total_chunks",0)))
        self._pipe_vals["ps-skipped"].configure(text=str(s.get("total_silent_skipped",0)))
        self._pipe_vals["ps-sessions"].configure(text=str(s.get("sessions_merged",0)))
        avg_t = s.get("avg_transcription_s")
        avg_d = s.get("avg_diarization_s")
        self._pipe_vals["ps-avg-t"].configure(text=f'{avg_t}s' if avg_t else "—")
        self._pipe_vals["ps-avg-d"].configure(text=f'{avg_d}s' if avg_d else "—")

    def _update_load(self, s):
        def upd(name, pct, val_text):
            self._load_bars[name].set(pct / 100)
            self._load_vals[name].configure(text=val_text)

        if s.get("cpu_percent") is not None:
            upd("CPU", s["cpu_percent"], f'{s["cpu_percent"]:.0f}%')
        if s.get("ram_percent") is not None:
            upd("RAM", s["ram_percent"],
                f'{s.get("ram_used_gb","?")} / {s.get("ram_total_gb","?")} GB')
        if s.get("gpu_util_percent") is not None:
            upd("GPU", s["gpu_util_percent"],
                f'{s["gpu_util_percent"]}%  {s.get("gpu_temp_c","?")}°C')
            upd("VRAM", s.get("gpu_mem_percent",0),
                f'{round(s.get("gpu_mem_used_mb",0)/1024,1)} / '
                f'{round(s.get("gpu_mem_total_mb",0)/1024,1)} GB')

    # ── Actions ────────────────────────────────────────────────────────────

    def _start_recorder(self):
        _api("/recorder/start", "POST")
        self._recorder_running = True
        self._update_rec_btn()

    def _stop_recorder(self):
        _api("/recorder/stop", "POST")
        self._recorder_running = False
        self._update_rec_btn()

    def _update_rec_btn(self):
        if self._recorder_running:
            self._start_btn.configure(state="disabled", text_color=MUTED,
                                       border_color=MUTED, fg_color="transparent")
            self._stop_btn.configure(state="normal")
        else:
            self._start_btn.configure(state="normal", text_color=ACCENT2,
                                       border_color=ACCENT2, fg_color="#1a3a1a")
            self._stop_btn.configure(state="disabled")

    def _toggle_pipeline(self):
        self._pipeline_open = not self._pipeline_open
        if self._pipeline_open:
            self._viewer.grid_remove()
            self._pipeline_frame.grid()
            self._pipeline_btn.configure(text_color=ACCENT, border_color=ACCENT)
        else:
            self._pipeline_frame.grid_remove()
            self._viewer.grid()
            self._pipeline_btn.configure(text_color=MUTED)

    def _refresh_all(self):
        for path, handler in [
            ("/status",       self._update_status),
            ("/transcripts",  self._update_transcripts),
            ("/speakers",     self._update_speakers),
            ("/active_meetings", self._update_meetings),
        ]:
            data = _api(path)
            if data:
                self.after(0, handler, data)

    def _fetch_config(self):
        cfg = _api("/config")
        if not cfg:
            self.after(1000, self._fetch_config)
            return
        self._cfg = cfg
        self._chunk_seconds = cfg.get("chunk_seconds", 300)

        v = self._cfg_vars
        if cfg.get("whisper_model"):     v["whisper_model"].set(cfg["whisper_model"])
        if cfg.get("whisper_device"):    v["whisper_device"].set(cfg.get("whisper_device","auto"))
        lang = cfg.get("language") or "auto"
        v["language"].set(lang)

        for key in ("chunk_seconds", "cpu_threads", "gpu_memory_fraction", "max_parallel_chunks"):
            val = cfg.get(key)
            if val is not None:
                w = v[key]
                w.delete(0, "end")
                w.insert(0, str(val))

        if not cfg.get("diarization_enabled", True):
            v["diarization_enabled"].deselect()

    def _save_config(self):
        v = self._cfg_vars
        try:
            lang = v["language"].get()
            body = {
                "whisper_model":       v["whisper_model"].get(),
                "whisper_device":      v["whisper_device"].get(),
                "language":            None if lang == "auto" else lang,
                "chunk_seconds":       int(v["chunk_seconds"].get()),
                "cpu_threads":         int(v["cpu_threads"].get()),
                "gpu_memory_fraction": float(v["gpu_memory_fraction"].get()),
                "max_parallel_chunks": int(v["max_parallel_chunks"].get()),
                "diarization_enabled": bool(v["diarization_enabled"].get()),
            }
            self._chunk_seconds = body["chunk_seconds"]
            _api("/config", "POST", body)
            self._save_msg.configure(text="✓ Сохранено. Перезапустите запись.")
            self.after(3000, lambda: self._save_msg.configure(text=""))
        except Exception as e:
            self._save_msg.configure(text=f"Ошибка: {e}")

    # ── Close ──────────────────────────────────────────────────────────────

    def on_close(self):
        if self._server_proc:
            try:
                self._server_proc.terminate()
            except Exception:
                pass
        self.destroy()


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = VoiceLoggerApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
