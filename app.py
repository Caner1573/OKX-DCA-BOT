import sqlite3
import json
import os
import sys
sys.stdout = sys.stderr

from flask import Flask, request, jsonify, render_template_string
import ccxt
import math
import threading
import time
import requests

app = Flask(__name__)

FIXED_API_KEY    = os.environ.get('OKX_API_KEY', '733c2c4a-1929-4817-b79a-345cf9deab0a')
FIXED_SECRET     = os.environ.get('OKX_SECRET',  'CACD00C63057E9AE722A007F00FCF03D')
FIXED_PASSPHRASE = os.environ.get('OKX_PASS',    'Caner157344.')
RENDER_URL       = os.environ.get('RENDER_URL',  'https://okx-dca-bot.onrender.com')

# ✅ DB ŞEMASI — otomatik migrate
def init_db():
    conn   = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_positions (
            symbol            TEXT PRIMARY KEY,
            side              TEXT,
            last_entry_price  REAL,
            current_step      INTEGER,
            current_contracts REAL DEFAULT 0,
            avg_entry_price   REAL DEFAULT 0,
            total_contracts   REAL DEFAULT 0,
            tp1_done          INTEGER DEFAULT 0,
            tp2_done          INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol     TEXT,
            side       TEXT,
            status     TEXT,
            pnl        REAL,
            steps_used TEXT
        )
    ''')
    # ✅ Eski DB'ye eksik kolonları ekle (migrate)
    existing = [row[1] for row in cursor.execute("PRAGMA table_info(active_positions)").fetchall()]
    for col, definition in [
        ('avg_entry_price',  'REAL DEFAULT 0'),
        ('total_contracts',  'REAL DEFAULT 0'),
        ('tp1_done',         'INTEGER DEFAULT 0'),
        ('tp2_done',         'INTEGER DEFAULT 0'),
    ]:
        if col not in existing:
            cursor.execute(f"ALTER TABLE active_positions ADD COLUMN {col} {definition}")
            print(f"🔧 DB migrate: {col} kolonu eklendi")
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
    val = row[0] if row else None
    if val is None or val == '':
        return default
    return val

def get_stats():
    conn = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), SUM(pnl) FROM trades")
        row       = cursor.fetchone()
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

# ✅ UYKU MODU ÇÖZÜMü — kendi kendine ping
def self_ping():
    while True:
        try:
            time.sleep(840)  # 14 dakikada bir ping (Render 15dk'da uyutur)
            url = RENDER_URL.rstrip('/') + '/ping'
            r   = requests.get(url, timeout=10)
            print(f"🏓 Self-ping → {r.status_code}")
        except Exception as e:
            print(f"⚠️ Ping hatası: {e}")

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "alive"}), 200

# ✅ TP İZLEYİCİ
def tp_monitor():
    while True:
        try:
            conn    = sqlite3.connect('bot_settings.db')
            cursor  = conn.cursor()
            cursor.execute("SELECT symbol, side, avg_entry_price, total_contracts, tp1_done, tp2_done FROM active_positions")
            positions = cursor.fetchall()
            conn.close()

            for pos in positions:
                symbol, side, avg_price, total_contracts, tp1_done, tp2_done = pos
                if not avg_price or avg_price == 0 or not total_contracts or total_contracts == 0:
                    continue

                try:
                    okx = get_okx()
                    if not okx:
                        continue
                    okx.load_markets()
                    ticker    = okx.fetch_ticker(symbol)
                    cur_price = ticker['last']

                    tp1_pct = float(get_setting('tp1_pct', 1.5)) / 100
                    tp2_pct = float(get_setting('tp2_pct', 3.0)) / 100
                    tp1_qty = float(get_setting('tp1_qty', 50)) / 100
                    tp2_qty = float(get_setting('tp2_qty', 50)) / 100

                    if side == 'buy':
                        tp1_price = avg_price * (1 + tp1_pct)
                        tp2_price = avg_price * (1 + tp2_pct)
                        hit_tp1   = cur_price >= tp1_price
                        hit_tp2   = cur_price >= tp2_price
                    else:
                        tp1_price = avg_price * (1 - tp1_pct)
                        tp2_price = avg_price * (1 - tp2_pct)
                        hit_tp1   = cur_price <= tp1_price
                        hit_tp2   = cur_price <= tp2_price

                    close_side = 'sell' if side == 'buy' else 'buy'
                    pos_side   = 'long' if side == 'buy' else 'short'

                    if hit_tp2 and not tp2_done:
                        qty = math.floor(total_contracts * tp2_qty * 10) / 10
                        if qty > 0:
                            okx.create_market_order(
                                symbol=symbol, side=close_side, amount=qty,
                                params={"posSide": pos_side, "reduceOnly": True}
                            )
                            print(f"✅ TP2 → {symbol} | {qty} kontrat | Fiyat:{cur_price}")
                            conn2  = sqlite3.connect('bot_settings.db')
                            cur2   = conn2.cursor()
                            remaining = total_contracts - qty
                            if remaining <= 0:
                                cur2.execute("DELETE FROM active_positions WHERE symbol=?", (symbol,))
                                print(f"🏁 Pozisyon tamamen kapatıldı: {symbol}")
                            else:
                                cur2.execute(
                                    "UPDATE active_positions SET tp2_done=1, total_contracts=? WHERE symbol=?",
                                    (remaining, symbol)
                                )
                            conn2.commit()
                            conn2.close()

                    elif hit_tp1 and not tp1_done:
                        qty = math.floor(total_contracts * tp1_qty * 10) / 10
                        if qty > 0:
                            okx.create_market_order(
                                symbol=symbol, side=close_side, amount=qty,
                                params={"posSide": pos_side, "reduceOnly": True}
                            )
                            print(f"✅ TP1 → {symbol} | {qty} kontrat | Fiyat:{cur_price}")
                            conn2  = sqlite3.connect('bot_settings.db')
                            cur2   = conn2.cursor()
                            remaining = total_contracts - qty
                            cur2.execute(
                                "UPDATE active_positions SET tp1_done=1, total_contracts=? WHERE symbol=?",
                                (remaining, symbol)
                            )
                            conn2.commit()
                            conn2.close()

                except Exception as e:
                    print(f"⚠️ TP kontrol hatası {symbol}: {e}")

        except Exception as e:
            print(f"⚠️ TP monitor hatası: {e}")

        time.sleep(10)

# Thread'leri başlat
threading.Thread(target=tp_monitor, daemon=True).start()
threading.Thread(target=self_ping,  daemon=True).start()

@app.route('/', methods=['GET'])
def dashboard():
    total, win_rate, total_pnl = get_stats()
    api_status = "✅ Bağlı" if get_okx() else "❌ API Eksik"

    # ✅ Aktif pozisyonları çek
    conn   = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, side, avg_entry_price, total_contracts, current_step, tp1_done, tp2_done FROM active_positions")
    active_pos = cursor.fetchall()
    conn.close()

    pos_rows = ""
    for p in active_pos:
        sym, sd, avg_px, tot_ct, step, tp1d, tp2d = p
        tp1_badge = "✅" if tp1d else "⏳"
        tp2_badge = "✅" if tp2d else "⏳"
        side_color = "#4caf50" if sd == "buy" else "#f44336"
        pos_rows += f'''
        <tr>
          <td>{sym}</td>
          <td style="color:{side_color}">{"LONG" if sd=="buy" else "SHORT"}</td>
          <td>{avg_px:.4f}</td>
          <td>{tot_ct}</td>
          <td>Step {step}</td>
          <td>{tp1_badge} TP1 &nbsp; {tp2_badge} TP2</td>
          <td><button onclick="closePos('{sym}','{sd}')" style="background:#f44336;padding:5px 10px;border:none;border-radius:4px;color:#fff;cursor:pointer">Kapat</button></td>
        </tr>'''

    if not pos_rows:
        pos_rows = '<tr><td colspan="7" style="text-align:center;color:#555;padding:20px">Aktif pozisyon yok</td></tr>'

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
        'pnl':      f"{total_pnl:.2f} USDT",
        'pos_rows': pos_rows
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
      button.save { width:100%; padding:14px; background:#4caf50; border:none;
               border-radius:4px; color:#fff; font-weight:bold; margin-top:10px; font-size:1rem; }
      .badge { display:inline-block; padding:4px 10px; border-radius:20px;
               background:#1e3a1e; color:#4caf50; font-size:0.8rem; margin-top:5px; }
      table { width:100%; border-collapse:collapse; font-size:0.78rem; }
      th { color:#4caf50; padding:8px 4px; border-bottom:1px solid #2a2a2e; text-align:left; }
      td { padding:8px 4px; border-bottom:1px solid #1e1e22; }
    </style></head>
    <body>
    <h3 style="text-align:center;color:#4caf50;">🤖 S-DCA KONTROL PANELİ</h3>

    <div class="stats">
      <div class="stat"><div class="val">{{total}}</div><div class="lbl">İşlem</div></div>
      <div class="stat"><div class="val">{{win_rate}}</div><div class="lbl">Kazanma</div></div>
      <div class="stat"><div class="val">{{pnl}}</div><div class="lbl">PnL</div></div>
    </div>

    <!-- AKTİF POZİSYONLAR -->
    <div class="card">
      <h2>📊 AKTİF POZİSYONLAR</h2>
      <table>
        <tr>
          <th>Sembol</th><th>Yön</th><th>Ort.Fiyat</th>
          <th>Kontrat</th><th>Step</th><th>TP</th><th>İşlem</th>
        </tr>
        {{pos_rows}}
      </table>
    </div>

    <div class="card">
      <h2>1. OKX API DURUMU</h2>
      <span class="badge">{{api_status}}</span>
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
    <button class="save" type="submit">💾 AYARLARI KAYDET</button>
    </form>

    <script>
    function closePos(symbol, side) {
      if (!confirm(symbol + ' pozisyonunu kapatmak istediğine emin misin?')) return;
      fetch('/close', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({symbol: symbol, side: side})
      })
      .then(r => r.json())
      .then(d => { alert(d.message || 'İşlem tamamlandı'); location.reload(); })
      .catch(e => alert('Hata: ' + e));
    }
    </script>
    </body></html>
    '''
    return render_template_string(html_template, **context)

@app.route('/save', methods=['POST'])
def save():
    for key in request.form:
        if key not in ['api_key', 'secret', 'passphrase']:
            save_setting(key, request.form[key])
    return '<script>alert("Ayarlar kaydedildi!"); window.location="/";</script>'

# ✅ MANUEL KAPATMA ENDPOİNTİ
@app.route('/close', methods=['POST'])
def close_position():
    data   = request.get_json()
    symbol = data.get('symbol')
    side   = data.get('side')

    if not symbol or not side:
        return jsonify({"status": "error", "message": "Eksik parametre"}), 200

    okx = get_okx()
    if not okx:
        return jsonify({"status": "error", "message": "API bağlantısı yok"}), 200

    try:
        okx.load_markets()

        conn   = sqlite3.connect('bot_settings.db')
        cursor = conn.cursor()
        cursor.execute("SELECT total_contracts FROM active_positions WHERE symbol=?", (symbol,))
        row = cursor.fetchone()
        conn.close()

        if not row or not row[0]:
            return jsonify({"status": "error", "message": "Pozisyon bulunamadı"}), 200

        total_contracts = row[0]
        close_side      = 'sell' if side == 'buy' else 'buy'
        pos_side        = 'long' if side == 'buy' else 'short'

        order = okx.create_market_order(
            symbol=symbol,
            side=close_side,
            amount=total_contracts,
            params={"posSide": pos_side, "reduceOnly": True}
        )
        print(f"🛑 MANUEL KAPATMA → {symbol} | {total_contracts} kontrat")

        conn2  = sqlite3.connect('bot_settings.db')
        cur2   = conn2.cursor()
        cur2.execute("DELETE FROM active_positions WHERE symbol=?", (symbol,))
        conn2.commit()
        conn2.close()

        return jsonify({"status": "success", "message": f"{symbol} pozisyonu kapatıldı"}), 200

    except Exception as e:
        print(f"❌ Manuel kapatma hatası: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200

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
    cursor.execute("SELECT side, last_entry_price, current_step, avg_entry_price, total_contracts FROM active_positions WHERE symbol=?", (symbol,))
    position = cursor.fetchone()

    if position:
        pos_side, last_entry_price, current_step, avg_entry_price, total_contracts = position

        if step == 1 and current_step >= 1:
            print(f"⛔ STEP 1 Engeli: {symbol} zaten aktif (step:{current_step})")
            conn.close()
            return jsonify({"status": "ignored", "message": "Pozisyon zaten açık"}), 200

        if step == current_step:
            print(f"⛔ Aynı Step Engeli: {symbol} step:{step} zaten işlendi")
            conn.close()
            return jsonify({"status": "ignored", "message": f"Step {step} zaten işlendi"}), 200

        if step > 1:
            if side == 'buy' and current_price >= last_entry_price:
                pct = (current_price - last_entry_price) / last_entry_price * 100
                print(f"⛔ LONG DCA Engeli: +%{pct:.2f}")
                conn.close()
                return jsonify({"status": "ignored", "message": "LONG DCA engeli"}), 200

            if side == 'sell' and current_price <= last_entry_price:
                pct = (last_entry_price - current_price) / last_entry_price * 100
                print(f"⛔ SHORT DCA Engeli: -%{pct:.2f}")
                conn.close()
                return jsonify({"status": "ignored", "message": "SHORT DCA engeli"}), 200

            price_diff_pct = abs(current_price - last_entry_price) / last_entry_price * 100
            if price_diff_pct < min_distance_filter:
                print(f"⛔ Mesafe Engeli: %{price_diff_pct:.2f} < %{min_distance_filter}")
                conn.close()
                return jsonify({"status": "ignored", "message": "Mesafe engeli"}), 200

    try:
        okx.load_markets()

        try:
            okx.set_margin_mode('cross', symbol)
        except Exception as e:
            print(f"ℹ️ Margin: {e}")

        try:
            okx.set_leverage(10, symbol, {'mgnMode': 'cross', 'posSide': 'long'})
            okx.set_leverage(10, symbol, {'mgnMode': 'cross', 'posSide': 'short'})
        except Exception as e:
            print(f"ℹ️ Leverage: {e}")

        market         = okx.market(symbol)
        contract_size  = market['contractSize']
        position_value = allocated_usd * 10

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

        pos_side = "long" if side == "buy" else "short"

        print(f"🚀 EMİR → {symbol} | {side.upper()} | {final_qty} kontrat | ${allocated_usd}")

        order = okx.create_market_order(
            symbol=symbol,
            side=side,
            amount=final_qty,
            params={"posSide": pos_side}
        )
        print(f"✅ İŞLEM AÇILDI! Order ID: {order.get('id', 'N/A')}")

        if not position:
            cursor.execute(
                "INSERT INTO active_positions (symbol, side, last_entry_price, current_step, avg_entry_price, total_contracts) VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, side, current_price, step, current_price, final_qty)
            )
        else:
            old_avg   = avg_entry_price or current_price
            old_total = total_contracts or 0
            new_total = old_total + final_qty
            new_avg   = ((old_avg * old_total) + (current_price * final_qty)) / new_total
            cursor.execute(
                "UPDATE active_positions SET last_entry_price=?, current_step=?, avg_entry_price=?, total_contracts=? WHERE symbol=?",
                (current_price, step, new_avg, new_total, symbol)
            )
            print(f"📐 Yeni Ort.Fiyat: {new_avg:.4f} | Toplam: {new_total} kontrat")

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
