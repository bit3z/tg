"""
export_json.py — called at the end of fetch_messages to write a plain
data/chats.json for the GitHub Pages frontend to consume.

If you want end-to-end encryption in the browser instead, skip this
and implement a Fernet JS decryptor in index.html.
"""

import os
import json
import base64
import hashlib
from pathlib import Path
from cryptography.fernet import Fernet

RAW_KEY  = os.environ["DATA_KEY"]
DATA_DIR = Path("data")

def _fernet(passphrase: str) -> Fernet:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(passphrase.encode()).digest()
    )
    return Fernet(key)

F = _fernet(RAW_KEY)

enc_path  = DATA_DIR / "chats.enc"
json_path = DATA_DIR / "chats.json"

if enc_path.exists():
    raw   = F.decrypt(enc_path.read_bytes())
    chats = json.loads(raw)
    json_path.write_text(json.dumps(chats, ensure_ascii=False, default=str))
    print(f"✓ Exported {len(chats)} chats to data/chats.json")
else:
    print("⚠ data/chats.enc not found — nothing exported")
