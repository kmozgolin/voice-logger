"""
voice-logger / recorder.py
Continuously records microphone audio in chunks, transcribes with Whisper,
enriches with Outlook calendar events, and saves markdown files to a watched
Google Drive folder (for NotebookLM ingestion).
"""

import os
import sys
import json
import time
import queue
import signal
import logging
import threading
import datetime
import tempfile
import argparse
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import whisper
import diarize as diarize_mod
import speakers as spk_mod
import labeler as labeler_mod
from audio_capture import AudioMixer, find_loopback_device
import merger as merger_mod

# ── Optional integrations (graceful degradation) ───────────────────────────
try:
    import win32com.client
    OUTLOOK_AVAILABLE = True
except ImportError:
    OUTLOOK_AVAILABLE = False

TODAY_MEETINGS_FILE = "logs/today_meetings.json"

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False

# ── Logging ────────────────────────────────────────────────────────────────
log = logging.getLogger("voice-logger")
if not log.handlers:
    log.setLevel(logging.INFO)
    log.propagate = False  # don't double-write via root logger
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _fh = logging.FileHandler("logs/voice-logger.log", encoding="utf-8")
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    log.addHandler(_fh)
    log.addHandler(_sh)

# ── Config ─────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "whisper_model": "large",          # tiny | base | small | medium | large
    "language": None,                  # None = auto-detect
    "sample_rate": 16000,
    "chunk_seconds": 60,              # chunk duration
    "silence_threshold": 0.001,        # RMS below this = silence
    "silence_min_seconds": 3,          # skip chunk if mostly silent
    "transcripts_dir": "transcripts",
    "recordings_dir": "recordings",
    "gdrive_folder_id": "",            # Google Drive folder ID for NotebookLM
    "gdrive_credentials": "credentials.json",
    "gdrive_token": "token.json",
    "keep_audio": False,               # delete .wav after transcription
    "status_file": "logs/status.json", # live status for the dashboard UI
    "forced_meeting_file": "logs/forced_meeting.json",  # user-selected meeting override
    "hf_token": "",                    # HuggingFace token for pyannote models
    "diarization_enabled": True,       # speaker diarization on/off
    "max_speakers": 8,                 # max expected speakers per chunk
    "loopback_device": None,           # None = auto-detect, int = device index
    "mic_device": None,                # None = system default
    "mic_gain": 1.0,                   # microphone volume multiplier
    "loopback_gain": 0.85,             # system audio volume (slightly lower to avoid clipping)
    "whisper_device": "auto",          # auto | cpu | cuda
    "cpu_threads": 4,                  # torch CPU threads (lower = less CPU load)
    "gpu_memory_fraction": 0.5,        # fraction of GPU VRAM allowed (0.0–1.0)
    "max_parallel_chunks": 1,          # max chunks processed simultaneously
    "save_training_data": False,       # keep audio+transcript pairs for Whisper fine-tuning
}


def load_config(path="config.json") -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if Path(path).exists():
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg: dict, path="config.json"):
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


# ── Status file (read by dashboard) ───────────────────────────────────────
_status = {
    "state": "idle",          # idle | recording | transcribing | diarizing | uploading
    "model_loaded": False,
    "current_chunk_start": None,
    "transcripts_today": 0,
    "last_transcript": None,
    "last_calendar_event": None,
    "loopback_active": False,
    "pending_labels": 0,
    "errors": [],
}


_is_finishing = False  # set True while waiting for threads after recording stops

def update_status(**kwargs):
    # Once recording has stopped, don't let chunk threads override 'finishing'
    _BLOCKED_WHEN_FINISHING = {'recording', 'transcribing', 'diarizing', 'uploading'}
    if _is_finishing and kwargs.get('state') in _BLOCKED_WHEN_FINISHING:
        kwargs.pop('state')
    if not kwargs:
        return
    _status.update(kwargs)
    _status["updated_at"] = datetime.datetime.now().isoformat()
    try:
        with open(cfg["status_file"], "w") as f:
            json.dump(_status, f, indent=2, default=str)
    except Exception:
        pass


# ── Recorder control file (written by dashboard, read by recorder) ─────────
_CONTROL_FILE = "logs/recorder_control.json"

def _read_control() -> dict:
    try:
        p = Path(_CONTROL_FILE)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# ── Pipeline stats ─────────────────────────────────────────────────────────
_pipeline_lock = threading.Lock()
_pipeline: dict = {
    "total_chunks":         0,
    "total_silent_skipped": 0,
    "active_processing":    0,
    "device":               "cpu",
    "recent":               [],   # rolling last 50 chunk entries
}
_PIPELINE_FILE = "logs/pipeline_stats.json"
_RECENT_MAX    = 50


def _save_pipeline():
    try:
        with open(_PIPELINE_FILE, "w") as f:
            json.dump(_pipeline, f, indent=2, default=str)
    except Exception:
        pass


def _inc_active(started_at: datetime.datetime = None):
    with _pipeline_lock:
        _pipeline["active_processing"] += 1
        if started_at:
            _pipeline.setdefault("active_chunk_starts", []).append(started_at.isoformat())
    _save_pipeline()


def _dec_active(started_at: datetime.datetime = None):
    with _pipeline_lock:
        _pipeline["active_processing"] = max(0, _pipeline["active_processing"] - 1)
        if started_at:
            try:
                _pipeline.get("active_chunk_starts", []).remove(started_at.isoformat())
            except ValueError:
                pass
    _save_pipeline()


def _record_chunk(entry: dict):
    """Thread-safe append of a processed chunk entry to rolling stats."""
    with _pipeline_lock:
        _pipeline["total_chunks"] += 1
        _pipeline["device"] = entry.get("device", "cpu")
        _pipeline["recent"].append(entry)
        if len(_pipeline["recent"]) > _RECENT_MAX:
            _pipeline["recent"].pop(0)
    _save_pipeline()


# ── Outlook calendar ───────────────────────────────────────────────────────

def refresh_today_meetings():
    """Query Outlook for all of today's meetings and write to TODAY_MEETINGS_FILE."""
    if not OUTLOOK_AVAILABLE:
        return
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns      = outlook.GetNamespace("MAPI")
        cal     = ns.GetDefaultFolder(9)  # olFolderCalendar
        items   = cal.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        today     = datetime.datetime.now().date()
        day_start = datetime.datetime.combine(today, datetime.time(0, 0))
        day_end   = datetime.datetime.combine(today, datetime.time(23, 59, 59))

        meetings = []
        for item in items:
            try:
                start = item.Start
                if hasattr(start, 'replace'):
                    start = start.replace(tzinfo=None)
                # Stop iterating once we've passed today
                if start.date() > today:
                    break
                if start.date() < today:
                    continue
                end = item.End
                if hasattr(end, 'replace'):
                    end = end.replace(tzinfo=None)
                subj     = str(getattr(item, "Subject", "") or "")
                subj_low = subj.lower().replace('с', 'c')
                if "cancelled" in subj_low or "canceled" in subj_low \
                        or "отменена" in subj_low or "отменен" in subj_low:
                    continue
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
            "date":       today.strftime("%Y-%m-%d"),
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "meetings":   meetings,
        }
        Path(TODAY_MEETINGS_FILE).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(f"Meetings refreshed: {len(meetings)} events for {today}")
    except Exception as e:
        log.warning(f"Failed to refresh today's meetings: {e}")


_OBSIDIAN_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "obsidian" / "Obsidian.exe"

def _ensure_obsidian():
    """Launch Obsidian if its REST API is unreachable and the exe exists."""
    url = cfg.get("obsidian_url", "")
    key = cfg.get("obsidian_api_key", "")
    if not url or not key:
        return
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url + "/", headers={"Authorization": f"Bearer {key}"})
        urllib.request.urlopen(req, context=ctx, timeout=3)
        return  # alive
    except Exception:
        pass
    # Not reachable — launch if exe exists
    if _OBSIDIAN_EXE.exists():
        import subprocess
        subprocess.Popen([str(_OBSIDIAN_EXE)], close_fds=True)
        log.info("Obsidian not responding — launched process")
    else:
        log.warning(f"Obsidian not responding and exe not found at {_OBSIDIAN_EXE}")


def _obsidian_watchdog():
    """Check Obsidian every 5 minutes; launch it if it's down."""
    _stop_event.wait(30)  # give it time to start on boot
    while not _stop_event.is_set():
        _ensure_obsidian()
        _stop_event.wait(5 * 60)


def _meetings_refresh_loop():
    """Background thread: refresh meeting cache every 15 min.
    COM must be initialised per-thread on Windows.
    """
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass
    try:
        refresh_today_meetings()
        while not _stop_event.wait(15 * 60):
            refresh_today_meetings()
    finally:
        try:
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:
            pass


def get_outlook_event_at(dt: datetime.datetime) -> dict | None:
    """Return the calendar event active at `dt`, reading from the cache file."""
    # Check for user-forced meeting override first
    try:
        fp = Path(cfg.get("forced_meeting_file", "logs/forced_meeting.json"))
        if fp.exists():
            forced = json.loads(fp.read_text(encoding="utf-8"))
            if forced:
                start_str = str(forced.get("start", ""))
                end_str   = str(forced.get("end", ""))
                if start_str and end_str:
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
                        try:
                            f_start = datetime.datetime.strptime(start_str[:19], fmt)
                            f_end   = datetime.datetime.strptime(end_str[:19],   fmt)
                            if f_start <= dt <= f_end + datetime.timedelta(minutes=5):
                                return forced
                            break
                        except ValueError:
                            continue
    except Exception as e:
        log.debug(f"Forced meeting read error: {e}")

    try:
        fp = Path(TODAY_MEETINGS_FILE)
        if fp.exists():
            data = json.loads(fp.read_text(encoding="utf-8"))
            for mtg in data.get("meetings", []):
                try:
                    start = datetime.datetime.strptime(mtg["start"], "%Y-%m-%dT%H:%M:%S")
                    end   = datetime.datetime.strptime(mtg["end"],   "%Y-%m-%dT%H:%M:%S")
                    if start <= dt <= end:
                        return mtg
                except Exception:
                    continue
    except Exception as e:
        log.warning(f"Meeting cache read error: {e}")
    return None
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")
        cal = ns.GetDefaultFolder(9)  # olFolderCalendar
        items = cal.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        window_start = dt - datetime.timedelta(minutes=90)
        window_end   = dt + datetime.timedelta(minutes=90)
        restriction = (
            f"[Start] >= '{window_start.strftime('%m/%d/%Y %H:%M')}' "
            f"AND [Start] <= '{window_end.strftime('%m/%d/%Y %H:%M')}'"
        )
        restricted = items.Restrict(restriction)

        for item in restricted:
            subj  = str(getattr(item, "Subject", "") or "").lower()
            subj_norm = subj.replace('\u0441', 'c')  # кириллическая с → латинская c
            if "cancelled" in subj_norm or "canceled" in subj_norm \
                    or "отменена" in subj or "отменен" in subj:
                continue
            start = item.Start.replace(tzinfo=None) if hasattr(item.Start, 'replace') else item.Start
            end   = item.End.replace(tzinfo=None)   if hasattr(item.End,   'replace') else item.End
            if start <= dt <= end:
                return {
                    "subject":    item.Subject,
                    "start":      start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "end":        end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "organizer":  getattr(item, "Organizer", ""),
                    "attendees":  getattr(item, "RequiredAttendees", ""),
                    "location":   getattr(item, "Location", ""),
                    "categories": getattr(item, "Categories", "") or "",
                }
    except Exception as e:
        log.warning(f"Outlook error: {e}")
    return None


# ── Google Drive upload ────────────────────────────────────────────────────
_gdrive_service = None
_gdrive_disabled = False   # set True after unrecoverable auth failure

def _get_gdrive_service():
    global _gdrive_service, _gdrive_disabled
    if _gdrive_disabled:
        return None
    if _gdrive_service:
        return _gdrive_service
    if not GDRIVE_AVAILABLE:
        return None
    SCOPES = ["https://www.googleapis.com/auth/drive.file"]
    creds = None
    token_path = cfg["gdrive_token"]
    creds_path = cfg["gdrive_credentials"]
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                log.error(f"Google Drive token refresh failed: {e}")
                # Token is revoked — delete it so user can re-auth via check_gdrive.py
                Path(token_path).unlink(missing_ok=True)
                _gdrive_disabled = True
                msg = "Google Drive: токен отозван. Запустите python check_gdrive.py для повторной авторизации."
                log.warning(msg)
                if msg not in _status.get("errors", []):
                    _status.setdefault("errors", []).append(msg)
                return None
        elif Path(creds_path).exists():
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            log.warning("Google Drive credentials not found – skipping upload.")
            return None
        with open(token_path, "w") as t:
            t.write(creds.to_json())
    _gdrive_service = build("drive", "v3", credentials=creds)
    return _gdrive_service


def upload_to_gdrive(
    file_path: Path,
    folder_id: str,
    file_id: str = None,
    as_gdoc: bool = False,
) -> str | None:
    """
    Upload or update a file on Google Drive.
    - file_id=None  → create new file; returns new file ID.
    - file_id=<id>  → overwrite existing file; returns same ID.
    - as_gdoc=True  → convert to Google Docs format (required for NotebookLM).
    """
    global _gdrive_service
    svc = _get_gdrive_service()
    if not svc or not folder_id:
        return None

    _TRANSIENT = ("ssl", "eof", "timeout", "connection", "reset", "broken pipe", "remote end")

    last_err = None
    for attempt in range(3):
        try:
            update_status(state="uploading")
            media = MediaFileUpload(str(file_path), mimetype="text/plain")
            if file_id:
                svc.files().update(fileId=file_id, media_body=media).execute()
                log.info(f"Updated {file_path.name} → Drive id={file_id}")
            else:
                meta = {"name": file_path.stem, "parents": [folder_id]}
                if as_gdoc:
                    meta["mimeType"] = "application/vnd.google-apps.document"
                result = svc.files().create(body=meta, media_body=media, fields="id").execute()
                file_id = result.get("id")
                log.info(f"Uploaded {file_path.name} → Drive id={file_id}")
            _status["errors"] = [e for e in _status.get("errors", [])
                                  if not any(s in e.lower() for s in _TRANSIENT)]
            return file_id
        except Exception as e:
            last_err = e
            is_transient = any(s in str(e).lower() for s in _TRANSIENT)
            if is_transient and attempt < 2:
                log.warning(f"Drive upload transient error (attempt {attempt+1}/3): {e} — retrying…")
                _gdrive_service = None
                svc = _get_gdrive_service()
                if not svc:
                    break
                time.sleep(2 ** attempt)
                continue
            break

    log.error(f"Drive upload failed: {last_err}")
    err_str = str(last_err)
    if err_str not in _status.get("errors", []):
        _status.setdefault("errors", []).append(err_str)
    _gdrive_service = None
    return None


# ── Whisper transcription ──────────────────────────────────────────────────
_whisper_model = None
_model_ready   = threading.Event()   # set once model is loaded; transcribe() waits on it

def load_whisper(model_name: str):
    global _whisper_model
    device = cfg.get("whisper_device", "auto")
    if device == "auto":
        try:
            import os, torch
            # Windows: PyTorch bundled CUDA DLLs may not be in PATH — add explicitly
            _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(_torch_lib)
            os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"

    try:
        import torch
        # Limit CPU threads to leave headroom for video calls
        cpu_threads = cfg.get("cpu_threads", 4)
        torch.set_num_threads(cpu_threads)
        torch.set_num_interop_threads(max(1, cpu_threads // 2))
        log.info(f"CPU threads limited to {cpu_threads}")
        # Limit GPU memory fraction
        if device == "cuda":
            frac = float(cfg.get("gpu_memory_fraction", 0.5))
            torch.cuda.set_per_process_memory_fraction(frac)
            log.info(f"GPU memory fraction limited to {frac:.0%}")
    except Exception as e:
        log.warning(f"Resource limit setup failed: {e}")

    # Lower process priority so video calls are not starved
    try:
        import psutil, os
        p = psutil.Process(os.getpid())
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        log.info("Process priority set to BELOW_NORMAL")
    except Exception:
        pass

    log.info(f"Loading Whisper model '{model_name}' on {device} ...")
    update_status(state="loading_model")
    try:
        _whisper_model = whisper.load_model(model_name, device=device)
    except Exception as e:
        if device == "cuda":
            log.warning(f"Failed to load on CUDA ({e}) — falling back to CPU")
            device = "cpu"
            _whisper_model = whisper.load_model(model_name, device=device)
        else:
            raise
    update_status(model_loaded=True, whisper_device=device)
    with _pipeline_lock:
        _pipeline["device"] = device
    _save_pipeline()
    log.info(f"Whisper ready on {device}.")
    _model_ready.set()


def _whisper_on_cuda() -> bool:
    try:
        return next(_whisper_model.parameters()).is_cuda
    except Exception:
        return False


def _build_initial_prompt(cal_event: dict | None = None) -> str:
    """
    Build a rich initial_prompt for Whisper by combining:
    - base prompt from config
    - enrolled speaker names (so Whisper learns to spell them)
    - meeting attendees from the active calendar event
    """
    parts = [cfg.get("whisper_initial_prompt", "Рабочее совещание на русском языке.")]

    try:
        import speakers as spk_mod
        names = [s["name"] for s in spk_mod.list_speakers()
                 if not s.get("auto_unknown") and not s.get("is_owner")]
        if names:
            parts.append("Участники: " + ", ".join(names) + ".")
    except Exception:
        pass

    if cal_event:
        attendees = cal_event.get("attendees") or []
        if attendees:
            first_names = [a.split()[0] for a in attendees if a.strip()]
            if first_names:
                parts.append("На встрече: " + ", ".join(first_names) + ".")

    return " ".join(p for p in parts if p).strip()


def transcribe(audio_path: Path, prompt: str | None = None) -> dict:
    """Return full Whisper result dict (includes word-level segments)."""
    if not _model_ready.is_set():
        log.info("Whisper model still loading — chunk queued, waiting...")
        update_status(state="loading_model")
        _model_ready.wait()
    if _whisper_model is None:
        raise RuntimeError("Whisper model failed to load")
    update_status(state="transcribing")
    effective_prompt = prompt or _build_initial_prompt()
    _transcribe_kwargs = dict(
        language=cfg.get("language") or "ru",
        verbose=False,
        word_timestamps=True,
        initial_prompt=effective_prompt,
        condition_on_previous_text=False,
        no_speech_threshold=cfg.get("whisper_no_speech_threshold", 0.3),
        logprob_threshold=cfg.get("whisper_logprob_threshold", -1.5),
        compression_ratio_threshold=cfg.get("whisper_compression_ratio_threshold", 3.0),
    )
    try:
        result = _whisper_model.transcribe(str(audio_path), **_transcribe_kwargs)
    except Exception as e:
        if _whisper_on_cuda():
            log.warning(f"GPU error during transcription ({e}) — reloading on CPU")
            cfg["whisper_device"] = "cpu"
            load_whisper(cfg["whisper_model"])
            result = _whisper_model.transcribe(str(audio_path), **_transcribe_kwargs)
        else:
            raise
    update_status(state="idle")
    return result


# ── Transcript file writer ─────────────────────────────────────────────────
def save_transcript(
    transcript_lines: list,
    started_at: datetime.datetime,
    ended_at: datetime.datetime,
    cal_event: dict | None,
    audio_path: Path,
) -> Path:
    date_str  = started_at.strftime("%Y-%m-%d")
    time_str  = started_at.strftime("%H-%M-%S")
    fname     = f"{date_str}_{time_str}.txt"
    out_dir   = Path(cfg["transcripts_dir"]) / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path  = out_dir / fname

    md_text = diarize_mod.format_transcript_md(
        lines=transcript_lines,
        started_at=started_at,
        ended_at=ended_at,
        cal_event=cal_event,
        whisper_model=cfg["whisper_model"],
        audio_file_name=audio_path.name,
    )

    out_path.write_text(md_text, encoding="utf-8")
    log.info(f"Saved transcript → {out_path}")

    _status["transcripts_today"] += 1
    update_status(last_transcript=str(out_path), last_calendar_event=cal_event)

    return out_path


# ── Recording loop ─────────────────────────────────────────────────────────
_stop_event        = threading.Event()
_chunk_semaphore   = None   # initialised in recording_loop() from config
_chunk_threads: list[threading.Thread] = []
_chunk_threads_lock = threading.Lock()
_paused_wavs: list  = []   # (wav_path, started_at, ended_at) saved while processing_paused
STOP_REQUEST_FILE   = "logs/stop_requested.txt"
RELOAD_WHISPER_FILE = "logs/reload_whisper.txt"


def _whisper_reload_watcher():
    """Background thread: watches for reload trigger, swaps model between chunks."""
    while True:
        time.sleep(2)
        if _stop_event.is_set():
            break
        if not Path(RELOAD_WHISPER_FILE).exists():
            continue
        try:
            Path(RELOAD_WHISPER_FILE).unlink(missing_ok=True)
        except Exception:
            pass
        if _chunk_semaphore is None:
            continue
        log.info("Whisper reload requested — waiting for active chunk to finish...")
        update_status(state="reloading")
        # Acquire all semaphore slots → exclusive access (no chunk is processing)
        max_p = cfg.get("max_parallel_chunks", 1)
        for _ in range(max_p):
            _chunk_semaphore.acquire()
        try:
            import json as _json
            cfg.update(_json.loads(Path("config.json").read_text(encoding="utf-8")))
        except Exception:
            pass
        load_whisper(cfg["whisper_model"])
        for _ in range(max_p):
            _chunk_semaphore.release()


def is_silent(audio: np.ndarray, threshold: float) -> bool:
    return float(np.sqrt(np.mean(audio ** 2))) < threshold


def recording_loop():
    global _chunk_semaphore
    sr          = cfg["sample_rate"]
    chunk_s     = cfg["chunk_seconds"]
    threshold   = cfg["silence_threshold"]
    min_speech  = cfg["silence_min_seconds"]
    max_parallel = max(1, int(cfg.get("max_parallel_chunks", 1)))
    if _chunk_semaphore is None:
        _chunk_semaphore = threading.Semaphore(max_parallel)
    log.info(f"Chunk parallelism limited to {max_parallel}")

    mixer = AudioMixer(
        sample_rate      = sr,
        mic_device       = cfg.get("mic_device"),
        loopback_device  = cfg.get("loopback_device"),  # None = auto-detect
        mic_gain         = cfg.get("mic_gain", 1.0),
        loopback_gain    = cfg.get("loopback_gain", 0.85),
    )
    mixer.start()
    update_status(loopback_active=mixer.using_loopback)

    if mixer.using_loopback:
        log.info("Recording: MIC + LOOPBACK (Teams/Zoom calls captured)")
    else:
        log.info("Recording: MIC only (enable Stereo Mix for Teams/Zoom)")

    rec_dir = Path(cfg["recordings_dir"])
    rec_dir.mkdir(parents=True, exist_ok=True)

    _prev_processing_paused = False

    try:
        while not _stop_event.is_set():
            if Path(STOP_REQUEST_FILE).exists():
                try:
                    Path(STOP_REQUEST_FILE).unlink()
                except Exception:
                    pass
                _stop_event.set()
                break

            collected  = []
            started_at = datetime.datetime.now()
            update_status(state="recording", current_chunk_start=started_at.isoformat())

            frames_needed = sr * chunk_s
            frames_got    = 0

            while frames_got < frames_needed and not _stop_event.is_set():
                if Path(STOP_REQUEST_FILE).exists():
                    try:
                        Path(STOP_REQUEST_FILE).unlink()
                    except Exception:
                        pass
                    _stop_event.set()
                    break
                block = mixer.get_audio(timeout=1.0)
                if block is None:
                    continue
                collected.append(block)
                frames_got += len(block)

            if not collected:
                continue

            # Read control flags once per chunk
            ctrl = _read_control()
            recording_paused   = ctrl.get("recording_paused",   False)
            processing_paused  = ctrl.get("processing_paused",  False)

            # Resume: processing was paused, now it's not — start only the WAVs saved during pause
            if _prev_processing_paused and not processing_paused:
                if _paused_wavs:
                    log.info(f"Processing resumed — starting {len(_paused_wavs)} paused chunk(s)")
                    for _pwav, _ps, _pe in _paused_wavs:
                        _inc_active(_ps)
                        _pt = threading.Thread(target=process_chunk, args=(_pwav, _ps, _pe), daemon=False)
                        with _chunk_threads_lock:
                            _chunk_threads.append(_pt)
                        _pt.start()
                    _paused_wavs.clear()
            _prev_processing_paused = processing_paused

            update_status(recording_paused=recording_paused,
                          processing_paused=processing_paused)

            if recording_paused:
                log.debug("Recording paused — chunk discarded")
                continue

            audio_np = np.concatenate(collected).flatten()
            ended_at = datetime.datetime.now()

            # Skip if mostly silent (no speech in call / room)
            speech_frames = sum(
                1 for i in range(0, len(audio_np), sr)
                if not is_silent(audio_np[i:i + sr], threshold)
            )
            if speech_frames < min_speech:
                log.info("Chunk skipped (silence / no call active)")
                with _pipeline_lock:
                    _pipeline["total_silent_skipped"] += 1
                _save_pipeline()
                continue

            wav_path = rec_dir / f"{started_at.strftime('%Y%m%d_%H%M%S')}.wav"
            sf.write(str(wav_path), audio_np, sr)

            if processing_paused:
                log.info(f"Processing paused — chunk saved, queued for later: {wav_path.name}")
                _paused_wavs.append((wav_path, started_at, ended_at))
                continue

            _inc_active(started_at)
            t = threading.Thread(
                target=process_chunk,
                args=(wav_path, started_at, ended_at),
                daemon=False,
            )
            with _chunk_threads_lock:
                _chunk_threads.append(t)
            t.start()

    finally:
        mixer.stop()
        log.info("Recording loop stopped.")


TRAINING_DIR = Path("training_data")

def _save_training_sample(wav_path: Path, whisper_result: dict,
                          started_at: datetime.datetime, ended_at: datetime.datetime,
                          transcript_lines: list | None = None):
    """
    Copy the chunk audio and save Whisper output to training_data/ for future fine-tuning.
    Each sample: training_data/YYYYMMDD_HHMMSS.wav + .json
    transcript_lines: diarized segments [{speaker, start, end, text}] — updated later as
    speakers get labeled by the user.
    """
    import shutil, json as _json
    try:
        TRAINING_DIR.mkdir(exist_ok=True)
        stem = started_at.strftime("%Y%m%d_%H%M%S")
        dst_wav  = TRAINING_DIR / f"{stem}.wav"
        dst_json = TRAINING_DIR / f"{stem}.json"

        if wav_path.exists() and not dst_wav.exists():
            shutil.copy2(str(wav_path), str(dst_wav))

        if not dst_json.exists():
            segments = [
                {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
                for s in (whisper_result.get("segments") or [])
            ]
            diarized = [
                {"speaker": l.get("speaker", ""), "start": l.get("start", 0),
                 "end": l.get("end", 0), "text": l.get("text", "").strip()}
                for l in (transcript_lines or [])
            ]
            meta = {
                "audio":      str(dst_wav),
                "text":       (whisper_result.get("text") or "").strip(),
                "language":   whisper_result.get("language", "ru"),
                "started_at": started_at.isoformat(),
                "ended_at":   ended_at.isoformat(),
                "duration_s": round((ended_at - started_at).total_seconds(), 1),
                "segments":   segments,
                "diarized":   diarized,
            }
            dst_json.write_text(_json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        log.debug(f"Training sample saved: {stem}")
    except Exception as e:
        log.warning(f"Could not save training sample: {e}")


def _update_training_speakers(answers: list[dict]):
    """
    After the user labels speakers, update the speaker names in corresponding
    training_data/*.json files so the diarized segments reflect real names.
    """
    import json as _json
    import datetime as _dt

    for ans in answers:
        old_name  = ans.get("speaker_name", "")
        new_name  = ans.get("name", "")
        started_raw = ans.get("chunk_started_at", "")
        if not old_name or not new_name or not started_raw:
            continue
        try:
            dt = _dt.datetime.fromisoformat(started_raw)
            stem = dt.strftime("%Y%m%d_%H%M%S")
        except Exception:
            continue

        training_json = TRAINING_DIR / f"{stem}.json"
        if not training_json.exists():
            continue
        try:
            meta = _json.loads(training_json.read_text(encoding="utf-8"))
            updated = False
            for seg in meta.get("diarized", []):
                if seg.get("speaker") == old_name:
                    seg["speaker"] = new_name
                    updated = True
            if updated:
                training_json.write_text(
                    _json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                log.debug(f"Training data updated: {stem} — {old_name} → {new_name}")
        except Exception as e:
            log.warning(f"Could not update training speakers for {stem}: {e}")


def process_chunk(wav_path: Path, started_at: datetime.datetime, ended_at: datetime.datetime):
    _chunk_semaphore.acquire()
    _t0          = time.time()
    _t_transcribe = 0.0
    _t_diarize    = 0.0
    _n_speakers   = 0
    _n_segments   = 0
    try:
        cal_event = get_outlook_event_at(started_at)
        if cal_event:
            update_status(last_calendar_event=cal_event)

        # ── Step 1: Whisper ────────────────────────────────────────────────
        _t1 = time.time()
        whisper_result = transcribe(wav_path, prompt=_build_initial_prompt(cal_event))
        _t_transcribe = round(time.time() - _t1, 2)

        # ── Step 2: Diarization ────────────────────────────────────────────
        new_speaker_ids: list[tuple[str, str]] = []  # (speaker_id, speaker_name)

        if cfg.get("diarization_enabled", True):
            update_status(state="diarizing")
            _t2 = time.time()
            diarization_segs = diarize_mod.diarize(
                wav_path,
                hf_token=cfg.get("hf_token") or None,
                max_speakers=cfg.get("max_speakers", 8),
            )
            _t_diarize = round(time.time() - _t2, 2)

            # Identify each unique pyannote label
            label_to_clips: dict[str, list[dict]] = {}
            for seg in diarization_segs:
                lbl = seg["speaker_label"]
                label_to_clips.setdefault(lbl, []).append(seg)

            speaker_map: dict[str, dict] = {}
            for label, segs in label_to_clips.items():
                longest = max(segs, key=lambda s: s["end"] - s["start"])
                dur = longest["end"] - longest["start"]
                if dur < 1.5:
                    speaker_map[label] = {"name": label, "color": "#888", "is_owner": False}
                    continue
                result = spk_mod.identify_speaker(wav_path, longest["start"], longest["end"])
                speaker_map[label] = result
                if result.get("new"):
                    # Queue this unknown speaker for user labeling
                    new_speaker_ids.append((result["id"], result["name"]))
                    log.info(f"Unknown speaker queued for labeling: {result['name']}")

            transcript_lines = diarize_mod.merge_whisper_and_diarization(
                whisper_result, diarization_segs, speaker_map
            )
        else:
            transcript_lines = [
                {
                    "speaker":  "Голос",
                    "color":    "#888",
                    "is_owner": False,
                    "start":    s.get("start", 0),
                    "end":      s.get("end", 0),
                    "text":     s.get("text", "").strip(),
                }
                for s in (whisper_result.get("segments") or [])
            ]

        _n_speakers = len({l.get("speaker") for l in transcript_lines if l.get("text")})
        _n_segments = len(transcript_lines)

        # ── Step 3: Save transcript ────────────────────────────────────────
        md_path = save_transcript(transcript_lines, started_at, ended_at,
                                  cal_event, wav_path)

        # ── Step 3b: Save training data (audio + transcript) ──────────────
        if cfg.get("save_training_data"):
            _save_training_sample(wav_path, whisper_result, started_at, ended_at,
                                  transcript_lines=transcript_lines)

        # ── Step 4: Enqueue unknown speakers for labeling ──────────────────
        for speaker_id, speaker_name in new_speaker_ids:
            labeler_mod.enqueue_labeling_request(
                speaker_id       = speaker_id,
                speaker_name     = speaker_name,
                transcript_path  = str(md_path),
                transcript_lines = transcript_lines,
                wav_path         = wav_path,
                chunk_started_at = started_at,
            )

        pending_count = len(labeler_mod.get_pending())
        update_status(pending_labels=pending_count)

        # ── Step 5: Apply any answers that came in while we were processing ─
        answers = labeler_mod.drain_answers()
        if answers:
            labeler_mod.apply_pending_renames(md_path, answers)
            if cfg.get("save_training_data"):
                _update_training_speakers(answers)
            update_status(pending_labels=len(labeler_mod.get_pending()))

        # ── Step 6: Upload to Google Drive ─────────────────────────────────
        if cfg.get("gdrive_folder_id"):
            upload_to_gdrive(md_path, cfg["gdrive_folder_id"])

        if not cfg.get("keep_audio"):
            wav_path.unlink(missing_ok=True)

        update_status(state="recording")

    except Exception as e:
        log.error(f"process_chunk error: {e}", exc_info=True)
        _status["errors"].append(str(e))
        update_status(state="recording")
    finally:
        _dec_active(started_at)
        if _chunk_semaphore:
            _chunk_semaphore.release()
        _record_chunk({
            "id":              started_at.strftime("%Y%m%d_%H%M%S"),
            "started_at":      started_at.isoformat(),
            "audio_s":         round((ended_at - started_at).total_seconds(), 1),
            "transcription_s": _t_transcribe,
            "diarization_s":   _t_diarize,
            "total_s":         round(time.time() - _t0, 2),
            "device":          cfg.get("whisper_device", "cpu"),
            "speakers":        _n_speakers,
            "segments":        _n_segments,
        })


# ── Entry point ────────────────────────────────────────────────────────────
cfg = {}

def _requeue_orphaned_wavs():
    """On startup, re-queue WAV files that were recorded but never transcribed.
    Only processes files from the last 2 days to avoid re-processing ancient backlog.
    Must be called AFTER _chunk_semaphore is initialised.
    """
    rec_dir = Path(cfg.get("recordings_dir", "recordings"))
    tx_dir  = Path(cfg.get("transcripts_dir", "transcripts"))
    cutoff  = datetime.datetime.now() - datetime.timedelta(days=2)
    wavs = sorted(rec_dir.glob("*.wav"))
    if not wavs:
        return
    requeued = 0
    for wav in wavs:
        try:
            started_at = datetime.datetime.strptime(wav.stem, "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        # Skip files older than cutoff
        if started_at < cutoff:
            continue
        date_str = started_at.strftime("%Y-%m-%d")
        tx_name  = started_at.strftime("%Y-%m-%d_%H-%M-%S") + ".txt"
        tx_path  = tx_dir / date_str / tx_name
        if tx_path.exists():
            if not cfg.get("keep_audio"):
                wav.unlink(missing_ok=True)
            continue
        try:
            import soundfile as sf
            info = sf.info(str(wav))
            ended_at = started_at + datetime.timedelta(seconds=info.duration)
        except Exception:
            ended_at = started_at + datetime.timedelta(seconds=cfg.get("chunk_seconds", 300))
        log.info(f"Re-queuing orphaned chunk: {wav.name}")
        _inc_active(started_at)
        t = threading.Thread(target=process_chunk, args=(wav, started_at, ended_at), daemon=False)
        with _chunk_threads_lock:
            _chunk_threads.append(t)
        t.start()
        requeued += 1
    if requeued:
        log.info(f"Re-queued {requeued} orphaned chunk(s) for transcription.")


def main():
    global cfg
    parser = argparse.ArgumentParser(description="Voice Logger Daemon")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--model", default=None, help="Override whisper model")
    parser.add_argument("--chunk", type=int, default=None, help="Chunk size in seconds")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.model:
        cfg["whisper_model"] = args.model
    if args.chunk:
        cfg["chunk_seconds"] = args.chunk

    Path(cfg["transcripts_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["recordings_dir"]).mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(parents=True, exist_ok=True)

    save_config(cfg, args.config)
    update_status(state="starting")

    # Write PID so dashboard can manage the process
    Path("logs/recorder.pid").write_text(str(os.getpid()))

    # Delete stale stop-request file left over from a previous crashed run
    Path(STOP_REQUEST_FILE).unlink(missing_ok=True)

    # Reset stale pipeline counters from previous run
    with _pipeline_lock:
        _pipeline["active_processing"] = 0
        _pipeline.pop("active_chunk_starts", None)
    _save_pipeline()

    def handle_signal(sig, frame):
        log.info("Shutting down ...")
        _stop_event.set()

    signal.signal(signal.SIGINT,  handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Semaphore must be ready before recording and orphan-requeue start
    global _chunk_semaphore
    _chunk_semaphore = threading.Semaphore(max(1, int(cfg.get("max_parallel_chunks", 1))))

    # Load Whisper in background — recording starts immediately without waiting
    model_thread = threading.Thread(
        target=load_whisper, args=(cfg["whisper_model"],),
        daemon=True, name="whisper-loader",
    )
    model_thread.start()

    # Re-queue orphaned WAV files from last 2 days (will wait for model inside transcribe)
    _requeue_orphaned_wavs()

    # Start session merger background thread
    _merger = merger_mod.SessionMerger(cfg, upload_fn=upload_to_gdrive)
    _merger.start()

    watcher = threading.Thread(target=_whisper_reload_watcher, daemon=True)
    watcher.start()

    meetings_thread = threading.Thread(target=_meetings_refresh_loop, daemon=True, name="meetings-refresh")
    meetings_thread.start()

    obsidian_thread = threading.Thread(target=_obsidian_watchdog, daemon=True, name="obsidian-watchdog")
    obsidian_thread.start()

    try:
        recording_loop()

        # Graceful finish: wait for any in-flight transcription threads
        with _chunk_threads_lock:
            threads_to_join = [t for t in _chunk_threads if t.is_alive()]

        if threads_to_join:
            log.info(f"Recording stopped. Waiting for {len(threads_to_join)} chunk(s) to finish...")
            global _is_finishing
            _is_finishing = True
            update_status(state="finishing")
            for t in threads_to_join:
                t.join()
            _is_finishing = False
            log.info("All chunks processed.")

        _merger.stop()
    finally:
        update_status(state="idle")
        Path("logs/recorder.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
