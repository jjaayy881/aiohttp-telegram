"""
generate_config.py
===================
Erzeugt die config.json für die Android-App (Kurigram/Chaquopy-Variante),
OHNE dass man den Session-String oder Topic-IDs von Hand heraussuchen muss.

Läuft NICHT auf dem Android-Gerät, sondern einmalig am PC (oder in Termux/
Pydroid3) - danach die fertige config.json per

    adb push config.json /sdcard/Android/data/com.example.tgserver/files/config.json

auf das Gerät kopieren (Pfad steht auch in der App selbst, siehe MainActivity).

Voraussetzung:  pip install kurigram tgcrypto
(NICHT "pyrogram" installieren - das offizielle Paket kennt keine
Forum-Topics, die App braucht explizit den Kurigram-Fork, siehe
app/build.gradle -> pip { install "kurigram" }.)
"""
import asyncio
import json
import os
import sys

from pyrogram import Client
from pyrogram.errors import RPCError

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _prompt(text, default=None):
    suffix = f" [{default}]" if default else ""
    val = input(f"{text}{suffix}: ").strip()
    return val if val else (default or "")


def _parse_selection(text, max_len):
    text = text.strip().lower()
    if text in ("alle", "all", "*"):
        return list(range(1, max_len + 1))
    return [int(x.strip()) for x in text.split(",") if x.strip().isdigit()]


async def run():
    print("=" * 55)
    print(" Streamcenter (Android/Chaquopy) - config.json erzeugen")
    print("=" * 55)
    print("API-ID und API-Hash: https://my.telegram.org/apps\n")

    api_id = int(_prompt("API ID"))
    api_hash = _prompt("API Hash")

    # in_memory=True: es wird KEINE .session-Datei angelegt, nur der
    # session_string am Ende gebraucht - genau das, was ConfigStore.kt/
    # ServerService.kt erwarten (die App startet ihre eigene Client-Instanz
    # aus diesem String, nicht aus einer Session-Datei).
    async with Client(
        "config_generator",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
    ) as app:
        print("\nLogin erfolgreich.\n")
        session_string = await app.export_session_string()

        print("Lade deine Kanäle/Gruppen...")
        candidates = []
        async for dialog in app.get_dialogs():
            chat = dialog.chat
            if chat.type.name in ("CHANNEL", "SUPERGROUP", "GROUP"):
                candidates.append(chat)

        if not candidates:
            print("Keine Kanäle/Gruppen gefunden. Abbruch.")
            sys.exit(1)

        print("\nGefundene Kanäle/Gruppen:")
        for i, c in enumerate(candidates, start=1):
            print(f"  {i}. {c.title}")

        selection = _prompt("\nWelche sollen genutzt werden? (Nummern kommagetrennt, z.B. 1,3)")
        indices = [int(x.strip()) for x in selection.split(",") if x.strip().isdigit()]

        channels = {}
        for idx in indices:
            if not (1 <= idx <= len(candidates)):
                continue
            chat = candidates[idx - 1]
            display_name = _prompt(f"Anzeigename für '{chat.title}'", default=chat.title)

            is_forum = bool(getattr(chat, "is_forum", False))
            topics = []
            if is_forum:
                print(f"  '{chat.title}' nutzt Themen (Topics):")
                try:
                    async for t in app.get_forum_topics(chat.id):
                        topics.append(t)
                except RPCError as e:
                    print(f"    [Warnung] Themen konnten nicht geladen werden ({e}), nutze den ganzen Kanal.")
                    topics = []

            if not topics:
                channels[display_name] = chat.id
                continue

            for i, t in enumerate(topics, start=1):
                print(f"    {i}. {t.title}")
            topic_selection = _prompt(
                "    Welche Themen sollen genutzt werden? (Nummern kommagetrennt, oder 'alle')",
                default="alle",
            )
            topic_indices = _parse_selection(topic_selection, len(topics))

            # Titel werden GLEICH mit in die config.json geschrieben
            # ({"id": "Titel"}) - dadurch muss die App beim Start nicht noch
            # extra bei Telegram nachfragen, was schneller startet
            # (siehe Kommentar in telegram_server.py/parse_channels_config).
            chosen_topics = {}
            for t_idx in topic_indices:
                if not (1 <= t_idx <= len(topics)):
                    continue
                t = topics[t_idx - 1]
                chosen_topics[str(t.id)] = t.title
                print(f"    -> '{t.title}' hinzugefügt")

            if chosen_topics:
                channels[display_name] = [chat.id, chosen_topics]

    config = {
        "api_id": str(api_id),
        "api_hash": api_hash,
        "session_string": session_string,
        "channels": channels,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"\nconfig.json wurde erstellt: {OUTPUT_FILE}")
    print("\nAuf das Gerät kopieren mit (im platform-tools-Ordner, USB-Debugging an):")
    print('  adb push config.json /sdcard/Android/data/com.example.tgserver/files/config.json')
    print("\n(Zielpfad steht auch in der App selbst, MainActivity zeigt ihn direkt an.)")


if __name__ == "__main__":
    asyncio.run(run())
