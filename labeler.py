"""
labeler.py — Interactive speaker labeling after each chunk.

After diarization, any new auto-unknown speakers trigger a notification
via Windows toast + a web API endpoint. The user can either:
  A) Answer in the console (if recorder.py is in a terminal window)
  B) Answer in the dashboard (preferred — non-blocking)

The labeling queue is thread-safe. Answers from the dashboard are
submitted via POST /api/label_speaker and picked up by the recorder thread.

Architecture:
  recorder.py (background thread)
       │  process_chunk() finds new speakers
       │  → enqueue_labeling_request()
       ↓
  labeling_queue (thread-safe deque)
       │  dashboard polls GET /api/pending_labels
       │  user fills in name → POST /api/label_speaker
       ↓
  speakers.py rename_speaker() + transcript re-label
"""

import json
import logging
import threading
import datetime
from collections import deque
from pathlib import Path
from typing import Optional

log = logging.getLogger("voice-logger.labeler")

# ── Shared state ───────────────────────────────────────────────────────────
_pending: deque  = deque()          # list of PendingLabel dicts
_answers: dict   = {}               # {request_id: name}
_lock = threading.Lock()


class PendingLabel:
    def __init__(self, request_id: str, speaker_id: str,
                 speaker_name: str, transcript_path: str,
                 audio_clip_path: Optional[str],
                 chunk_started_at: str, sample_lines: list[str]):
        self.request_id       = request_id
        self.speaker_id       = speaker_id
        self.speaker_name     = speaker_name          # current auto-name, e.g. "Speaker_2"
        self.transcript_path  = transcript_path
        self.audio_clip_path  = audio_clip_path       # short WAV clip of this speaker
        self.chunk_started_at = chunk_started_at
        self.sample_lines     = sample_lines          # first 3 transcript lines of this speaker
        self.created_at       = datetime.datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "request_id":       self.request_id,
            "speaker_id":       self.speaker_id,
            "speaker_name":     self.speaker_name,
            "transcript_path":  self.transcript_path,
            "audio_clip_path":  self.audio_clip_path,
            "chunk_started_at": self.chunk_started_at,
            "sample_lines":     self.sample_lines,
            "created_at":       self.created_at,
        }


def enqueue_labeling_request(
    speaker_id: str,
    speaker_name: str,
    transcript_path: str,
    transcript_lines: list[dict],
    wav_path: Path,
    chunk_started_at: datetime.datetime,
) -> str:
    """
    Add a new unknown speaker to the labeling queue.
    Saves a short audio clip of this speaker for playback in the dashboard.
    Returns the request_id.
    """
    import uuid, soundfile as sf, numpy as np

    request_id = str(uuid.uuid4())[:8]

    # Extract a short audio clip (first 10 sec segment of this speaker) for preview
    audio_clip_path = None
    try:
        speaker_lines = [l for l in transcript_lines if l.get("speaker") == speaker_name]
        if speaker_lines and wav_path.exists():
            first_line = speaker_lines[0]
            clip_start = max(0, first_line["start"] - 0.3)
            clip_end   = min(first_line["end"] + 0.5,
                             clip_start + 10.0)  # max 10 sec clip

            data, sr = sf.read(str(wav_path))
            s, e = int(clip_start * sr), int(clip_end * sr)
            clip = data[s:e]

            clip_dir  = Path("speakers/clips")
            clip_dir.mkdir(parents=True, exist_ok=True)
            clip_path = clip_dir / f"{request_id}.wav"
            sf.write(str(clip_path), clip, sr)
            audio_clip_path = str(clip_path)
            # Store clip path in speaker registry so the speakers tab can play it
            try:
                from speakers import set_speaker_clip
                set_speaker_clip(speaker_id, audio_clip_path)
            except Exception:
                pass
    except Exception as ex:
        log.warning(f"Could not extract clip for {speaker_name}: {ex}")

    # Sample lines for display
    sample_lines = [
        l["text"] for l in transcript_lines
        if l.get("speaker") == speaker_name
    ][:3]

    label = PendingLabel(
        request_id       = request_id,
        speaker_id       = speaker_id,
        speaker_name     = speaker_name,
        transcript_path  = str(transcript_path),
        audio_clip_path  = audio_clip_path,
        chunk_started_at = chunk_started_at.isoformat(),
        sample_lines     = sample_lines,
    )

    with _lock:
        _pending.append(label)

    log.info(f"Labeling request queued: {speaker_name} (req={request_id})")

    # Windows toast notification
    _toast(speaker_name, sample_lines)

    return request_id


def _toast(speaker_name: str, sample_lines: list[str]):
    """Show Windows toast notification (non-blocking, best-effort)."""
    try:
        from win10toast import ToastNotifier
        text = sample_lines[0][:80] if sample_lines else "неизвестный спикер"
        ToastNotifier().show_toast(
            title=f"🎙 Новый спикер: {speaker_name}",
            msg=f'"{text}…"\n→ Откройте дашборд чтобы назвать его.',
            duration=8,
            threaded=True,
        )
    except Exception:
        # win10toast not installed or toast failed — silent
        pass


def get_pending() -> list[dict]:
    """Return all pending labeling requests as dicts (for dashboard API)."""
    with _lock:
        return [p.to_dict() for p in _pending]


def submit_answer(request_id: str, name: str, target_speaker_id: Optional[str] = None) -> bool:
    """
    Record a user's answer for a labeling request.
    target_speaker_id: if set, the unknown will be merged into this existing speaker.
    Returns True if the request was found and answered.
    """
    with _lock:
        for p in _pending:
            if p.request_id == request_id:
                _answers[request_id] = {
                    "speaker_id":        p.speaker_id,
                    "speaker_name":      p.speaker_name,   # old auto-name for transcript rewrite
                    "name":              name.strip(),
                    "audio_clip_path":   p.audio_clip_path,
                    "target_speaker_id": target_speaker_id,
                    "chunk_started_at":  p.chunk_started_at,
                }
                _pending.remove(p)
                log.info(f"Label answer: {p.speaker_name} → '{name}' (req={request_id}, target={target_speaker_id})")
                return True
    return False


def dismiss(request_id: str):
    """Dismiss a request without renaming (keep auto-name)."""
    with _lock:
        for p in _pending:
            if p.request_id == request_id:
                _pending.remove(p)
                break


def drain_answers() -> list[dict]:
    """
    Pop and return all answered labels.
    Called by recorder after saving transcript, to apply renames
    and re-write the transcript file.
    """
    with _lock:
        items = list(_answers.values())
        _answers.clear()
        return items


def apply_pending_renames(transcript_path: Path, answers: list[dict]):
    """
    Re-write a transcript file substituting old auto-names with real names.
    Also persists speaker changes (rename or merge-into-existing) in the registry.
    """
    if not answers or not transcript_path.exists():
        return

    from speakers import rename_speaker, delete_speaker, add_sample

    rename_map: dict[str, str] = {}
    for ans in answers:
        old_name   = ans.get("speaker_name", "")  # e.g. "Speaker_3"
        new_name   = ans["name"]
        target_id  = ans.get("target_speaker_id")
        clip_path  = ans.get("audio_clip_path")

        if target_id:
            # Merge unknown into existing speaker: refine embedding + delete unknown entry
            if clip_path:
                from pathlib import Path as _P
                add_sample(target_id, _P(clip_path))
            delete_speaker(ans["speaker_id"])
            if old_name:
                rename_map[old_name] = new_name
        else:
            # Simple rename: just update the name in registry
            ok = rename_speaker(ans["speaker_id"], new_name)
            if ok and old_name:
                rename_map[old_name] = new_name

    if not rename_map:
        return

    content = transcript_path.read_text(encoding="utf-8")
    for old, new in rename_map.items():
        content = content.replace(f"[{old}]", f"[{new}]")
        content = content.replace(f"**[{old}]**", f"**[{new}]**")

    transcript_path.write_text(content, encoding="utf-8")
    log.info(f"Transcript re-labeled: {transcript_path.name} — {rename_map}")
