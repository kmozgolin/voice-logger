# Voice Logger

Windows background service that continuously records meeting audio, transcribes it with Whisper, identifies speakers with pyannote, and syncs structured Markdown transcripts to Obsidian.

## Features

- **Dual-stream capture** — mic + Windows WASAPI loopback (Zoom, Teams, Meet)
- **Whisper transcription** — word-level timestamps, GPU-accelerated
- **Speaker diarization** — pyannote 3.1, automatic unknown speaker registration
- **Voice enrollment** — enroll your own voice, get starred ★ in transcripts
- **Outlook calendar integration** — groups chunks into sessions by meeting title
- **Obsidian sync** — auto-uploads sessions via Local REST API plugin
- **Web dashboard** — live status, speaker management, transcript browser, system diagnostics

![Dashboard](dashboard/preview.png)

## Requirements

- Windows 10/11
- Python 3.11
- NVIDIA GPU recommended (runs on CPU too, just slower)
- [ffmpeg](https://ffmpeg.org/) in PATH
- Microsoft Outlook (optional, for calendar grouping)

## Setup

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/voice-logger.git
cd voice-logger

# 2. Install dependencies (PyTorch + everything else)
python setup.py

# 3. Copy config and fill in your tokens
cp config.example.json config.json
# Edit config.json — see "Configuration" below

# 4. Enroll your voice (optional but recommended)
python enroll_me.py

# 5. Start
start.bat
```

The dashboard opens at **http://localhost:7331**

## Required tokens and credentials

### 1. HuggingFace token — required for speaker diarization

Pyannote diarization models are gated on HuggingFace — you need a free account and must accept each model's license:

1. Create account at https://huggingface.co and go to https://huggingface.co/settings/tokens
2. Create a token with **Read** access
3. Accept the license for both models (you must be logged in):
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0
4. Put the token in `config.json` → `hf_token`

Without this token diarization is disabled and all speakers appear as "Speaker_N".

### 2. Obsidian Local REST API key — optional, for syncing transcripts to Obsidian

1. Install the [Local REST API](https://github.com/coddingtonbear/obsidian-local-rest-api) community plugin in Obsidian
2. Open plugin settings → copy the **API Key**
3. Put it in `config.json` → `obsidian_api_key`
4. Set `obsidian_url` to `https://127.0.0.1:27124` (default) or whatever port the plugin shows

Without this key Obsidian sync is skipped; transcripts are still saved locally.

## Configuration

Copy `config.example.json` → `config.json` and set:

| Key | Required | Description |
|-----|----------|-------------|
| `hf_token` | Yes (for diarization) | HuggingFace token — see section above |
| `whisper_model` | No | `tiny` / `base` / `small` / `medium` / `large` (default `large`) |
| `language` | No | `ru`, `en`, etc. — or `null` for auto-detect |
| `chunk_seconds` | No | Recording chunk length in seconds (default 300) |
| `obsidian_api_key` | No | Obsidian Local REST API key — see section above |
| `obsidian_url` | No | Obsidian REST API URL (default `https://127.0.0.1:27124`) |
| `loopback_device` | No | WASAPI loopback device index — `null` = auto-detect |
| `gpu_memory_fraction` | No | Fraction of GPU VRAM to use (default `0.8`, raise to `0.9` if OOM) |

## Running

```bash
start.bat    # starts recorder + dashboard, opens browser
stop.bat     # stops both processes
```

Or manually:
```bash
python recorder.py                           # main recording daemon
python recorder.py --model small --chunk 60  # lighter config
python dashboard_server.py                   # dashboard at :7331
```

## Architecture

```
start.bat
├── recorder.py          — main loop: capture → transcribe → diarize → upload
│   ├── audio_capture.py — dual-stream WASAPI capture
│   ├── diarize.py       — pyannote wrapper + Whisper alignment
│   ├── speakers.py      — voice registry, cosine-similarity ID
│   ├── labeler.py       — unknown speaker queue + toast notifications
│   └── merger.py        — session grouping by calendar / silence gap
└── dashboard_server.py  — Flask API + static dashboard UI
```

Transcripts are saved as Markdown to `transcripts/YYYY-MM-DD/` and merged into session files under `transcripts/sessions/`. Permanent per-category files in `transcripts/permanent/` are synced to Obsidian.

## License

MIT
