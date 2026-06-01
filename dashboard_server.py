"""
dashboard_server.py — lightweight Flask API for the voice-logger dashboard.
Run alongside recorder.py:
    python dashboard_server.py
"""
import datetime
import json
import glob
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/dashboard.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("voice-logger.dashboard")

app = Flask(__name__, static_folder="dashboard")
CORS(app)

STATUS_FILE          = "logs/status.json"
TRANSCRIPTS_DIR      = "transcripts"
CONFIG_FILE          = "config.json"
RECORDER_PID         = "logs/recorder.pid"
FORCED_MEETING_FILE  = "logs/forced_meeting.json"
TODAY_MEETINGS_FILE  = "logs/today_meetings.json"

# ── Windows Event Log monitoring ──────────────────────────────────────────
_sys_errors_cache: list[dict] = []
_sys_errors_lock  = threading.Lock()
_sys_errors_ts    = datetime.datetime.min

# (source_lower, event_id) → (title, detail, prevention)
# event_id=None matches any event from that source.
_WIN_LABELS: dict[tuple, tuple[str, str | None, str]] = {
    ('nvlddmkm', 13): (
        'Зависание GPU (TDR)',
        'Видеодрайвер NVIDIA перестал отвечать и был перезапущен. '
        'Вероятные причины: скачок питания, перегрев GPU или нехватка VRAM.',
        '1. Установите ИБП или сетевой фильтр с защитой от скачков.\n'
        '2. Проверьте температуру GPU под нагрузкой (MSI Afterburner).\n'
        '3. Задайте лимит мощности GPU −10% в Afterburner.\n'
        '4. Обновите или откатите драйвер NVIDIA.\n'
        '5. Проверьте надёжность разъёмов питания PCIe на видеокарте.',
    ),
    ('nvlddmkm', 14): (
        'Сбой GPU — восстановление не удалось',
        'Видеодрайвер NVIDIA завис и не смог автоматически восстановиться. '
        'Потребовалась перезагрузка системы.',
        '1. Установите ИБП — блок питания не справляется с пиковой нагрузкой.\n'
        '2. Проверьте мощность PSU: GPU + CPU не должны превышать 80% от номинала.\n'
        '3. Переустановите драйвер NVIDIA через DDU (Display Driver Uninstaller).\n'
        '4. Проверьте слот PCIe и контакты видеокарты.',
    ),
    ('nvlddmkm', None): (
        'Ошибка GPU драйвера NVIDIA',
        None,
        'Обновите или переустановите драйвер NVIDIA через DDU.\n'
        'Проверьте температуру и питание GPU.',
    ),
    ('kernel-power', 41): (
        'Неожиданное выключение системы',
        'Система была выключена без корректного завершения работы. '
        'Вероятные причины: скачок питания, перегрев или полное зависание.',
        '1. Установите ИБП (источник бесперебойного питания).\n'
        '2. Проверьте блок питания: под нагрузкой напряжение не должно проседать.\n'
        '3. Проверьте температуры CPU и GPU — возможен троттлинг и зависание.\n'
        '4. Проверьте оперативную память: запустите MemTest86.',
    ),
    ('kernel-power', 109): (
        'Принудительная перезагрузка ядра',
        'Ядро Windows инициировало аварийную перезагрузку.',
        'Проверьте минидамп в C:\\Windows\\Minidump для выяснения причины.\n'
        'Запустите sfc /scannow в командной строке от администратора.',
    ),
    ('kernel-power', None): (
        'Событие питания',
        None,
        'Проверьте стабильность питания системы и состояние PSU.',
    ),
    ('bugcheck', 1001): (
        'Синий экран (BSOD)',
        'Система аварийно остановилась из-за критической ошибки и была перезагружена.',
        '1. Откройте C:\\Windows\\Minidump — там лежат дампы с кодом ошибки.\n'
        '2. Запустите: sfc /scannow и DISM /Online /Cleanup-Image /RestoreHealth\n'
        '3. Проверьте RAM через MemTest86 (ночной прогон).\n'
        '4. Проверьте диск: chkdsk C: /f /r\n'
        '5. Обновите драйверы чипсета и GPU.',
    ),
    ('application error', 1000): (
        'Падение приложения', None,
        'Проверьте логи приложения. '
        'Если падает recorder.py — проверьте журнал logs/voice-logger.log.',
    ),
    ('application error', 1002): (
        'Приложение зависло', None,
        'Настройте автоматический перезапуск через Task Scheduler или добавьте watchdog.',
    ),
    ('application hang', 1002): (
        'Приложение зависло', None,
        'Настройте автоматический перезапуск через Task Scheduler или добавьте watchdog.',
    ),
    ('win32k', None): (
        'Ошибка графической подсистемы Windows',
        'Критическая ошибка в драйвере win32k (ядро графики Windows).',
        'Обновите драйверы GPU и чипсета.\n'
        'Запустите sfc /scannow.\n'
        'Проверьте наличие обновлений Windows.',
    ),
    ('disk', None): (
        'Ошибка дискового устройства',
        'Обнаружена ошибка ввода-вывода диска.',
        '1. Скачайте CrystalDiskInfo — проверьте S.M.A.R.T. состояние диска.\n'
        '2. Запустите: chkdsk C: /f /r\n'
        '3. Если диск помечен как "Тревога" — срочно сделайте резервную копию.',
    ),
    ('storahci', None): (
        'Ошибка контроллера AHCI/NVMe',
        None,
        'Обновите драйвер контроллера хранилища в диспетчере устройств.\n'
        'Проверьте кабели SATA или слот M.2.',
    ),
}

_WIN_WATCH = {
    'nvlddmkm', 'kernel-power', 'bugcheck',
    'application error', 'application hang',
    'win32k', 'disk', 'storahci', 'iastoravc',
}


def _win_label(source: str, event_id: int, raw: str) -> tuple[str, str, str]:
    src = source.lower()
    for (ks, ke), (title, detail, prevention) in _WIN_LABELS.items():
        if ks == src and (ke is None or ke == event_id):
            if detail is None:
                m = (re.search(r'Faulting application name:\s*([^\r\n,]+)', raw)
                     or re.search(r'Имя сбойного приложения:\s*([^\r\n,]+)', raw))
                detail = f'Процесс «{m.group(1).strip()}» аварийно завершился.' if m else raw[:150]
            return title, detail, prevention
    return (f'Системная ошибка [{source}]',
            raw[:150] if raw else f'EventID {event_id}',
            'Обратитесь к документации Windows Event Log для EventID ' + str(event_id) + '.')


def _refresh_win_events():
    global _sys_errors_cache, _sys_errors_ts
    since = datetime.datetime.now() - datetime.timedelta(hours=24)
    found: list[dict] = []
    try:
        import win32evtlog, win32evtlogutil
        for log_name in ('System', 'Application'):
            try:
                h = win32evtlog.OpenEventLog(None, log_name)
                flags = (win32evtlog.EVENTLOG_BACKWARDS_READ |
                         win32evtlog.EVENTLOG_SEQUENTIAL_READ)
                stop = False
                while not stop:
                    recs = win32evtlog.ReadEventLog(h, flags, 0)
                    if not recs:
                        break
                    for rec in recs:
                        try:
                            t = rec.TimeGenerated
                            rec_dt = datetime.datetime(
                                t.year, t.month, t.day, t.hour, t.minute, t.second)
                        except Exception:
                            continue
                        if rec_dt < since:
                            stop = True
                            break
                        if rec.EventType not in (
                                win32evtlog.EVENTLOG_ERROR_TYPE,
                                win32evtlog.EVENTLOG_WARNING_TYPE):
                            continue
                        src = (rec.SourceName or '').strip()
                        if not any(w in src.lower() for w in _WIN_WATCH):
                            continue
                        try:
                            raw = win32evtlogutil.SafeFormatMessage(rec, log_name) or ''
                        except Exception:
                            raw = ''
                        eid = rec.EventID & 0xFFFF
                        title, detail, prevention = _win_label(src, eid, raw.strip())
                        found.append({
                            'time':       rec_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                            'source':     src,
                            'event_id':   eid,
                            'log':        log_name,
                            'title':      title,
                            'detail':     detail,
                            'prevention': prevention,
                            'severity':   ('critical'
                                           if rec.EventType == win32evtlog.EVENTLOG_ERROR_TYPE
                                           else 'warn'),
                        })
                win32evtlog.CloseEventLog(h)
            except Exception:
                pass
    except ImportError:
        pass
    except Exception:
        pass

    # Deduplicate by (source, event_id, minute)
    seen: set = set()
    deduped: list[dict] = []
    for e in found:
        key = (e['source'], e['event_id'], e['time'][:15])
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    deduped.sort(key=lambda e: e['time'], reverse=True)

    with _sys_errors_lock:
        _sys_errors_cache = deduped[:30]
        _sys_errors_ts    = datetime.datetime.now()


def _win_events_loop():
    _refresh_win_events()
    while True:
        time.sleep(60)
        _refresh_win_events()


threading.Thread(target=_win_events_loop, daemon=True, name='win-events').start()


# ── Recorder process management ────────────────────────────────────────────
def _get_recorder_pid() -> int | None:
    try:
        pid = int(Path(RECORDER_PID).read_text().strip())
        # Verify process is still alive
        import psutil
        if psutil.pid_exists(pid):
            return pid
    except Exception:
        pass
    return None


@app.post("/api/recorder/start")
def recorder_start():
    if _get_recorder_pid():
        return jsonify({"ok": True, "msg": "already running"})
    try:
        py = Path(CONFIG_FILE).parent / "python_path.txt"
        python = py.read_text().strip() if py.exists() else sys.executable
        proc = subprocess.Popen(
            [python, "recorder.py"],
            cwd=str(Path(CONFIG_FILE).parent),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        Path(RECORDER_PID).write_text(str(proc.pid))
        return jsonify({"ok": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/recorder/stop")
def recorder_stop():
    pid = _get_recorder_pid()
    if not pid:
        return jsonify({"ok": True, "msg": "not running"})
    try:
        # Write stop-request file; recorder.py picks it up, exits the recording
        # loop gracefully, waits for in-flight transcription threads, then quits.
        stop_file = Path("logs/stop_requested.txt")
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text(str(pid))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _do_cleanup() -> dict:
    """Clean up ghost speakers, amplify clips, reset stuck pipeline counters."""
    result = {}
    try:
        from speakers import cleanup_speakers_without_clips
        result["speakers_removed"] = cleanup_speakers_without_clips()
    except Exception as e:
        result["speakers_error"] = str(e)

    try:
        from labeler import amplify_existing_clips
        result["clips_amplified"] = amplify_existing_clips()
    except Exception as e:
        result["clips_error"] = str(e)

    try:
        stats_path = Path("logs/pipeline_stats.json")
        if stats_path.exists():
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            if stats.get("active_processing", 0) > 0:
                stats["active_processing"] = 0
                stats.pop("active_chunk_starts", None)
                stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
                result["pipeline_reset"] = True
    except Exception as e:
        result["pipeline_error"] = str(e)

    try:
        st_path = Path(STATUS_FILE)
        if st_path.exists():
            st = json.loads(st_path.read_text(encoding="utf-8"))
            st["errors"] = []
            st_path.write_text(json.dumps(st, indent=2, default=str), encoding="utf-8")
            result["errors_cleared"] = True
    except Exception as e:
        result["status_error"] = str(e)

    result["ok"] = True
    return result


@app.post("/api/recorder/restart")
def recorder_restart():
    """Force-kill the recorder, run cleanup, then start fresh."""
    import time as _time
    _do_cleanup()
    pid = _get_recorder_pid()
    if pid:
        try:
            import psutil
            proc = psutil.Process(pid)
            proc.kill()
            try:
                proc.wait(timeout=8)
            except psutil.TimeoutExpired:
                pass
        except Exception:
            pass
        for f in ("logs/recorder.pid", "logs/stop_requested.txt", "logs/reload_whisper.txt"):
            Path(f).unlink(missing_ok=True)
        _time.sleep(5)
    return recorder_start()


@app.post("/api/cleanup")
def cleanup_service():
    return jsonify(_do_cleanup())


_CONTROL_FILE = "logs/recorder_control.json"

def _read_control() -> dict:
    try:
        p = Path(_CONTROL_FILE)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _write_control(**kwargs):
    ctrl = _read_control()
    ctrl.update(kwargs)
    Path(_CONTROL_FILE).parent.mkdir(exist_ok=True)
    Path(_CONTROL_FILE).write_text(json.dumps(ctrl, indent=2), encoding="utf-8")


@app.post("/api/recorder/recording/pause")
def recording_pause():
    _write_control(recording_paused=True)
    return jsonify({"ok": True})

@app.post("/api/recorder/recording/resume")
def recording_resume():
    _write_control(recording_paused=False)
    return jsonify({"ok": True})

@app.post("/api/recorder/processing/pause")
def processing_pause():
    _write_control(processing_paused=True)
    return jsonify({"ok": True})

@app.post("/api/recorder/processing/resume")
def processing_resume():
    _write_control(processing_paused=False)
    return jsonify({"ok": True})


@app.post("/api/reload_whisper")
def reload_whisper():
    if not _get_recorder_pid():
        return jsonify({"ok": False, "msg": "recorder not running"})
    f = Path("logs/reload_whisper.txt")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("1")
    return jsonify({"ok": True})


@app.get("/api/recorder/status")
def recorder_running():
    pid = _get_recorder_pid()
    return jsonify({"running": pid is not None, "pid": pid})


def _is_cancelled_event(event: dict) -> bool:
    """Return True if the calendar event is a cancellation notice."""
    if not event:
        return False
    subj = str(event.get("subject", "") or "").lower()
    subj_norm = subj.replace('\u0441', 'c')   # кириллическая с → латинская c
    return (
        "cancelled" in subj_norm or "canceled" in subj_norm
        or "отменена" in subj or "отменен" in subj
    )


@app.get("/api/state")
def get_state():
    """Unified state: status + pipeline_stats + system_load + recorder_running."""
    result = {}

    # ── Recorder running? ──────────────────────────────────────────────────
    pid = _get_recorder_pid()
    result["recorder_running"] = pid is not None

    # ── Status (from status.json) ──────────────────────────────────────────
    if Path(STATUS_FILE).exists():
        try:
            result.update(json.loads(Path(STATUS_FILE).read_text()))
        except Exception:
            pass
    if not pid:
        result["state"] = "idle"
    ctrl = _read_control()
    result["recording_paused"]  = ctrl.get("recording_paused",  False)
    result["processing_paused"] = ctrl.get("processing_paused", False)
    if _is_cancelled_event(result.get("last_calendar_event")):
        result["last_calendar_event"] = None

    # ── Pipeline stats ─────────────────────────────────────────────────────
    stats_path = Path("logs/pipeline_stats.json")
    if stats_path.exists():
        try:
            result.update(json.loads(stats_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    recent = result.get("recent", [])
    if recent:
        chunks_with_audio = [c for c in recent[-10:] if c.get("audio_s", 0) > 0]
        if chunks_with_audio:
            result["avg_transcription_rtf"] = round(
                sum(c["transcription_s"] / c["audio_s"] for c in chunks_with_audio)
                / len(chunks_with_audio), 2
            )
        result["avg_transcription_s"] = round(sum(c.get("transcription_s", 0) for c in recent) / len(recent), 1)
        result["avg_diarization_s"]   = round(sum(c.get("diarization_s",   0) for c in recent) / len(recent), 1)
        result["avg_total_s"]         = round(sum(c.get("total_s",         0) for c in recent) / len(recent), 1)
    sessions_dir = Path("transcripts/sessions")
    result["sessions_merged"] = len(list(sessions_dir.glob("*.txt"))) if sessions_dir.exists() else 0
    try:
        merged_set = set(json.loads(Path("logs/merged_chunks.json").read_text())) \
            if Path("logs/merged_chunks.json").exists() else set()
        result["chunks_pending_merge"] = sum(
            1 for p in Path("transcripts").rglob("*.txt")
            if "sessions" not in p.parts and "permanent" not in p.parts
            and str(p) not in merged_set
        )
    except Exception:
        result["chunks_pending_merge"] = 0

    # ── Pending WAVs (recorded but not yet transcribed) ───────────────────
    try:
        import datetime as _dt
        today = _dt.date.today().strftime("%Y-%m-%d")
        rec_dir = Path("recordings")
        tx_dir  = Path("transcripts") / today
        pending = []
        if rec_dir.exists():
            for wav in rec_dir.glob("*.wav"):
                m = __import__('re').match(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", wav.stem)
                if not m:
                    continue
                y, mo, d, h, mi, s = m.groups()
                if f"{y}-{mo}-{d}" != today:
                    continue
                tx = tx_dir / f"{today}_{h}-{mi}-{s}.txt"
                if not tx.exists():
                    pending.append(f"{y}-{mo}-{d}T{h}:{mi}:{s}")
        result["pending_wavs"] = pending
    except Exception:
        result["pending_wavs"] = []

    # ── System load ────────────────────────────────────────────────────────
    try:
        import psutil
        result["cpu_percent"]  = psutil.cpu_percent(interval=0.15)
        vm = psutil.virtual_memory()
        result["ram_percent"]  = vm.percent
        result["ram_used_gb"]  = round(vm.used  / 1e9, 1)
        result["ram_total_gb"] = round(vm.total / 1e9, 1)
    except Exception:
        pass
    try:
        import subprocess as _sp
        r = _sp.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = [x.strip() for x in r.stdout.strip().split(",")]
            result["gpu_util_percent"] = int(parts[0])
            result["gpu_mem_used_mb"]  = int(parts[1])
            result["gpu_mem_total_mb"] = int(parts[2])
            result["gpu_temp_c"]       = int(parts[3])
            result["gpu_mem_percent"]  = round(int(parts[1]) / max(int(parts[2]), 1) * 100, 1)
    except Exception:
        pass

    with _sys_errors_lock:
        result['system_errors'] = list(_sys_errors_cache)

    return jsonify(result)


@app.get("/api/status")
def get_status():
    if not _get_recorder_pid():
        # Recorder is not running — always report idle regardless of stale status file
        status = {}
        if Path(STATUS_FILE).exists():
            try:
                status = json.loads(Path(STATUS_FILE).read_text())
            except Exception:
                pass
        status["state"] = "idle"
        if _is_cancelled_event(status.get("last_calendar_event")):
            status["last_calendar_event"] = None
        return jsonify(status)
    if Path(STATUS_FILE).exists():
        try:
            status = json.loads(Path(STATUS_FILE).read_text())
            if _is_cancelled_event(status.get("last_calendar_event")):
                status["last_calendar_event"] = None
            return jsonify(status)
        except Exception:
            pass
    return jsonify({"state": "unknown"})


@app.get("/api/transcripts")
def list_transcripts():
    tx_root = Path(TRANSCRIPTS_DIR)
    result = []

    def _entry(f):
        p = Path(f)
        return {
            "path":     f,
            "name":     p.name,
            "date":     p.parent.name,
            "size_kb":  round(p.stat().st_size / 1024, 1),
            "modified": p.stat().st_mtime,
        }

    # Chunk files: date-named subdirectories (YYYY-MM-DD)
    import re as _re
    chunk_files = sorted([
        f for f in glob.glob(f"{TRANSCRIPTS_DIR}/**/*.txt", recursive=True)
        + glob.glob(f"{TRANSCRIPTS_DIR}/**/*.md", recursive=True)
        if _re.match(r"\d{4}-\d{2}-\d{2}$", Path(f).parent.name)
    ], reverse=True)[:200]

    # Session files
    session_files = sorted(
        glob.glob(f"{TRANSCRIPTS_DIR}/sessions/*.txt") +
        glob.glob(f"{TRANSCRIPTS_DIR}/sessions/*.md"),
        reverse=True,
    )[:100]

    seen = set()
    for f in chunk_files + session_files:
        if f not in seen:
            seen.add(f)
            result.append(_entry(f))

    return jsonify(result)


@app.get("/api/transcript")
def read_transcript():
    path = request.args.get("path", "")
    p    = Path(path)
    # Safety: only serve files inside transcripts dir
    if not str(p.resolve()).startswith(str(Path(TRANSCRIPTS_DIR).resolve())):
        return jsonify({"error": "forbidden"}), 403
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify({"content": p.read_text(encoding="utf-8")})


@app.post("/api/transcript/relabel_block")
def relabel_block():
    """Replace speaker name on a single block (line) in a transcript file."""
    import re
    data        = request.get_json() or {}
    path        = data.get("path", "")
    line_no     = data.get("line_no", -1)
    new_speaker = (data.get("new_speaker") or "").strip()
    if not path or line_no < 0 or not new_speaker:
        return jsonify({"ok": False, "error": "invalid params"}), 400
    p = Path(path)
    if not str(p.resolve()).startswith(str(Path(TRANSCRIPTS_DIR).resolve())):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if not p.exists():
        return jsonify({"ok": False, "error": "not found"}), 404
    lines = p.read_text(encoding="utf-8").split("\n")
    if line_no >= len(lines):
        return jsonify({"ok": False, "error": "line out of range"}), 400
    lines[line_no] = re.sub(r"^\*\*\[.+?\]\*\*", f"**[{new_speaker}]**", lines[line_no])
    p.write_text("\n".join(lines), encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/api/config")
def get_config():
    if Path(CONFIG_FILE).exists():
        return jsonify(json.loads(Path(CONFIG_FILE).read_text()))
    return jsonify({})


@app.post("/api/config")
def set_config():
    data = request.get_json()
    allowed = {
        "whisper_model", "language", "chunk_seconds",
        "silence_threshold", "silence_min_seconds",
        "keep_audio", "diarization_enabled",
        "mic_gain", "loopback_gain", "whisper_device",
        "cpu_threads", "ram_limit_gb", "gpu_memory_fraction", "max_parallel_chunks",
    }
    if Path(CONFIG_FILE).exists():
        cfg = json.loads(Path(CONFIG_FILE).read_text())
    else:
        cfg = {}
    for k, v in data.items():
        if k in allowed:
            cfg[k] = v
    Path(CONFIG_FILE).write_text(json.dumps(cfg, indent=2))
    return jsonify({"ok": True})


@app.get("/api/speakers")
def get_speakers():
    try:
        from speakers import list_speakers
        return jsonify(list_speakers())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/speakers/<speaker_id>/rename")
def rename_speaker(speaker_id):
    data = request.get_json()
    new_name = (data or {}).get("name", "").strip()
    if not new_name:
        return jsonify({"error": "name required"}), 400
    try:
        from speakers import rename_speaker as _rename
        ok = _rename(speaker_id, new_name)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.delete("/api/speakers/<speaker_id>")
def delete_speaker(speaker_id):
    try:
        from speakers import delete_speaker as _delete
        ok = _delete(speaker_id)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/speakers/similar")
def get_similar_speakers():
    try:
        from speakers import find_similar_pairs, cleanup_speakers_without_clips
        cleanup_speakers_without_clips()  # prune ghosts before computing pairs
        pairs = find_similar_pairs()
        return jsonify(pairs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/speakers/merge")
def merge_speakers_api():
    data = request.get_json() or {}
    keep_id  = data.get("keep_id", "").strip()
    drop_id  = data.get("drop_id", "").strip()
    new_name = data.get("new_name", "").strip()
    if not keep_id or not drop_id:
        return jsonify({"error": "keep_id and drop_id required"}), 400
    try:
        from speakers import merge_speakers as _merge, rename_speaker as _rename
        ok = _merge(keep_id, drop_id)
        if ok and new_name:
            _rename(keep_id, new_name)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/speakers/dismiss_pair")
def dismiss_pair_api():
    data = request.get_json() or {}
    id_a = data.get("id_a", "").strip()
    id_b = data.get("id_b", "").strip()
    if not id_a or not id_b:
        return jsonify({"error": "id_a and id_b required"}), 400
    try:
        from speakers import dismiss_pair as _dismiss
        _dismiss(id_a, id_b)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Voice enrollment ───────────────────────────────────────────────────────
_ENROLL_SECONDS = 20
_ENROLL_SR      = 16000
_enroll_lock    = threading.Lock()
_enroll_state: dict = {"status": "idle", "progress": 0.0, "samples": 0,
                       "message": "", "name": ""}
_enroll_stop    = threading.Event()


def _owner_samples() -> tuple[str, int]:
    """Return (owner_name, sample_count) from registry, or ('', 0)."""
    try:
        from speakers import get_owner
        owner = get_owner()
        if owner:
            return owner.get("name", ""), owner.get("samples", 0)
    except Exception:
        pass
    return "", 0


def _enroll_thread(name: str):
    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    temp_wav = Path("speakers/enroll_temp.wav")
    _enroll_stop.clear()

    try:
        frames = _ENROLL_SECONDS * _ENROLL_SR
        recording = sd.rec(frames, samplerate=_ENROLL_SR, channels=1, dtype="float32")

        start = time.time()
        while True:
            elapsed = time.time() - start
            with _enroll_lock:
                _enroll_state["progress"] = min(1.0, elapsed / _ENROLL_SECONDS)
                if _enroll_stop.is_set():
                    sd.stop()
                    _enroll_state["status"] = "idle"
                    _enroll_state["progress"] = 0.0
                    return
            if elapsed >= _ENROLL_SECONDS:
                break
            time.sleep(0.1)

        sd.wait()
        with _enroll_lock:
            _enroll_state["status"] = "processing"

        Path("speakers").mkdir(exist_ok=True)
        sf.write(str(temp_wav), recording.flatten(), _ENROLL_SR)

        import torchaudio_compat  # noqa — must patch before pyannote
        from speakers import enroll_speaker, get_owner
        enroll_speaker(temp_wav, name=name, is_owner=True)
        _, samples = _owner_samples()

        with _enroll_lock:
            _enroll_state["status"]   = "done"
            _enroll_state["samples"]  = samples
            _enroll_state["progress"] = 1.0
            _enroll_state["message"]  = f"Образец добавлен. Всего: {samples}"
    except Exception as e:
        with _enroll_lock:
            _enroll_state["status"]  = "error"
            _enroll_state["message"] = str(e)
    finally:
        temp_wav.unlink(missing_ok=True)


@app.get("/api/enroll/status")
def enroll_status():
    name, samples = _owner_samples()
    with _enroll_lock:
        state = dict(_enroll_state)
    state["owner_name"]    = name
    state["owner_samples"] = samples
    return jsonify(state)


@app.post("/api/enroll/start")
def enroll_start():
    with _enroll_lock:
        if _enroll_state["status"] in ("recording", "processing"):
            return jsonify({"error": "Запись уже идёт"}), 409
        name = (request.get_json() or {}).get("name", "").strip()
        if not name:
            return jsonify({"error": "Имя обязательно"}), 400
        _enroll_state.update({"status": "recording", "progress": 0.0,
                               "message": "", "name": name})
    t = threading.Thread(target=_enroll_thread, args=(name,), daemon=True)
    t.start()
    return jsonify({"ok": True, "duration": _ENROLL_SECONDS})


@app.post("/api/enroll/cancel")
def enroll_cancel():
    _enroll_stop.set()
    return jsonify({"ok": True})


# ── Labeling endpoints ─────────────────────────────────────────────────────
@app.get("/api/pending_labels")
def get_pending_labels():
    try:
        import labeler as lbl
        return jsonify(lbl.get_pending())
    except Exception as e:
        return jsonify([])


@app.post("/api/label_speaker")
def label_speaker():
    data = request.get_json() or {}
    request_id       = data.get("request_id", "")
    name             = data.get("name", "").strip()
    dismiss          = data.get("dismiss", False)
    target_speaker_id = data.get("target_speaker_id") or None
    try:
        import labeler as lbl
        if dismiss:
            lbl.dismiss(request_id)
            return jsonify({"ok": True, "dismissed": True})
        if not name:
            return jsonify({"error": "name required"}), 400
        ok = lbl.submit_answer(request_id, name, target_speaker_id=target_speaker_id)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/speaker_clip/<path:clip_path>")
def serve_speaker_clip(clip_path):
    """Serve a short WAV clip for playback in the labeling UI."""
    from flask import send_file
    full = Path(clip_path)
    if not str(full.resolve()).startswith(str(Path("speakers/clips").resolve())):
        return jsonify({"error": "forbidden"}), 403
    if not full.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(full), mimetype="audio/wav")


@app.get("/api/speakers/clip")
def serve_speaker_clip_qs():
    """Serve a clip by query-string path: ?path=speakers/clips/xxx.wav"""
    from flask import send_file
    clip_path = request.args.get("path", "")
    full = Path(clip_path)
    if not str(full.resolve()).startswith(str(Path("speakers/clips").resolve())):
        return jsonify({"error": "forbidden"}), 403
    if not full.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(full), mimetype="audio/wav")


# ── Device discovery ───────────────────────────────────────────────────────
@app.get("/api/audio_devices")
def get_audio_devices():
    try:
        from audio_capture import list_audio_devices, find_loopback_device
        devices  = list_audio_devices()
        loopback = find_loopback_device()
        return jsonify({"devices": devices, "detected_loopback": loopback})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/pipeline_stats")
def get_pipeline_stats():
    result = {}

    # Core pipeline stats written by recorder.py
    stats_path = Path("logs/pipeline_stats.json")
    if stats_path.exists():
        try:
            result = json.loads(stats_path.read_text(encoding="utf-8"))
        except Exception:
            result = {}

    # Count merged sessions
    sessions_dir = Path("transcripts/sessions")
    result["sessions_merged"] = (
        len(list(sessions_dir.glob("*.txt"))) if sessions_dir.exists() else 0
    )

    # Count chunk files not yet merged (pending merge queue)
    try:
        merged_set: set = set()
        merged_log = Path("logs/merged_chunks.json")
        if merged_log.exists():
            merged_set = set(json.loads(merged_log.read_text()))
        pending = [
            p for p in Path("transcripts").rglob("*.txt")
            if "sessions" not in p.parts and "permanent" not in p.parts
            and str(p) not in merged_set
        ]
        result["chunks_pending_merge"] = len(pending)
    except Exception:
        result["chunks_pending_merge"] = 0

    # Compute averages from recent chunks
    recent = result.get("recent", [])
    if recent:
        chunks_with_audio = [c for c in recent[-10:] if c.get("audio_s", 0) > 0]
        if chunks_with_audio:
            result["avg_transcription_rtf"] = round(
                sum(c["transcription_s"] / c["audio_s"] for c in chunks_with_audio) / len(chunks_with_audio), 2
            )
        result["avg_transcription_s"] = round(
            sum(c.get("transcription_s", 0) for c in recent) / len(recent), 1
        )
        result["avg_diarization_s"] = round(
            sum(c.get("diarization_s", 0) for c in recent) / len(recent), 1
        )
        result["avg_total_s"] = round(
            sum(c.get("total_s", 0) for c in recent) / len(recent), 1
        )

    return jsonify(result)


@app.get("/api/system_load")
def get_system_load():
    result = {}

    # CPU + RAM via psutil
    try:
        import psutil
        result["cpu_percent"]  = psutil.cpu_percent(interval=0.15)
        vm = psutil.virtual_memory()
        result["ram_percent"]  = vm.percent
        result["ram_used_gb"]  = round(vm.used  / 1e9, 1)
        result["ram_total_gb"] = round(vm.total / 1e9, 1)
    except Exception:
        pass

    # GPU via nvidia-smi (optional — no GPU on this machine yet)
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = [x.strip() for x in r.stdout.strip().split(",")]
            result["gpu_util_percent"]  = int(parts[0])
            result["gpu_mem_used_mb"]   = int(parts[1])
            result["gpu_mem_total_mb"]  = int(parts[2])
            result["gpu_temp_c"]        = int(parts[3])
            result["gpu_mem_percent"]   = round(
                int(parts[1]) / max(int(parts[2]), 1) * 100, 1
            )
    except Exception:
        pass

    return jsonify(result)


# ── Session conflicts (overlapping calendar events) ────────────────────────

@app.get("/api/session_conflicts")
def get_session_conflicts():
    p = Path("logs/session_conflicts.json")
    if not p.exists():
        return jsonify([])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        unresolved = [{"id": k, **v} for k, v in data.items() if not v.get("resolved")]
        return jsonify(unresolved)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/resolve_conflict")
def resolve_conflict():
    data        = request.get_json() or {}
    conflict_id = data.get("conflict_id", "")
    to_event    = data.get("to_event")    # None = keep current assignment
    dismiss     = data.get("dismiss", False)

    p = Path("logs/session_conflicts.json")
    if not p.exists():
        return jsonify({"ok": False, "error": "no conflicts file"}), 404

    try:
        conflicts = json.loads(p.read_text(encoding="utf-8"))
        if conflict_id not in conflicts:
            return jsonify({"ok": False, "error": "conflict not found"}), 404

        conflict = conflicts[conflict_id]

        if dismiss or to_event is None:
            # Keep current assignment — just mark resolved
            conflict["resolved"]    = True
            conflict["resolved_at"] = datetime.datetime.now().isoformat()
            p.write_text(json.dumps(conflicts, indent=2, ensure_ascii=False), encoding="utf-8")
            return jsonify({"ok": True})

        # Reassign session to the chosen alternative event's category
        import merger as mgr
        from_event = conflict["chosen_event"]
        ok = mgr.reassign_session(
            conflict_id  = conflict_id,
            session_path = conflict["session_path"],
            from_event   = from_event,
            to_event     = to_event,
        )
        if ok:
            from_cat = mgr._category_key(from_event.get("categories", ""))
            to_cat   = mgr._category_key(to_event.get("categories", ""))
            for cat in {from_cat, to_cat}:
                mgr.rebuild_permanent_file_standalone(cat)
        return jsonify({"ok": ok})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Meetings registry (read from recorder-maintained cache file) ────────────

def _read_today_meetings() -> list[dict]:
    """Return today's meetings from the cache file, refreshing it if stale."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    fp = Path(TODAY_MEETINGS_FILE)
    try:
        if fp.exists():
            data = json.loads(fp.read_text(encoding="utf-8"))
            if data.get("date") == today:
                return data.get("meetings", [])
    except Exception:
        pass
    # File missing or from a previous day — bootstrap from Outlook
    _refresh_meetings_cache(today)
    try:
        if fp.exists():
            data = json.loads(fp.read_text(encoding="utf-8"))
            return data.get("meetings", [])
    except Exception:
        pass
    return []


def _refresh_meetings_cache(today_str: str):
    """Query Outlook and write TODAY_MEETINGS_FILE with naive local ISO times."""
    try:
        import win32com.client, pythoncom
        pythoncom.CoInitialize()
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns      = outlook.GetNamespace("MAPI")
        cal     = ns.GetDefaultFolder(9)
        items   = cal.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")
        today     = datetime.date.today()
        day_start = datetime.datetime.combine(today, datetime.time(0, 0))
        day_end   = datetime.datetime.combine(today, datetime.time(23, 59))
        restriction = (
            f"[Start] >= '{day_start.strftime('%m/%d/%Y %H:%M')}' "
            f"AND [Start] <= '{day_end.strftime('%m/%d/%Y %H:%M')}'"
        )
        meetings = []
        for item in items.Restrict(restriction):
            try:
                subj     = str(getattr(item, "Subject", "") or "")
                subj_low = subj.lower().replace('с', 'c')
                if "cancelled" in subj_low or "canceled" in subj_low \
                        or "отменена" in subj_low or "отменен" in subj_low:
                    continue
                start = item.Start.replace(tzinfo=None) if hasattr(item.Start, 'replace') else item.Start
                end   = item.End.replace(tzinfo=None)   if hasattr(item.End,   'replace') else item.End
                meetings.append({
                    "subject":    subj,
                    "start":      start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "end":        end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "organizer":  getattr(item, "Organizer", "") or "",
                    "attendees":  getattr(item, "RequiredAttendees", "") or "",
                    "location":   getattr(item, "Location", "") or "",
                    "categories": getattr(item, "Categories", "") or "",
                })
            except Exception:
                continue
        data = {
            "date":       today_str,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "meetings":   meetings,
        }
        Path(TODAY_MEETINGS_FILE).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _get_active_outlook_events() -> list[dict]:
    """Return meetings from the cache that are currently active."""
    now = datetime.datetime.now()
    result = []
    for mtg in _read_today_meetings():
        try:
            start = datetime.datetime.strptime(mtg["start"], "%Y-%m-%dT%H:%M:%S")
            end   = datetime.datetime.strptime(mtg["end"],   "%Y-%m-%dT%H:%M:%S")
            if start <= now <= end:
                result.append(mtg)
        except Exception:
            continue
    return result


@app.get("/api/today_meetings")
def get_today_meetings():
    """Return all today's meetings from the cache file."""
    return jsonify({"meetings": _read_today_meetings()})


@app.get("/api/active_meetings")
def get_active_meetings():
    events = _get_active_outlook_events()
    forced = None
    try:
        fp = Path(FORCED_MEETING_FILE)
        if fp.exists():
            forced = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        pass
    return jsonify({"meetings": events, "forced": forced})


@app.post("/api/select_meeting")
def select_meeting():
    data  = request.get_json() or {}
    event = data.get("event")   # None → clear forced selection
    fp    = Path(FORCED_MEETING_FILE)
    fp.parent.mkdir(parents=True, exist_ok=True)
    if event is None:
        fp.unlink(missing_ok=True)
    else:
        fp.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/api/diagnostics")
def get_diagnostics():
    import shutil as _shutil, time as _time

    phase = request.args.get("phase", "pipeline")  # init | recording | pipeline

    uptime_s = None
    try:
        uptime_s = _time.time() - Path("logs/recorder.pid").stat().st_mtime
    except Exception:
        pass
    starting = uptime_s is not None and uptime_s < 90  # 90s grace window after start

    checks = []

    def _check(key, name, fn, only_phases=None):
        if only_phases and phase not in only_phases:
            return
        try:
            status, detail = fn()
        except Exception as exc:
            status, detail = "unknown", str(exc)[:80]
        checks.append({"key": key, "name": name, "status": status, "detail": detail})

    def _starting(msg):
        return "unknown", f"запускается ({int(uptime_s or 0)}с) · {msg}"

    # ── Whisper ───────────────────────────────────────────────────────────────
    def chk_whisper():
        cfg_d  = json.loads(Path(CONFIG_FILE).read_text()) if Path(CONFIG_FILE).exists() else {}
        model  = cfg_d.get("whisper_model", "?")
        st     = json.loads(Path(STATUS_FILE).read_text()) if Path(STATUS_FILE).exists() else {}
        loaded = st.get("model_loaded", False)
        device = st.get("whisper_device") or cfg_d.get("whisper_device", "auto")
        if not loaded:
            return _starting(f"загружает модель {model}") if starting else ("warn", f"модель {model} не загружена")
        stats_path = Path("logs/pipeline_stats.json")
        recent = []
        if stats_path.exists():
            recent = [c for c in json.loads(stats_path.read_text(encoding="utf-8")).get("recent", [])
                      if c.get("audio_s", 0) > 0]
        rtf_str, status = "", "ok"
        if recent:
            rtf = sum(c["transcription_s"] / c["audio_s"] for c in recent[-10:]) / min(len(recent), 10)
            rtf_str = f" · RTF {rtf:.2f}x"
            status = "ok" if rtf < 0.5 else ("warn" if rtf < 1.0 else "error")
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        gpu_crashes = 0
        log_path = Path("logs/voice-logger.log")
        if log_path.exists():
            for ln in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if today_str in ln and "GPU error during transcription" in ln:
                    gpu_crashes += 1
        crash_str = f" · ⚠ GPU падал {gpu_crashes}× сегодня" if gpu_crashes else ""
        if gpu_crashes and device == "cpu":
            status = "warn"
        return status, f"{model} · {device}{rtf_str}{crash_str}"

    _check("whisper", "Whisper", chk_whisper)

    # ── Запись активна (recording/pipeline) ───────────────────────────────────
    def chk_recording():
        st     = json.loads(Path(STATUS_FILE).read_text()) if Path(STATUS_FILE).exists() else {}
        state  = st.get("state", "")
        paused = st.get("recording_paused", False)
        if paused:
            return "warn", "запись на паузе"
        if state in ("recording", "standby", "transcribing", "diarizing", "uploading"):
            chunk_start = st.get("current_chunk_start", "")
            if chunk_start:
                age = (datetime.datetime.now() - datetime.datetime.fromisoformat(chunk_start)).total_seconds()
                return "ok", f"идёт запись · чанк {int(age)}с"
            return "ok", "запись активна"
        if state == "loading_model":
            return _starting("модель загружается") if starting else ("warn", "модель не загружена")
        return "warn", f"состояние: {state or 'неизвестно'}"

    _check("recording", "Запись", chk_recording, only_phases=["recording", "pipeline"])

    # ── Diarization ───────────────────────────────────────────────────────────
    def chk_diarization():
        cfg_d = json.loads(Path(CONFIG_FILE).read_text()) if Path(CONFIG_FILE).exists() else {}
        if not cfg_d.get("diarization_enabled", True):
            return "warn", "отключена в настройках"
        if not cfg_d.get("hf_token", ""):
            return "error", "hf_token не задан — диаризация не запустится"
        stats_path = Path("logs/pipeline_stats.json")
        if not stats_path.exists():
            return ("unknown", "ждём первого чанка") if phase != "pipeline" else ("unknown", "нет статистики")
        recent = json.loads(stats_path.read_text(encoding="utf-8")).get("recent", [])
        if not recent:
            return ("unknown", "ждём первого чанка") if phase != "pipeline" else ("unknown", "нет данных")
        avg_di   = sum(c.get("diarization_s", 0) for c in recent) / len(recent)
        last_spk = recent[-1].get("speakers", "?")
        return "ok", f"включена · avg {avg_di:.0f}с/чанк · {last_spk} спикеров в посл. чанке"

    _check("diarization", "Диаризация", chk_diarization)

    # ── Пайплайн (только pipeline) ────────────────────────────────────────────
    def chk_pipeline():
        st      = json.loads(Path(STATUS_FILE).read_text()) if Path(STATUS_FILE).exists() else {}
        tx      = st.get("transcripts_today", 0)
        last_tx = (st.get("last_transcript") or "")[:16]
        if tx == 0:
            return "warn", "ни одного чанка за сегодня"
        stats_path = Path("logs/pipeline_stats.json")
        if stats_path.exists():
            recent = json.loads(stats_path.read_text(encoding="utf-8")).get("recent", [])
            if recent:
                c = recent[-1]
                return "ok", (f"{tx} чанков сегодня · посл: "
                              f"{c.get('transcription_s',0):.0f}с тр + "
                              f"{c.get('diarization_s',0):.0f}с диар = "
                              f"{c.get('total_s',0):.0f}с · {last_tx}")
        return "ok", f"{tx} чанков сегодня"

    _check("pipeline", "Пайплайн", chk_pipeline, only_phases=["pipeline"])

    # ── Outlook Calendar ──────────────────────────────────────────────────────
    def chk_calendar():
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        fp = Path(TODAY_MEETINGS_FILE)
        if not fp.exists():
            return _starting("загружает календарь") if starting else ("warn", "кэш событий не найден")
        data = json.loads(fp.read_text(encoding="utf-8"))
        if data.get("date") != today_str:
            return "warn", f"кэш устарел (от {data.get('date', '?')})"
        meetings = data.get("meetings", [])
        updated  = data.get("updated_at", "")[:16]
        return "ok", f"{len(meetings)} событий сегодня · обновлено {updated}"

    _check("calendar", "Outlook Calendar", chk_calendar)

    # ── Obsidian ──────────────────────────────────────────────────────────────
    def chk_obsidian():
        import urllib.request as _ur, ssl as _ssl
        cfg_d = json.loads(Path(CONFIG_FILE).read_text()) if Path(CONFIG_FILE).exists() else {}
        url   = cfg_d.get("obsidian_url", "").rstrip("/")
        key   = cfg_d.get("obsidian_api_key", "")
        if not url or not key:
            return "warn", "obsidian_url / obsidian_api_key не настроены"
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = _ssl.CERT_NONE
        t0 = _time.time()
        try:
            req = _ur.Request(url + "/", headers={"Authorization": f"Bearer {key}"})
            _ur.urlopen(req, context=ctx, timeout=3)
            return "ok", f"отвечает · {int((_time.time()-t0)*1000)}мс"
        except Exception as e:
            launched_path = Path("logs/obsidian_launched.json")
            obsidian_starting = starting
            if not obsidian_starting and launched_path.exists():
                try:
                    launched_at = datetime.datetime.fromisoformat(
                        json.loads(launched_path.read_text())["launched_at"]
                    )
                    obsidian_starting = (datetime.datetime.now() - launched_at).total_seconds() < 120
                except Exception:
                    pass
            if obsidian_starting:
                return _starting(f"не отвечает: {str(e)[:40]}")
            return "error", f"не отвечает: {str(e)[:60]}"

    _check("obsidian", "Obsidian", chk_obsidian)

    # ── Google Drive ──────────────────────────────────────────────────────────
    def chk_gdrive():
        cfg_d = json.loads(Path(CONFIG_FILE).read_text()) if Path(CONFIG_FILE).exists() else {}
        if not cfg_d.get("gdrive_folder_id", ""):
            return "warn", "gdrive_folder_id не настроен"
        creds = Path("credentials.json")
        token = Path("token.json")
        if not creds.exists():
            return "error", "credentials.json не найден"
        if not token.exists():
            return "warn", "token.json не найден — требуется авторизация"
        drive_files = Path("logs/drive_files.json")
        if not drive_files.exists():
            return "warn", "нет загруженных файлов (drive_files.json отсутствует)"
        files = json.loads(drive_files.read_text(encoding="utf-8"))
        count = len(files)
        if count == 0:
            return "warn", "файлы не загружались"
        stats_path = Path("logs/pipeline_stats.json")
        last_ts = None
        if stats_path.exists():
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            for c in reversed(stats.get("recent", [])):
                if c.get("upload_s", 0) > 0:
                    last_ts = c.get("started_at", "")[:16]
                    break
        detail = f"{count} файлов на Drive"
        if last_ts:
            detail += f" · посл. загрузка {last_ts}"
        return "ok", detail

    _check("gdrive", "Google Drive", chk_gdrive)

    # ── Audio ─────────────────────────────────────────────────────────────────
    def chk_audio():
        if not Path(STATUS_FILE).exists():
            return _starting("статус ещё не создан") if starting else ("unknown", "статус недоступен")
        st       = json.loads(Path(STATUS_FILE).read_text())
        loopback = st.get("loopback_active", False)
        if loopback:
            return "ok", "микрофон + loopback активны"
        return "warn", "loopback не обнаружен · только микрофон"

    _check("audio", "Аудио", chk_audio)

    # ── Speakers ──────────────────────────────────────────────────────────────
    def chk_speakers():
        reg = Path("speakers/registry.json")
        if not reg.exists():
            return "warn", "реестр спикеров не найден"
        speakers = json.loads(reg.read_text(encoding="utf-8")).get("speakers", {})
        if not isinstance(speakers, dict):
            return "warn", "неожиданный формат реестра"
        total     = len(speakers)
        named     = sum(1 for s in speakers.values()
                        if isinstance(s, dict) and not str(s.get("name", "")).startswith("Speaker_"))
        owner_cnt = sum(1 for s in speakers.values() if isinstance(s, dict) and s.get("is_owner"))
        return "ok", f"{total} спикеров · {named} с именем · {owner_cnt} владелец"

    _check("speakers", "Спикеры", chk_speakers)

    # ── Disk ──────────────────────────────────────────────────────────────────
    def chk_disk():
        u        = _shutil.disk_usage(".")
        free_gb  = u.free  / 1e9
        total_gb = u.total / 1e9
        pct      = u.used  / u.total * 100
        status   = "ok" if free_gb > 5 else ("warn" if free_gb > 1 else "error")
        return status, f"свободно {free_gb:.1f} ГБ из {total_gb:.0f} ГБ ({pct:.0f}% занято)"

    _check("disk", "Диск", chk_disk)

    # ── GPU ───────────────────────────────────────────────────────────────────
    def chk_gpu():
        import subprocess as _sp
        try:
            r = _sp.run(
                ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
        except FileNotFoundError:
            return "unknown", "nvidia-smi не найден"
        if r.returncode != 0 or not r.stdout.strip():
            return "unknown", "GPU не обнаружен"
        p = [x.strip() for x in r.stdout.strip().split(",")]
        util, mem_used, mem_total, temp = int(p[1]), int(p[2]), int(p[3]), int(p[4])
        status = "ok" if temp < 80 else ("warn" if temp < 90 else "error")
        return status, f"{p[0]} · {util}% · VRAM {mem_used}/{mem_total}МБ · {temp}°C"

    _check("gpu", "GPU", chk_gpu)

    return jsonify({
        "checks":    checks,
        "phase":     phase,
        "uptime_s":  int(uptime_s) if uptime_s is not None else None,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    })


@app.post("/api/diagnostics/fix")
def diagnostics_fix():
    data = request.get_json() or {}
    failed = data.get("checks", [])
    if not failed:
        return jsonify({"ok": False, "msg": "no failed checks"})

    lines = "\n".join(
        f"- [{c['status'].upper()}] {c['name']}: {c['detail']}"
        for c in failed
    )
    prompt = (
        "Voice Logger самодиагностика обнаружила проблемы:\n\n"
        + lines
        + "\n\nПожалуйста, разберись и исправь их."
    )

    cwd = str(Path(__file__).parent)
    prompt_file = Path(cwd) / "logs" / "_fix_prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    ps1 = Path(cwd) / "logs" / "_autofix.ps1"
    ps1.write_text(
        f'Set-Location "{cwd}"\n'
        '$p = (Get-Content "logs\\_fix_prompt.txt" -Raw -Encoding UTF8).Trim()\n'
        'claude $p\n',
        encoding="utf-8-sig",
    )

    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1)],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=cwd,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.get("/")
def index():
    return send_from_directory("dashboard", "index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7331, debug=False)
