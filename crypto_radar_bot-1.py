import os
import time
import json
import logging
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template_string

# Crypto Radar — read-only scanner / paper research only.
# No exchange API keys, no order placement.

BINANCE_BASE = os.getenv("BINANCE_BASE", "https://api.binance.com")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "180"))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME_USDT", "500000"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS_PER_SCAN", "120"))
ALERT_SCORE = int(os.getenv("ALERT_SCORE", "65"))
KLINE_INTERVAL = os.getenv("KLINE_INTERVAL", "5m")
KLINE_LIMIT = int(os.getenv("KLINE_LIMIT", "50"))
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "5000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("crypto-radar")
session = requests.Session()
latest_results = []
last_alerts = {}
last_scan_at = None
scan_error = None

app = Flask(__name__)

HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Radar</title>
<style>
body{font-family:Arial,sans-serif;background:#10141c;color:#eef2f8;margin:0;padding:18px}
.wrap{max-width:1100px;margin:auto}
h1{margin-bottom:5px}.muted{color:#aab5c5;font-size:14px}
.card{background:#1a2230;border:1px solid #2d394c;border-radius:12px;padding:14px;margin:12px 0}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:right;padding:10px;border-bottom:1px solid #2d394c}
th{color:#aab5c5}.score{font-weight:bold}
.up{color:#6ee7a8}.down{color:#ff8b8b}.neutral{color:#ffd479}
.pill{display:inline-block;padding:4px 8px;border-radius:8px;background:#293548}
small{color:#aab5c5}
</style>
</head>
<body><div class="wrap">
<h1>Crypto Radar</h1>
<div class="muted">ماسح تجريبي للبيانات العامة — لا ينفّذ صفقات</div>
<div class="card">
<div>آخر فحص: <span id="scanat">—</span></div>
<div>عدد النتائج: <span id="count">—</span></div>
<div id="err" class="muted"></div>
</div>
<div class="card"><h3>أعلى العملات المرشحة</h3>
<div style="overflow-x:auto"><table>
<thead><tr><th>الزوج</th><th>السعر</th><th>تغير 24س</th><th>حجم USDT</th><th>الزخم القصير</th><th>الدرجة</th><th>الحالة</th></tr></thead>
<tbody id="rows"><tr><td colspan="7">بانتظار أول فحص…</td></tr></tbody>
</table></div></div>
<div class="muted">الدرجة ترتيب تجريبي وليست احتمالًا إحصائيًا أو توصية مالية. قد تتأخر البيانات أو تكون الإشارة كاذبة.</div>
</div>
<script>
async function refresh(){
 try{
  const r=await fetch('/api/results'); const d=await r.json();
  document.getElementById('scanat').textContent=d.last_scan_at||'لم يبدأ';
  document.getElementById('count').textContent=d.results.length;
  document.getElementById('err').textContent=d.error?'خطأ: '+d.error:'';
  const body=document.getElementById('rows');
  if(!d.results.length){body.innerHTML='<tr><td colspan="7">لا توجد نتائج بعد</td></tr>';return;}
  body.innerHTML=d.results.map(x=>{
   const cls=x.change_24h>=0?'up':'down';
   const mcls=x.momentum_pct>=0?'up':'down';
   const status=x.score>=65?'نشاط مرتفع':(x.score>=45?'مراقبة':'عادي');
   return `<tr><td><b>${x.symbol}</b></td><td>${x.price}</td><td class="${cls}">${x.change_24h.toFixed(2)}%</td><td>${Math.round(x.quote_volume).toLocaleString()}</td><td class="${mcls}">${x.momentum_pct.toFixed(2)}%</td><td class="score">${x.score}/100</td><td><span class="pill">${status}</span></td></tr>`;
  }).join('');
 }catch(e){document.getElementById('err').textContent='تعذر جلب النتائج من الخادم';}
}
refresh(); setInterval(refresh,15000);
</script></body></html>
"""

def get_json(path, params=None):
    response = session.get(f"{BINANCE_BASE}{path}", params=params, timeout=15)
    response.raise_for_status()
    return response.json()

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = session.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        }, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Telegram send failed: %s", exc)

def support_resistance(klines):
    # Simple rolling local levels; not a full market-structure model.
    lows = [float(k[3]) for k in klines[:-1]]
    highs = [float(k[2]) for k in klines[:-1]]
    if not lows or not highs:
        return None, None
    return min(lows[-20:]), max(highs[-20:])

def analyze_symbol(ticker):
    symbol = ticker["symbol"]
    price = float(ticker["lastPrice"])
    change = float(ticker["priceChangePercent"])
    quote_volume = float(ticker["quoteVolume"])

    klines = get_json("/api/v3/klines", {
        "symbol": symbol, "interval": KLINE_INTERVAL, "limit": KLINE_LIMIT
    })
    if len(klines) < 25:
        return None

    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    last_closed = closes[-2]
    previous_closed = closes[-3]
    momentum = ((last_closed / previous_closed) - 1) * 100 if previous_closed else 0

    recent_vol = sum(volumes[-6:-1]) / max(1, len(volumes[-6:-1]))
    base_vols = volumes[-25:-6]
    base_vol = sum(base_vols) / max(1, len(base_vols))
    volume_ratio = recent_vol / base_vol if base_vol > 0 else 0

    support, resistance = support_resistance(klines)
    # Transparent heuristic scoring, not a prediction probability.
    score = 0
    if volume_ratio >= 1.5: score += 25
    elif volume_ratio >= 1.2: score += 15
    if momentum >= 0.8: score += 25
    elif momentum >= 0.3: score += 15
    elif momentum > 0: score += 8
    if change > 0: score += 10
    if quote_volume >= MIN_QUOTE_VOLUME * 3: score += 20
    elif quote_volume >= MIN_QUOTE_VOLUME: score += 12
    if support and resistance and resistance > support:
        range_position = (price - support) / (resistance - support)
        if 0.55 <= range_position <= 0.9: score += 10
        if range_position > 0.98: score += 5
    score = min(100, score)

    return {
        "symbol": symbol,
        "price": price,
        "change_24h": change,
        "quote_volume": quote_volume,
        "momentum_pct": momentum,
        "volume_ratio": volume_ratio,
        "support": support,
        "resistance": resistance,
        "score": score,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

def scan_once():
    global latest_results, last_scan_at, scan_error
    log.info("Starting scan")
    try:
        tickers = get_json("/api/v3/ticker/24hr")
        exchange_info = get_json("/api/v3/exchangeInfo")
        allowed = {
            s["symbol"] for s in exchange_info["symbols"]
            if s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and s.get("isSpotTradingAllowed", True)
        }
        candidates = [
            t for t in tickers
            if t.get("symbol") in allowed
            and float(t.get("quoteVolume", 0)) >= MIN_QUOTE_VOLUME
        ]
        candidates.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
        candidates = candidates[:MAX_SYMBOLS]

        results = []
        for ticker in candidates:
            try:
                item = analyze_symbol(ticker)
                if item:
                    results.append(item)
                time.sleep(0.08)  # be gentle to public endpoints
            except requests.RequestException as exc:
                log.warning("Data request failed for %s: %s", ticker.get("symbol"), exc)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                log.warning("Bad data for %s: %s", ticker.get("symbol"), exc)

        results.sort(key=lambda x: x["score"], reverse=True)
        latest_results = results
        last_scan_at = datetime.now(timezone.utc).isoformat()
        scan_error = None

        for item in results[:20]:
            symbol = item["symbol"]
            now = time.time()
            if item["score"] >= ALERT_SCORE and now - last_alerts.get(symbol, 0) > 3600:
                msg = (
                    f"🔎 Crypto Radar — تنبيه مراقبة\n"
                    f"الزوج: {symbol}\n"
                    f"الدرجة التجريبية: {item['score']}/100\n"
                    f"تغير 24س: {item['change_24h']:.2f}%\n"
                    f"زخم قصير: {item['momentum_pct']:.2f}%\n"
                    f"نسبة حجم الشموع: {item['volume_ratio']:.2f}x\n"
                    f"حجم التداول 24س: {item['quote_volume']:,.0f} USDT\n"
                    f"دعم تقريبي: {item['support']:.8g}\n"
                    f"مقاومة تقريبية: {item['resistance']:.8g}\n"
                    "تنبيه بحثي فقط — ليس توصية أو أمر تداول."
                )
                send_telegram(msg)
                last_alerts[symbol] = now
        log.info("Scan complete: %s symbols", len(results))
    except requests.RequestException as exc:
        scan_error = str(exc)
        log.exception("Scan failed")

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/api/results")
def api_results():
    return jsonify({
        "results": latest_results,
        "last_scan_at": last_scan_at,
        "error": scan_error
    })

def scanner_loop():
    while True:
        scan_once()
        time.sleep(max(60, SCAN_INTERVAL))

if __name__ == "__main__":
    import threading
    worker = threading.Thread(target=scanner_loop, daemon=True)
    worker.start()
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)
