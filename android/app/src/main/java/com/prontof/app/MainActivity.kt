package com.prontof.app

import android.app.Activity
import android.app.AlertDialog
import android.os.Bundle
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.view.Gravity
import android.view.Menu
import android.view.MenuItem
import android.view.ViewGroup
import android.graphics.Color
import android.content.Context

class MainActivity : Activity() {
    private lateinit var webView: WebView
    private val prefs by lazy { getSharedPreferences("prontof", Context.MODE_PRIVATE) }
    private val defaultUrl = "http://100.87.176.13:30810/"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        webView = WebView(this)
        webView.layoutParams = ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT
        )
        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.allowFileAccess = false
        webView.webChromeClient = WebChromeClient()
        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedError(view: WebView?, request: android.webkit.WebResourceRequest?, error: android.webkit.WebResourceError?) {
                super.onReceivedError(view, request, error)
            }
        }
        setContentView(webView)
        val saved = prefs.getString("server_url", null)
        if (saved.isNullOrBlank()) showServerDialog() else loadServer(saved)
    }

    private fun normalizeUrl(value: String): String {
        var url = value.trim()
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "http://$url"
        return url.trimEnd('/') + "/"
    }

    private fun loadServer(value: String) {
        val url = normalizeUrl(value)
        prefs.edit().putString("server_url", url).apply()
        webView.loadUrl(url)
    }

    private fun showServerDialog() {
        val input = EditText(this)
        input.setSingleLine(true)
        input.hint = "http://IP-адрес-NAS:30810/"
        input.setText(prefs.getString("server_url", defaultUrl))
        val layout = LinearLayout(this)
        layout.orientation = LinearLayout.VERTICAL
        layout.setPadding(48, 12, 48, 0)
        val help = TextView(this)
        help.text = "Введите адрес ProntoF. Телефон должен иметь доступ к NAS по локальной сети или Tailscale."
        help.setTextColor(Color.DKGRAY)
        layout.addView(help)
        layout.addView(input)
        AlertDialog.Builder(this)
            .setTitle("Подключение к ProntoF")
            .setView(layout)
            .setCancelable(false)
            .setPositiveButton("Подключить") { _, _ ->
                val value = input.text.toString().trim()
                if (value.isNotEmpty()) loadServer(value) else showServerDialog()
            }
            .setNegativeButton("Позже") { _, _ -> finish() }
            .show()
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menu.add("Адрес сервера").setShowAsAction(MenuItem.SHOW_AS_ACTION_NEVER)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (item.title == "Адрес сервера") {
            val input = EditText(this)
            input.setSingleLine(true)
            input.setText(prefs.getString("server_url", defaultUrl))
            AlertDialog.Builder(this)
                .setTitle("Адрес ProntoF")
                .setView(input)
                .setNegativeButton("Отмена", null)
                .setPositiveButton("Сохранить") { _, _ -> loadServer(input.text.toString()) }
                .show()
            return true
        }
        return super.onOptionsItemSelected(item)
    }

    @Deprecated("Deprecated in Android")
    override fun onBackPressed() {
        if (::webView.isInitialized && webView.canGoBack()) webView.goBack()
        else super.onBackPressed()
    }
}
