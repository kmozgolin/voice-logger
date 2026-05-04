"""
obsidian.py — Upload session transcripts to Obsidian via Local REST API.

Each merged session is PUT to:
  Claude vault/Voice Logger/<YYYY-MM-DD> <title>.md

Self-signed cert is accepted (Obsidian plugin uses a local cert).
"""
import datetime
import logging
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("voice-logger.obsidian")

_VAULT_FOLDER = "Claude vault/Voice Logger"

# Characters forbidden in Obsidian note names
_FORBIDDEN = re.compile(r'[\\/:*?"<>|#^[\]]')


def _sanitize(title: str) -> str:
    return _FORBIDDEN.sub("", title).strip() or "Untitled"


def upload_session(
    session_path: Path,
    title: str,
    start_time: datetime.datetime,
    obsidian_url: str,
    api_key: str,
) -> bool:
    """
    Upload a session transcript file to Obsidian.
    Returns True on success, False on failure (never raises).
    """
    date_prefix = start_time.strftime("%Y-%m-%d") if start_time else ""
    safe_title  = _sanitize(title)
    filename    = f"{date_prefix} {safe_title}.md" if date_prefix else f"{safe_title}.md"
    vault_path  = f"{_VAULT_FOLDER}/{filename}"
    url         = f"{obsidian_url.rstrip('/')}/vault/{urllib.parse.quote(vault_path)}"

    try:
        content = session_path.read_text(encoding="utf-8").encode("utf-8")
    except Exception as e:
        log.warning(f"Obsidian: cannot read session file '{session_path}': {e}")
        return False

    req = urllib.request.Request(
        url,
        data=content,
        method="PUT",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "text/markdown; charset=utf-8",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            log.info(f"Obsidian upload OK: '{filename}' (HTTP {resp.status})")
            return True
    except Exception as e:
        log.warning(f"Obsidian upload failed for '{filename}': {e}")
        return False
