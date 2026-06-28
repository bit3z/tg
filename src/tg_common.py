"""
tg_common.py — shared helpers for fetch_messages.py and process_queue.py.

Centralizing this avoids fixing a bug (e.g. the reply-quote / service-message
handling, or the encryption scheme) in one script and forgetting the other.
"""

import json
import base64
import hashlib
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from pyrogram import Client, raw, types, utils
from pyrogram.enums import ChatType, MessageMediaType, MessageServiceType
from pyrogram.errors import FloodWait, MessageIdsEmpty
from cryptography.fernet import Fernet


# ── encryption ───────────────────────────────────────────────────────────────
def fernet_for(passphrase: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest()))

def encrypt(obj, f: Fernet) -> bytes:
    return f.encrypt(json.dumps(obj, ensure_ascii=False, default=str).encode())

def decrypt(data: bytes, f: Fernet):
    return json.loads(f.decrypt(data))

def decrypt_or_default(path: Path, f: Fernet, default):
    if not path.exists():
        return default
    try:
        return decrypt(path.read_bytes(), f)
    except Exception:
        return default

def hashed_name(*parts) -> str:
    """Opaque filename with no chat/message/user IDs embedded in it."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return h[:24]


# ── dialogs (folder-aware — Client.get_dialogs() hardcodes folder 0) ────────
async def iter_dialogs(app: Client, folder_id: int = 0, limit: int = 0):
    current = 0
    total = limit or (1 << 31) - 1
    chunk = min(100, total)
    offset_date, offset_id = 0, 0
    offset_peer = raw.types.InputPeerEmpty()

    while True:
        r = await app.invoke(
            raw.functions.messages.GetDialogs(
                offset_date=offset_date, offset_id=offset_id,
                offset_peer=offset_peer, limit=chunk, hash=0,
                folder_id=folder_id,
            ),
            sleep_threshold=60,
        )
        users = {i.id: i for i in r.users}
        chats = {i.id: i for i in r.chats}
        messages = {}
        for message in r.messages:
            if isinstance(message, raw.types.MessageEmpty):
                continue
            chat_id = utils.get_peer_id(message.peer_id)
            messages[chat_id] = await types.Message._parse(app, message, users, chats)

        dialogs = [d for d in r.dialogs if isinstance(d, raw.types.Dialog)]
        if not dialogs:
            return
        parsed = [types.Dialog._parse(app, d, messages, users, chats) for d in dialogs]

        last = parsed[-1]
        offset_id = last.top_message.id if last.top_message else 0
        offset_date = utils.datetime_to_timestamp(last.top_message.date) if last.top_message else 0
        offset_peer = await app.resolve_peer(last.chat.id)

        for d in parsed:
            yield d
            current += 1
            if current >= total:
                return


def chat_kind(chat_type: ChatType) -> str:
    return {
        ChatType.PRIVATE:    "user",
        ChatType.BOT:        "bot",
        ChatType.GROUP:      "group",
        ChatType.SUPERGROUP: "group",
        ChatType.CHANNEL:    "channel",
    }.get(chat_type, "unknown")


def media_info(msg) -> dict | None:
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


def reactions_info(msg) -> list:
    if not msg.reactions:
        return []
    return [{"emoji": r.emoji, "count": r.count} for r in msg.reactions.reactions]


def inline_keyboard_info(msg) -> list | None:
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


def service_text(msg) -> str | None:
    """Turn a Pyrogram service message into readable text instead of a blank bubble."""
    svc = msg.service
    if not svc:
        return None
    try:
        if svc == MessageServiceType.NEW_CHAT_TITLE:
            return f"✏️ Changed the group name to \u201c{msg.new_chat_title}\u201d"
        if svc == MessageServiceType.NEW_CHAT_PHOTO:
            return "🖼️ Changed the group photo"
        if svc == MessageServiceType.DELETE_CHAT_PHOTO:
            return "🗑️ Removed the group photo"
        if svc == MessageServiceType.NEW_CHAT_MEMBERS:
            names = ", ".join(
                (u.first_name or u.username or str(u.id)) for u in (msg.new_chat_members or [])
            )
            return f"➕ Added {names}" if names else "➕ New member joined"
        if svc == MessageServiceType.LEFT_CHAT_MEMBERS:
            u = getattr(msg, "left_chat_member", None)
            name = (u.first_name or u.username or str(u.id)) if u else "A member"
            return f"➖ {name} left the group"
        if svc == MessageServiceType.GROUP_CHAT_CREATED:
            return "👥 Group created"
        if svc == MessageServiceType.CHANNEL_CHAT_CREATED:
            return "📢 Channel created"
        if svc == MessageServiceType.MIGRATE_TO_CHAT_ID:
            return "⬆️ Group upgraded to a supergroup"
        if svc == MessageServiceType.MIGRATE_FROM_CHAT_ID:
            return "⬆️ Group was upgraded from a basic group"
        if svc == MessageServiceType.PINNED_MESSAGE:
            pin = getattr(msg, "pinned_message", None)
            snippet = (pin.text or pin.caption or "a message")[:60] if pin else "a message"
            return f"📌 Pinned: \u201c{snippet}\u201d"
        if svc == MessageServiceType.VIDEO_CHAT_STARTED:
            return "🎥 Video chat started"
        if svc == MessageServiceType.VIDEO_CHAT_ENDED:
            return "🎥 Video chat ended"
        if svc == MessageServiceType.VIDEO_CHAT_SCHEDULED:
            return "🎥 Video chat scheduled"
        if svc == MessageServiceType.VIDEO_CHAT_MEMBERS_INVITED:
            return "🎥 Invited members to the video chat"
        if svc == MessageServiceType.GAME_HIGH_SCORE:
            return "🏆 New high score"
        if svc == MessageServiceType.WEB_APP_DATA:
            return "🌐 Web app data received"
    except Exception:
        pass
    return f"ℹ️ {svc.name.replace('_', ' ').title()}"


def serialize_message(msg, chat_id: int, reply_preview_map: dict) -> dict:
    from_id = None
    if msg.from_user:
        from_id = msg.from_user.id
    elif msg.sender_chat:
        from_id = msg.sender_chat.id

    svc_text = service_text(msg)
    reply_preview = reply_preview_map.get(msg.reply_to_message_id) if msg.reply_to_message_id else None

    return {
        "id":              msg.id,
        "chat_id":         chat_id,
        "date":            msg.date.isoformat() if msg.date else None,
        "from_id":         from_id,
        "text":            msg.text or msg.caption or "",
        "service_text":    svc_text,
        "is_service":      bool(msg.service),
        "media":           media_info(msg),
        "reactions":       reactions_info(msg),
        "reply_to":        msg.reply_to_message_id,
        "reply_preview":   reply_preview,
        "pinned":          getattr(msg, "pinned", False),
        "out":             getattr(msg, "outgoing", False),
        "views":           getattr(msg, "views", None),
        "forwards":        getattr(msg, "forwards", None),
        "inline_keyboard": inline_keyboard_info(msg),
        "via_bot":         msg.via_bot.id if msg.via_bot else None,
    }


async def build_reply_previews(app: Client, chat_id: int, messages: list) -> dict:
    """
    Pyrogram's get_chat_history() always requests replies=0, so
    msg.reply_to_message is never populated and quoted replies show no
    preview text. Batch-fetch the actual replied-to messages instead.
    """
    ids = sorted({m.reply_to_message_id for m in messages if m.reply_to_message_id})
    if not ids:
        return {}
    preview_map = {}
    try:
        for i in range(0, len(ids), 100):
            batch = ids[i:i+100]
            fetched = await app.get_messages(chat_id, message_ids=batch)
            if not isinstance(fetched, list):
                fetched = [fetched]
            for rm in fetched:
                if rm is None or getattr(rm, "empty", False):
                    continue
                rm_text = rm.text or rm.caption or service_text(rm) or ""
                preview_map[rm.id] = {
                    "id":      rm.id,
                    "text":    rm_text,
                    "from_id": rm.from_user.id if rm.from_user else None,
                    "media":   media_info(rm),
                }
    except MessageIdsEmpty:
        pass
    except Exception as e:
        print(f"    (reply preview fetch failed: {e})")
    return preview_map


async def fetch_chat_info(app: Client, chat_id) -> dict:
    try:
        full = await app.get_chat(chat_id)
        return {
            "bio":         getattr(full, "bio", None) or getattr(full, "description", None),
            "members_count": getattr(full, "members_count", None),
            "is_verified": getattr(full, "is_verified", False),
            "is_scam":     getattr(full, "is_scam", False),
            "is_fake":     getattr(full, "is_fake", False),
            "dc_id":       getattr(full, "dc_id", None),
            "_photo":      getattr(full, "photo", None),
            "_chat":       full,
        }
    except Exception:
        return {}


async def download_profile_pic(app: Client, chat_id, photo, files_index: dict, files_dir: Path, f_ui: Fernet) -> str | None:
    """Download+encrypt a small avatar thumbnail. The filename has no chat ID in it."""
    if not photo:
        return None
    file_id = getattr(photo, "small_file_id", None) or getattr(photo, "big_file_id", None)
    if not file_id:
        return None
    safe_name = hashed_name("avatar", chat_id) + ".enc"
    enc_path = files_dir / safe_name
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = str(Path(tmp) / "avatar.jpg")
            downloaded = await app.download_media(file_id, file_name=tmp_path)
            if not downloaded:
                return None
            raw_bytes = Path(downloaded).read_bytes()
        enc_path.write_bytes(f_ui.encrypt(raw_bytes))
        files_index[safe_name] = {
            "chat_id": chat_id, "msg_id": None, "filename": "avatar.jpg",
            "safe_name": safe_name, "enc_path": f"data/files/{safe_name}",
            "size": len(raw_bytes), "mime_type": "image/jpeg", "label": "profile_pic",
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        return safe_name
    except Exception as e:
        print(f"    (avatar download failed for {chat_id}: {e})")
        return None


async def sync_chat(app: Client, chat_id, *, chats_data: dict, files_index: dict,
                     files_dir: Path, f_ui: Fernet, fetch_n: int,
                     archived: bool | None = None, pinned: bool | None = None,
                     unread: int | None = None, ghost_mode: bool = False,
                     me_id: int | None = None) -> dict | None:
    """
    Refresh a single chat's data: fetch its latest messages (with real
    reply/quote previews and service-message text), merge with whatever is
    already cached, and optionally refresh extended info (bio/avatar).
    Used both by the scheduled fetch and by process_queue.py's post-action
    sync, so a sent/edited/deleted message shows up immediately.
    """
    key = str(chat_id)
    try:
        chat = await app.get_chat(chat_id)
    except Exception as e:
        print(f"    (sync_chat: could not resolve {chat_id}: {e})")
        return None

    is_saved = me_id is not None and chat.type == ChatType.PRIVATE and chat.id == me_id
    name = ("Saved Messages" if is_saved else (
        getattr(chat, "title", None)
        or f"{getattr(chat,'first_name','') or ''} {getattr(chat,'last_name','') or ''}".strip()
        or str(chat.id)
    ))
    kind = "saved" if is_saved else chat_kind(chat.type)
    is_bot = chat.type == ChatType.BOT

    messages = []
    try:
        async for msg in app.get_chat_history(chat.id, limit=fetch_n):
            messages.append(msg)
    except FloodWait as e:
        print(f"    FloodWait {e.value}s on {name}")
        await asyncio.sleep(min(e.value, 30))
    except Exception as e:
        print(f"    Error fetching {name}: {e}")
        return None

    reply_preview_map = await build_reply_previews(app, chat.id, messages)
    serialized = [serialize_message(m, chat.id, reply_preview_map) for m in messages]

    existing_entry = chats_data.get(key, {})
    existing_msgs = {m["id"]: m for m in existing_entry.get("messages", [])}
    for m in serialized:
        existing_msgs[m["id"]] = m

    needs_info = "bio" not in existing_entry or not existing_entry.get("_avatar_checked")
    extra = await fetch_chat_info(app, chat.id) if needs_info else {}

    avatar_name = existing_entry.get("avatar")
    if needs_info and extra.get("_photo") and not ghost_mode:
        avatar_name = await download_profile_pic(app, chat.id, extra["_photo"], files_index, files_dir, f_ui) or avatar_name

    entry = {
        "id":                chat.id,
        "name":              name,
        "kind":              kind,
        "is_bot":            is_bot,
        "username":          getattr(chat, "username", None),
        "unread_count":      0 if ghost_mode else (unread if unread is not None else existing_entry.get("unread_count", 0)),
        "archived":          archived if archived is not None else existing_entry.get("archived", False),
        "pinned":            pinned if pinned is not None else existing_entry.get("pinned", False),
        "last_message_date": serialized[0]["date"] if serialized else existing_entry.get("last_message_date"),
        "messages":          list(existing_msgs.values()),
        "fetch_count":       fetch_n,
        "bio":               extra.get("bio") or existing_entry.get("bio"),
        "members_count":     extra.get("members_count") or existing_entry.get("members_count"),
        "is_verified":       extra.get("is_verified", existing_entry.get("is_verified", False)),
        "is_scam":           extra.get("is_scam", existing_entry.get("is_scam", False)),
        "avatar":            avatar_name,
        "_avatar_checked":   True,
    }
    chats_data[key] = entry
    return entry
