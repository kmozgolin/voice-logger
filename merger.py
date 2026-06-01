"""
merger.py — Session-aware transcript merger.

Two merge strategies (mutually exclusive per time window):

  A) Outlook-based (priority):
     When an Outlook calendar event ends, collect all chunk .md files
     whose timestamps fall within [event.start, event.end] and merge
     them into one file named after the event subject.

  B) Silence-gap fallback (only when no Outlook event covers the window):
     Monitor chunk timestamps. If the gap between two consecutive chunks
     exceeds SILENCE_GAP_MINUTES, close the current session and merge
     everything accumulated so far.

After merging, each session is appended to a *permanent* file keyed by the
Outlook category (calendar colour) of the event:
  transcripts/permanent/<Category>.txt

These permanent files are uploaded to Google Drive as Google Docs (so
NotebookLM can index them). The user adds each permanent file to NotebookLM
once; subsequent meetings automatically update the same document.

Drive file IDs are persisted in logs/drive_files.json so we always overwrite
the same Google Doc rather than creating new files.

Permanent files are also synced to Obsidian via the Local REST API plugin.
"""

import json
import logging
import re
import threading
import time
import datetime
from pathlib import Path

import obsidian as obsidian_mod
from typing import Optional

log = logging.getLogger("voice-logger.merger")

SILENCE_GAP_MINUTES   = 1       # gap threshold for fallback strategy
CHECK_INTERVAL_SEC    = 30      # how often to check for completed sessions
SESSIONS_DIR          = "transcripts/sessions"
PERMANENT_DIR         = "transcripts/permanent"
DRIVE_FILES_LOG       = "logs/drive_files.json"
CATEGORY_SESSIONS_LOG = "logs/category_sessions.json"
CONFLICTS_LOG         = "logs/session_conflicts.json"

# Outlook categories that map to "Прочее" (placeholder / blocker events)
CATEGORY_IGNORE  = {"заглушка"}
CATEGORY_DEFAULT = "Прочее"


# ── Category helpers ───────────────────────────────────────────────────────

def _category_key(categories_str: str) -> str:
    """
    Map an Outlook Categories string to the permanent-file key.
    Uses the first category listed; 'Заглушка' and empty → 'Прочее'.
    """
    if not categories_str:
        return CATEGORY_DEFAULT
    first = categories_str.split(";")[0].strip()
    if first.lower() in CATEGORY_IGNORE:
        return CATEGORY_DEFAULT
    return first


# ── Outlook helpers ────────────────────────────────────────────────────────

def _get_outlook_events_today() -> list[dict]:
    """Return today's calendar events as list of dicts."""
    try:
        import win32com.client
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns      = outlook.GetNamespace("MAPI")
        cal     = ns.GetDefaultFolder(9)
        items   = cal.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        today     = datetime.date.today()
        today_str = today.strftime("%m/%d/%Y")
        restriction = (
            f"[Start] >= '{today_str} 00:00' AND "
            f"[Start] <= '{today_str} 23:59'"
        )
        events = []
        for item in items.Restrict(restriction):
            try:
                start = _com_to_dt(item.Start)
                end   = _com_to_dt(item.End)
                events.append({
                    "subject":    item.Subject,
                    "start":      start,
                    "end":        end,
                    "organizer":  getattr(item, "Organizer", ""),
                    "attendees":  getattr(item, "RequiredAttendees", ""),
                    "location":   getattr(item, "Location", ""),
                    "categories": getattr(item, "Categories", "") or "",
                })
            except Exception:
                continue
        return events
    except Exception as e:
        log.debug(f"Outlook events fetch failed: {e}")
        return []


def _com_to_dt(com_time) -> datetime.datetime:
    """Convert COM date to Python datetime."""
    import pywintypes
    if isinstance(com_time, pywintypes.TimeType):
        return datetime.datetime(
            com_time.year, com_time.month, com_time.day,
            com_time.hour, com_time.minute, com_time.second,
        )
    return com_time


def _outlook_event_for_window(
    start: datetime.datetime,
    end: datetime.datetime,
    events: list[dict],
) -> Optional[dict]:
    """Return Outlook event that best covers [start, end], or None."""
    best         = None
    best_overlap = datetime.timedelta(0)
    for ev in events:
        overlap_start = max(start, ev["start"])
        overlap_end   = min(end,   ev["end"])
        overlap = overlap_end - overlap_start
        if overlap > best_overlap:
            best_overlap = overlap
            best = ev
    window = end - start
    if window.total_seconds() > 0 and best_overlap / window >= 0.5:
        return best
    return None


def _get_category_for_session(subject: str, start_dt: datetime.datetime) -> str:
    """
    Look up the Outlook category for a past event by subject and date.
    Used during backfill of existing session files.
    """
    try:
        import win32com.client
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns      = outlook.GetNamespace("MAPI")
        cal     = ns.GetDefaultFolder(9)
        items   = cal.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")
        date_str = start_dt.strftime("%m/%d/%Y")
        restriction = (
            f"[Start] >= '{date_str} 00:00' AND "
            f"[Start] <= '{date_str} 23:59'"
        )
        for item in items.Restrict(restriction):
            try:
                if item.Subject.strip() == subject.strip():
                    return getattr(item, "Categories", "") or ""
            except Exception:
                continue
    except Exception as e:
        log.debug(f"Category lookup failed for '{subject}': {e}")
    return ""


# ── Chunk file helpers ─────────────────────────────────────────────────────

def _chunk_timestamp(path: Path) -> Optional[datetime.datetime]:
    """Parse datetime from chunk filename like 2026-04-01_10-05-00.txt"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})", path.stem)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(
            f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H-%M-%S"
        )
    except ValueError:
        return None


def _list_unmerged_chunks(transcripts_dir: str) -> list[tuple[datetime.datetime, Path]]:
    """Return (timestamp, path) for all chunk files not yet merged, sorted by time."""
    base   = Path(transcripts_dir)
    chunks = []
    for txt in base.rglob("*.txt"):
        if "sessions" in txt.parts or "permanent" in txt.parts:
            continue
        ts = _chunk_timestamp(txt)
        if ts:
            chunks.append((ts, txt))
    chunks.sort(key=lambda x: x[0])
    return chunks


# ── Session file helpers ───────────────────────────────────────────────────

def _parse_session_file(path: Path) -> Optional[dict]:
    """
    Parse a merged session .txt to extract title, start datetime, and
    calendar event subject (for category backfill).
    Returns None if the file can't be parsed.
    """
    try:
        content   = path.read_text(encoding="utf-8")
        title     = ""
        start_str = ""
        subject   = ""
        for line in content.splitlines():
            if line.startswith("# ") and not title:
                title = line[2:].strip()
            elif "**Start:**" in line and not start_str:
                start_str = line.split("**Start:**")[-1].strip()
            elif "**Subject:**" in line and not subject:
                subject = line.split("**Subject:**")[-1].strip()
        if not title or not start_str:
            return None
        start_dt = datetime.datetime.strptime(start_str.strip(), "%Y-%m-%d %H:%M:%S")
        return {
            "title":     title,
            "start_str": start_dt.strftime("%Y-%m-%d %H:%M"),
            "start_dt":  start_dt,
            "subject":   subject or title,
        }
    except Exception as e:
        log.debug(f"Could not parse {path.name}: {e}")
        return None


# ── Merge logic ────────────────────────────────────────────────────────────

def _merge_chunks(
    chunks: list[tuple[datetime.datetime, Path]],
    title: str,
    cal_event: Optional[dict],
    out_dir: Path,
) -> Optional[Path]:
    """Merge a list of chunk .txt files into one session file."""
    if not chunks:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)

    first_ts = chunks[0][0]
    last_ts  = chunks[-1][0]
    fname    = f"{first_ts.strftime('%Y-%m-%d_%H-%M-%S')}_{_safe_name(title)}.txt"
    out_path = out_dir / fname

    duration_s = int((last_ts - first_ts).total_seconds())
    dur_fmt    = f"{duration_s//3600:02d}:{(duration_s%3600)//60:02d}:{duration_s%60:02d}"

    lines = [
        f"# {title}",
        "",
        "## Session metadata",
        f"- **Start:**    {first_ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **End:**      {last_ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Duration:** {dur_fmt}",
        f"- **Chunks:**   {len(chunks)}",
        "",
    ]

    if cal_event:
        lines += [
            "## Calendar event",
            f"- **Subject:**    {cal_event.get('subject', '—')}",
            f"- **Categories:** {cal_event.get('categories', '—')}",
            f"- **Organizer:**  {cal_event.get('organizer', '—')}",
            f"- **Location:**   {cal_event.get('location', '—')}",
            f"- **Attendees:**  {cal_event.get('attendees', '—')}",
            "",
        ]

    lines += ["## Transcript", ""]

    for ts, path in chunks:
        try:
            content = path.read_text(encoding="utf-8")
            in_transcript = False
            for line in content.splitlines():
                if line.strip() == "## Transcript":
                    in_transcript = True
                    continue
                if in_transcript and line.startswith("## "):
                    break
                if in_transcript:
                    lines.append(line)
        except Exception as e:
            log.warning(f"Could not read chunk {path}: {e}")
            continue

    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"Merged {len(chunks)} chunks -> {out_path.name}")
    return out_path


def _safe_name(s: str) -> str:
    """Make a string safe for use as a filename."""
    s = re.sub(r'[<>:"/\\|?*]', '', s)
    s = re.sub(r'\s+', '_', s.strip())
    return s[:60] or "session"


# ── Event serialisation helper ─────────────────────────────────────────────

def _event_to_dict(ev: dict) -> dict:
    """Convert an Outlook event dict to a JSON-safe dict (datetimes → str)."""
    return {
        "subject":    ev.get("subject", ""),
        "start":      str(ev.get("start", "")),
        "end":        str(ev.get("end", "")),
        "categories": ev.get("categories", ""),
        "organizer":  ev.get("organizer", ""),
    }


# ── Standalone helpers (usable from dashboard without a running merger) ────

def rebuild_permanent_file_standalone(category: str) -> Optional[Path]:
    """Rebuild transcripts/permanent/<category>.txt from category_sessions.json."""
    p = Path(CATEGORY_SESSIONS_LOG)
    if not p.exists():
        return None
    try:
        cat_sessions = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

    sessions = cat_sessions.get(category, [])
    if not sessions:
        return None

    out_dir = Path(PERMANENT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[<>:"/\\|?*]', '', category)
    out_path  = out_dir / f"{safe_name}.txt"

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# {category} — история встреч",
        "",
        f"> Обновлено: {now_str}",
        "",
    ]
    for entry in sessions:
        path = Path(entry["path"])
        if not path.exists():
            continue
        lines += ["---", "", f"## {entry['start']} | {entry['title']}", ""]
        content = path.read_text(encoding="utf-8")
        in_transcript = False
        for line in content.splitlines():
            if line.strip() == "## Transcript":
                in_transcript = True
                continue
            if in_transcript and line.startswith("## "):
                break
            if in_transcript:
                lines.append(line)
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"Permanent '{category}' rebuilt: {out_path.name} ({len(sessions)} sessions)")
    return out_path


def reassign_session(
    conflict_id: str,
    session_path: str,
    from_event: dict,
    to_event: dict,
) -> bool:
    """
    Move a session entry from one Outlook-category bucket to another.
    Updates category_sessions.json and marks the conflict resolved.
    Does NOT re-upload to Drive (caller should do that if needed).
    """
    from_category = _category_key(from_event.get("categories", ""))
    to_category   = _category_key(to_event.get("categories", ""))

    p = Path(CATEGORY_SESSIONS_LOG)
    cat_sessions: dict = {}
    if p.exists():
        try:
            cat_sessions = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Remove from old category
    entry: Optional[dict] = None
    if from_category in cat_sessions:
        for e in cat_sessions[from_category]:
            if e["path"] == session_path:
                entry = e.copy()
                break
        cat_sessions[from_category] = [
            e for e in cat_sessions[from_category]
            if e["path"] != session_path
        ]

    # Reconstruct entry if missing
    if entry is None:
        sp = Path(session_path)
        m  = re.search(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})", sp.stem)
        start_str = m.group(1).replace("_", " ").replace("-", ":", 2) if m else ""
        entry = {"path": session_path, "title": to_event.get("subject", sp.stem), "start": start_str}
    else:
        entry["title"] = to_event.get("subject", entry.get("title", ""))

    # Add to new category
    cat_list = cat_sessions.setdefault(to_category, [])
    if not any(e["path"] == session_path for e in cat_list):
        cat_list.append(entry)
    cat_sessions[to_category] = sorted(cat_list, key=lambda x: x["start"])
    p.write_text(json.dumps(cat_sessions, indent=2, ensure_ascii=False), encoding="utf-8")

    # Mark conflict resolved
    cp = Path(CONFLICTS_LOG)
    if cp.exists():
        try:
            conflicts = json.loads(cp.read_text(encoding="utf-8"))
            if conflict_id in conflicts:
                conflicts[conflict_id]["resolved"]    = True
                conflicts[conflict_id]["resolved_to"] = to_event.get("subject", "")
                cp.write_text(json.dumps(conflicts, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    log.info(f"Session '{Path(session_path).name}' reassigned: '{from_category}' → '{to_category}'")
    return True


# ── Session detector ───────────────────────────────────────────────────────

class SessionMerger:
    """
    Background thread that watches for completed sessions and merges chunks.
    Also maintains permanent per-category files for NotebookLM.
    """

    def __init__(self, cfg: dict, upload_fn=None):
        self.cfg       = cfg
        self.upload_fn = upload_fn   # upload_to_gdrive(path, folder_id, file_id, as_gdoc)
        self._stop     = threading.Event()
        self._thread   = None
        self._merged: set[str] = set()
        self._load_merged_log()

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("SessionMerger started.")
        # Backfill existing session files into permanent files (background)
        threading.Thread(target=self._backfill_permanent_files, daemon=True).start()

    def stop(self):
        self._stop.set()

    # ── Merged-chunks log ──────────────────────────────────────────────────

    def _merged_log_path(self) -> Path:
        return Path("logs/merged_chunks.json")

    def _load_merged_log(self):
        p = self._merged_log_path()
        if p.exists():
            try:
                self._merged = set(json.loads(p.read_text()))
            except Exception:
                self._merged = set()

    def _save_merged_log(self):
        p = self._merged_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(list(self._merged)))

    # ── Drive file ID tracking ─────────────────────────────────────────────

    def _load_drive_files(self) -> dict:
        """Load {category: drive_file_id} mapping."""
        p = Path(DRIVE_FILES_LOG)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_drive_files(self, data: dict):
        p = Path(DRIVE_FILES_LOG)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── Conflict tracking ──────────────────────────────────────────────────

    def _load_conflicts(self) -> dict:
        p = Path(CONFLICTS_LOG)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_conflicts(self, data: dict):
        p = Path(CONFLICTS_LOG)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def _record_conflict(
        self,
        session_path: Path,
        session_title: str,
        chosen_event: dict,
        alternatives: list[dict],
    ):
        import uuid as _uuid
        conflicts = self._load_conflicts()
        cid = str(_uuid.uuid4())[:8]
        conflicts[cid] = {
            "session_path":  str(session_path),
            "session_title": session_title,
            "chosen_event":  _event_to_dict(chosen_event),
            "alternatives":  [_event_to_dict(e) for e in alternatives],
            "resolved":      False,
            "created_at":    datetime.datetime.now().isoformat(),
        }
        self._save_conflicts(conflicts)
        log.info(
            f"Conflict logged for '{session_title}': chosen '{chosen_event.get('subject')}', "
            f"alternatives: {[e.get('subject') for e in alternatives]}"
        )

    # ── Category → session tracking ────────────────────────────────────────

    def _load_cat_sessions(self) -> dict:
        """Load {category: [{path, title, start}, ...]} mapping."""
        p = Path(CATEGORY_SESSIONS_LOG)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_cat_sessions(self, data: dict):
        p = Path(CATEGORY_SESSIONS_LOG)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── Permanent file ─────────────────────────────────────────────────────

    def _rebuild_permanent_file(self, category: str) -> Optional[Path]:
        """
        Rebuild transcripts/permanent/<category>.txt from all known sessions
        for that category (chronological order).
        """
        cat_sessions = self._load_cat_sessions()
        sessions     = cat_sessions.get(category, [])
        if not sessions:
            return None

        out_dir = Path(PERMANENT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[<>:"/\\|?*]', '', category)
        out_path  = out_dir / f"{safe_name}.txt"

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# {category} — история встреч",
            "",
            f"> Обновлено: {now_str}",
            "",
        ]

        for entry in sessions:
            path = Path(entry["path"])
            if not path.exists():
                continue
            lines += ["---", "", f"## {entry['start']} | {entry['title']}", ""]
            # Extract only the Transcript section from the session file
            content      = path.read_text(encoding="utf-8")
            in_transcript = False
            for line in content.splitlines():
                if line.strip() == "## Transcript":
                    in_transcript = True
                    continue
                if in_transcript and line.startswith("## "):
                    break
                if in_transcript:
                    lines.append(line)
            lines.append("")

        out_path.write_text("\n".join(lines), encoding="utf-8")
        log.info(
            f"Permanent '{category}': rebuilt {out_path.name} "
            f"({len(sessions)} session(s))"
        )
        return out_path

    def _upload_permanent(self, path: Path, category: str):
        """Upload (create or overwrite) a permanent file to Google Drive as a Google Doc."""
        folder_id = self.cfg.get("gdrive_folder_id", "")
        if not self.upload_fn or not folder_id:
            return
        drive_files = self._load_drive_files()
        existing_id = drive_files.get(category)
        try:
            new_id = self.upload_fn(
                path, folder_id,
                file_id=existing_id,
                as_gdoc=True,
            )
            if new_id:
                drive_files[category] = new_id
                self._save_drive_files(drive_files)
                action = "updated" if existing_id else "created"
                log.info(f"Permanent '{category}' {action} on Drive (id={new_id})")
        except Exception as e:
            log.warning(f"Permanent file upload failed for '{category}': {e}")

    def _register_session(
        self,
        out_path: Path,
        title: str,
        first_ts: datetime.datetime,
        category: str,
    ) -> bool:
        """
        Add a session to the category tracking log.
        Returns True if it was newly added (needs permanent rebuild).
        """
        cat_sessions = self._load_cat_sessions()
        cat_list     = cat_sessions.setdefault(category, [])
        entry        = {
            "path":  str(out_path),
            "title": title,
            "start": first_ts.strftime("%Y-%m-%d %H:%M"),
        }
        if any(e["path"] == str(out_path) for e in cat_list):
            return False  # already tracked
        cat_list.append(entry)
        cat_sessions[category] = sorted(cat_list, key=lambda x: x["start"])
        self._save_cat_sessions(cat_sessions)
        return True

    # ── Backfill ───────────────────────────────────────────────────────────

    def _backfill_permanent_files(self):
        """
        On startup: scan transcripts/sessions/ for files not yet tracked,
        look up their Outlook categories, and rebuild permanent files.
        """
        cat_sessions = self._load_cat_sessions()
        already_tracked = {
            e["path"]
            for entries in cat_sessions.values()
            for e in entries
        }

        sessions_dir = (
            Path(self.cfg.get("transcripts_dir", "transcripts")) / "sessions"
        )
        if not sessions_dir.exists():
            return

        new_entries: list[tuple[str, dict]] = []
        for path in sorted(sessions_dir.glob("*.txt")):
            if str(path) in already_tracked:
                continue
            info = _parse_session_file(path)
            if not info:
                continue
            cat_str  = _get_category_for_session(info["subject"], info["start_dt"])
            category = _category_key(cat_str)
            new_entries.append((
                category,
                {"path": str(path), "title": info["title"], "start": info["start_str"]},
            ))

        if not new_entries:
            log.info("Permanent files: nothing to backfill.")
            return

        log.info(f"Backfilling {len(new_entries)} session(s) into permanent files...")
        cats_to_rebuild: set[str] = set()
        for category, entry in new_entries:
            cat_list = cat_sessions.setdefault(category, [])
            if not any(e["path"] == entry["path"] for e in cat_list):
                cat_list.append(entry)
                cats_to_rebuild.add(category)

        for cat in cats_to_rebuild:
            cat_sessions[cat] = sorted(cat_sessions[cat], key=lambda x: x["start"])
        self._save_cat_sessions(cat_sessions)

        for cat in cats_to_rebuild:
            perm = self._rebuild_permanent_file(cat)
            if perm:
                self._upload_permanent(perm, cat)

        log.info("Backfill complete.")

    # ── Main loop ──────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop.wait(CHECK_INTERVAL_SEC):
            try:
                self._check_sessions()
            except Exception as e:
                log.error(f"SessionMerger error: {e}", exc_info=True)

    def _check_sessions(self):
        trans_dir = self.cfg.get("transcripts_dir", "transcripts")
        chunks    = _list_unmerged_chunks(trans_dir)
        chunks    = [(ts, p) for ts, p in chunks if str(p) not in self._merged]

        if not chunks:
            return

        now    = datetime.datetime.now()
        events = _get_outlook_events_today()

        # ── Strategy A: Outlook-based ──────────────────────────────────────
        event_groups: dict[str, list] = {}
        no_event_chunks = []
        # {chunk_path_str: [alternative events not chosen]}
        _chunk_alts: dict[str, list[dict]] = {}

        for ts, path in chunks:
            # Collect ALL events that cover this timestamp
            matching = [e for e in events if e["start"] <= ts <= e["end"]]
            if not matching:
                no_event_chunks.append((ts, path))
                continue

            if len(matching) > 1:
                # Multiple events overlap → prefer newest (latest start time)
                matching.sort(key=lambda e: e["start"], reverse=True)
                _chunk_alts[str(path)] = matching[1:]   # alternatives for conflict log

            ev  = matching[0]
            key = f"{ev['start'].isoformat()}_{ev['subject']}"
            event_groups.setdefault(key, []).append((ts, path, ev))

        for key, group in event_groups.items():
            ev = group[0][2]
            if now < ev["end"] + datetime.timedelta(minutes=2):
                continue
            chunk_list = [(ts, p) for ts, p, _ in group]

            # Collect unique alternative events across all chunks in this group
            alts_seen: set = set()
            conflict_alts: list[dict] = []
            for _, p, _ in group:
                for alt in _chunk_alts.get(str(p), []):
                    dedup_key = f"{alt['start']}_{alt['subject']}"
                    if dedup_key not in alts_seen:
                        alts_seen.add(dedup_key)
                        conflict_alts.append(alt)

            self._do_merge(
                chunk_list,
                title=ev["subject"],
                cal_event=ev,
                trans_dir=trans_dir,
                conflict_alternatives=conflict_alts,
            )

        # ── Strategy B: Silence-gap fallback ──────────────────────────────
        if not no_event_chunks:
            return

        gap      = datetime.timedelta(minutes=SILENCE_GAP_MINUTES)
        sessions = []
        current  = [no_event_chunks[0]]

        for ts, path in no_event_chunks[1:]:
            if ts - current[-1][0] > gap:
                sessions.append(current)
                current = []
            current.append((ts, path))
        sessions.append(current)

        for session in sessions:
            last_ts = session[-1][0]
            if now - last_ts < gap:
                continue
            if len(session) < 2:
                ts, path = session[0]
                if now - ts > datetime.timedelta(hours=2):
                    self._merged.add(str(path))
                continue

            first_ts = session[0][0]
            title    = f"Session {first_ts.strftime('%H:%M')}"
            self._do_merge(session, title=title, cal_event=None, trans_dir=trans_dir)

        self._save_merged_log()

    def _do_merge(
        self,
        chunks: list[tuple[datetime.datetime, Path]],
        title: str,
        cal_event: Optional[dict],
        trans_dir: str,
        conflict_alternatives: Optional[list[dict]] = None,
    ):
        out_dir  = Path(trans_dir) / "sessions"
        out_path = _merge_chunks(chunks, title, cal_event, out_dir)
        if not out_path:
            return

        # Mark originals as merged
        for _, p in chunks:
            self._merged.add(str(p))
        self._save_merged_log()

        # Determine category
        categories_str = (cal_event or {}).get("categories", "")
        category       = _category_key(categories_str)

        # Register in category log and rebuild permanent file
        is_new = self._register_session(out_path, title, chunks[0][0], category)
        if is_new:
            perm_path = self._rebuild_permanent_file(category)
            if perm_path:
                self._upload_permanent(perm_path, category)

        # Upload session to Obsidian (one meeting = one file)
        obs_url = self.cfg.get("obsidian_url", "")
        obs_key = self.cfg.get("obsidian_api_key", "")
        if obs_url and obs_key:
            obsidian_mod.upload_session(out_path, title, chunks[0][0], obs_url, obs_key)

        # Record conflict if there were alternative events the user could reassign to
        if conflict_alternatives and cal_event:
            self._record_conflict(out_path, title, cal_event, conflict_alternatives)
