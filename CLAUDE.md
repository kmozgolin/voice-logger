# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

**voice-logger** is a Windows background service that continuously records meeting audio (microphone + system loopback), transcribes it with Whisper, diarizes speakers with pyannote.audio, and syncs structured Markdown transcripts to Google Drive for NotebookLM ingestion.

## Commands

**First-time setup (run once):**
```
python setup.py          # installs deps, creates config.json, start.bat, stop.bat
python enroll_me.py      # records 30s of your voice → creates owner speaker profile
python check_gdrive.py   # verifies Google Drive credentials and tests upload
```

**Running the system:**
```
start.bat                # starts recorder.py + dashboard_server.py + opens browser
stop.bat                 # kills both processes
```

**Or manually:**
```
python recorder.py                          # main recording daemon
python recorder.py --model small --chunk 120  # override model and chunk size
python dashboard_server.py                  # Flask API + dashboard UI at http://localhost:7331
```

**Dependencies:**
```
pip install -r requirements.txt
# PyTorch must be installed separately (see setup.py for CUDA vs CPU variants)
# Requires ffmpeg in PATH (for Whisper)
# Requires HuggingFace token in config.json for pyannote diarization models
```

## Architecture

The system runs as two independent processes:

### recorder.py (main daemon)
Drives the full pipeline in a loop:
1. **AudioMixer** (`audio_capture.py`) — captures mic + Windows WASAPI loopback simultaneously, mixes to mono 16kHz, emits 500ms blocks
2. **Silence detection** — skips chunks below RMS threshold (no active meeting)
3. **Whisper transcription** — word-level timestamps, runs on GPU if available
4. **Diarization** (`diarize.py`) — pyannote `speaker-diarization-3.1` identifies who speaks when
5. **Speaker identification** (`speakers.py`) — matches diarization segments against enrolled voice embeddings; auto-registers unknowns as `Speaker_N`
6. **Labeling** (`labeler.py`) — queues unknown speakers for user naming via the dashboard; sends Windows toast notifications
7. **Transcript save** — Markdown files to `transcripts/YYYY-MM-DD/YYYY-MM-DD_HH-MM-SS.txt`
8. **Session merge** (`merger.py`) — background thread groups chunks into sessions by Outlook calendar event or silence gap; writes to `transcripts/sessions/`; maintains per-category permanent files in `transcripts/permanent/` for NotebookLM
9. **Google Drive upload** — each transcript chunk and permanent file is uploaded/updated as a Google Doc

Steps 3–9 run in parallel threads per chunk (one thread per chunk via `process_chunk()`).

### dashboard_server.py (Flask API)
Serves `index.html` (static dashboard) and REST API:
- `GET /api/status` — recorder state, current chunk, loopback status
- `GET/POST /api/config` — read/write `config.json` (allowlisted keys only)
- `GET /api/transcripts`, `GET /api/transcript?path=...` — browse and read transcripts
- `GET /api/speakers`, `POST /api/speakers/<id>/rename`, `DELETE /api/speakers/<id>` — speaker registry management
- `GET /api/pending_labels`, `POST /api/label_speaker` — interactive speaker labeling flow
- `GET /api/pipeline_stats`, `GET /api/system_load` — monitoring
- `POST /api/recorder/start`, `POST /api/recorder/stop` — process management via PID file

### Key modules

| File | Role |
|------|------|
| `audio_capture.py` | `AudioMixer` class: dual-stream capture with thread-safe queue |
| `diarize.py` | pyannote wrapper + Whisper↔diarization alignment + Markdown formatter |
| `speakers.py` | Speaker registry (`speakers/registry.json` + `speakers/embeddings/*.npy`); enrollment, cosine-similarity identification, auto-unknown registration |
| `labeler.py` | Thread-safe labeling queue; audio clip extraction for dashboard playback |
| `merger.py` | `SessionMerger` background thread; Outlook event grouping; permanent per-category files |
| `recorder.py` | Config, status file, Whisper loader, Outlook calendar lookup, Drive upload, main loop |

## Configuration

All runtime settings live in `config.json` (auto-created by `setup.py`). Key fields:

```json
{
  "whisper_model": "large",        // tiny|base|small|medium|large
  "language": "ru",                // null = auto-detect
  "chunk_seconds": 300,            // recording chunk length
  "diarization_enabled": true,
  "hf_token": "hf_...",           // HuggingFace token for pyannote
  "gdrive_folder_id": "...",       // Google Drive folder for NotebookLM
  "loopback_device": null,         // null = auto-detect WASAPI loopback
  "keep_audio": false              // delete .wav after transcription
}
```

## Runtime file layout

```
logs/
  voice-logger.log        ← recorder stdout log
  status.json             ← live state read by dashboard
  pipeline_stats.json     ← per-chunk timing stats
  recorder.pid            ← PID for dashboard process management
  merged_chunks.json      ← set of chunk paths already merged into sessions
  drive_files.json        ← {category → Google Drive file ID} for upsert
  category_sessions.json  ← {category → [{path, title, start}]} index
speakers/
  registry.json           ← speaker metadata
  embeddings/<id>.npy     ← 512-dim mean voice embedding per speaker
  clips/<id>.wav          ← short preview clips for labeling UI
transcripts/
  YYYY-MM-DD/             ← raw chunk files
  sessions/               ← merged session files (named by Outlook subject)
  permanent/              ← one file per Outlook category, appended over time
```

## Speaker identification details

- Similarity threshold: `0.75` for regular speakers, `0.70` for the owner (in `speakers.py`)
- Owner profile (enrolled via `enroll_me.py`) uses a running mean of multiple samples for robustness
- Unknown speakers are auto-registered as `Speaker_N` and their embeddings are updated when re-identified with score ≥ 0.85
- `identify_speaker()` slices a temp WAV for each diarization segment before embedding extraction

## Session merging strategy

`merger.py` runs every 30 seconds and applies two strategies:
1. **Outlook-based** (priority): groups chunks by the calendar event active at chunk time; merges 2 minutes after event ends
2. **Silence-gap fallback**: groups by consecutive chunks with gap < 1 minute; merges sessions idle for > 1 minute with ≥ 2 chunks

Permanent files (`transcripts/permanent/<Category>.txt`) aggregate all sessions for a given Outlook category and are uploaded to Drive as Google Docs. NotebookLM sources point to these permanent files, which are overwritten in-place on Drive.
