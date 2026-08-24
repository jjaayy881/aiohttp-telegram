import os
import re
import time
import asyncio
import logging
import secrets
import urllib.parse
from aiohttp import web
from pyrogram import Client

# Logging konfigurieren
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TelegramStalker")

# Telegram API Konfiguration
API_ID = 1234567  # Deine API ID eintragen
API_HASH = "DEIN_API_HASH"  # Dein API Hash eintragen
BOT_TOKEN = None  # Wenn Userbot, None lassen

XTREAM_USER = "admin"
XTREAM_PASS = "admin"

# Kanäle konfigurieren (Name: Chat ID)
CHANNEL_CHAT_ID = {
    "Film1": -1002187259012,
    "Film2": -1002350882842,
    "Filme3": -1002358546576,
    "Filme4": -1001188033420,
    "Filme5": -1003979214219,
    "Filme6": -1001256373139,
}

CHANNEL_IS_FORUM = {}
CHANNEL_FIXED_TOPICS = {}

# Interne Caches
STALKER_SESSIONS = {}
_vod_meta_cache = {}
_vod_id_to_key = {}
_vod_counter = 1000

def _next_vod_id():
    global _vod_counter
    _vod_counter += 1
    return _vod_counter

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def get_filename(msg):
    if msg.video and msg.video.file_name:
        return msg.video.file_name
    if msg.document and msg.document.file_name:
        return msg.document.file_name
    return f"Video_{msg.id}.mp4"

def clean_filename(name):
    clean = re.sub(r"\.(mp4|mkv|avi|mov)$", "", name, flags=re.IGNORECASE)
    clean = clean.replace(".", " ").replace("_", " ")
    return clean.strip(), ""

def _stalker_mac(request):
    mac = request.query.get("mac") or request.headers.get("X-User-MAC")
    if not mac and "metrics" in request.query:
        try:
            import json
            metrics = json.loads(request.query.get("metrics", "{}"))
            mac = metrics.get("mac")
        except Exception:
            pass
    return mac or "00:1A:00:11:11:11"

def _stalker_issue_token(mac):
    token = secrets.token_hex(16)
    STALKER_SESSIONS[token] = {"mac": mac, "expires": time.time() + 86400}
    return token

def _find_vod_item(vod_id):
    try:
        vod_id = int(vod_id)
    except ValueError:
        return None
    key = _vod_id_to_key.get(vod_id)
    return _vod_meta_cache.get(key) if key else None

async def iter_media_messages(chat_id, is_forum, fixed_topics, limit=50):
    """ Hilfsfunktion zum Laden von Nachrichten aus Telegram """
    async for msg in client.get_chat_history(chat_id, limit=limit):
        if msg.video or msg.document:
            topic_title = None
            if is_forum and msg.reply_to_message_id:
                topic_id = msg.reply_to_message_id
                if fixed_topics and topic_id in fixed_topics:
                    topic_title = fixed_topics[topic_id]
            yield msg.reply_to_message_id, topic_title, msg

# --- STALKER PORTAL HANDLER (Catch-All Routing) ---

async def stalker_portal_handler(request):
    action = request.query.get("action", "")
    req_type = request.query.get("type", "")

    if action == "handshake":
        mac = _stalker_mac(request)
        token = _stalker_issue_token(mac)
        log(f"[Stalker] Handshake erfolgreich für MAC={mac}")
        return web.json_response({"js": {"token": token, "random": secrets.token_hex(8)}})

    if action == "get_profile":
        mac = _stalker_mac(request)
        return web.json_response({"js": {
            "id": 1,
            "mac": mac,
            "stb_type": "MAG250",
            "status": 0,
            "max_online_time": 0,
            "tariff_plan_id": 0,
        }})

    if action == "get_main_info" or (req_type == "account_info" and action == "get_main_info"):
        return web.json_response({"js": {"status": 0, "phone": "", "end_date": ""}})

    if req_type == "watchdog" or action == "get_events":
        return web.json_response({"js": {"cur_play_type": 0, "event": "none"}})

    if req_type == "vod" and action == "get_categories":
        cats = [
            {"id": str(idx + 1), "title": name, "alias": name}
            for idx, name in enumerate(CHANNEL_CHAT_ID.keys())
        ]
        return web.json_response({"js": cats})

    if req_type == "vod" and action == "get_ordered_list":
        requested_cat = request.query.get("category")
        page = int(request.query.get("p") or 1)
        page_size = 14

        all_items = []
        for idx, channel_name in enumerate(CHANNEL_CHAT_ID.keys(), start=1):
            if requested_cat and requested_cat != "*" and str(idx) != str(requested_cat):
                continue

            for key, item in _vod_meta_cache.items():
                if key[0] == channel_name:
                    all_items.append({
                        "id": str(item["vod_id"]),
                        "name": item["name"],
                        "o_name": item["name"],
                        "screenshot_uri": item["poster"],
                        "year": item.get("year", ""),
                        "description": item.get("overview", ""),
                        "cmd": f"vod_id:{item['vod_id']}",
                    })

        start = (page - 1) * page_size
        page_items = all_items[start:start + page_size]
        return web.json_response({"js": {
            "total_items": len(all_items),
            "max_page_items": page_size,
            "selected_item": 0,
            "data": page_items,
        }})

    if req_type == "vod" and action == "create_link":
        cmd = request.query.get("cmd", "")
        vod_id = cmd.split(":")[-1] if ":" in cmd else cmd
        item = _find_vod_item(vod_id)
        if not item:
            return web.json_response({"js": {}}, status=404)
        base = f"{request.scheme}://{request.host}"
        stream_url = f"{base}/movie/{XTREAM_USER}/{XTREAM_PASS}/{item['vod_id']}.mp4"
        return web.json_response({"js": {"cmd": stream_url, "id": item["vod_id"]}})

    if req_type in ("itv", "epg") or action in ("get_all_channels", "get_genres"):
        return web.json_response({"js": {"data": []}})

    return web.json_response({"js": {}}, status=200)

# --- STREAMING HANDLER FOR MULTIPLE PLAYERS (HTTP 206 PARTIAL CONTENT) ---

async def stream_movie_handler(request):
    vod_id = request.match_info.get("vod_id", "").replace(".mp4", "")
    item = _find_vod_item(vod_id)
    if not item:
        return web.Response(status=404, text="Video nicht gefunden")

    chat_id = CHANNEL_CHAT_ID[item["channel"]]
    msg_id = item["msg_id"]

    try:
        msg = await client.get_messages(chat_id, msg_id)
        media = msg.video or msg.document or msg.animation
        if not media:
            return web.Response(status=404, text="Medium fehlt")

        file_size = media.file_size
        range_header = request.headers.get("Range")

        from_bytes = 0
        until_bytes = file_size - 1

        if range_header:
            try:
                bytes_range = range_header.replace("bytes=", "").split("-")
                from_bytes = int(bytes_range[0])
                if len(bytes_range) > 1 and bytes_range[1]:
                    until_bytes = int(bytes_range[1])
            except ValueError:
                return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})

        content_length = (until_bytes - from_bytes) + 1
        headers = {
            "Content-Type": getattr(media, "mime_type", "video/mp4") or "video/mp4",
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Length": str(content_length),
            "Access-Control-Allow-Origin": "*",
        }

        response = web.StreamResponse(status=206 if range_header else 200, headers=headers)
        await response.prepare(request)

        async for chunk in client.stream_media(msg, offset=from_bytes):
            await response.write(chunk)
            if response.written >= content_length:
                break

        await response.write_eof()
        return response
    except Exception as e:
        log(f"[Stream Fehler] VOD {vod_id}: {e}")
        return web.Response(status=500)

# --- SERVER ROUTING & CACHE PREPARATION ---

async def preload_cache():
    log("Wärme Medienspeicher für Kanäle auf...")
    for channel_name, chat_id in CHANNEL_CHAT_ID.items():
        is_forum = CHANNEL_IS_FORUM.get(channel_name, False)
        fixed_topics = CHANNEL_FIXED_TOPICS.get(channel_name)
        async for topic_id, topic_title, msg in iter_media_messages(chat_id, is_forum, fixed_topics, limit=50):
            key = (channel_name, msg.id)
            if key not in _vod_meta_cache:
                raw_name = get_filename(msg)
                clean_name, _ = clean_filename(raw_name)
                vod_id = _next_vod_id()
                display_name = f"[{topic_title}] {clean_name}" if topic_title else clean_name
                item = {
                    "vod_id": vod_id,
                    "channel": channel_name,
                    "msg_id": msg.id,
                    "name": display_name,
                    "poster": "",
                    "overview": getattr(msg, "caption", None) or "Keine Beschreibung",
                    "year": "",
                }
                _vod_meta_cache[key] = item
                _vod_id_to_key[vod_id] = key
    log("Medienspeicher bereit!")

def create_app():
    app = web.Application()
    # Flexible Routen für 127.0.0.1:8585, /portal.php und load.php
    app.router.add_get(r"/{path:.*load.php.*}", stalker_portal_handler)
    app.router.add_get(r"/{path:.*portal.php.*}", stalker_portal_handler)
    app.router.add_get("/", stalker_portal_handler)
    app.router.add_get(r"/movie/{user}/{password}/{vod_id}", stream_movie_handler)
    return app

# --- MAIN ENGINE ---

client = Client("my_account", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

async def main():
    log("Starte Pyrogram-Client ...")
    await client.start()
    log("Pyrogram-Client gestartet.")
    
    await preload_cache()

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = 8585
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log(f"Server läuft auf Port {port}")
    log("Nutze als Portal-URL im Player: http://127.0.0.1:8585")

    await asyncio.Event().wait()

# Bridge für Chaquopy / Android App Aufruf
def start_blocking():
    """ Wird vom Java / Kotlin Code im Android App Wrapper aufgerufen """
    asyncio.run(main())

if __name__ == "__main__":
    try:
        start_blocking()
    except KeyboardInterrupt:
        log("Server gestoppt.")