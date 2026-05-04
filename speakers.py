"""
speakers.py — Speaker registry with voice-embedding enrollment and identification.

Flow:
  1. Enrollment: record ~30 sec of a person → extract embedding → save to DB
  2. Identification: for each pyannote segment, compare embedding vs DB
     - cosine similarity ≥ threshold → known speaker (use saved name)
     - below threshold → "Speaker_N" (auto-numbered, auto-saved for future)
  3. Owner profile: special "You" entry enrolled at setup; gets priority match.

Storage layout  (speakers/ folder):
  speakers/
    registry.json          ← {id: {name, enrolled_at, samples, is_owner, ...}}
    embeddings/
      <id>.npy             ← mean embedding vector (shape: [512])
"""

import json
import uuid
import logging
import datetime
import numpy as np
from pathlib import Path
from typing import Optional
import torchaudio_compat  # noqa: F401 — patches torchaudio before pyannote imports it

log = logging.getLogger("voice-logger.speakers")

SPEAKERS_DIR   = Path("speakers")
EMBEDDINGS_DIR = SPEAKERS_DIR / "embeddings"
REGISTRY_FILE  = SPEAKERS_DIR / "registry.json"

SIMILARITY_THRESHOLD = 0.65    # cosine similarity; lowered for mic-only conditions
OWNER_THRESHOLD      = 0.58    # slightly looser for the owner (you talk in many styles)
SUSPICIOUS_LOW       = 0.50    # pairs in [SUSPICIOUS_LOW, SIMILARITY_THRESHOLD) shown for manual review

DISMISSED_FILE = SPEAKERS_DIR / "dismissed_pairs.json"

# ── lazy model holder ──────────────────────────────────────────────────────
_embedding_model = None

def _get_embedding_model():
    """Load pyannote SpeakerEmbedding model (once)."""
    global _embedding_model
    if _embedding_model is not None:
        return _embedding_model
    try:
        from pyannote.audio import Model, Inference
        # Read HF token from config if available
        token = None
        try:
            import json
            cfg_path = Path("config.json")
            if cfg_path.exists():
                token = json.loads(cfg_path.read_text()).get("hf_token") or None
        except Exception:
            pass
        model = Model.from_pretrained(
            "pyannote/wespeaker-voxceleb-resnet34-LM",
            use_auth_token=token,
        )
        _embedding_model = Inference(model, window="whole")
        log.info("Speaker embedding model loaded.")
    except Exception as e:
        log.error(f"Failed to load embedding model: {e}")
        _embedding_model = None
    return _embedding_model


# ── Registry I/O ───────────────────────────────────────────────────────────
def _load_registry() -> dict:
    SPEAKERS_DIR.mkdir(parents=True, exist_ok=True)
    EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    return {}


def _save_registry(reg: dict):
    REGISTRY_FILE.write_text(
        json.dumps(reg, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def list_speakers() -> list[dict]:
    reg = _load_registry()
    return [{"id": k, **v} for k, v in reg.items()]


def get_owner() -> Optional[dict]:
    reg = _load_registry()
    for sid, info in reg.items():
        if info.get("is_owner"):
            return {"id": sid, **info}
    return None


# ── Embedding helpers ──────────────────────────────────────────────────────
def _extract_embedding(audio_path: Path) -> Optional[np.ndarray]:
    model = _get_embedding_model()
    if model is None:
        return None
    try:
        emb = model(str(audio_path))
        # emb may be (1, D) or (D,)
        vec = np.array(emb).flatten()
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 1e-9 else vec
    except Exception as e:
        log.error(f"Embedding extraction error: {e}")
        return None


def _load_embedding(speaker_id: str) -> Optional[np.ndarray]:
    path = EMBEDDINGS_DIR / f"{speaker_id}.npy"
    if not path.exists():
        return None
    return np.load(str(path))


def _save_embedding(speaker_id: str, vec: np.ndarray):
    np.save(str(EMBEDDINGS_DIR / f"{speaker_id}.npy"), vec)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ── Enrollment ─────────────────────────────────────────────────────────────
def enroll_speaker(
    audio_path: Path,
    name: str,
    is_owner: bool = False,
) -> Optional[str]:
    """
    Enroll a speaker from an audio file.
    Returns the new speaker ID, or None on failure.
    Recommended audio: 20–60 sec of clean speech, one speaker.
    """
    log.info(f"Enrolling speaker '{name}' from {audio_path} …")
    vec = _extract_embedding(audio_path)
    if vec is None:
        log.error("Could not extract embedding — enrollment failed.")
        return None

    reg = _load_registry()

    # If owner already exists, just update embedding
    if is_owner:
        for sid, info in reg.items():
            if info.get("is_owner"):
                existing = _load_embedding(sid)
                if existing is not None:
                    # running mean of embeddings for robustness
                    merged = (existing * info.get("samples", 1) + vec) / (info.get("samples", 1) + 1)
                    norm = np.linalg.norm(merged)
                    merged = merged / norm if norm > 1e-9 else merged
                    _save_embedding(sid, merged)
                else:
                    _save_embedding(sid, vec)
                reg[sid]["samples"] = info.get("samples", 1) + 1
                reg[sid]["last_updated"] = datetime.datetime.now().isoformat()
                _save_registry(reg)
                log.info(f"Owner embedding updated (sid={sid})")
                return sid

    speaker_id = str(uuid.uuid4())[:8]
    reg[speaker_id] = {
        "name":        name,
        "is_owner":    is_owner,
        "enrolled_at": datetime.datetime.now().isoformat(),
        "samples":     1,
        "color":       _next_color(reg),
    }
    _save_embedding(speaker_id, vec)
    _save_registry(reg)
    log.info(f"Enrolled speaker '{name}' with id={speaker_id}")
    return speaker_id


def _next_color(reg: dict) -> str:
    palette = ["#7c6af7", "#3de8a0", "#f76c6c", "#f7c86c",
               "#6cb2f7", "#f76cb2", "#a3f76c", "#f7a36c"]
    used = {v.get("color") for v in reg.values()}
    for c in palette:
        if c not in used:
            return c
    return palette[len(reg) % len(palette)]


# ── Identification ─────────────────────────────────────────────────────────
def identify_speaker(audio_path: Path, segment_start: float, segment_end: float) -> dict:
    """
    Identify who speaks in [segment_start, segment_end] of audio_path.

    Returns:
        {
          "id":       speaker_id | None,
          "name":     display name,
          "score":    cosine similarity,
          "is_owner": bool,
          "color":    hex color,
          "new":      True if this segment auto-created a new unknown speaker,
        }
    """
    # Slice the segment to a temp file for embedding
    import tempfile, soundfile as sf, numpy as np_

    try:
        data, sr = sf.read(str(audio_path))
        start_s = int(segment_start * sr)
        end_s   = int(segment_end   * sr)
        segment_audio = data[start_s:end_s]

        if len(segment_audio) < sr * 1:  # < 1 second — skip
            return _unknown_result()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        sf.write(str(tmp_path), segment_audio, sr)

        vec = _extract_embedding(tmp_path)
        tmp_path.unlink(missing_ok=True)

        if vec is None:
            return _unknown_result()

    except Exception as e:
        log.warning(f"Segment extraction error: {e}")
        return _unknown_result()

    reg = _load_registry()
    best_id    = None
    best_score = -1.0
    best_threshold = SIMILARITY_THRESHOLD

    for sid, info in reg.items():
        emb = _load_embedding(sid)
        if emb is None:
            continue
        score = _cosine(vec, emb)
        threshold = OWNER_THRESHOLD if info.get("is_owner") else SIMILARITY_THRESHOLD
        if score > best_score:
            best_score = score
            best_id    = sid
            best_threshold = threshold

    if best_id and best_score >= best_threshold:
        info = reg[best_id]
        # Update running mean embedding for the matched speaker
        existing = _load_embedding(best_id)
        if existing is not None and best_score >= 0.70:
            n = info.get("samples", 1)
            merged = (existing * n + vec) / (n + 1)
            norm = np.linalg.norm(merged)
            _save_embedding(best_id, merged / norm if norm > 1e-9 else merged)
            reg[best_id]["samples"] = n + 1
            _save_registry(reg)
        return {
            "id":       best_id,
            "name":     info["name"],
            "score":    round(best_score, 3),
            "is_owner": info.get("is_owner", False),
            "color":    info.get("color", "#888"),
            "new":      False,
        }

    # Unknown speaker — auto-register as Speaker_N
    n_unknown = sum(1 for v in reg.values() if v.get("auto_unknown"))
    speaker_id = str(uuid.uuid4())[:8]
    reg[speaker_id] = {
        "name":         f"Speaker_{n_unknown + 1}",
        "is_owner":     False,
        "enrolled_at":  datetime.datetime.now().isoformat(),
        "samples":      1,
        "auto_unknown": True,
        "color":        _next_color(reg),
    }
    _save_embedding(speaker_id, vec)
    _save_registry(reg)
    log.info(f"New unknown speaker auto-registered: Speaker_{n_unknown + 1} (id={speaker_id})")

    return {
        "id":       speaker_id,
        "name":     f"Speaker_{n_unknown + 1}",
        "score":    round(best_score if best_score >= 0 else 0, 3),
        "is_owner": False,
        "color":    reg[speaker_id]["color"],
        "new":      True,
    }


def _unknown_result() -> dict:
    return {"id": None, "name": "Unknown", "score": 0.0,
            "is_owner": False, "color": "#888", "new": False}


def add_sample(speaker_id: str, audio_path) -> bool:
    """Refine an existing speaker's embedding by merging in a new audio sample (running mean)."""
    reg = _load_registry()
    if speaker_id not in reg:
        log.warning(f"add_sample: speaker {speaker_id} not found")
        return False
    vec = _extract_embedding(Path(audio_path))
    if vec is None:
        return False
    existing = _load_embedding(speaker_id)
    if existing is not None:
        n = reg[speaker_id].get("samples", 1)
        merged = (existing * n + vec) / (n + 1)
        norm = np.linalg.norm(merged)
        merged = merged / norm if norm > 1e-9 else merged
        _save_embedding(speaker_id, merged)
        reg[speaker_id]["samples"] = n + 1
    else:
        _save_embedding(speaker_id, vec)
        reg[speaker_id]["samples"] = 1
    reg[speaker_id]["last_updated"] = datetime.datetime.now().isoformat()
    _save_registry(reg)
    log.info(f"add_sample: {reg[speaker_id]['name']} updated (sid={speaker_id}, samples={reg[speaker_id]['samples']})")
    return True


def set_speaker_clip(speaker_id: str, clip_path: str):
    """Store the path to a short preview clip in the speaker's registry entry."""
    reg = _load_registry()
    if speaker_id in reg:
        reg[speaker_id]["clip_path"] = clip_path
        _save_registry(reg)


def rename_speaker(speaker_id: str, new_name: str) -> bool:
    """Rename a speaker (e.g. 'Speaker_1' → 'Андрей')."""
    reg = _load_registry()
    if speaker_id not in reg:
        return False
    reg[speaker_id]["name"] = new_name
    reg[speaker_id]["auto_unknown"] = False
    _save_registry(reg)
    log.info(f"Speaker {speaker_id} renamed to '{new_name}'")
    return True


def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def _load_dismissed() -> set:
    if DISMISSED_FILE.exists():
        return set(json.loads(DISMISSED_FILE.read_text(encoding="utf-8")))
    return set()


def _save_dismissed(dismissed: set):
    DISMISSED_FILE.write_text(
        json.dumps(sorted(dismissed), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def find_similar_pairs() -> list[dict]:
    """Return speaker pairs with similarity in [SUSPICIOUS_LOW, SIMILARITY_THRESHOLD).

    These are candidates for manual merge — similar enough to be suspicious,
    but below the auto-match threshold.
    """
    reg  = _load_registry()
    ids  = list(reg.keys())
    dismissed = _load_dismissed()

    embs: dict[str, np.ndarray] = {}
    for sid in ids:
        e = _load_embedding(sid)
        if e is not None:
            embs[sid] = e

    pairs = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if a not in embs or b not in embs:
                continue
            if _pair_key(a, b) in dismissed:
                continue
            s = _cosine(embs[a], embs[b])
            if SUSPICIOUS_LOW <= s < SIMILARITY_THRESHOLD:
                pairs.append({
                    "id_a":       a,
                    "name_a":     reg[a]["name"],
                    "clip_a":     reg[a].get("clip_path"),
                    "samples_a":  reg[a].get("samples", 1),
                    "id_b":       b,
                    "name_b":     reg[b]["name"],
                    "clip_b":     reg[b].get("clip_path"),
                    "samples_b":  reg[b].get("samples", 1),
                    "similarity": round(s, 3),
                })

    return sorted(pairs, key=lambda p: -p["similarity"])


def merge_speakers(keep_id: str, drop_id: str) -> bool:
    """Merge drop_id into keep_id: combine embeddings, delete drop entry."""
    reg = _load_registry()
    if keep_id not in reg or drop_id not in reg:
        return False

    keep_emb = _load_embedding(keep_id)
    drop_emb = _load_embedding(drop_id)
    if keep_emb is not None and drop_emb is not None:
        k_n = reg[keep_id].get("samples", 1)
        d_n = reg[drop_id].get("samples", 1)
        merged = (keep_emb * k_n + drop_emb * d_n) / (k_n + d_n)
        norm = np.linalg.norm(merged)
        _save_embedding(keep_id, merged / norm if norm > 1e-9 else merged)
        reg[keep_id]["samples"] = k_n + d_n

    del reg[drop_id]
    (EMBEDDINGS_DIR / f"{drop_id}.npy").unlink(missing_ok=True)
    reg[keep_id]["last_updated"] = datetime.datetime.now().isoformat()
    _save_registry(reg)
    log.info(f"Merged speaker {drop_id} into {keep_id} ({reg[keep_id]['name']})")
    return True


def dismiss_pair(id_a: str, id_b: str):
    """Mark a pair as confirmed-different so it won't show up again."""
    dismissed = _load_dismissed()
    dismissed.add(_pair_key(id_a, id_b))
    _save_dismissed(dismissed)


def delete_speaker(speaker_id: str) -> bool:
    reg = _load_registry()
    if speaker_id not in reg:
        return False
    del reg[speaker_id]
    _save_registry(reg)
    emb = EMBEDDINGS_DIR / f"{speaker_id}.npy"
    emb.unlink(missing_ok=True)
    log.info(f"Speaker {speaker_id} deleted")
    return True
