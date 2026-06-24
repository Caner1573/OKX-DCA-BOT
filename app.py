import sqlite3
import json
import os
from flask import Flask, request, jsonify, render_template_string
import ccxt
import math

app = Flask(__name__)

FIXED_API_KEY    = os.environ.get('OKX_API_KEY', '24e933df-c8ab-4511-aba3-2c0e112e3ab1')
FIXED_SECRET     = os.environ.get('OKX_SECRET',  'EAE744COC9889D243885487D8332D38')
FIXED_PASSPHRASE = os.environ.get('OKX_PASS',    'Caner157344...')

def init_db():
    conn = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_positions (
            symbol TEXT PRIMARY KEY,
            side TEXT,
            highest_price REAL,
            lowest_price REAL,
            last_entry_price REAL,
            current_step INTEGER,
            current_contracts REAL DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            side TEXT,
            status TEXT,
            pnl REAL,
            steps_used TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_setting(key, value):
    conn = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_setting(key, default):
    conn = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def get_stats():
    conn = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), SUM(pnl) FROM trades")
        row = cursor.fetchone()
        total     = row[0] if row[0] else 0
        wins      = row[1] if row[1] else 0
        total_pnl = row[2] if row[2] else 0.0
        win_rate  = (wins / total * 100) if total > 0 else 0
    except:
        total, win_rate, total_pnl = 0, 0, 0.0
    finally:
        conn.close()
    return total, win_rate, total_pnl

init_db()

def get_okx():
    api_key    = FIXED_API_KEY.strip()
    secret     = FIXED_SECRET.strip()
    passphrase = FIXED_PASSPHRASE.strip()
    if not api_key or not secret or not passphrase:
        return None
    if 'BURAYA' in api_key:
        return None
    return ccxt.okx({
        'apiKey':   api_key,
        'secret':   secret,
        'password': passphrase,
        'options':  {'defaultType': 'swap'},
        'enableRateLimit': True
    })

@app.route('/', methods=['GET'])
def dashboard():
    total, win_rate, total_pnl = get_stats()
    api_status = "✅ Bağlı" if get_okx() else "❌ API Eksik"
    context = {
        'api_status': api_status,
        'l1_usd':   get_setting('l1_usd',  '40'),
        'd1_usd':   get_setting('d1_usd',  '60'),
        'd2_usd':   get_setting('d2_usd',  '90'),
        'd3_usd':   get_setting('d3_usd',  '135'),
        'd4_usd':   get_setting('d4_usd',  '202.5'),
        'min_dist': get_setting('min_dist','2.0'),
        'tp1_pct':  get_setting('tp1_pct', '1.5'),
        'tp1_qty':  get_setting('tp1_qty', '50'),
        'tp2_pct':  get_setting('tp2_pct', '3.0'),
        'tp2_qty':  get_setting('tp2_qty', '50'),
        'total':    total,
        'win_rate': f"{win_rate:.1f}%",
        'pnl':      f"{total_pnl:.2f} USDT"
    }
    html_template = '''
    <!DOCTYPE html>
    <html><head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OKX DCA Bot</title>
    <style>
      body { font-family: sans-serif; background:#121214; color:#fff; padding:10px; margin:0; }
      .card { background:#1a1a1e; padding:15px; border-radius:8px; margin-bottom:15px; }
      .stats { display:flex; gap:10px; margin-bottom:15px; }
      .stat { flex:1; background:#1a1a1e; padding:12px; border-radius:8px; text-align:center; }
      .stat .val { font-size:1.3rem; font-weight:bold; color:#4caf50; }
      .stat .lbl { font-size:0.7rem; color:#888; margin-top:4px; }
      h2 { color:#4caf50; font-size:0.95rem; margin-top:0; }
      label { display:block; margin:8px 0 2px; color:#aaa; font-size:0.8rem; }
      input { width:100%; padding:10px; background:#26262b; border:1px solid #3a3a42;
              border-radius:4px; color:#fff; box-sizing:border-box; }
      button { width:100%; padding:14px; background:#4caf50; border:none;
               border-radius:4px; color:#fff; font-weight:bold; margin-top:10px; font-size:1rem; }
      .badge { display:inline-block; padding:4px 10px; border-radius:20px;
               background:#1e3a1e; color:#4caf50; font-size:0.8rem; margin-top:5px; }
    </style></head>
    <body>
    <h3 style="text-align:center;color:#4caf50;">🤖 S-DCA KONTROL PANELİ</h3>

    <div class="stats">
      <div class="stat"><div class="val">{{total}}</div><div class="lbl">İşlem</div></div>
      <div class="stat"><div class="val">{{win_rate}}</div><div class="lbl">Kazanma</div></div>
      <div class="stat"><div class="val">{{pnl}}</div><div class="lbl">Toplam PnL</div></div>
    </div>

    <div class="card">
      <h2>1. OKX API DURUMU</h2>
      <span class="badge">{{api_status}}</span>
      <p style="color:#888;font-size:0.75rem;margin:8px 0 0;">
        Render → Environment Variables:<br>
        OKX_API_KEY / OKX_SECRET / OKX_PASS
      </p>
    </div>

    <form action="/save" method="POST">
    <div class="card">
      <h2>2. KADEMELİ BÜTÇE ($)</h2>
      <label>LONG/SHORT 1 (İlk Giriş):</label><input type="number" name="l1_usd" value="{{l1_usd}}">
      <label>DCA 1:</label><input type="number" name="d1_usd" value="{{d1_usd}}">
      <label>DCA 2:</label><input type="number" name="d2_usd" value="{{d2_usd}}">
      <label>DCA 3:</label><input type="number" name="d3_usd" value="{{d3_usd}}">
      <label>DCA 4:</label><input type="number" name="d4_usd" value="{{d4_usd}}">
    </div>
    <div class="card">
      <h2>3. FİLTRELER & KAR AL</h2>
      <label>Min. Fiyat Uzaklık Filtresi (%):</label>
      <input type="number" step="0.1" name="min_dist" value="{{min_dist}}">
      <label>TP 1 Oranı (%):</label>
      <input type="number" step="0.1" name="tp1_pct" value="{{tp1_pct}}">
      <label>TP 1 Satış Oranı (%):</label>
      <input type="number" name="tp1_qty" value="{{tp1_qty}}">
      <label>TP 2 Oranı (%):</label>
      <input type="number" step="0.1" name="tp2_pct" value="{{tp2_pct}}">
      <label>TP 2 Satış Oranı (%):</label>
      <input type="number" name="tp2_qty" value="{{tp2_qty}}">
    </div>
    <button type="submit">💾 AYARLARI KAYDET</button>
    </form>
    </body></html>
    '''
    return render_template_string(html_template, **context)

@app.route('/save', methods=['POST'])
def save():
    for key in request.form:
        if key not in ['api_key', 'secret', 'passphrase']:
            save_setting(key, request.form[key])
    return '<script>alert("Ayarlar kaydedildi!"); window.location="/";</script>'

@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.data.decode('utf-8')
    print(f"📡 [SİNYAL] Gelen: {payload}")

    try:
        data = json.loads(payload)
    except:
        print("❌ JSON parse hatası")
        return jsonify({"status": "error", "message": "Gecersiz JSON"}), 200

    raw_symbol    = data.get('symbol')
    side          = data.get('side', 'buy')
    step          = int(data.get('step', 1))
    current_price = float(data.get('price', 0))

    if not raw_symbol or not current_price:
        print("❌ Eksik veri")
        return jsonify({"status": "error", "message": "Eksik veri"}), 200

    # Sembol dönüşümü: BTCUSDT.P → BTC/USDT:USDT
    symbol = raw_symbol.replace('.P', '').replace('-', '').replace('_', '').strip()
    if "USDT" in symbol and ":" not in symbol:
        symbol = symbol.replace("USDT", "/USDT:USDT")

    print(f"📊 {symbol} | {side.upper()} | Step:{step} | Fiyat:{current_price}")

    budgets = {
        1: float(get_setting('l1_usd',  40)),
        2: float(get_setting('d1_usd',  60)),
        3: float(get_setting('d2_usd',  90)),
        4: float(get_setting('d3_usd',  135)),
        5: float(get_setting('d4_usd',  202.5))
    }
    allocated_usd       = budgets.get(step, 40.0)
    min_distance_filter = float(get_setting('min_dist', 2.0))

    okx = get_okx()
    if not okx:
        print("❌ API anahtarları eksik!")
        return jsonify({"status": "error", "message": "API anahtarlari eksik"}), 200

    conn   = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT side, last_entry_price, current_step FROM active_positions WHERE symbol=?", (symbol,))
    position = cursor.fetchone()

    # -------------------------------------------------------
    # ANA KURAL: Fiyat bazlı DCA filtresi
    # LONG  → yeni fiyat, son girişten DÜŞÜK olmalı (daha kötüye gitmiş)
    # SHORT → yeni fiyat, son girişten YÜKSEK olmalı (daha kötüye gitmiş)
    # -------------------------------------------------------
    if position and step > 1:
        pos_side, last_entry_price, current_step = position

        if side == 'buy':
            # LONG için: fiyat düşmemişse DCA yapma
            if current_price >= last_entry_price:
                pct = (current_price - last_entry_price) / last_entry_price * 100
                print(f"⛔ LONG DCA Engeli: Fiyat düşmedi. Şimdi:{current_price} >= Giriş:{last_entry_price} (+%{pct:.2f})")
                conn.close()
                return jsonify({"status": "ignored", "message": f"LONG DCA engeli: fiyat yukarda (%+{pct:.2f})"}), 200

        elif side == 'sell':
            # SHORT için: fiyat yükselmemişse DCA yapma
            if current_price <= last_entry_price:
                pct = (last_entry_price - current_price) / last_entry_price * 100
                print(f"⛔ SHORT DCA Engeli: Fiyat yükselmedi. Şimdi:{current_price} <= Giriş:{last_entry_price} (-%{pct:.2f})")
                conn.close()
                return jsonify({"status": "ignored", "message": f"SHORT DCA engeli: fiyat asagida (%-{pct:.2f})"}), 200

        # Min mesafe filtresi (yeterince uzaklaşmış mı?)
        price_diff_pct = abs(current_price - last_entry_price) / last_entry_price * 100
        if price_diff_pct < min_distance_filter:
            print(f"⛔ Mesafe Engeli: %{price_diff_pct:.2f} < Sınır %{min_distance_filter}")
            conn.close()
            return jsonify({"status": "ignored", "message": f"Mesafe engeli: %{price_diff_pct:.2f}"}), 200

    try:
        okx.load_markets()

        try:
            okx.set_margin_mode('cross', symbol)
        except Exception as e:
            print(f"ℹ️ Margin: {e}")

        try:
            okx.set_leverage(10, symbol, {'mgnMode': 'cross'})
        except Exception as e:
            print(f"ℹ️ Leverage: {e}")

        market         = okx.market(symbol)
        contract_size  = market['contractSize']
        position_value = allocated_usd * 10  # 10x kaldıraç

        calculated_qty = position_value / (current_price * contract_size)
        min_qty        = market['limits']['amount']['min']

        if calculated_qty < min_qty:
            calculated_qty = min_qty

        precision = market['precision']['amount']
        if precision and precision > 0:
            d         = int(round(-math.log10(precision)))
            final_qty = math.floor(calculated_qty * (10 ** d)) / (10 ** d)
        else:
            final_qty = math.floor(calculated_qty)

        if final_qty <= 0:
            final_qty = min_qty if min_qty else 1

        print(f"🚀 EMİR → {symbol} | {side.upper()} | {final_qty} kontrat | ${allocated_usd}")

        order = okx.create_market_order(symbol=symbol, side=side, amount=final_qty)
        print(f"✅ İŞLEM AÇILDI! Order ID: {order.get('id', 'N/A')}")

        # Pozisyon kaydı
        if not position:
            cursor.execute(
                "INSERT INTO active_positions (symbol, side, highest_price, lowest_price, last_entry_price, current_step) VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, side, current_price, current_price, current_price, step)
            )
        else:
            cursor.execute(
                "UPDATE active_positions SET last_entry_price=?, current_step=? WHERE symbol=?",
                (current_price, step, symbol)
            )

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "order_id": order.get('id')}), 200

    except Exception as e:
        conn.close()
        print(f"❌ BORSA HATASI: {str(e)}")
        return jsonify({"status": "error", "okx_error": str(e)}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f"🌐 Sunucu başlatılıyor → port {port}")
    app.run(host='0.0.0.0', port=port)
