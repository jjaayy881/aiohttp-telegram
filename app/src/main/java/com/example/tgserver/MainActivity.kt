package com.example.tgserver

import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {

    private lateinit var statusText: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Ruft getExternalFilesDir(null) auf und legt damit bei Bedarf den
        // Zielordner für config.json an, BEVOR der Service danach sucht.
        val configFile = ConfigStore.configFile(this)

        startServer()
        setContentView(buildUi(configFile))
        refreshStatus()
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    private fun buildUi(configFile: java.io.File): ScrollView {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }

        root.addView(TextView(this).apply {
            text = "TG Filme Server"
            textSize = 22f
            setPadding(0, 0, 0, 16)
        })

        root.addView(TextView(this).apply {
            text = "Die Zugangsdaten werden NICHT hier eingetippt. Stattdessen " +
                    "einmal \"generate_config.py\" am PC/Termux laufen lassen " +
                    "(fragt API-ID/Hash ab, loggt sich ein, lässt dich Kanäle " +
                    "und Themen auswählen und schreibt die fertige " +
                    "\"${ConfigStore.CONFIG_FILE_NAME}\" automatisch) - die " +
                    "Datei dann per USB-Kabel auf das Gerät kopieren (adb push)."
            setPadding(0, 0, 0, 24)
        })

        root.addView(TextView(this).apply {
            text = "Zielpfad auf dem Gerät:"
            textSize = 16f
        })
        root.addView(TextView(this).apply {
            text = configFile.absolutePath
            setPadding(0, 4, 0, 24)
            setTextIsSelectable(true)
        })

        root.addView(TextView(this).apply {
            text = "Befehle am PC:"
            textSize = 16f
        })
        root.addView(TextView(this).apply {
            text = "python generate_config.py\n" +
                    "adb push config.json \"${configFile.absolutePath}\""
            setPadding(0, 4, 0, 24)
            setTextIsSelectable(true)
        })

        root.addView(TextView(this).apply {
            text = "Xtream Codes API (für TiviMate / IPTV Smarters, mit " +
                    "großen Postern statt kleiner Sender-Logos):\n" +
                    "Server:   http://<GERÄTE-IP>:8585\n" +
                    "Username/Passwort: wie in config.json (\"xtream_user\"/" +
                    "\"xtream_pass\", Standard admin/admin falls nicht gesetzt).\n\n" +
                    "playlist.m3u (einfache Alternative, kleine Logos statt " +
                    "großer Poster) bleibt weiterhin unter " +
                    "http://<GERÄTE-IP>:8585/playlist.m3u erreichbar."
            setPadding(0, 4, 0, 24)
            setTextIsSelectable(true)
        })

        root.addView(TextView(this).apply {
            text = "Erwarteter Inhalt von config.json:"
            textSize = 16f
        })
        root.addView(TextView(this).apply {
            text = "{\n" +
                    "  \"api_id\": \"12345678\",\n" +
                    "  \"api_hash\": \"0123456789abcdef0123456789abcdef\",\n" +
                    "  \"session_string\": \"BAFKpMIA...\",\n" +
                    "  \"channels\": {\n" +
                    "    \"Filme\": -1009999999999,\n" +
                    "    \"Kiosk\": [-1002187259012, {\"92956\": \"Film1\", \"11154\": \"Film2\"}]\n" +
                    "  }\n" +
                    "}\n\n" +
                    "Kanal-Format: einfache Zahl = normaler Kanal.\n" +
                    "[chat_id, topic_id] oder [chat_id, [id1, id2]] = " +
                    "Forum-Kanal, Titel wird von Telegram nachgeladen " +
                    "(langsamer bei Kanälen mit vielen Topics).\n" +
                    "[chat_id, {\"id1\": \"Titel1\", \"id2\": \"Titel2\"}] = " +
                    "Forum-Kanal MIT Titel direkt angegeben -> schnellster " +
                    "Start, kein zusätzlicher Telegram-Aufruf nötig."
            setPadding(0, 4, 0, 24)
            setTextIsSelectable(true)
        })

        statusText = TextView(this).apply {
            textSize = 16f
            setPadding(0, 8, 0, 24)
        }
        root.addView(statusText)

        val reloadButton = Button(this).apply {
            text = "Neu laden"
            setOnClickListener {
                startServer()
                refreshStatus()
            }
        }
        root.addView(reloadButton)

        root.addView(TextView(this).apply {
            text = "\nHinweis: Wenn der Server schon mit einer ANDEREN " +
                    "config.json lief und du die Datei geändert hast, reicht " +
                    "\"Neu laden\" hier nicht - die App einmal komplett per " +
                    "\"Beenden erzwingen\" (Android-Einstellungen -> Apps) " +
                    "stoppen und neu öffnen, damit die neuen Werte sicher " +
                    "greifen."
            textSize = 12f
        })

        val scroll = ScrollView(this)
        scroll.addView(root)
        return scroll
    }

    private fun refreshStatus() {
        statusText.text = when (val result = ConfigStore.load(this)) {
            is ConfigStore.LoadResult.Success -> {
                "✅ config.json gefunden und gültig.\n" +
                        "API_ID: ${result.config.apiId}\n" +
                        "Kanäle: ${result.config.channelsJson}\n" +
                        "TMDB-Key gesetzt: ${result.config.tmdbKey.isNotBlank()}\n" +
                        "Xtream-Login: ${result.config.xtreamUser} / ${result.config.xtreamPass}\n\n" +
                        "Server sollte laufen auf Port 8585."
            }
            is ConfigStore.LoadResult.Error -> {
                "❌ ${result.message}"
            }
        }
    }

    private fun startServer() {
        val serviceIntent = Intent(this, ServerService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }
    }
}
