"""
fetch_messages.py — with bio, bot flag, inline keyboards, dual-key encryption
"""

import os
import json
import asyncio
import base64
import hashlib
from pathlib import Path
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.enums import ChatType, MessageMediaType
from pyrogram.errors import FloodWait
from cryptography.fernet import Fernet

API_ID         = int(os.environ["TG_API_ID"])
API_HASH       = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION_STRING"].strip()
RAW_KEY        = os.environ["DATA_KEY"]
UI_KEY         = os.environ["UI_ENCRYPTION_KEY"]
FORCE          = os.environ.get("FORCE_FULL", "false").lower() == "true"
INIT_N         = int(os.environ.get("DEFAULT_FETCH_COUNT",  "20"))
UPD_N          = int(os.environ.get("DEFAULT_UPDATE_COUNT", "50"))

GH_DISPATCH_TOKEN    = os.environ["GH_DISPATCH_TOKEN"]
MASTER_PASSWORD_HASH = os.environ["MASTER_PASSWORD_HASH"]
GH_OWNER             = os.environ["GH_OWNER"]
GH_REPO              = os.environ["GH_REPO"]

DATA_DIR   = Path("data")
DATA_DIR.mkdir(exist_ok=True)
META_FILE  = DATA_DIR / "meta.json"
CHATS_ENC  = DATA_DIR / "chats.enc"
CHATS_JSON = DATA_DIR / "chats.json"
CONFIG_ENC = DATA_DIR / "config.enc"

def _fernet(p: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(p.encode()).digest()))

F    = _fernet(RAW_KEY)
F_ui = _fernet(UI_KEY)

def encrypt(obj, f: Fernet = None) -> bytes:
    return (f or F).encrypt(json.dumps(obj, ensure_ascii=False, default=str).encode())

def decrypt(data: bytes, f: Fernet = None):
    return json.loads((f or F).decrypt(data))

def _chat_kind(chat_type: ChatType) -> str:
    return {
        ChatType.PRIVATE:    "user",
        ChatType.BOT:        "bot",
        ChatType.GROUP:      "group",
        ChatType.SUPERGROUP: "group",
        ChatType.CHANNEL:    "channel",
    }.get(chat_type, "unknown")

def _media_info(msg) -> dict | None:
    if msg.media is None:
        return None
    mt = msg.media
    if mt == MessageMediaType.PHOTO:
        return {"type": "photo", "id": msg.id}
    if mt == MessageMediaType.DOCUMENT and msg.document:
        doc = msg.document
        return {"type": "document", "id": doc.file_id,
                "filename": doc.file_name, "mime_type": doc.mime_type, "size": doc.file_size}
    if mt == MessageMediaType.VIDEO and msg.video:
        v = msg.video
        return {"type": "document", "id": v.file_id,
                "filename": v.file_name or "video.mp4", "mime_type": v.mime_type, "size": v.file_size}
    if mt == MessageMediaType.AUDIO and msg.audio:
        a = msg.audio
        return {"type": "document", "id": a.file_id,
                "filename": a.file_name or "audio", "mime_type": a.mime_type, "size": a.file_size}
    if mt == MessageMediaType.VOICE and msg.voice:
        return {"type": "document", "id": msg.voice.file_id,
                "filename": "voice.ogg", "mime_type": "audio/ogg", "size": msg.voice.file_size}
    if mt == MessageMediaType.STICKER and msg.sticker:
        return {"type": "sticker", "emoji": msg.sticker.emoji,
                "id": msg.sticker.file_id, "is_animated": msg.sticker.is_animated,
                "mime_type": "image/webp", "size": msg.sticker.file_size}
    if mt == MessageMediaType.ANIMATION and msg.animation:
        return {"type": "document", "id": msg.animation.file_id,
                "filename": msg.animation.file_name or "animation.gif",
                "mime_type": msg.animation.mime_type, "size": msg.animation.file_size}
    if mt == MessageMediaType.VIDEO_NOTE and msg.video_note:
        return {"type": "document", "id": msg.video_note.file_id,
                "filename": "video_note.mp4", "mime_type": "video/mp4",
                "size": msg.video_note.file_size}
    if mt == MessageMediaType.WEB_PAGE and msg.web_page:
        wp = msg.web_page
        return {"type": "webpage", "url": wp.url, "title": wp.title,
                "description": wp.description}
    if mt == MessageMediaType.POLL and msg.poll:
        return {"type": "poll", "question": msg.poll.question,
                "options": [o.text for o in msg.poll.options]}
    if mt == MessageMediaType.CONTACT and msg.contact:
        return {"type": "contact", "name": f"{msg.contact.first_name or ''} {msg.contact.last_name or ''}".strip(),
                "phone": msg.contact.phone_number}
    if mt == MessageMediaType.LOCATION and msg.location:
        return {"type": "location", "lat": msg.location.latitude, "lng": msg.location.longitude}
    return {"type": mt.name.lower() if mt else "unknown"}

def _reactions(msg) -> list:
    if not msg.reactions:
        return []
    return [{"emoji": r.emoji, "count": r.count} for r in msg.reactions.reactions]

def _inline_keyboard(msg) -> list | None:
    """Extract inline keyboard buttons from a bot message."""
    if not msg.reply_markup:
        return None
    try:
        rows = []
        for row in msg.reply_markup.inline_keyboard:
            buttons = []
            for btn in row:
                buttons.append({
                    "text":          btn.text,
                    "callback_data": getattr(btn, "callback_data", None),
                    "url":           getattr(btn, "url", None),
                })
            rows.append(buttons)
        return rows
    except Exception:
        return None

def _serialize_message(msg, chat_id: int) -> dict:
    from_id = None
    if msg.from_user:
        from_id = msg.from_user.id
    elif msg.sender_chat:
        from_id = msg.sender_chat.id
    return {
        "id":              msg.id,
        "chat_id":         chat_id,
        "date":            msg.date.isoformat() if msg.date else None,
        "from_id":         from_id,
        "text":            msg.text or msg.caption or "",
        "media":           _media_info(msg),
        "reactions":       _reactions(msg),
        "reply_to":        msg.reply_to_message_id,
        "pinned":          getattr(msg, "pinned", False),
        "out":             getattr(msg, "outgoing", False),
        "views":           getattr(msg, "views", None),
        "forwards":        getattr(msg, "forwards", None),
        "inline_keyboard": _inline_keyboard(msg),
        "via_bot":         msg.via_bot.id if msg.via_bot else None,
    }

async def _fetch_chat_info(app: Client, chat_id: int) -> dict:
    """Fetch extended info: bio, last_seen, is_bot, etc."""
    try:
        full = await app.get_chat(chat_id)
        info = {
            "bio":         getattr(full, "bio", None) or getattr(full, "description", None),
            "members_count": getattr(full, "members_count", None),
            "is_verified": getattr(full, "is_verified", False),
            "is_scam":     getattr(full, "is_scam", False),
            "is_fake":     getattr(full, "is_fake", False),
            "dc_id":       getattr(full, "dc_id", None),
        }
        return info
    except Exception:
        return {}

async def main():
    meta = {}
    if META_FILE.exists():
        meta = json.loads(META_FILE.read_text())

    first_run = FORCE or not meta.get("initialized", False)
    fetch_n   = INIT_N if first_run else UPD_N
    print(f"first_run={first_run}, fetch_n={fetch_n}")

    app = Client(
        name="relay",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
    )

    async with app:
        me = await app.get_me()
        my_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        meta["me"] = {"id": me.id, "name": my_name, "username": me.username}
        print(f"✓ Connected as {my_name}")

        chats_data: dict[str, dict] = {}
        if CHATS_ENC.exists() and not first_run:
            chats_data = decrypt(CHATS_ENC.read_bytes())

        action_log = []

        async for dialog in app.get_dialogs():
            chat    = dialog.chat
            chat_id = chat.id
            key     = str(chat_id)
            unread  = dialog.unread_messages_count or 0

            if not first_run and unread == 0:
                continue

            name = (
                getattr(chat, "title", None)
                or f"{getattr(chat,'first_name','') or ''} {getattr(chat,'last_name','') or ''}".strip()
                or str(chat_id)
            )
            kind = _chat_kind(chat.type)
            is_bot = chat.type == ChatType.BOT

            messages = []
            try:
                async for msg in app.get_chat_history(chat_id, limit=fetch_n):
                    messages.append(_serialize_message(msg, chat_id))
            except FloodWait as e:
                print(f"  FloodWait {e.value}s on {name}, skipping")
                await asyncio.sleep(e.value)
                continue
            except Exception as e:
                print(f"  Error fetching {name}: {e}")
                continue

            # fetch extended info on first run or if missing
            extra = {}
            if first_run or key not in chats_data or "bio" not in chats_data.get(key, {}):
                extra = await _fetch_chat_info(app, chat_id)
                await asyncio.sleep(0.2)

            existing = {m["id"]: m for m in chats_data.get(key, {}).get("messages", [])}
            for m in messages:
                existing[m["id"]] = m

            chats_data[key] = {
                "id":                chat_id,
                "name":              name,
                "kind":              kind,
                "is_bot":            is_bot,
                "username":          getattr(chat, "username", None),
                "unread_count":      unread,
                "last_message_date": messages[0]["date"] if messages else None,
                "messages":          list(existing.values()),
                # extended info
                "bio":               extra.get("bio") or chats_data.get(key, {}).get("bio"),
                "members_count":     extra.get("members_count") or chats_data.get(key, {}).get("members_count"),
                "is_verified":       extra.get("is_verified", False),
                "is_scam":           extra.get("is_scam", False),
            }
            action_log.append({"chat": name, "fetched": len(messages)})
            print(f"  ↳ {name}: {len(messages)} messages")

        CHATS_ENC.write_bytes(encrypt(chats_data, F))
        CHATS_JSON.write_text(json.dumps(chats_data, ensure_ascii=False, default=str, indent=2))

        config_payload = {
            "gh_token":      GH_DISPATCH_TOKEN,
            "password_hash": MASTER_PASSWORD_HASH,
            "gh_owner":      GH_OWNER,
            "gh_repo":       GH_REPO,
        }
        CONFIG_ENC.write_bytes(encrypt(config_payload, F_ui))
        print("✓ config.enc written")

        meta["initialized"] = True
        meta["last_sync"]   = datetime.now(timezone.utc).isoformat()
        meta["sync_log"]    = action_log
        meta["total_chats"] = len(chats_data)
        META_FILE.write_text(json.dumps(meta, indent=2, default=str))
        print(f"✓ Done — {len(chats_data)} chats written")

asyncio.run(main())
