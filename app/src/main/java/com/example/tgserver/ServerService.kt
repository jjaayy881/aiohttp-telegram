package com.example.tgserver

import android.app.*
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.chaquo.python.PyException
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class ServerService : Service() {

    private val channelId = "tg_server_channel"
    private var pythonThread: Thread? = null

    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val result = ConfigStore.load(this)

        val notification = NotificationCompat.Builder(this, channelId)
            .setContentTitle("TG Filme Server")
            .setContentText(
                when (result) {
                    is ConfigStore.LoadResult.Success -> "Läuft auf Port 8585"
                    is ConfigStore.LoadResult.Error -> "config.json fehlt/ungültig – siehe App"
                }
            )
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .build()

        startForeground(1, notification)

        val config = when (result) {
            is ConfigStore.LoadResult.Error -> {
                // Kein Python-Start ohne gültige config.json - würde sonst crashen.
                return START_STICKY
            }
            is ConfigStore.LoadResult.Success -> result.config
        }

        if (pythonThread == null || pythonThread?.isAlive == false) {
            pythonThread = Thread {
                try {
                    val py = Python.getInstance()
                    val module = py.getModule("telegram_server")
                    // Blockierender Aufruf: startet asyncio.run(main()) auf diesem Thread.
                    // filesDir.absolutePath ist der einzige Ort, an dem die App
                    // garantiert Schreibrechte hat (z.B. /data/data/com.example.tgserver/files)
                    module.callAttr(
                        "start_blocking",
                        filesDir.absolutePath,
                        config.apiId,
                        config.apiHash,
                        config.sessionString,
                        config.channelsJson,
                        config.tmdbKey,
                        "de-DE",
                        config.xtreamUser,
                        config.xtreamPass
                    )
                } catch (e: PyException) {
                    // Hier ggf. Logging/Neustart-Logik ergänzen
                    e.printStackTrace()
                }
            }
            pythonThread?.isDaemon = true
            pythonThread?.start()
        }
        // Hinweis: Läuft der Thread bereits (z.B. nach Änderung der
        // config.json), wird er hier NICHT automatisch neu gestartet - das
        // würde mitten in einer laufenden asyncio-Schleife aus einem anderen
        // Thread heraus zu Inkonsistenzen führen. Nach dem Ändern der
        // config.json muss die App einmal per "Beenden erzwingen"
        // (Android-Einstellungen -> Apps) komplett gestoppt und neu geöffnet
        // werden.

        // START_STICKY: Android versucht den Service nach einem Kill neu zu starten
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                channelId,
                "TG Server",
                NotificationManager.IMPORTANCE_LOW
            )
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }
}
