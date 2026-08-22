package com.example.tgserver

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper

class BootReceiver : BroadcastReceiver() {

    // Paketname von Kodi auf Fire OS. Mit "adb shell pm list packages | grep kodi"
    // prüfen, falls es abweicht (z.B. bei Kodi-Nightlies).
    private val kodiPackage = "org.xbmc.kodi"

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == "android.intent.action.QUICKBOOT_POWERON"
        ) {
            // 1. Server-Service starten
            val serviceIntent = Intent(context, ServerService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent)
            } else {
                context.startService(serviceIntent)
            }

            // 2. Kodi mit kurzer Verzögerung starten, damit der Server
            //    schon steht, wenn das Addon beim Kodi-Start Daten abruft
            Handler(Looper.getMainLooper()).postDelayed({
                val kodiIntent = context.packageManager.getLaunchIntentForPackage(kodiPackage)
                if (kodiIntent != null) {
                    kodiIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    context.startActivity(kodiIntent)
                }
            }, 5000)
        }
    }
}
