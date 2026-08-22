package com.example.tgserver

import android.content.Context
import org.json.JSONObject
import java.io.File

/**
 * Liest die Zugangsdaten (API_ID, API_HASH, SESSION_STRING, Kanäle) aus einer
 * config.json im app-eigenen externen Speicherordner, statt sie über die
 * Bildschirmtastatur einzutippen. Der Ordner ist ohne Runtime-Berechtigungen
 * erreichbar und lässt sich bequem per "adb push" vom PC aus befüllen:
 *
 *   adb push config.json /sdcard/Android/data/com.example.tgserver/files/config.json
 *
 * Erwartetes Format der config.json:
 * {
 *   "api_id": "12345678",
 *   "api_hash": "0123456789abcdef0123456789abcdef",
 *   "session_string": "BAFKpMIA...",
 *   "channels": { "Filme": -1009999999999 },
 *   "tmdb_key": "optional, für Poster",
 *   "xtream_user": "optional, Default 'admin'",
 *   "xtream_pass": "optional, Default 'admin'"
 * }
 *
 * Am einfachsten NICHT von Hand erstellen, sondern mit generate_config.py
 * (im Projekt-Root) am PC erzeugen lassen - das Skript loggt sich per
 * Kurigram/Pyrogram ein, erzeugt session_string automatisch und lässt
 * Kanäle/Themen interaktiv auswählen.
 */
object ConfigStore {
    const val CONFIG_FILE_NAME = "config.json"

    data class ServerConfig(
        val apiId: String,
        val apiHash: String,
        val sessionString: String,
        // Als fertiger JSON-String, wird 1:1 an Python (start_blocking) weitergereicht.
        val channelsJson: String,
        val tmdbKey: String,
        val xtreamUser: String,
        val xtreamPass: String
    )

    sealed class LoadResult {
        data class Success(val config: ServerConfig) : LoadResult()
        data class Error(val message: String) : LoadResult()
    }

    /**
     * Pfad zur config.json. context.getExternalFilesDir(null) legt den
     * Ordner bei Bedarf automatisch an (z.B. beim ersten App-Start), sodass
     * "adb push" danach nicht mit "No such file or directory" fehlschlägt.
     */
    fun configFile(context: Context): File {
        val dir = context.getExternalFilesDir(null)
        return File(dir, CONFIG_FILE_NAME)
    }

    fun load(context: Context): LoadResult {
        val file = configFile(context)

        if (!file.exists()) {
            return LoadResult.Error("Datei nicht gefunden:\n${file.absolutePath}")
        }

        return try {
            val json = JSONObject(file.readText())

            val apiId = json.optString("api_id", "")
            val apiHash = json.optString("api_hash", "")
            val sessionString = json.optString("session_string", "")
            val channels = json.optJSONObject("channels")
            // Optional, mit Defaults - eine ALTE config.json ohne diese
            // Felder bleibt dadurch gültig (nur eben ohne Poster / mit
            // Standard-Zugangsdaten admin/admin für die Xtream-API).
            val tmdbKey = json.optString("tmdb_key", "")
            val xtreamUser = json.optString("xtream_user", "admin")
            val xtreamPass = json.optString("xtream_pass", "admin")

            if (apiId.isBlank() || apiHash.isBlank() || sessionString.isBlank() ||
                channels == null || channels.length() == 0
            ) {
                return LoadResult.Error(
                    "config.json ist unvollständig.\n" +
                            "api_id, api_hash, session_string und channels prüfen."
                )
            }

            LoadResult.Success(
                ServerConfig(
                    apiId = apiId,
                    apiHash = apiHash,
                    sessionString = sessionString,
                    channelsJson = channels.toString(),
                    tmdbKey = tmdbKey,
                    xtreamUser = xtreamUser,
                    xtreamPass = xtreamPass
                )
            )
        } catch (e: Exception) {
            LoadResult.Error("config.json ist kein gültiges JSON:\n${e.message}")
        }
    }
}
