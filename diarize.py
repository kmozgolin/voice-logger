"""
diarize.py — Speaker diarization using pyannote.audio.

Given a WAV file, returns a list of segments:
  [
    {"start": 0.0, "end": 4.2, "speaker_label": "SPEAKER_0"},
    {"start": 4.5, "end": 9.1, "speaker_label": "SPEAKER_1"},
    ...
  ]

Then combines with Whisper word-level timestamps to produce
speaker-attributed transcript lines.
"""

import logging
import tempfile
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import torchaudio_compat  # noqa: F401 — patches torchaudio before pyannote imports it

log = logging.getLogger("voice-logger.diarize")

_pipeline      = None    # pyannote diarization pipeline (lazy)
_pipeline_lock = threading.Lock()


def _get_pipeline(hf_token: Optional[str] = None):
    """
    Load pyannote speaker-diarization-3.1 pipeline.
    Requires HuggingFace token with access to:
      - pyannote/speaker-diarization-3.1
      - pyannote/segmentation-3.0
    Accept both model cards on HuggingFace first (one-time).
    """
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    with _pipeline_lock:
        if _pipeline is not None:  # another thread loaded it while we waited
            return _pipeline
        try:
            from pyannote.audio import Pipeline
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            log.info(f"Loading pyannote pipeline on {device} …")
            _pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=hf_token,
            )
            _pipeline.to(torch.device(device))
            log.info("Pyannote pipeline ready.")
        except Exception as e:
            log.error(f"Failed to load pyannote pipeline: {e}")
            _pipeline = None
    return _pipeline


def diarize(audio_path: Path, hf_token: Optional[str] = None,
            min_speakers: int = 1, max_speakers: int = 8) -> list[dict]:
    """
    Run pyannote diarization on audio_path.
    Returns list of {start, end, speaker_label} dicts.
    """
    pipeline = _get_pipeline(hf_token)
    if pipeline is None:
        log.warning("Diarization pipeline unavailable — returning single-speaker fallback.")
        import soundfile as sf
        info = sf.info(str(audio_path))
        return [{"start": 0.0, "end": info.duration, "speaker_label": "SPEAKER_0"}]

    try:
        import torch
        with torch.no_grad():
            diarization = pipeline(
                str(audio_path),
                min_speakers=min_speakers,
                max_speakers=max_speakers,
            )

        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append({
                "start":         round(turn.start, 3),
                "end":           round(turn.end,   3),
                "speaker_label": speaker,
            })

        log.info(f"Diarization: {len(segments)} segments, "
                 f"{len({s['speaker_label'] for s in segments})} speakers")
        return segments

    except Exception as e:
        log.error(f"Diarization error: {e}")
        import soundfile as sf
        info = sf.info(str(audio_path))
        return [{"start": 0.0, "end": info.duration, "speaker_label": "SPEAKER_0"}]


def merge_whisper_and_diarization(
    whisper_result: dict,
    diarization_segments: list[dict],
    speaker_map: dict,       # {speaker_label → {name, color, is_owner, ...}}
) -> list[dict]:
    """
    Align Whisper word-level timestamps with diarization segments.
    Returns list of transcript lines:
      [
        {"speaker": "Вы", "color": "#f76c6c", "is_owner": True,
         "start": 0.0, "end": 4.2, "text": "Давайте обсудим план."},
        ...
      ]

    Strategy: for each Whisper segment, find the diarization segment
    with maximum overlap → assign that speaker.
    """
    if not diarization_segments:
        # No diarization — return plain segments
        return [
            {
                "speaker":  "Голос",
                "color":    "#888",
                "is_owner": False,
                "start":    s.get("start", 0),
                "end":      s.get("end",   0),
                "text":     s.get("text", "").strip(),
            }
            for s in (whisper_result.get("segments") or [])
        ]

    lines = []
    whisper_segments = whisper_result.get("segments") or []

    def _overlap(a_start, a_end, b_start, b_end) -> float:
        return max(0, min(a_end, b_end) - max(a_start, b_start))

    for ws in whisper_segments:
        ws_start = ws.get("start", 0)
        ws_end   = ws.get("end",   ws_start + 1)
        text     = ws.get("text", "").strip()
        if not text:
            continue

        # Find best-matching diarization segment
        best_seg   = None
        best_overlap = 0.0
        for ds in diarization_segments:
            ov = _overlap(ws_start, ws_end, ds["start"], ds["end"])
            if ov > best_overlap:
                best_overlap = ov
                best_seg     = ds

        if best_seg is None:
            label = "SPEAKER_0"
        else:
            label = best_seg["speaker_label"]

        speaker_info = speaker_map.get(label, {})
        lines.append({
            "speaker":  speaker_info.get("name", label),
            "color":    speaker_info.get("color", "#888"),
            "is_owner": speaker_info.get("is_owner", False),
            "start":    ws_start,
            "end":      ws_end,
            "text":     text,
        })

    # Merge consecutive segments from the same speaker
    return _merge_consecutive(lines)


def _merge_consecutive(lines: list[dict], gap_threshold: float = 1.5) -> list[dict]:
    """
    Merge adjacent lines from the same speaker if the gap between them
    is less than gap_threshold seconds.
    """
    if not lines:
        return lines

    merged = [lines[0].copy()]
    for line in lines[1:]:
        prev = merged[-1]
        gap  = line["start"] - prev["end"]
        if line["speaker"] == prev["speaker"] and gap <= gap_threshold:
            prev["end"]  = line["end"]
            prev["text"] = prev["text"].rstrip() + " " + line["text"].lstrip()
        else:
            merged.append(line.copy())
    return merged


def format_transcript_md(
    lines: list[dict],
    started_at,
    ended_at,
    cal_event: Optional[dict],
    whisper_model: str,
    audio_file_name: str,
) -> str:
    """
    Render the full Markdown transcript with speaker labels.
    """
    import datetime

    date_str     = started_at.strftime("%A, %d %B %Y")
    duration_s   = int((ended_at - started_at).total_seconds())
    duration_fmt = f"{duration_s//3600:02d}:{(duration_s%3600)//60:02d}:{duration_s%60:02d}"

    md = [
        f"# Transcript — {date_str}",
        "",
        "## Metadata",
        f"- **Start:** {started_at.strftime('%H:%M:%S')}",
        f"- **End:**   {ended_at.strftime('%H:%M:%S')}",
        f"- **Duration:** {duration_fmt}",
        f"- **Whisper model:** {whisper_model}",
        f"- **Audio:** {audio_file_name}",
        f"- **Speakers:** {len({l['speaker'] for l in lines})}",
        "",
    ]

    if cal_event:
        md += [
            "## Calendar event",
            f"- **Subject:** {cal_event.get('subject', '—')}",
            f"- **Organizer:** {cal_event.get('organizer', '—')}",
            f"- **Location:** {cal_event.get('location', '—')}",
            f"- **Attendees:** {cal_event.get('attendees', '—')}",
            f"- **Start:** {cal_event.get('start', '—')}",
            f"- **End:** {cal_event.get('end', '—')}",
            "",
        ]

    md.append("## Transcript")
    md.append("")

    if not lines:
        md.append("_[no speech detected]_")
    else:
        prev_speaker = None
        for line in lines:
            speaker = line["speaker"]
            ts      = _fmt_ts(line["start"])
            text    = line["text"]

            if speaker != prev_speaker:
                if prev_speaker is not None:
                    md.append("")
                # Speaker header
                owner_mark = " ★" if line.get("is_owner") else ""
                md.append(f"**[{speaker}{owner_mark}]** _{ts}_")
                prev_speaker = speaker

            md.append(text)

    md.append("")
    return "\n".join(md)


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"
