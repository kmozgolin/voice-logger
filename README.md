# Voice Logger

Windows background service that continuously records meeting audio, transcribes it with Whisper, identifies speakers with pyannote, and syncs structured Markdown transcripts to Google Drive for NotebookLM ingestion.

## Features

- **Dual-stream capture** — mic + Windows WASAPI loopback (Zoom, Teams, Meet)
- **Whisper transcription** — word-level timestamps, GPU-accelerated
- **Speaker diarization** — pyannote 3.1, automatic unknown speaker registration
- **Voice enrollment** — enroll your own voice, get starred ★ in transcripts
- **Outlook calendar integration** — groups chunks into sessions by meeting title
- **Google Drive sync** — uploads as Google Docs for NotebookLM
- **Obsidian integration** — optional sync via Local REST API plugin
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

# 4. Set up Google Drive (optional)
#    Download credentials.json from Google Cloud Console
#    Enable Drive API, create OAuth 2.0 Desktop credentials
python check_gdrive.py

# 5. Enroll your voice (optional but recommended)
python enroll_me.py

# 6. Start
start.bat
```

The dashboard opens at **http://localhost:7331**

## Configuration

Copy `config.example.json` → `config.json` and set:

| Key | Description |
|-----|-------------|
| `hf_token` | HuggingFace token — needed for pyannote diarization models |
| `gdrive_folder_id` | Google Drive folder ID for transcript upload |
| `whisper_model` | `tiny` / `base` / `small` / `medium` / `large` |
| `language` | `ru`, `en`, etc. — or `null` for auto-detect |
| `chunk_seconds` | Recording chunk length in seconds (default 300) |
| `obsidian_url` | Local REST API URL if using Obsidian sync |
| `obsidian_api_key` | Local REST API key (from Obsidian plugin settings) |

HuggingFace token: https://huggingface.co/settings/tokens  
You must also accept the pyannote model license at:  
https://huggingface.co/pyannote/speaker-diarization-3.1

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

Transcripts are saved as Markdown to `transcripts/YYYY-MM-DD/` and merged into session files under `transcripts/sessions/`. Permanent per-category files in `transcripts/permanent/` are uploaded to Google Drive.

## License

MIT
