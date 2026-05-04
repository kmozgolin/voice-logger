"""
dedup_speakers.py — merge duplicate Speaker_N entries in the registry.

Uses union-find to group speakers whose embeddings are closer than DEDUP_THRESHOLD.
The "canonical" speaker in each group is the one with the most samples,
or a named speaker if any in the group has a real name.

Usage:
    python dedup_speakers.py           # dry run — print plan only
    python dedup_speakers.py --apply   # apply merges
"""

import json, sys, shutil, datetime
import numpy as np
from pathlib import Path

SPEAKERS_DIR   = Path("speakers")
EMBEDDINGS_DIR = SPEAKERS_DIR / "embeddings"
REGISTRY_FILE  = SPEAKERS_DIR / "registry.json"
DEDUP_THRESHOLD = 0.70  # merge speakers with cosine similarity ≥ this

apply = "--apply" in sys.argv


def cosine(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def load_embeddings(reg):
    embs = {}
    for sid in reg:
        p = EMBEDDINGS_DIR / f"{sid}.npy"
        if p.exists():
            embs[sid] = np.load(str(p))
    return embs


# ── Union-Find ────────────────────────────────────────────────────────────────
def make_uf(ids):
    parent = {i: i for i in ids}
    rank   = {i: 0  for i in ids}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(x, y):
        rx, ry = find(x), find(y)
        if rx == ry: return
        if rank[rx] < rank[ry]: rx, ry = ry, rx
        parent[ry] = rx
        if rank[rx] == rank[ry]: rank[rx] += 1
    return find, union


reg = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
embs = load_embeddings(reg)

ids = list(embs.keys())
find, union = make_uf(ids)

# Build pairs above threshold
pairs = []
for i in range(len(ids)):
    for j in range(i + 1, len(ids)):
        a, b = ids[i], ids[j]
        s = cosine(embs[a], embs[b])
        if s >= DEDUP_THRESHOLD:
            pairs.append((s, a, b))
            union(a, b)

# Group by root
groups: dict[str, list] = {}
for sid in ids:
    root = find(sid)
    groups.setdefault(root, []).append(sid)

# Only groups with > 1 member
merge_groups = {r: members for r, members in groups.items() if len(members) > 1}

print(f"\nRegistry: {len(reg)} speakers, {len(embs)} with embeddings")
print(f"Threshold: {DEDUP_THRESHOLD}  |  Pairs above threshold: {len(pairs)}")
print(f"Groups to merge: {len(merge_groups)}\n")

if not merge_groups:
    print("Nothing to merge.")
    sys.exit(0)

total_removed = 0
merges = []  # (canonical_id, canonical_name, merged_ids)

for root, members in sorted(merge_groups.items(), key=lambda x: -len(x[1])):
    # Pick canonical: prefer named (non-auto_unknown), then most samples
    def priority(sid):
        info = reg[sid]
        named = 0 if info.get("auto_unknown", True) else 1
        return (named, info.get("samples", 1))

    members_sorted = sorted(members, key=priority, reverse=True)
    canonical = members_sorted[0]
    duplicates = members_sorted[1:]
    total_removed += len(duplicates)

    c_info = reg[canonical]
    dup_names = [reg[d]["name"] for d in duplicates]
    merges.append((canonical, c_info["name"], duplicates, dup_names))

    print(f"  KEEP  {c_info['name']:30s} (id={canonical}, samples={c_info.get('samples',1)})")
    for d, dn in zip(duplicates, dup_names):
        s = cosine(embs[canonical], embs[d])
        print(f"    +- merge {dn:28s} (id={d}, sim={s:.3f})")
    print()

print(f"Will remove {total_removed} duplicate entries, keeping {len(reg) - total_removed} speakers.")

if not apply:
    print("\nDry run - pass --apply to execute.")
    sys.exit(0)

# ── Backup ────────────────────────────────────────────────────────────────────
backup = REGISTRY_FILE.with_suffix(f".bak.{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
shutil.copy(REGISTRY_FILE, backup)
print(f"\nBackup saved: {backup.name}")

# ── Apply merges ──────────────────────────────────────────────────────────────
for canonical_id, canonical_name, dup_ids, dup_names in merges:
    c_emb = embs[canonical_id]
    c_n   = reg[canonical_id].get("samples", 1)

    for dup_id in dup_ids:
        d_emb = embs.get(dup_id)
        if d_emb is not None:
            # Weighted average into canonical embedding
            d_n   = reg[dup_id].get("samples", 1)
            total = c_n + d_n
            merged = (c_emb * c_n + d_emb * d_n) / total
            norm = np.linalg.norm(merged)
            c_emb = merged / norm if norm > 1e-9 else merged
            c_n   = total

        # Delete duplicate embedding file
        (EMBEDDINGS_DIR / f"{dup_id}.npy").unlink(missing_ok=True)
        del reg[dup_id]

    # Save merged embedding
    np.save(str(EMBEDDINGS_DIR / f"{canonical_id}.npy"), c_emb)
    reg[canonical_id]["samples"] = c_n
    reg[canonical_id]["last_updated"] = datetime.datetime.now().isoformat()

REGISTRY_FILE.write_text(
    json.dumps(reg, indent=2, ensure_ascii=False, default=str),
    encoding="utf-8",
)
print(f"Done. Registry now has {len(reg)} speakers (removed {total_removed}).")
