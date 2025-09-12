import os, re, yaml
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

KB_PATH = os.getenv("PLAYBOOK_KB_PATH", "knowledge/playbook_map.yaml")

def _load_kb(path: str = KB_PATH) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def normalize_host(text: str, aliases: Dict[str, List[str]]) -> Optional[str]:
    t = text.lower()
    for canon, vs in aliases.items():
        for v in vs + [canon]:
            if v.lower() in t:
                return canon
    return None

def select_playbook(text: str, feature_hint: Optional[str] = None) -> Tuple[Optional[str], Dict[str, Any]]:
    kb = _load_kb()
    defaults = (kb.get("defaults") or {})
    aliases = (kb.get("aliases") or {}).get("host", {})
    t = (text or "").lower()

    feature = (feature_hint or "bgp").lower()
    conf = defaults.get(feature)
    host = normalize_host(t, aliases) if aliases else None

    if not conf:
        return None, {"feature": feature, "host": host, "reason": "no feature in kb"}

    # Prefer rules
    for rule in conf.get("prefer", []):
        file = rule.get("file")
        cond = (rule.get("when") or {})
        kws = [k.lower() for k in (cond.get("any_keywords") or [])]
        if not kws or any(k in t for k in kws):
            return file, {"feature": feature, "host": host, "reason": "prefer rule matched", "keywords": kws}

    # Fallback rules
    for rule in conf.get("fallback", []):
        file = rule.get("file")
        if file:
            return file, {"feature": feature, "host": host, "reason": "fallback used"}

    return None, {"feature": feature, "host": host, "reason": "no rule matched"}
