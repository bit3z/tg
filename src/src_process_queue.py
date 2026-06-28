"""
process_queue.py

Executes queued actions dispatched from the UI, then re-syncs every chat
that was touched so the result (sent message, downloaded file, pin, etc.)
shows up immediately instead of waiting for the next scheduled fetch.

Fixes vs the previous version:
  - FIXED: profile_pic / all_profile_media download referenced an
    undefined `buf` (NameError — these always crashed before).
  - Downloaded files are now named from an opaque hash, not
    "<chat_id>_<msg_id>.ext" — no numeric IDs leak into filenames.
  - files_index.json is now Fernet-encrypted (UI key) instead of plaintext.
  - send_message / start_bot can target a brand-new username that has no
    existing chat yet (falls back to get_users() to resolve it).
  - New action types: start_bot, set_fetch_count, set_ghost_mode.
  - "sync": after the queue finishes, every chat touched by an action is
    re-fetched (with the reply/quote + service-message fixes from
    tg_common) and chats.json.enc is rewritten — so dispatching always
    leaves you with fresh data, not just a queue_results.json.
"""

import os
import json
import asyncio
import base64
from pathlib import Path
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.errors import FloodWait, PeerIdInvalid, ChatIdInvalid, UsernameNotOccupied

import tg_common as tc

API_ID         = int(os.environ["TG_API_ID"])
API_HASH       = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION_STRING"].strip()
RAW_KEY        = os.environ["DATA_KEY"]
UI_KEY         = os.environ["UI_ENCRYPTION_KEY"]
QUEUE_B64      = os.environ["QUEUE_B64"]
UPD_N          = int(os.environ.get("DEFAULT_UPDATE_COUNT", "50"))

DATA_DIR     = Path("data")
FILES_DIR    = DATA_DIR / "files"
DATA_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(exist_ok=True)

RESULTS_FILE    = DATA_DIR / "queue_results.json"
FILES_INDEX_ENC = DATA_DIR / "files_index.json.enc"
CHATS_ENC       = DATA_DIR / "chats.enc"
CHATS_BROWSER   = DATA_DIR / "chats.json.enc"
META_FILE       = DATA_DIR / "meta.json.enc"
SETTINGS_ENC    = DATA_DIR / "settings.enc"

# legacy plaintext index from older versions
_legacy_idx = DATA_DIR / "files_index.json"
if _legacy_idx.exists():
    _legacy_idx.unlink()

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

F    = tc.fernet_for(RAW_KEY)   # also used for settings.enc (python-only)
F_ui = tc.fernet_for(UI_KEY)    # browser-readable files

def encrypt_bytes(data: bytes) -> bytes:
    return F_ui.encrypt(data)

def load_files_index() -> dict:
    return tc.decrypt_or_default(FILES_INDEX_ENC, F_ui, {})

def save_files_index(index: dict):
    FILES_INDEX_ENC.write_bytes(tc.encrypt(index, F_ui))

def load_settings() -> dict:
    return tc.decrypt_or_default(SETTINGS_ENC, F, {})

def save_settings(settings: dict):
    SETTINGS_ENC.write_bytes(tc.encrypt(settings, F))


# ── peer resolution (also handles brand-new usernames not yet in dialogs) ──
async def resolve_peer(app: Client, chat_id):
    try:
        return await app.get_chat(chat_id)
    except (PeerIdInvalid, ChatIdInvalid):
        pass
    except Exception:
        pass
    if isinstance(chat_id, str):
        try:
            users = await app.get_users([chat_id])
            if users:
                return users[0] if isinstance(users, list) else users
        except Exception:
            pass
    async for dialog in app.get_dialogs():
        if dialog.chat.id == chat_id:
            return dialog.chat
    raise PeerIdInvalid(f"Could not resolve peer: {chat_id}")


def _norm_username(target: str) -> str:
    return str(target).replace("https://t.me/", "").replace("@", "").strip()


# ── file download helper ──────────────────────────────────────────────────
async def download_and_store(app: Client, msg, label: str) -> dict:
    if msg.media is None:
        return {"ok": False, "error": "No media in message"}

    media_obj = (
        msg.document or msg.photo or msg.video or msg.audio
        or msg.voice or msg.sticker or msg.video_note or msg.animation
    )
    if media_obj and hasattr(media_obj, "file_size"):
        size = media_obj.file_size or 0
        if size > MAX_FILE_SIZE:
            return {"ok": False, "error": f"File too large ({size/1048576:.1f} MB > {MAX_FILE_SIZE//1048576} MB limit)"}

    orig_filename = getattr(media_obj, "file_name", None) or f"file{msg.id}"
    ext = Path(orig_filename).suffix or ""
    if not ext and hasattr(media_obj, "mime_type") and media_obj.mime_type:
        import mimetypes
        ext = mimetypes.guess_extension(media_obj.mime_type) or ""
    safe_name = tc.hashed_name("file", msg.chat.id, msg.id) + ext + ".enc"
    enc_path = FILES_DIR / safe_name

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = str(Path(tmp) / ("dl" + ext))
        downloaded = await app.download_media(msg, file_name=tmp_path)
        if not downloaded:
            return {"ok": False, "error": "download_media returned None"}
        raw = Path(downloaded).read_bytes()

    enc_path.write_bytes(encrypt_bytes(raw))

    index = load_files_index()
    index[safe_name] = {
        "chat_id":   msg.chat.id,
        "msg_id":    msg.id,
        "filename":  orig_filename,
        "safe_name": safe_name,
        "enc_path":  f"data/files/{safe_name}",
        "size":      len(raw),
        "mime_type": getattr(media_obj, "mime_type", None),
        "label":     label,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }
    save_files_index(index)

    return {"ok": True, "safe_name": safe_name, "filename": orig_filename,
            "size": len(raw), "enc_path": f"data/files/{safe_name}"}


async def _download_raw(app: Client, file_ref, chat_id, label: str, filename: str, mime: str) -> dict:
    """Shared helper for profile-pic style downloads (no Message object)."""
    safe_name = tc.hashed_name(label, chat_id, file_ref if isinstance(file_ref, str) else id(file_ref)) + ".jpg.enc"
    enc_path = FILES_DIR / safe_name
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = str(Path(tmp) / "f.jpg")
        downloaded = await app.download_media(file_ref, file_name=tmp_path)
        raw_bytes = Path(downloaded).read_bytes() if downloaded else b""
    if not raw_bytes:
        return {"ok": False, "error": "download returned no data"}
    enc_path.write_bytes(encrypt_bytes(raw_bytes))
    index = load_files_index()
    index[safe_name] = {
        "chat_id": chat_id, "msg_id": None, "filename": filename,
        "safe_name": safe_name, "enc_path": f"data/files/{safe_name}",
        "size": len(raw_bytes), "mime_type": mime, "label": label,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }
    save_files_index(index)
    return {"ok": True, "safe_name": safe_name, "size": len(raw_bytes)}


# ── action handlers ─────────────────────────────────────────────────────────
TOUCHED_CHATS: set = set()  # chat_ids to re-sync at the end

async def handle_send_message(app: Client, action: dict) -> dict:
    chat_id  = action["chat_id"]
    text     = action["text"]
    reply_to = action.get("reply_to")
    await resolve_peer(app, chat_id)
    msg = await app.send_message(chat_id, text, reply_to_message_id=reply_to)
    TOUCHED_CHATS.add(msg.chat.id)
    return {"ok": True, "msg_id": msg.id, "chat_id": msg.chat.id}

async def handle_start_bot(app: Client, action: dict) -> dict:
    """Start a conversation with a bot (sends /start, optionally with a payload)."""
    target  = action.get("target") or action.get("chat_id")
    payload = action.get("payload", "")
    username = _norm_username(target) if isinstance(target, str) else target
    await resolve_peer(app, username)
    text = "/start" + (f" {payload}" if payload else "")
    msg = await app.send_message(username, text)
    TOUCHED_CHATS.add(msg.chat.id)
    return {"ok": True, "msg_id": msg.id, "chat_id": msg.chat.id}

async def handle_react(app: Client, action: dict) -> dict:
    chat_id, msg_id, emoji = action["chat_id"], action["msg_id"], action["emoji"]
    await resolve_peer(app, chat_id)
    await app.send_reaction(chat_id, msg_id, emoji)
    TOUCHED_CHATS.add(chat_id)
    return {"ok": True}

async def handle_mark_read(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]
    await resolve_peer(app, chat_id)
    await app.read_chat_history(chat_id)
    TOUCHED_CHATS.add(chat_id)
    return {"ok": True}

async def handle_download_request(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]
    msg_id  = action.get("msg_id")
    kind    = action.get("kind", "file")
    await resolve_peer(app, chat_id)
    TOUCHED_CHATS.add(chat_id)

    if kind == "profile_pic":
        chat = await app.get_chat(chat_id)
        if not chat.photo:
            return {"ok": False, "error": "No profile photo"}
        return await _download_raw(app, chat.photo.big_file_id, chat_id, "profile_pic", "profile.jpg", "image/jpeg")

    if kind == "all_profile_media":
        results = []
        async for photo in app.get_chat_photos(chat_id):
            r = await _download_raw(app, photo.file_id, chat_id, "profile_media", "profile.jpg", "image/jpeg")
            if r.get("ok"):
                results.append(r["safe_name"])
            await asyncio.sleep(0.3)
        return {"ok": True, "downloaded": len(results), "files": results}

    msg = await app.get_messages(chat_id, msg_id)
    if not msg:
        return {"ok": False, "error": f"Message {msg_id} not found"}
    return await download_and_store(app, msg, kind)

async def handle_pin_message(app: Client, action: dict) -> dict:
    chat_id, msg_id = action["chat_id"], action["msg_id"]
    await resolve_peer(app, chat_id)
    await app.pin_chat_message(chat_id, msg_id)
    TOUCHED_CHATS.add(chat_id)
    return {"ok": True}

async def handle_unpin_message(app: Client, action: dict) -> dict:
    chat_id, msg_id = action["chat_id"], action.get("msg_id")
    await resolve_peer(app, chat_id)
    if msg_id:
        await app.unpin_chat_message(chat_id, msg_id)
    else:
        await app.unpin_all_chat_messages(chat_id)
    TOUCHED_CHATS.add(chat_id)
    return {"ok": True}

async def handle_forward(app: Client, action: dict) -> dict:
    from_chat, msg_id, to_chat = action["from_chat_id"], action["msg_id"], action["to_chat_id"]
    await resolve_peer(app, from_chat)
    await resolve_peer(app, to_chat)
    await app.forward_messages(to_chat, from_chat, msg_id)
    TOUCHED_CHATS.update({from_chat, to_chat} if isinstance(to_chat, int) else {from_chat})
    return {"ok": True}

async def handle_delete_message(app: Client, action: dict) -> dict:
    chat_id, msg_id = action["chat_id"], action["msg_id"]
    revoke = action.get("revoke", False)
    await resolve_peer(app, chat_id)
    await app.delete_messages(chat_id, msg_id, revoke=revoke)
    TOUCHED_CHATS.add(chat_id)
    return {"ok": True}

async def handle_edit_message(app: Client, action: dict) -> dict:
    chat_id, msg_id, text = action["chat_id"], action["msg_id"], action["text"]
    await resolve_peer(app, chat_id)
    await app.edit_message_text(chat_id, msg_id, text)
    TOUCHED_CHATS.add(chat_id)
    return {"ok": True}

async def handle_bot_callback(app: Client, action: dict) -> dict:
    chat_id, msg_id, callback_data = action["chat_id"], action["msg_id"], action["callback_data"]
    await resolve_peer(app, chat_id)
    await app.request_callback_answer(chat_id, msg_id, callback_data)
    TOUCHED_CHATS.add(chat_id)
    return {"ok": True}

async def handle_join_chat(app: Client, action: dict) -> dict:
    target = action.get("target", "")
    if "joinchat" in str(target) or "+t.me" in str(target) or str(target).startswith("https://t.me/+"):
        result = await app.join_chat(target)
    elif str(target).lstrip("-").isdigit():
        result = await app.join_chat(int(target))
    else:
        result = await app.join_chat(_norm_username(target))
    TOUCHED_CHATS.add(result.id)
    return {"ok": True, "chat_id": result.id, "title": result.title or result.first_name}

async def handle_set_fetch_count(app: Client, action: dict) -> dict:
    """Per-chat 'how many messages to fetch' override, set from the chat's Info panel."""
    chat_id = action["chat_id"]
    count   = max(1, min(int(action["count"]), 500))
    settings = load_settings()
    overrides = settings.setdefault("fetch_overrides", {})
    overrides[str(chat_id)] = count
    save_settings(settings)
    TOUCHED_CHATS.add(chat_id)
    return {"ok": True, "chat_id": chat_id, "count": count}

async def handle_set_ghost_mode(app: Client, action: dict) -> dict:
    enabled = bool(action.get("enabled"))
    settings = load_settings()
    settings["ghost_mode"] = enabled
    save_settings(settings)
    return {"ok": True, "ghost_mode": enabled}

HANDLERS = {
    "send_message":     handle_send_message,
    "start_bot":         handle_start_bot,
    "react":             handle_react,
    "mark_read":         handle_mark_read,
    "download_request":  handle_download_request,
    "pin_message":       handle_pin_message,
    "unpin_message":     handle_unpin_message,
    "forward":           handle_forward,
    "delete_message":    handle_delete_message,
    "edit_message":      handle_edit_message,
    "bot_callback":      handle_bot_callback,
    "join_chat":         handle_join_chat,
    "set_fetch_count":   handle_set_fetch_count,
    "set_ghost_mode":    handle_set_ghost_mode,
}


async def main():
    raw_json = base64.b64decode(QUEUE_B64).decode()
    queue: list[dict] = json.loads(raw_json)
    print(f"Processing {len(queue)} queued action(s) …")

    app = Client(
        name="relay", api_id=API_ID, api_hash=API_HASH,
        session_string=SESSION_STRING, in_memory=True,
    )

    async with app:
        me = await app.get_me()
        print(f"✓ Connected as {me.first_name} (@{me.username})")

        print("  Pre-warming peer cache…")
        async for _ in app.get_dialogs():
            pass

        results = []
        for i, action in enumerate(queue):
            atype = action.get("type", "unknown")
            handler = HANDLERS.get(atype)
            try:
                if handler is None:
                    raise ValueError(f"Unknown action type: '{atype}'")
                result = await handler(app, action)
                results.append({"index": i, "type": atype, "status": "ok", "result": result})
                print(f"  [{i}] {atype} → ok")
            except FloodWait as e:
                print(f"  [{i}] {atype} → FloodWait {e.value}s, retrying…")
                await asyncio.sleep(e.value)
                try:
                    result = await handler(app, action)
                    results.append({"index": i, "type": atype, "status": "ok", "result": result})
                    print(f"  [{i}] {atype} → ok (after flood wait)")
                except Exception as e2:
                    results.append({"index": i, "type": atype, "status": "error", "error": str(e2)})
                    print(f"  [{i}] {atype} → error after retry: {e2}")
            except Exception as e:
                results.append({"index": i, "type": atype, "status": "error", "error": str(e)})
                print(f"  [{i}] {atype} → error: {e}")
            await asyncio.sleep(0.6)

        # ── SYNC: re-fetch every chat touched by an action so the result is
        # visible immediately, without waiting for the next cron run ──────
        settings = load_settings()
        ghost_mode = bool(settings.get("ghost_mode", False))
        fetch_overrides = settings.get("fetch_overrides", {})

        chats_data = tc.decrypt_or_default(CHATS_ENC, F, {})
        files_index = load_files_index()
        synced = []
        if TOUCHED_CHATS:
            print(f"  Syncing {len(TOUCHED_CHATS)} touched chat(s)…")
        for chat_id in TOUCHED_CHATS:
            override = fetch_overrides.get(str(chat_id))
            fetch_n = override if override else UPD_N
            try:
                entry = await tc.sync_chat(
                    app, chat_id, chats_data=chats_data, files_index=files_index,
                    files_dir=FILES_DIR, f_ui=F_ui, fetch_n=fetch_n,
                    ghost_mode=ghost_mode, me_id=me.id,
                )
                if entry:
                    synced.append(entry["name"])
                    print(f"    synced {entry['name']}")
            except Exception as e:
                print(f"    sync failed for {chat_id}: {e}")

        if TOUCHED_CHATS:
            CHATS_ENC.write_bytes(tc.encrypt(chats_data, F))
            CHATS_BROWSER.write_bytes(tc.encrypt(chats_data, F_ui))
            save_files_index(files_index)
            meta = tc.decrypt_or_default(META_FILE, F_ui, {})
            meta["last_sync"] = datetime.now(timezone.utc).isoformat()
            meta["last_queue_sync"] = synced
            META_FILE.write_bytes(tc.encrypt(meta, F_ui))

    RESULTS_FILE.write_text(json.dumps({
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "total":   len(queue),
        "ok":      sum(1 for r in results if r["status"] == "ok"),
        "errors":  sum(1 for r in results if r["status"] == "error"),
        "synced_chats": synced,
        "results": results,
    }, indent=2))
    print(f"✓ Done — results written to {RESULTS_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
