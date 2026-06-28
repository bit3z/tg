"""
cleanup_files.py — deletes downloaded files in data/files/ to keep the repo
small (every file lives in git history via GitHub Actions commits).

Run via the "Cleanup Downloaded Files" workflow, with an option to keep
profile-picture avatars (since those are small and used throughout the UI).
"""

import os
import json
import base64
import hashlib
from pathlib import Path

from cryptography.fernet import Fernet

RAW_KEY  = os.environ["DATA_KEY"]
UI_KEY   = os.environ["UI_ENCRYPTION_KEY"]
KEEP_PROFILE_PICS = os.environ.get("KEEP_PROFILE_PICS", "true").lower() == "true"

DATA_DIR  = Path("data")
FILES_DIR = DATA_DIR / "files"
FILES_INDEX_ENC = DATA_DIR / "files_index.json.enc"


def _fernet(p: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(p.encode()).digest()))

F_ui = _fernet(UI_KEY)


def main():
    if not FILES_INDEX_ENC.exists():
        print("No files_index.json.enc found — nothing to clean up.")
        return

    index = json.loads(F_ui.decrypt(FILES_INDEX_ENC.read_bytes()))

    kept, removed = {}, 0
    for safe_name, entry in index.items():
        is_profile = entry.get("label") in ("profile_pic", "profile_media")
        if KEEP_PROFILE_PICS and is_profile:
            kept[safe_name] = entry
            continue
        f = FILES_DIR / safe_name
        if f.exists():
            f.unlink()
            removed += 1

    FILES_INDEX_ENC.write_bytes(F_ui.encrypt(json.dumps(kept, default=str).encode()))
    print(f"✓ Removed {removed} file(s). Kept {len(kept)} (profile pictures: {KEEP_PROFILE_PICS}).")


if __name__ == "__main__":
    main()
