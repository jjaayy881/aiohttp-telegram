import asyncio
import os

# WICHTIG: Muss VOR "from pyrogram import Client" passieren.
# Pyrogram holt sich beim Import (pyrogram/sync.py) sofort einen Event-Loop
# fuer den aktuellen Thread. Der Kotlin-Hintergrund-Thread (ServerService.kt)
# hat aber von Haus aus keinen -> ohne das hier: RuntimeError "no current
# event loop in thread 'Dummy-1'" schon beim reinen Import.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import json
import re
import time
import urllib.parse

import aiohttp
from aiohttp import web
from pyrogram import Client

# =========================
# CONFIG
# =========================
# API_ID, API_HASH, SESSION_STRING und CHANNELS kommen NICHT hartkodiert aus
# dieser Datei, sondern werden von Kotlin (ServerService.kt) aus config.json
# an start_blocking() übergeben.
#
# Format der "channels" in config.json:
#   "Filme": -1001188033420                     -> normaler Kanal
#   "Kiosk": [-1002187259012, 92956]             -> Forum, NUR Topic 92956
#   "Kiosk": [-1002187259012, [92956, 11154]]    -> Forum, NUR diese Topics
CHANNELS = {}
CHANNEL_CHAT_ID = {}
CHANNEL_FIXED_TOPICS = {}
CHANNEL_IS_FORUM = {}

PORT = 8585

# TMDB (Poster/Titel) und Xtream-Codes-Zugangsdaten - kommen wie alles
# andere über start_blocking() aus config.json, mit Defaults, damit eine
# ALTE config.json ohne diese Felder weiterhin funktioniert (nur eben ohne
# Poster / mit Standard-Zugangsdaten "admin"/"admin").
TMDB_KEY = ""
TMDB_LANG = "de-DE"
XTREAM_USER = "admin"
XTREAM_PASS = "admin"
TMDB_SESSION: "aiohttp.ClientSession | None" = None

# client wird erst in start_blocking() erzeugt
client: Client = None


# =========================
# LOGGING
# =========================
# Chaquopy leitet print() bereits automatisch an Logcat weiter, daher reicht
# ein einfaches print() hier - kein Datei-Log wie im Pydroid-Setup nötig.
def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# =========================
# HELPERS
# =========================
def get_filename(msg):
    if msg:
        if msg.document and msg.document.file_name:
            return msg.document.file_name
        if msg.video and msg.video.file_name:
            return msg.video.file_name
    return f"{msg.id}.mp4"


def parse_channels_config(channels_json: str):
    """
    Zerlegt die channels_json in CHANNEL_CHAT_ID (Name -> chat_id) und
    CHANNEL_FIXED_TOPICS (Name -> {topic_id: title_oder_None}).

    channels_json Format pro Kanal:
      chat_id                                    -> normaler Kanal, keine Topics
      [chat_id, topic_id]                        -> EIN Topic, Titel wird bei
                                                     Bedarf von Telegram geholt
      [chat_id, [id1, id2, ...]]                 -> mehrere Topics, Titel
                                                     werden von Telegram geholt
      [chat_id, {"id1": "Titel 1", "id2": "..."}] -> Topics MIT Titel direkt
                                                     angegeben -> KEIN
                                                     zusätzlicher Telegram-
                                                     Aufruf nötig, deutlich
                                                     schneller bei Kanälen mit
                                                     vielen Topics!
    """
    raw = json.loads(channels_json)
    chat_ids, fixed_topics = {}, {}
    for name, value in raw.items():
        if isinstance(value, list):
            chat_id, topics = value[0], value[1]
            if isinstance(topics, dict):
                # {"92956": "Film1", "11154": "Film2"} -> {92956: "Film1", ...}
                fixed_topics[name] = {int(k): v for k, v in topics.items()}
            elif isinstance(topics, list):
                fixed_topics[name] = {int(t): None for t in topics}
            else:
                fixed_topics[name] = {int(topics): None}
            chat_ids[name] = chat_id
        else:
            chat_ids[name] = value
            fixed_topics[name] = None
    return chat_ids, fixed_topics


_topics_list_cache = {}  # chat_id -> Liste von {id, title}


async def get_topics(chat_id, limit=200):
    """Liefert alle Forum-Topics eines Kanals (gecached, damit wiederholte
    Aufrufe nicht jedes Mal die komplette Liste neu von Telegram laden)."""
    if chat_id in _topics_list_cache:
        return _topics_list_cache[chat_id]

    topics = []
    try:
        async for topic in client.get_forum_topics(chat_id, limit=limit):
            topics.append({"id": topic.id, "title": topic.title or f"Topic {topic.id}"})
    except Exception as e:
        log(f"⚠ Konnte Topics nicht laden: {e}")
        return topics  # bei Fehler NICHT cachen, damit ein erneuter Versuch möglich bleibt

    _topics_list_cache[chat_id] = topics
    return topics


_msglist_cache = {}  # (chat_id, topic_id) -> (timestamp, [msg, ...])
MSGLIST_CACHE_TTL = 180  # Sekunden - solange gilt eine geladene Filmliste als "frisch genug"


async def get_cached_messages(chat_id, topic_id, limit=50):
    """
    Lädt die Filmliste eines Kanals/Topics gecached. Verhindert, dass
    mehrere Anfragen kurz hintereinander (z.B. Browser-Test + Kodi
    gleichzeitig) denselben search_messages()-Aufruf wiederholen und
    dadurch Telegrams Flood-Control auslösen ("Waiting for X seconds ...").
    """
    key = (chat_id, topic_id)
    cached = _msglist_cache.get(key)
    if cached and (time.monotonic() - cached[0]) < MSGLIST_CACHE_TTL:
        return cached[1]

    msgs = []
    try:
        if topic_id is not None:
            async for msg in client.search_messages(chat_id, message_thread_id=topic_id, limit=limit):
                if msg.document or msg.video or msg.audio:
                    msgs.append(msg)
        else:
            async for msg in client.get_chat_history(chat_id, limit=limit):
                if msg.document or msg.video or msg.audio:
                    msgs.append(msg)
    except Exception as e:
        log(f"⚠ Fehler beim Laden von Chat={chat_id} Topic={topic_id}: {e}")
        if cached:
            return cached[1]  # bei Fehler lieber alte Daten zeigen als gar keine
        return msgs

    _msglist_cache[key] = (time.monotonic(), msgs)
    return msgs


async def iter_media_messages(chat_id, is_forum, fixed_topics=None, limit=50):
    """
    Generator, der (topic_id, topic_title, msg) liefert. Bei normalen
    Kanälen ist topic_id/topic_title = None. Bei Forum-Kanälen mit
    fixed_topics ({topic_id: titel_oder_None}) wird NUR diese Liste
    durchsucht (schnell). Ist ein Titel manuell in der config.json
    angegeben, wird er direkt verwendet - sonst on-demand von Telegram
    nachgeladen (get_topics(), gecached). Nutzt intern den Filmlisten-
    Cache, um wiederholte Telegram-Anfragen zu vermeiden.
    """
    if is_forum and fixed_topics:
        need_lookup = any(t is None for t in fixed_topics.values())
        all_topics_by_id = {}
        if need_lookup:
            all_topics_by_id = {t["id"]: t["title"] for t in await get_topics(chat_id)}

        for topic_id, given_title in fixed_topics.items():
            title = given_title or all_topics_by_id.get(topic_id, f"Topic {topic_id}")
            for msg in await get_cached_messages(chat_id, topic_id, limit):
                yield topic_id, title, msg
    elif is_forum:
        topics = await get_topics(chat_id)
        for topic in topics:
            for msg in await get_cached_messages(chat_id, topic["id"], limit):
                yield topic["id"], topic["title"], msg
    else:
        for msg in await get_cached_messages(chat_id, None, limit):
            yield None, None, msg


async def warmup_peer_cache():
    """Lädt einmal alle Dialoge, damit rohe Chat-IDs sicher aufgelöst werden können."""
    log("Peer-Cache wird aufgewärmt ...")
    count = 0
    async for _ in client.get_dialogs():
        count += 1
    log(f"✓ {count} Dialoge geladen, Peer-Cache bereit.")


async def ensure_chats_resolved():
    log("Kanäle werden überprüft und registriert ...")
    for name, chat_id in CHANNEL_CHAT_ID.items():
        try:
            await client.join_chat(chat_id)
            log(f"✓ Kanal '{name}' ({chat_id}) erfolgreich aufgelöst.")
        except Exception:
            try:
                await client.get_chat(chat_id)
                log(f"✓ Kanal '{name}' ({chat_id}) war bereits bekannt.")
            except Exception as inner_e:
                log(f"⚠ Fehler beim Laden von '{name}' ({chat_id}): {inner_e}")

        try:
            log(f"  … prüfe Forum-Status von '{name}' ...")
            fresh_chat = await client.get_chat(chat_id)
            is_forum = bool(getattr(fresh_chat, "is_forum", False))
            CHANNEL_IS_FORUM[name] = is_forum
            fixed_topics = CHANNEL_FIXED_TOPICS.get(name)
            if fixed_topics:
                log(f"  → '{name}': is_forum={is_forum}, beschränkt auf Topic-IDs {fixed_topics}")
                if is_forum:
                    # Filmlisten der festgelegten Topics schon im Hintergrund
                    # vorladen, statt erst zu warten, bis Kodi den Kanal
                    # öffnet (vermeidet Flood-Wait bei Telegram).
                    for topic_id in fixed_topics:
                        asyncio.create_task(get_cached_messages(chat_id, topic_id))
                    # Die komplette Topic-Liste (für Titel-Auflösung) nur
                    # laden, wenn wirklich mindestens ein Titel fehlt -
                    # sonst unnötig teuer bei Kanälen mit vielen Topics.
                    if any(t is None for t in fixed_topics.values()):
                        asyncio.create_task(get_topics(chat_id))
            else:
                log(f"  → '{name}': is_forum={is_forum}")
        except Exception as e:
            CHANNEL_IS_FORUM[name] = False
            log(f"⚠ Konnte Forum-Status von '{name}' nicht bestimmen: {e}")
    log("Alle Kanäle geprüft.")


# =========================
# HEAD/TAIL CACHE
# =========================
# Player fragen beim Öffnen fast immer zuerst Dateianfang UND Dateiende
# gleichzeitig an (Container-Metadaten). Zwei parallele große Sprünge in
# derselben Telegram-Datei sind sehr langsam (20+ Sekunden) - deshalb
# cachen wir diese beiden Randbereiche einmalig im Speicher.
HEAD_CACHE_BYTES = 6 * 1024 * 1024  # erste 6 MB
TAIL_CACHE_BYTES = 6 * 1024 * 1024  # letzte 6 MB

_file_cache = {}          # (msg_id, "head"/"tail") -> bytes
_file_cache_locks = {}    # (msg_id, "head"/"tail") -> asyncio.Lock


def fix_moov_overflow(data: bytes, base_offset: int, file_size: int):
    """
    Sicherheitsnetz: Falls eine 'moov'-Box (MP4-Index) im Tail-Cache eine
    Größe deklariert, die über das Dateiende hinausragt, wird das
    Größenfeld auf den tatsächlich verfügbaren Wert korrigiert. Das
    schützt vor kaputten Repacks, ohne echte, korrekte Dateien anzufassen
    (die overflow-Prüfung greift dort einfach nicht).
    """
    idx = data.find(b"moov")
    if idx < 4:
        return data
    box_start = idx - 4
    declared_size = int.from_bytes(data[box_start:box_start + 4], "big")
    box_abs_start = base_offset + box_start
    available = file_size - box_abs_start
    if declared_size <= available:
        return data
    patched = bytearray(data)
    patched[box_start:box_start + 4] = available.to_bytes(4, "big")
    log(f"[CACHE] moov-Box war {declared_size - available} bytes zu groß deklariert -> korrigiert")
    return bytes(patched)


async def get_cached_region(msg, msg_id, file_size, region):
    """Lädt den Anfang/das Ende einer Datei einmalig und cached ihn im Speicher."""
    key = (msg_id, region)
    if key in _file_cache:
        return _file_cache[key]

    lock = _file_cache_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if key in _file_cache:
            return _file_cache[key]

        chunk_size = 1024 * 1024
        need = min(HEAD_CACHE_BYTES if region == "head" else TAIL_CACHE_BYTES, file_size)

        if region == "head":
            offset_chunks = 0
            target_total = need
        else:
            tail_start = file_size - need
            offset_chunks = tail_start // chunk_size
            # WICHTIG: offset_chunks rundet auf die 1-MiB-Chunk-Grenze VOR
            # tail_start ab. Deshalb bis zum ECHTEN Dateiende laden, sonst
            # fehlen die letzten paar hundert KB samt moov-Atom!
            target_total = file_size - (offset_chunks * chunk_size)

        log(f"[CACHE] msg={msg_id} lade {region}-Bereich ({target_total} bytes) von Telegram ...")
        t0 = time.monotonic()
        buf = bytearray()
        async for chunk in client.stream_media(msg, offset=offset_chunks):
            buf.extend(chunk)
            if len(buf) >= target_total:
                break
        data = bytes(buf)

        if region == "tail":
            data = fix_moov_overflow(data, offset_chunks * chunk_size, file_size)

        _file_cache[key] = data
        log(f"[CACHE] msg={msg_id} {region}-Bereich fertig geladen ({len(data)} bytes in {time.monotonic()-t0:.2f}s)")
        return data


def region_for_range(start_byte, end_byte, file_size):
    if end_byte < HEAD_CACHE_BYTES:
        return "head"
    if start_byte >= file_size - TAIL_CACHE_BYTES:
        return "tail"
    return None


# =========================
# STREAM
# =========================
async def stream_handler(request):
    try:
        msg_id = int(request.query.get("msg"))
        channel = request.query.get("channel", "Filme")
    except (TypeError, ValueError):
        return web.Response(text="Fehlender/ungültiger Parameter 'msg'", status=400)
    return await _stream_core(request, channel, msg_id)


async def stream_handler_path(request):
    """
    Wie stream_handler, nur mit Kanal/Nachricht-ID im URL-PFAD statt als
    Query-Parameter, mit ".mp4"-Endung: /stream/Filme/123.mp4

    Grund: Viele IPTV-Player (z.B. StreamVault) erkennen VOD-Einträge in
    einer M3U-Playlist nur, wenn die URL selbst auf eine bekannte
    Video-Endung endet.
    """
    try:
        channel = request.match_info["channel"]
        msg_id = int(request.match_info["msg"])
    except (KeyError, ValueError):
        return web.Response(text="Ungültiger Pfad", status=400)
    return await _stream_core(request, channel, msg_id)


async def _stream_core(request, channel, msg_id):
    req_start = time.monotonic()

    def is_alive():
        try:
            transport = request.transport
            return transport is not None and not transport.is_closing()
        except Exception:
            return False

    DISCONNECT_EXCEPTIONS = (
        ConnectionError, ConnectionResetError, ConnectionAbortedError,
        asyncio.CancelledError, BrokenPipeError,
    )

    try:
        CHAT_ID = CHANNEL_CHAT_ID.get(channel)
        if not CHAT_ID:
            return web.Response(text="Channel not found", status=404)

        msg = await client.get_messages(CHAT_ID, message_ids=msg_id)
        media = msg.document or msg.video or msg.audio
        if not media:
            return web.Response(text="No media found in message", status=404)

        file_size = getattr(media, "file_size", 0)
        if not file_size:
            return web.Response(text="File size unknown", status=500)

        if request.method == "HEAD":
            mime_type = getattr(media, "mime_type", "video/mp4") or "video/mp4"
            # Anfang/Ende schon mal im Hintergrund vorladen, während der
            # Player noch die HEAD-Antwort verarbeitet.
            if (msg_id, "head") not in _file_cache:
                asyncio.create_task(get_cached_region(msg, msg_id, file_size, "head"))
            if (msg_id, "tail") not in _file_cache:
                asyncio.create_task(get_cached_region(msg, msg_id, file_size, "tail"))
            return web.Response(status=200, headers={
                "Content-Type": mime_type,
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Access-Control-Allow-Origin": "*",
            })

        range_header = request.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            status_code = 206
            range_str = range_header.replace("bytes=", "")
            parts = range_str.split("-")
            start_byte = int(parts[0]) if parts[0] else 0
            end_byte = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        else:
            status_code = 200
            start_byte = 0
            end_byte = file_size - 1

        percent = (start_byte / file_size * 100) if file_size else 0
        log(f"[STREAM] msg={msg_id} Range={start_byte}-{end_byte} ({percent:.1f}%) angefragt")

        if start_byte >= file_size or end_byte >= file_size or start_byte > end_byte:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})

        mime_type = getattr(media, "mime_type", "video/mp4") or "video/mp4"
        headers = {
            "Content-Type": mime_type,
            "Accept-Ranges": "bytes",
            "Content-Length": str(end_byte - start_byte + 1),
            "Access-Control-Allow-Origin": "*",
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{file_size}"

        # --- Fall A: komplette Anfrage liegt in Head- oder Tail-Fenster ---
        region = region_for_range(start_byte, end_byte, file_size)
        if region is not None:
            cached = await get_cached_region(msg, msg_id, file_size, region)
            if region == "head":
                slice_start = start_byte
            else:
                slice_start = start_byte - (file_size - len(cached))
            slice_end = slice_start + (end_byte - start_byte + 1)
            payload = cached[slice_start:slice_end]
            ttfb = time.monotonic() - req_start
            log(f"[STREAM] msg={msg_id} aus {region}-Cache bedient nach {ttfb:.2f}s ({len(payload)} bytes)")
            return web.Response(status=status_code, headers=headers, body=payload)

        if not is_alive():
            return web.Response(status=204)

        response = web.StreamResponse(status=status_code, headers=headers)
        await response.prepare(request)

        bytes_to_send = end_byte - start_byte + 1
        live_start = start_byte
        first_chunk = True

        try:
            # --- Fall B: Anfrage BEGINNT im Head-Cache-Fenster, geht aber
            # weiter (typisch für "gib mir alles ab Byte 0"). Anfang sofort
            # aus Cache liefern, Rest live nachliefern.
            if start_byte < HEAD_CACHE_BYTES:
                cached = await get_cached_region(msg, msg_id, file_size, "head")
                if start_byte < len(cached):
                    prefix_end = min(len(cached), start_byte + bytes_to_send)
                    prefix = cached[start_byte:prefix_end]
                    if prefix:
                        if not is_alive():
                            return response
                        ttfb = time.monotonic() - req_start
                        log(f"[STREAM] msg={msg_id} Kopf-Vorschub aus Cache nach {ttfb:.2f}s ({len(prefix)} bytes)")
                        await response.write(prefix)
                        bytes_to_send -= len(prefix)
                        live_start = start_byte + len(prefix)
                        first_chunk = False

            if bytes_to_send > 0 and is_alive():
                chunk_size = 1024 * 1024
                offset_chunks = live_start // chunk_size
                skip_bytes = live_start % chunk_size

                async for chunk in client.stream_media(msg, offset=offset_chunks):
                    if not is_alive():
                        break
                    if first_chunk:
                        ttfb = time.monotonic() - req_start
                        log(f"[STREAM] msg={msg_id} erstes Live-Chunk nach {ttfb:.2f}s erhalten")
                        first_chunk = False

                    if skip_bytes > 0:
                        if skip_bytes >= len(chunk):
                            skip_bytes -= len(chunk)
                            continue
                        else:
                            chunk = chunk[skip_bytes:]
                            skip_bytes = 0

                    if len(chunk) > bytes_to_send:
                        chunk = chunk[:bytes_to_send]

                    try:
                        await response.write(chunk)
                    except DISCONNECT_EXCEPTIONS:
                        break

                    bytes_to_send -= len(chunk)
                    if bytes_to_send <= 0:
                        break
        except DISCONNECT_EXCEPTIONS:
            pass

        total_time = time.monotonic() - req_start
        log(f"[STREAM] msg={msg_id} Range={start_byte}-{end_byte} fertig nach {total_time:.2f}s")
        return response

    except DISCONNECT_EXCEPTIONS:
        return web.Response(status=204)
    except Exception as e:
        log(f"[STREAM] ❌ FEHLER: {type(e).__name__}: {e}")
        return web.Response(text=str(e), status=500)


# =========================
# THUMBNAIL
# =========================
async def thumb_handler(request):
    try:
        msg_id = int(request.query.get("msg"))
        channel = request.query.get("channel", "Filme")

        CHAT_ID = CHANNEL_CHAT_ID.get(channel)
        if not CHAT_ID:
            return web.Response(status=404)

        msg = await client.get_messages(CHAT_ID, message_ids=msg_id)
        if not msg:
            return web.Response(status=404)

        media = msg.document or msg.video or msg.audio
        thumbs = getattr(media, "thumbs", None) if media else None
        if not thumbs:
            return web.Response(status=404)

        try:
            thumb_bytes = await asyncio.wait_for(
                client.download_media(thumbs[-1], in_memory=True), timeout=8
            )
        except asyncio.TimeoutError:
            return web.Response(status=504)

        if not thumb_bytes:
            return web.Response(status=404)

        thumb_data = thumb_bytes.getbuffer() if hasattr(thumb_bytes, "getbuffer") else thumb_bytes
        return web.Response(
            body=thumb_data,
            content_type="image/jpeg",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception as e:
        log(f"[THUMB] FEHLER: {e}")
        return web.Response(text=str(e), status=500)


# =========================
# TOPICS
# =========================
async def topics_handler(request):
    channel = request.query.get("channel", "Filme")
    chat_id = CHANNEL_CHAT_ID.get(channel)
    if not chat_id:
        return web.Response(text="Channel not found", status=404)
    if not CHANNEL_IS_FORUM.get(channel):
        return web.json_response([])

    fixed_topics = CHANNEL_FIXED_TOPICS.get(channel)
    if fixed_topics:
        need_lookup = any(t is None for t in fixed_topics.values())
        all_topics_by_id = {}
        if need_lookup:
            all_topics_by_id = {t["id"]: t["title"] for t in await get_topics(chat_id)}
        result = [
            {"id": tid, "title": given_title or all_topics_by_id.get(tid, f"Topic {tid}")}
            for tid, given_title in fixed_topics.items()
        ]
        return web.json_response(result)

    topics = await get_topics(chat_id)
    return web.json_response(topics)


# =========================
# HTML BROWSER
# =========================
async def browse_handler(request):
    html = "<h1>Filme Server</h1><hr>"
    for channel_name, chat_id in CHANNEL_CHAT_ID.items():
        html += f"<h2>{channel_name}</h2><pre>"
        is_forum = CHANNEL_IS_FORUM.get(channel_name, False)
        fixed_topics = CHANNEL_FIXED_TOPICS.get(channel_name)
        current_topic = None
        async for topic_id, topic_title, msg in iter_media_messages(chat_id, is_forum, fixed_topics, limit=50):
            if is_forum and topic_title != current_topic:
                current_topic = topic_title
                html += f"\n<b>[{topic_title}]</b>\n"
            name = get_filename(msg)
            safe_channel = urllib.parse.quote(channel_name, safe="")
            url = f"/stream?channel={safe_channel}&msg={msg.id}"
            html += f'  <a href="{url}">{name}</a>\n'
        html += "</pre>"
    return web.Response(text=html, content_type="text/html")


# =========================
# JSON (für Kodi-Addon / Android-TV-Client)
# =========================
async def json_handler(request):
    data = []
    requested_channel = request.query.get("channel")
    requested_topic = request.query.get("topic")

    channels_to_scan = (
        {requested_channel: CHANNEL_CHAT_ID[requested_channel]}
        if requested_channel and requested_channel in CHANNEL_CHAT_ID
        else CHANNEL_CHAT_ID
    )

    for channel_name, chat_id in channels_to_scan.items():
        is_forum = CHANNEL_IS_FORUM.get(channel_name, False)
        fixed_topics = CHANNEL_FIXED_TOPICS.get(channel_name)
        safe_channel = urllib.parse.quote(channel_name, safe="")

        if requested_topic is not None and is_forum:
            for msg in await get_cached_messages(chat_id, int(requested_topic), limit=50):
                data.append({
                    "name": get_filename(msg),
                    "channel": channel_name,
                    "topic": int(requested_topic),
                    "msg": msg.id,
                    "url": f"/stream?channel={safe_channel}&msg={msg.id}",
                    "thumb": f"/thumb?channel={safe_channel}&msg={msg.id}",
                })
        else:
            async for topic_id, topic_title, msg in iter_media_messages(chat_id, is_forum, fixed_topics, limit=50):
                data.append({
                    "name": get_filename(msg),
                    "channel": channel_name,
                    "topic": topic_id,
                    "topic_title": topic_title,
                    "msg": msg.id,
                    "url": f"/stream?channel={safe_channel}&msg={msg.id}",
                    "thumb": f"/thumb?channel={safe_channel}&msg={msg.id}",
                })

    return web.json_response(data)


# =========================
# TMDB MATCHING (für Poster in der Xtream-Codes-API)
# =========================
YEAR_PATTERN = re.compile(r"\b(19\d{2}|20\d{2})\b")


def clean_filename(filename):
    """Gibt (bereinigter_titel, jahr_oder_None) zurück.
    Wichtig: die Jahreszahl wird HERAUSGELÖST statt im Suchtext zu bleiben -
    'Nightmare on Elm Street 3 -Freddy Krüger lebt -1987' als TMDB-Suchtext
    (inkl. "-1987") liefert bei Titeln mit Untertitel oft KEINEN Treffer.
    Getrennt als Jahresfilter übergeben trifft deutlich zuverlässiger."""
    name = os.path.splitext(filename)[0]
    patterns = [
        r'\b(1080p|720p|4k|2160p|480p|hdrip|web-dl|webrip|bluray|x264|x265|h264|h265|aac|dts)\b',
        r'\[.*?\]', r'\(.*?\)', r'\.'
    ]
    for p in patterns:
        name = re.sub(p, ' ', name, flags=re.IGNORECASE)

    year_match = YEAR_PATTERN.search(name)
    year = year_match.group(1) if year_match else None
    if year_match:
        name = name[:year_match.start()] + name[year_match.end():]

    name = name.strip(" -._")
    name = re.sub(r"\s+", " ", name)
    return name, year


async def fetch_tmdb_metadata(clean_title, year=None):
    if not TMDB_KEY or TMDB_SESSION is None or not clean_title:
        return None
    url = "https://api.themoviedb.org/3/search/movie"
    params = {"api_key": TMDB_KEY, "query": clean_title, "language": TMDB_LANG}
    if year:
        params["year"] = year
    try:
        async with TMDB_SESSION.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as res:
            if res.status == 200:
                data = await res.json()
                results = data.get("results", [])
                # Fallback: falls die Jahres-gefilterte Suche nichts findet,
                # nochmal ohne Jahr versuchen (Jahr in der Datenbank kann bei
                # internationalen Erst-/Zweitveröffentlichungen abweichen).
                if not results and year:
                    params.pop("year", None)
                    async with TMDB_SESSION.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as res2:
                        if res2.status == 200:
                            data = await res2.json()
                            results = data.get("results", [])
                if results:
                    movie = results[0]
                    poster = f"https://image.tmdb.org/t/p/w500{movie['poster_path']}" if movie.get("poster_path") else ""
                    release_date = movie.get("release_date", "")
                    movie_year = release_date.split("-")[0] if release_date else ""
                    return {
                        "title": movie.get("title"),
                        "overview": movie.get("overview", ""),
                        "poster": poster,
                        "year": movie_year,
                    }
    except Exception as e:
        log(f"[TMDB] Fehler bei '{clean_title}': {e}")
    return None


# (channel_name, msg_id) -> {vod_id, channel, msg_id, name, poster, overview, year}
# Bewusst NUR im Speicher (wie alle anderen Caches in dieser Datei) - nach
# einem Neustart des Service werden vod_id's neu vergeben. Das ist okay,
# weil TiviMate/IPTV Smarters die VOD-Liste bei jedem Öffnen neu abrufen.
_vod_meta_cache = {}
_vod_id_to_key = {}
_next_vod_id_counter = [1000]


def _next_vod_id():
    vod_id = _next_vod_id_counter[0]
    _next_vod_id_counter[0] += 1
    return vod_id


async def resolve_vod_item(channel_name, topic_title, msg):
    key = (channel_name, msg.id)
    if key in _vod_meta_cache:
        return _vod_meta_cache[key]

    raw_name = get_filename(msg)
    clean_name, filename_year = clean_filename(raw_name)
    tmdb_meta = await fetch_tmdb_metadata(clean_name, year=filename_year)

    display_name = tmdb_meta["title"] if tmdb_meta else clean_name
    if topic_title:
        display_name = f"[{topic_title}] {display_name}"
    if tmdb_meta and tmdb_meta.get("year"):
        display_name += f" ({tmdb_meta['year']})"

    vod_id = _next_vod_id()
    item = {
        "vod_id": vod_id,
        "channel": channel_name,
        "msg_id": msg.id,
        "name": display_name,
        "poster": (tmdb_meta or {}).get("poster") or "",
        "overview": (tmdb_meta or {}).get("overview") or (getattr(msg, "caption", None) or "Keine Beschreibung"),
        "year": (tmdb_meta or {}).get("year") or "",
    }
    _vod_meta_cache[key] = item
    _vod_id_to_key[vod_id] = key
    return item


def _find_vod_item(vod_id):
    try:
        vod_id = int(vod_id)
    except (TypeError, ValueError):
        return None
    key = _vod_id_to_key.get(vod_id)
    if key is None:
        return None
    return _vod_meta_cache.get(key)


# =========================
# XTREAM CODES API (für TiviMate / IPTV Smarters - großes Poster-Grid)
# =========================
# M3U wird von TiviMate & Co. als reine Live-TV-Kanalliste behandelt
# (kleine Sender-Logos). Ein echtes Film-Poster-Grid gibt es nur über dieses
# Protokoll - siehe playlist.m3u weiter unten, das bleibt als einfache
# Alternative für Player ohne Xtream-Unterstützung bestehen.
def _xtream_auth_ok(username, password):
    return username == XTREAM_USER and password == XTREAM_PASS


async def _xtream_params(request):
    """Liest Parameter aus GET-Query UND POST-Body.
    Manche IPTV-Player (TiviMate u.a.) senden den Xtream-Login als POST mit
    application/x-www-form-urlencoded statt als GET-Query-String - mit nur
    request.query bleiben username/password dann leer -> auth schlägt fehl,
    ohne dass am Server sichtbar etwas falsch wäre."""
    params = dict(request.query)
    if request.method == "POST":
        try:
            post_params = await request.post()
            for key, value in post_params.items():
                if not params.get(key):
                    params[key] = value
        except Exception:
            pass  # kein Form-Body vorhanden (z.B. leer oder JSON) - GET-Werte bleiben gültig
    return params


async def stalker_static_js_handler(request):
    """Leere, aber gültige (200 OK) Antwort für /c/*.js - manche Stalker-
    Clients laden diese "Web-UI"-Ressourcen der echten Ministra-Middleware
    nach und geben auf, wenn sie 404 bekommen. Wir bauen die Web-Oberfläche
    NICHT nach, sondern liefern nur ein leeres, aber valides JS zurück."""
    return web.Response(text="// noop", content_type="application/javascript")


async def player_api_handler(request):
    params = await _xtream_params(request)
    username = params.get("username", "")
    password = params.get("password", "")

    # Diagnose: JEDE Anfrage loggen (Methode + Parameter, Passwort maskiert),
    # damit sichtbar ist, ob ein Player überhaupt ankommt und was er schickt.
    safe_params = {k: ("***" if k == "password" else v) for k, v in params.items()}
    log(f"[Xtream] {request.method} player_api.php von {request.remote} - Parameter: {safe_params}")

    if not _xtream_auth_ok(username, password):
        # server_info MUSS auch bei falschem Login mitgeschickt werden -
        # manche Player (TiviMate) erwarten das Feld unbedingt und zeigen
        # sonst eine unklare "Fehler bei der Verarbeitung"-Meldung statt
        # einer klaren Login-Fehlermeldung.
        host_only = (request.host or "127.0.0.1").split(":")[0]
        return web.json_response({
            "user_info": {"auth": 0, "status": "Disabled"},
            "server_info": {
                "url": host_only,
                "port": str(PORT),
                "https_port": "0",
                "server_protocol": "http",
                "rtmp_port": "0",
                "timezone": "Europe/Vienna",
                "timestamp_now": int(time.time()),
                "time_now": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        })

    action = params.get("action", "")

    if action == "":
        # server_info.url MUSS eine echte, erreichbare Adresse sein - viele
        # Player (TiviMate, Sparkle) bauen daraus die Stream-URLs. Wir leiten
        # sie aus dem tatsächlichen Request ab (Host-Header), statt eine
        # feste Adresse zu raten - funktioniert dadurch unabhängig davon,
        # unter welcher IP das Gerät gerade erreichbar ist.
        host_header = request.host or "127.0.0.1"
        host_only = host_header.split(":")[0]

        return web.json_response({
            "user_info": {
                "username": XTREAM_USER,
                "password": XTREAM_PASS,
                "message": "",
                "auth": 1,
                "status": "Active",
                "exp_date": "4102444800",  # Jahr 2100 - manche Player kommen mit null/None nicht klar
                "is_trial": "0",
                "active_cons": "0",
                "created_at": "1700000000",
                "max_connections": "1",
                "allowed_output_formats": ["m3u8", "ts", "mp4"],
            },
            "server_info": {
                "url": host_only,
                "port": str(PORT),
                "https_port": "0",
                "server_protocol": "http",
                "rtmp_port": "0",
                "timezone": "Europe/Vienna",
                "timestamp_now": int(time.time()),
                "time_now": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        })

    if action in ("get_live_categories", "get_live_streams", "get_series_categories", "get_series"):
        return web.json_response([])

    if action == "get_vod_categories":
        cats = [
            {"category_id": str(idx + 1), "category_name": name, "parent_id": 0}
            for idx, name in enumerate(CHANNEL_CHAT_ID.keys())
        ]
        return web.json_response(cats)

    if action == "get_vod_streams":
        requested_cat = params.get("category_id")
        result = []
        for idx, channel_name in enumerate(CHANNEL_CHAT_ID.keys(), start=1):
            if requested_cat and str(idx) != requested_cat:
                continue
            chat_id = CHANNEL_CHAT_ID[channel_name]
            is_forum = CHANNEL_IS_FORUM.get(channel_name, False)
            fixed_topics = CHANNEL_FIXED_TOPICS.get(channel_name)
            async for topic_id, topic_title, msg in iter_media_messages(chat_id, is_forum, fixed_topics, limit=50):
                item = await resolve_vod_item(channel_name, topic_title, msg)
                result.append({
                    "num": item["vod_id"],
                    "name": item["name"],
                    "stream_type": "movie",
                    "stream_id": item["vod_id"],
                    "stream_icon": item["poster"],
                    "rating": "0",
                    "rating_5based": 0,
                    "added": str(int(time.time())),
                    "category_id": str(idx),
                    "container_extension": "mp4",
                })
        return web.json_response(result)

    if action == "get_vod_info":
        item = _find_vod_item(params.get("vod_id"))
        if not item:
            return web.json_response({}, status=404)
        return web.json_response({
            "info": {
                "movie_image": item["poster"],
                "plot": item["overview"],
                "name": item["name"],
                "releasedate": item["year"],
            },
            "movie_data": {
                "stream_id": item["vod_id"],
                "container_extension": "mp4",
            }
        })

    return web.json_response({"error": "unknown action"}, status=400)


async def panel_api_handler(request):
    return await player_api_handler(request)


async def movie_stream_handler(request):
    """Xtream-Codes-VOD-Streaming-Route: /movie/{user}/{pass}/{stream_id}.mp4
    Nutzt intern denselben _stream_core wie /stream - Head/Tail-Cache,
    moov-Fix und Flood-Wait-Schutz gelten hier genauso."""
    username = request.match_info.get("username", "")
    password = request.match_info.get("password", "")
    if not _xtream_auth_ok(username, password):
        return web.Response(status=401, text="unauthorized")

    item = _find_vod_item(request.match_info.get("stream_id"))
    if not item:
        return web.Response(status=404, text="not found")

    return await _stream_core(request, item["channel"], item["msg_id"])


# =========================
# STALKER PORTAL (MAG-Emulatoren wie StbEmu)
# =========================
# Komplett anderes Protokoll als Xtream: keine Username/Passwort-Anmeldung,
# sondern eine MAC-Adresse (kommt als Cookie "mac" mit) + ein Token, der
# einmalig per "handshake" geholt und danach bei JEDER weiteren Anfrage im
# Authorization-Header ("Bearer <token>") mitgeschickt werden muss.
# Antworten sind immer in {"js": ...} eingepackt (Stalker-Konvention).
import secrets

_stalker_tokens = {}  # token -> mac (rein im Speicher, wie alle anderen Caches hier)


def _stalker_issue_token(mac):
    token = secrets.token_hex(16)
    _stalker_tokens[token] = mac
    return token


def _stalker_token_ok(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] in _stalker_tokens
    return False


def _stalker_mac(request):
    return request.cookies.get("mac", "unknown")


def _extract_vod_id_from_cmd(cmd):
    """Manche Stalker-Clients (StbEmu u.a.) senden bei create_link NICHT das
    von uns vorgegebene 'vod_id:123'-Format, sondern bauen sich selbst einen
    Pfad wie '/media/file_123.mpg' zusammen. Beide Formen werden erkannt."""
    if cmd.startswith("vod_id:"):
        return cmd.split(":", 1)[1]
    match = re.search(r"(\d+)(?:\.\w+)?$", cmd)
    if match:
        return match.group(1)
    return cmd


async def stalker_portal_handler(request):
    action = request.query.get("action", "")
    req_type = request.query.get("type", "")

    # Diagnose: JEDE Stalker-Anfrage loggen (Methode, type, action, alle
    # Query-Parameter) - vorher wurde nur der Handshake geloggt, dadurch war
    # nicht sichtbar, was beim Klick auf eine Kategorie genau ankommt.
    log(f"[Stalker] {request.method} type={req_type!r} action={action!r} "
        f"Query={dict(request.query)}")

    if action == "handshake":
        mac = _stalker_mac(request)
        token = _stalker_issue_token(mac)
        log(f"[Stalker] handshake von MAC={mac}")
        return web.json_response({"js": {"token": token, "random": secrets.token_hex(8)}})

    # Ab hier: gültiger Token per Authorization-Header Pflicht
    if not _stalker_token_ok(request):
        log(f"[Stalker] Token fehlt/ungültig für action={action!r} - "
            f"Authorization-Header war: {request.headers.get('Authorization', '(keiner)')!r}")
        return web.json_response({"js": {}}, status=403)

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

    if req_type == "vod" and action == "get_categories":
        cats = [
            {"id": str(idx + 1), "title": name, "alias": name}
            for idx, name in enumerate(CHANNEL_CHAT_ID.keys())
        ]
        return web.json_response({"js": cats})

    if req_type == "vod" and action == "get_ordered_list":
        # StbEmu fragt Einzelfilme gezielt per movie_id ab (z.B. für die
        # Detailansicht) - dafür NICHT die komplette Bibliothek neu
        # durchsuchen (das dauerte beim echten Test 7 Sekunden), sondern
        # direkt aus dem schon gefüllten Cache beantworten.
        movie_id_param = request.query.get("movie_id")
        if movie_id_param:
            item = _find_vod_item(movie_id_param)
            if not item:
                return web.json_response({"js": {"total_items": 0, "max_page_items": 1, "selected_item": 0, "data": []}})
            data_item = {
                "id": str(item["vod_id"]),
                "name": item["name"],
                "o_name": item["name"],
                "screenshot_uri": item["poster"],
                "year": item.get("year", ""),
                "description": item.get("overview", ""),
                "cmd": f"vod_id:{item['vod_id']}",
            }
            return web.json_response({"js": {
                "total_items": 1, "max_page_items": 1, "selected_item": 0, "data": [data_item],
            }})

        requested_cat = request.query.get("category")
        page = int(request.query.get("p") or 1)
        page_size = 14  # typischer Stalker-Client-Default

        all_items = []
        for idx, channel_name in enumerate(CHANNEL_CHAT_ID.keys(), start=1):
            if requested_cat and requested_cat != "*" and str(idx) != str(requested_cat):
                continue
            chat_id = CHANNEL_CHAT_ID[channel_name]
            is_forum = CHANNEL_IS_FORUM.get(channel_name, False)
            fixed_topics = CHANNEL_FIXED_TOPICS.get(channel_name)
            async for topic_id, topic_title, msg in iter_media_messages(chat_id, is_forum, fixed_topics, limit=50):
                item = await resolve_vod_item(channel_name, topic_title, msg)
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
        vod_id = _extract_vod_id_from_cmd(cmd)
        item = _find_vod_item(vod_id)
        if not item:
            return web.json_response({"js": {}}, status=404)
        base = f"{request.scheme}://{request.host}"
        stream_url = f"{base}/movie/{XTREAM_USER}/{XTREAM_PASS}/{item['vod_id']}.mp4"
        # Stalker-Clients erwarten den fertigen Play-Befehl im "cmd"-Feld
        return web.json_response({"js": {"cmd": stream_url, "id": item["vod_id"]}})

    if req_type == "watchdog":
        # Playback-Metriken (Bufferings, Fehler etc.) - wir werten das nicht
        # aus, aber der Client soll keine 400er dafür bekommen.
        return web.json_response({"js": {}})

    if req_type in ("itv", "epg") or action in ("get_all_channels", "get_genres"):
        # Live-TV bieten wir (noch) nicht an
        return web.json_response({"js": {"data": []}})

    return web.json_response({"js": {}}, status=400)


# =========================
# M3U PLAYLIST (für TiviMate & andere IPTV-Player)
# =========================
async def m3u_handler(request):
    base = f"{request.scheme}://{request.host}"
    lines = ["#EXTM3U"]

    for channel_name, chat_id in CHANNEL_CHAT_ID.items():
        is_forum = CHANNEL_IS_FORUM.get(channel_name, False)
        fixed_topics = CHANNEL_FIXED_TOPICS.get(channel_name)
        async for topic_id, topic_title, msg in iter_media_messages(chat_id, is_forum, fixed_topics, limit=50):
            name = get_filename(msg)
            if topic_title:
                name = f"[{topic_title}] {name}"
            safe_channel = urllib.parse.quote(channel_name, safe="")
            stream_url = f"{base}/stream/{safe_channel}/{msg.id}.mp4"
            lines.append(
                f'#EXTINF:-1 tvg-name="{name}" tvg-type="movie" '
                f'group-title="VOD - {channel_name}",{name}'
            )
            lines.append(stream_url)

    body = "\n".join(lines) + "\n"
    return web.Response(
        text=body,
        content_type="application/x-mpegurl",
        headers={"Access-Control-Allow-Origin": "*"},
    )


# =========================
# START
# =========================
async def main():
    global TMDB_SESSION

    log("Starte Pyrogram-Client ...")
    await client.start()
    log("✓ Pyrogram-Client gestartet.")

    TMDB_SESSION = aiohttp.ClientSession()

    await warmup_peer_cache()
    await ensure_chats_resolved()

    log("Baue aiohttp-Server auf ...")

    @web.middleware
    async def not_found_logger(request, handler):
        try:
            return await handler(request)
        except web.HTTPNotFound:
            log(f"[404] Unbekannter Pfad: {request.method} {request.path} "
                f"- Query: {dict(request.query)} - Headers: {dict(request.headers)}")
            raise

    app = web.Application(middlewares=[not_found_logger])
    app.router.add_get("/", browse_handler)
    app.router.add_get("/stream", stream_handler, allow_head=False)
    app.router.add_head("/stream", stream_handler)
    app.router.add_get("/stream/{channel}/{msg}.mp4", stream_handler_path, allow_head=False)
    app.router.add_head("/stream/{channel}/{msg}.mp4", stream_handler_path)
    app.router.add_get("/thumb", thumb_handler)
    app.router.add_get("/topics", topics_handler)
    app.router.add_get("/list.json", json_handler)
    app.router.add_get("/playlist.m3u", m3u_handler)
    # add_route("*", ...) statt add_get: manche Player (TiviMate) senden den
    # Xtream-Login als POST statt GET.
    app.router.add_route("*", "/player_api.php", player_api_handler)
    app.router.add_route("*", "/panel_api.php", panel_api_handler)

    # Stalker Portal: verschiedene Clients (StbEmu u.a.) probieren
    # unterschiedliche Standard-Pfade - beide auf denselben Handler legen.
    app.router.add_route("*", "/portal.php", stalker_portal_handler)
    app.router.add_route("*", "/stalker_portal/server/load.php", stalker_portal_handler)
    app.router.add_route("*", "/c/portal.php", stalker_portal_handler)
    app.router.add_route("*", "/server/load.php", stalker_portal_handler)

    # Manche Stalker-Clients (z.B. Sparkle Player) laden vor dem eigentlichen
    # API-Handshake noch "Web-UI"-Ressourcen der echten Ministra-Middleware
    # nach und blockieren, wenn die 404 liefern. Wir bauen NICHT die ganze
    # Web-Oberfläche nach - eine leere, aber gültige (200 OK) JS-Antwort
    # reicht oft schon aus, damit der Client trotzdem weitermacht.
    app.router.add_get("/c/{filename:.+\\.js}", stalker_static_js_handler)
    app.router.add_get("/movie/{username}/{password}/{stream_id}.mp4", movie_stream_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    log("✓ aiohttp-Runner bereit, starte TCP-Site ...")

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log(f"Server läuft auf Port {PORT}")
    log(f"Xtream Codes API (TiviMate/IPTV Smarters): Username={XTREAM_USER}")
    try:
        await asyncio.Event().wait()
    finally:
        await TMDB_SESSION.close()


def start_blocking(files_dir, api_id, api_hash, session_string, channels_json,
                    tmdb_key="", tmdb_lang="de-DE", xtream_user="admin", xtream_pass="admin"):
    """
    Einstiegspunkt für Kotlin/Chaquopy.
    channels_json Beispiel:
      {"Filme": -1001188033420, "Kiosk": [-1002187259012, [92956, 11154]]}

    tmdb_key/xtream_user/xtream_pass sind optional (Default: kein TMDB,
    admin/admin) - eine ALTE config.json ohne diese Felder funktioniert
    also unverändert weiter, nur eben ohne Poster.
    """
    global client, CHANNEL_CHAT_ID, CHANNEL_FIXED_TOPICS, TMDB_KEY, TMDB_LANG, XTREAM_USER, XTREAM_PASS

    CHANNEL_CHAT_ID, CHANNEL_FIXED_TOPICS = parse_channels_config(channels_json)
    TMDB_KEY = tmdb_key or ""
    TMDB_LANG = tmdb_lang or "de-DE"
    XTREAM_USER = xtream_user or "admin"
    XTREAM_PASS = xtream_pass or "admin"

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client = Client(
        name="pyrogram_session",
        api_id=int(api_id),
        api_hash=api_hash,
        session_string=session_string,
        workdir=files_dir,
        max_concurrent_transmissions=6,  # Standard ist 1 -> blockiert parallele Range-Requests
    )

    loop.run_until_complete(main())