"""
fetch_messages.py — runs inside GitHub Actions to pull messages from Telegram
and write encrypted JSON blobs to data/.
"""

import os
import json
import asyncio
import base64
import hashlib
import time
from pathlib import Path
from datetime import datetime, timezone

from telethon.sessions import StringSession
from telethon import TelegramClient
from telethon.tl.types import (
    User, Chat, Channel,
    MessageMediaPhoto, MessageMediaDocument,
    MessageMediaWebPage, ReactionEmoji,
)
from cryptography.fernet import Fernet

# ── config ────────────────────────────────────────────────────────────────────
API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
RAW_KEY  = os.environ["DATA_KEY"]          # arbitrary passphrase
FORCE    = os.environ.get("FORCE_FULL", "false").lower() == "true"
INIT_N   = int(os.environ.get("DEFAULT_FETCH_COUNT",  "20"))
UPD_N    = int(os.environ.get("DEFAULT_UPDATE_COUNT", "50"))

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
META_FILE   = DATA_DIR / "meta.json"
CHATS_FILE  = DATA_DIR / "chats.enc"

# derive a valid Fernet key from the passphrase
def _fernet(passphrase: str) -> Fernet:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(passphrase.encode()).digest()
    )
    return Fernet(key)

F = _fernet(RAW_KEY)

def encrypt(obj) -> bytes:
    return F.encrypt(json.dumps(obj, ensure_ascii=False, default=str).encode())

def decrypt(data: bytes):
    return json.loads(F.decrypt(data))

# ── helpers ───────────────────────────────────────────────────────────────────
def _entity_name(entity) -> str:
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        return " ".join(p for p in parts if p).strip() or str(entity.id)
    return getattr(entity, "title", None) or str(entity.id)

def _media_info(msg) -> dict | None:
    m = msg.media
    if m is None:
        return None
    if isinstance(m, MessageMediaPhoto):
        return {"type": "photo", "id": msg.id}
    if isinstance(m, MessageMediaDocument):
        doc = m.document
        fname = next(
            (a.file_name for a in (doc.attributes or []) if hasattr(a, "file_name")),
            None,
        )
        return {
            "type": "document",
            "id": doc.id,
            "filename": fname,
            "mime_type": doc.mime_type,
            "size": doc.size,
        }
    if isinstance(m, MessageMediaWebPage):
        wp = m.webpage
        return {
            "type": "webpage",
            "url": getattr(wp, "url", None),
            "title": getattr(wp, "title", None),
        }
    return {"type": type(m).__name__}

def _reactions(msg) -> list:
    if not getattr(msg, "reactions", None):
        return []
    out = []
    for r in msg.reactions.results:
        emoji = r.reaction
        out.append({
            "emoji": emoji.emoticon if isinstance(emoji, ReactionEmoji) else "?",
            "count": r.count,
        })
    return out

def _serialize_message(msg, entity_id: int) -> dict:
    return {
        "id": msg.id,
        "chat_id": entity_id,
        "date": msg.date.isoformat() if msg.date else None,
        "from_id": getattr(msg.from_id, "user_id", None),
        "text": msg.raw_text or "",
        "media": _media_info(msg),
        "reactions": _reactions(msg),
        "reply_to": getattr(msg.reply_to, "reply_to_msg_id", None),
        "pinned": getattr(msg, "pinned", False),
        "out": getattr(msg, "out", False),
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
    }

# ── main ──────────────────────────────────────────────────────────────────────
async def main():
    meta = {}
    if META_FILE.exists():
        meta = json.loads(META_FILE.read_text())

    first_run = FORCE or not meta.get("initialized", False)
    fetch_n   = INIT_N if first_run else UPD_N

    SESSION_STRING = os.environ["TG_SESSION_STRING"]
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()
    print(f"✓ Connected. first_run={first_run}, fetch_n={fetch_n}")

    me = await client.get_me()
    meta["me"] = {
        "id": me.id,
        "name": _entity_name(me),
        "username": me.username,
    }

    chats_data: dict[str, dict] = {}
    if CHATS_FILE.exists() and not first_run:
        chats_data = decrypt(CHATS_FILE.read_bytes())

    dialogs = await client.get_dialogs(limit=None)
    print(f"  Found {len(dialogs)} dialogs")

    action_log = []

    for dlg in dialogs:
        eid = dlg.entity.id
        key = str(eid)

        # skip if up-to-date
        if not first_run and dlg.unread_count == 0:
            continue

        name = _entity_name(dlg.entity)
        kind = (
            "user"    if isinstance(dlg.entity, User)    else
            "group"   if isinstance(dlg.entity, Chat)    else
            "channel" if isinstance(dlg.entity, Channel) else "unknown"
        )

        messages = []
        async for msg in client.iter_messages(dlg.entity, limit=fetch_n):
            messages.append(_serialize_message(msg, eid))

        existing_msgs = {m["id"]: m for m in chats_data.get(key, {}).get("messages", [])}
        for m in messages:
            existing_msgs[m["id"]] = m

        chats_data[key] = {
            "id": eid,
            "name": name,
            "kind": kind,
            "username": getattr(dlg.entity, "username", None),
            "unread_count": dlg.unread_count,
            "last_message_date": messages[0]["date"] if messages else None,
            "messages": list(existing_msgs.values()),
            "photo": {"pending": True, "entity_id": eid},  # fetched on demand
        }

        action_log.append({"chat": name, "fetched": len(messages)})
        print(f"  ↳ {name}: {len(messages)} messages")

    CHATS_FILE.write_bytes(encrypt(chats_data))

    meta["initialized"]  = True
    meta["last_sync"]    = datetime.now(timezone.utc).isoformat()
    meta["sync_log"]     = action_log
    meta["total_chats"]  = len(chats_data)
    META_FILE.write_text(json.dumps(meta, indent=2, default=str))

    await client.disconnect()
    print("✓ Done")

asyncio.run(main())
