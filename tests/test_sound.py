from pathlib import Path
from server_py.sound import sound_info


def test_sound_info_nonexistent_file(tmp_path: Path):
    p = tmp_path / "nope.mp3"
    info = sound_info(str(p))
    assert info.available is False and info.url == ""

def test_sound_info_hash_url(tmp_path: Path):
    p = tmp_path / "ding.mp3"
    p.write_bytes(b"abc")  # small content; just need a hash
    info = sound_info(str(p))
    assert info.available is True and info.url.startswith("/sounds/ding.mp3?v="), f"Bad URL: {info}"
