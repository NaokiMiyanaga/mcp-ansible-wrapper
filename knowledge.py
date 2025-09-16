from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Tuple
from collections import Counter
import string, yaml

# ---------------- Text utilities ----------------
def safe_lower(x: Any) -> str:
    """Safely lowercase any value to a string (None-safe)."""
    try:
        return str(x or "").lower()
    except Exception:
        return ""

def _tokenize(text: str) -> List[str]:
    t = safe_lower(text)
    # normalize punctuation to spaces
    tbl = str.maketrans({c: " " for c in string.punctuation})
    t = t.translate(tbl)
    return [w for w in t.split() if w]

def _bow(text: str) -> Counter:
    return Counter(_tokenize(text))

def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b.get(k, 0) for k in a.keys())
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

# ---------------- Index loading ----------------
def load_playbook_index(base_dir: Path) -> List[Dict[str, Any]]:
    """Load knowledge/playbook_index.yaml under the given base_dir."""
    idx_file = base_dir / "knowledge" / "playbook_index.yaml"
    if not idx_file.exists():
        return []
    try:
        with idx_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
            return data if isinstance(data, list) else []
    except Exception:
        return []

# ---------------- Search API ----------------
def search_playbook(action: str, base_dir: Path, topk: int = 5) -> List[Tuple[float, Dict[str, Any]]]:
    """Return top-k matches by cosine similarity over intent/description/keywords/examples."""
    index = load_playbook_index(base_dir)
    q = _bow(action)
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for item in index:
        intent = item.get("intent", "")
        desc = item.get("description", "")
        kws = item.get("keywords", [])
        exs = item.get("examples", [])
        text = " ".join(filter(None, [intent, desc, " ".join(kws), " ".join(exs)]))
        score = _cosine(q, _bow(text))
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:topk]