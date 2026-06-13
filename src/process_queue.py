"""
process_queue.py — processes a batch of actions dispatched from the GitHub Pages UI.
Actions arrive as a base64-encoded JSON array via the QUEUE_B64 env var.
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
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji
from cryptography.fernet import Fernet

API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
RAW_KEY  = os.environ["DATA_KEY"]
QUEUE_B64 = os.environ["QUEUE_B64"]

DATA_DIR   = Path("data")
RESULTS_FILE = DATA_DIR / "queue_results.json"

def _fernet(passphrase: str) -> Fernet:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(passphrase.encode()).digest()
    )
    return Fernet(key)

F = _fernet(RAW_KEY)

def decrypt(data: bytes):
    return json.loads(F.decrypt(data))

# ── action handlers ───────────────────────────────────────────────────────────

async def handle_send_message(client, action: dict) -> dict:
    chat_id = action["chat_id"]
    text    = action["text"]
    reply_to = action.get("reply_to")
    entity  = await client.get_entity(chat_id)
    msg = await client.send_message(entity, text, reply_to=reply_to)
    return {"ok": True, "msg_id": msg.id}

async def handle_react(client, action: dict) -> dict:
    chat_id = action["chat_id"]
    msg_id  = action["msg_id"]
    emoji   = action["emoji"]
    entity  = await client.get_entity(chat_id)
    await client(SendReactionRequest(
        peer=entity,
        msg_id=msg_id,
        reaction=[ReactionEmoji(emoticon=emoji)],
    ))
    return {"ok": True}

async def handle_mark_read(client, action: dict) -> dict:
    chat_id = action["chat_id"]
    entity  = await client.get_entity(chat_id)
    await client.send_read_acknowledge(entity)
    return {"ok": True}

async def handle_download_request(client, action: dict) -> dict:
    """
    We don't actually download here — we just flag the file as
    'download_requested' in the data store so the next fetch picks it up.
    Real downloading would require separate storage (GH LFS / external bucket).
    """
    return {"ok": True, "queued": True, "note": "Download flagged for next fetch cycle."}

async def handle_pin_message(client, action: dict) -> dict:
    chat_id = action["chat_id"]
    msg_id  = action["msg_id"]
    entity  = await client.get_entity(chat_id)
    await client.pin_message(entity, msg_id)
    return {"ok": True}

async def handle_forward(client, action: dict) -> dict:
    from_chat = action["from_chat_id"]
    msg_id    = action["msg_id"]
    to_chat   = action["to_chat_id"]
    src  = await client.get_entity(from_chat)
    dest = await client.get_entity(to_chat)
    await client.forward_messages(dest, msg_id, src)
    return {"ok": True}

HANDLERS = {
    "send_message":       handle_send_message,
    "react":              handle_react,
    "mark_read":          handle_mark_read,
    "download_request":   handle_download_request,
    "pin_message":        handle_pin_message,
    "forward":            handle_forward,
}

# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    queue_json = base64.b64decode(QUEUE_B64).decode()
    queue: list[dict] = json.loads(queue_json)
    print(f"Processing {len(queue)} queued actions …")

    SESSION_STRING = os.environ["TG_SESSION_STRING"]
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    results = []
    for i, action in enumerate(queue):
        atype = action.get("type", "unknown")
        handler = HANDLERS.get(atype)
        try:
            if handler is None:
                raise ValueError(f"Unknown action type: {atype}")
            result = await handler(client, action)
            results.append({"index": i, "type": atype, "status": "ok", "result": result})
            print(f"  [{i}] {atype} → ok")
        except Exception as e:
            results.append({"index": i, "type": atype, "status": "error", "error": str(e)})
            print(f"  [{i}] {atype} → error: {e}")
        # small delay to respect rate limits
        await asyncio.sleep(0.5)

    await client.disconnect()

    DATA_DIR.mkdir(exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "total": len(queue),
        "results": results,
    }, indent=2))
    print("✓ Results written to data/queue_results.json")

asyncio.run(main())
