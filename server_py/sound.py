from __future__ import annotations
import hashlib
from dataclasses import dataclass
from pathlib import Path

@dataclass
class SoundInfo:
    available: bool
    url: str

def sound_info(sound_file: str) -> SoundInfo:
    p = Path(sound_file)
    if not p.exists() or p.is_dir():
        return SoundInfo(False, "")
    h = hashlib.sha1(p.read_bytes()).hexdigest()
    # Your web folder already uses /sounds/<name> — keep that contract
    return SoundInfo(True, f"/sounds/{p.name}?v={h}")
