import sqlite3
import json
import os
import sys
sys.stdout = sys.stderr

from flask import Flask, request, jsonify, render_template_string, Markup
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
    existing = [row[1] for row in cursor.execute("PRAGMA table_info(active_positions)").fetchall()]
    for col, definition in [
        ('avg_entry_price', 'REAL DEFAULT 0'),
        ('total_contracts', 'REAL DEFAULT 0'),
        ('tp1_done',        'INTEGER DEFAULT 0'),
        ('tp2_done',        'INTEGER DEFAULT 0'),
    ]:
        if col not in existing:
            cursor.execute(f"ALTER TABLE active_positions ADD COLUMN {col} {definition}")
            print(f"🔧 DB migrate: {col} eklendi")
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

# ✅ UYKU MODU — self ping
def self_ping():
    while True:
        try:
            time.sleep(840)
            url = RENDER_URL.rstrip('/') + '/ping'
            r   = requests.get(url, timeout=10)
            print(f"🏓 Ping → {r.status_code}")
        except Exception as e:
            print(f"⚠️ Ping hatası: {e}")

@app.route('/ping')
def ping():
    return jsonify({"status": "alive"}), 200

# ✅ TP İZLEYİCİ — düzeltildi
def tp_monitor():
    while True:
        try:
            conn   = sqlite3.connect('bot_settings.db')
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, side, avg_entry_price, total_contracts, tp1_done, tp2_done FROM active_positions")
            positions = cursor.fetchall()
            conn.close()

            for pos in positions:
                symbol, side, avg_price, total_contracts, tp1_done, tp2_done = pos

                if not avg_price or avg_price == 0:
                    print(f"⚠️ {symbol} avg_price=0, TP atlandı")
                    continue
                if not total_contracts or total_contracts == 0:
                    print(f"⚠️ {symbol} total_contracts=0, TP atlandı")
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

                    print(f"👁 {symbol} | Fiyat:{cur_price} | Ort:{avg_price:.4f} | TP1:{tp1_price:.4f} | TP2:{tp2_price:.4f} | tp1_done:{tp1_done} | tp2_done:{tp2_done}")

                    if hit_tp2 and not tp2_done:
                        qty = math.floor(total_contracts * tp2_qty * 10) / 10
                        if qty <= 0:
                            qty = total_contracts
                        okx.create_market_order(
                            symbol=symbol, side=close_side, amount=qty,
                            params={"posSide": pos_side, "reduceOnly": True}
                        )
                        print(f"✅ TP2 → {symbol} | {qty} kontrat kapatıldı")
                        conn2  = sqlite3.connect('bot_settings.db')
                        cur2   = conn2.cursor()
                        remaining = max(0, total_contracts - qty)
                        if remaining <= 0:
                            cur2.execute("DELETE FROM active_positions WHERE symbol=?", (symbol,))
                            print(f"🏁 {symbol} tamamen kapatıldı")
                        else:
                            cur2.execute(
                                "UPDATE active_positions SET tp2_done=1, total_contracts=? WHERE symbol=?",
                                (remaining, symbol)
                            )
                        conn2.commit()
                        conn2.close()

                    elif hit_tp1 and not tp1_done:
                        qty = math.floor(total_contracts * tp1_qty * 10) / 10
                        if qty <= 0:
                            qty = total_contracts
                        okx.create_market_order(
                            symbol=symbol, side=close_side, amount=qty,
                            params={"posSide": pos_side, "reduceOnly": True}
                        )
                        print(f"✅ TP1 → {symbol} | {qty} kontrat kapatıldı")
                        conn2  = sqlite3.connect('bot_settings.db')
                        cur2   = conn2.cursor()
                        remaining = max(0, total_contracts - qty)
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

threading.Thread(target=tp_monitor, daemon=True).start()
threading.Thread(target=self_ping,  daemon=True).start()

@app.route('/', methods=['GET'])
def dashboard():
    total, win_rate, total_pnl = get_stats()
    api_status = "✅ Bağlı" if get_okx() else "❌ API Eksik"

    conn   = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, side, avg_entry_price, total_contracts, current_step, tp1_done, tp2_done FROM active_positions")
    active_pos = cursor.fetchall()
    conn.close()

    # ✅ Markup ile güvenli HTML injection
    pos_rows_html = ""
    for p in active_pos:
        sym, sd, avg_px, tot_ct, step, tp1d, tp2d = p
        yön        = "LONG" if sd == "buy" else "SHORT"
        yön_renk   = "#4caf50" if sd == "buy" else "#f44336"
        tp1_badge  = '<span style="color:#4caf50">✅TP1</span>' if tp1d else '<span style="color:#888">⏳TP1</span>'
        tp2_badge  = '<span style="color:#4caf50">✅TP2</span>' if tp2d else '<span style="color:#888">⏳TP2</span>'
        avg_str    = f"{avg_px:.4f}" if avg_px else "-"
        sym_short  = sym.replace('/USDT:USDT', '')
        pos_rows_html += f"""
        <tr>
          <td><b>{sym_short}</b></td>
          <td style="color:{yön_renk};font-weight:bold">{yön}</td>
          <td>{avg_str}</td>
          <td>{tot_ct}</td>
          <td>#{step}</td>
          <td>{tp1_badge} {tp2_badge}</td>
          <td>
            <button onclick="closePos('{sym}','{sd}')"
              style="background:#f44336;padding:6px 12px;border:none;border-radius:6px;
                     color:#fff;cursor:pointer;font-size:0.8rem;font-weight:bold">
              ✖ Kapat
            </button>
          </td>
        </tr>"""

    if not pos_rows_html:
        pos_rows_html = '<tr><td colspan="7" style="text-align:center;color:#555;padding:24px;font-size:0.85rem">Aktif pozisyon yok</td></tr>'

    context = {
        'api_status':  api_status,
        'l1_usd':      get_setting('l1_usd',  '40'),
        'd1_usd':      get_setting('d1_usd',  '60'),
        'd2_usd':      get_setting('d2_usd',  '90'),
        'd3_usd':      get_setting('d3_usd',  '135'),
        'd4_usd':      get_setting('d4_usd',  '202.5'),
        'min_dist':    get_setting('min_dist', '2.0'),
        'tp1_pct':     get_setting('tp1_pct',  '1.5'),
        'tp1_qty':     get_setting('tp1_qty',  '50'),
        'tp2_pct':     get_setting('tp2_pct',  '3.0'),
        'tp2_qty':     get_setting('tp2_qty',  '50'),
        'total':       total,
        'win_rate':    f"{win_rate:.1f}%",
        'pnl':         f"{total_pnl:.2f} USDT",
        'pos_rows':    Markup(pos_rows_html),
    }

    html = '''<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>OKX DCA Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:#0d0d0f;color:#e0e0e0;padding:12px}
.header{text-align:center;padding:16px 0 20px;font-size:1.1rem;
        font-weight:700;color:#4caf50;letter-spacing:1px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
.stat{background:#16161a;border:1px solid #222;border-radius:10px;
      padding:14px 8px;text-align:center}
.stat .val{font-size:1.25rem;font-weight:700;color:#4caf50}
.stat .lbl{font-size:0.68rem;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:.5px}
.card{background:#16161a;border:1px solid #222;border-radius:10px;
      padding:14px;margin-bottom:14px}
.card h2{font-size:0.82rem;color:#4caf50;text-transform:uppercase;
         letter-spacing:.8px;margin-bottom:12px;padding-bottom:8px;
         border-bottom:1px solid #222}
table{width:100%;border-collapse:collapse;font-size:0.75rem}
th{color:#555;font-weight:600;padding:6px 4px;border-bottom:1px solid #1e1e22;
   text-align:left;font-size:0.68rem;text-transform:uppercase;letter-spacing:.5px}
td{padding:10px 4px;border-bottom:1px solid #1a1a1e;vertical-align:middle}
label{display:block;margin:10px 0 4px;color:#666;font-size:0.75rem;
      text-transform:uppercase;letter-spacing:.4px}
input{width:100%;padding:10px 12px;background:#0d0d0f;border:1px solid #2a2a2e;
      border-radius:8px;color:#e0e0e0;font-size:0.9rem}
input:focus{outline:none;border-color:#4caf50}
.btn-save{width:100%;padding:14px;background:#4caf50;border:none;border-radius:8px;
          color:#fff;font-weight:700;margin-top:12px;font-size:0.95rem;cursor:pointer}
.badge{display:inline-block;padding:4px 12px;border-radius:20px;
       background:#0a1f0a;color:#4caf50;font-size:0.8rem;border:1px solid #1e3a1e}
.no-pos{text-align:center;color:#333;padding:24px;font-size:0.82rem}
</style></head>
<body>
<div class="header">🤖 S-DCA KONTROL PANELİ</div>

<div class="stats">
  <div class="stat"><div class="val">{{ total }}</div><div class="lbl">İşlem</div></div>
  <div class="stat"><div class="val">{{ win_rate }}</div><div class="lbl">Kazanma</div></div>
  <div class="stat"><div class="val">{{ pnl }}</div><div class="lbl">PnL</div></div>
</div>

<div class="card">
  <h2>📊 Aktif Pozisyonlar</h2>
  <table>
    <thead><tr>
      <th>Sembol</th><th>Yön</th><th>Ort.Fiyat</th>
      <th>Kontrat</th><th>Step</th><th>TP</th><th></th>
    </tr></thead>
    <tbody>{{ pos_rows }}</tbody>
  </table>
</div>

<div class="card">
  <h2>🔌 API Durumu</h2>
  <span class="badge">{{ api_status }}</span>
</div>

<form action="/save" method="POST">
<div class="card">
  <h2>💰 Kademeli Bütçe ($)</h2>
  <label>Giriş 1 (LONG/SHORT)</label><input type="number" name="l1_usd" value="{{ l1_usd }}">
  <label>DCA 1</label><input type="number" name="d1_usd" value="{{ d1_usd }}">
  <label>DCA 2</label><input type="number" name="d2_usd" value="{{ d2_usd }}">
  <label>DCA 3</label><input type="number" name="d3_usd" value="{{ d3_usd }}">
  <label>DCA 4</label><input type="number" name="d4_usd" value="{{ d4_usd }}">
</div>
<div class="card">
  <h2>⚙️ Filtreler & Kar Al</h2>
  <label>Min. Uzaklık Filtresi (%)</label>
  <input type="number" step="0.1" name="min_dist" value="{{ min_dist }}">
  <label>TP 1 Oranı (%)</label>
  <input type="number" step="0.1" name="tp1_pct" value="{{ tp1_pct }}">
  <label>TP 1 Satış Oranı (%)</label>
  <input type="number" name="tp1_qty" value="{{ tp1_qty }}">
  <label>TP 2 Oranı (%)</label>
  <input type="number" step="0.1" name="tp2_pct" value="{{ tp2_pct }}">
  <label>TP 2 Satış Oranı (%)</label>
  <input type="number" name="tp2_qty" value="{{ tp2_qty }}">
</div>
<button class="btn-save" type="submit">💾 Ayarları Kaydet</button>
</form>

<script>
function closePos(symbol, side) {
  if (!confirm(symbol.replace("/USDT:USDT","") + " pozisyonunu kapatmak istediğine emin misin?")) return;
  fetch("/close", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({symbol:symbol, side:side})
  })
  .then(r=>r.json())
  .then(d=>{ alert(d.message||"Tamamlandı"); location.reload(); })
  .catch(e=>alert("Hata: "+e));
}
</script>
</body></html>'''

    return render_template_string(html, **context)

@app.route('/save', methods=['POST'])
def save():
    for key in request.form:
        if key not in ['api_key','secret','passphrase']:
            save_setting(key, request.form[key])
    return '<script>alert("Ayarlar kaydedildi!"); window.location="/";</script>'

@app.route('/close', methods=['POST'])
def close_position():
    data   = request.get_json()
    symbol = data.get('symbol')
    side   = data.get('side')
    if not symbol or not side:
        return jsonify({"status":"error","message":"Eksik parametre"}), 200
    okx = get_okx()
    if not okx:
        return jsonify({"status":"error","message":"API bağlantısı yok"}), 200
    try:
        okx.load_markets()
        conn   = sqlite3.connect('bot_settings.db')
        cursor = conn.cursor()
        cursor.execute("SELECT total_contracts FROM active_positions WHERE symbol=?", (symbol,))
        row = cursor.fetchone()
        conn.close()
        if not row or not row[0]:
            return jsonify({"status":"error","message":"Pozisyon bulunamadı"}), 200
        total_contracts = row[0]
        close_side = 'sell' if side == 'buy' else 'buy'
        pos_side   = 'long' if side == 'buy' else 'short'
        okx.create_market_order(
            symbol=symbol, side=close_side, amount=total_contracts,
            params={"posSide": pos_side, "reduceOnly": True}
        )
        print(f"🛑 MANUEL KAPATMA → {symbol} | {total_contracts} kontrat")
        conn2  = sqlite3.connect('bot_settings.db')
        cur2   = conn2.cursor()
        cur2.execute("DELETE FROM active_positions WHERE symbol=?", (symbol,))
        conn2.commit()
        conn2.close()
        return jsonify({"status":"success","message":f"{symbol.replace('/USDT:USDT','')} kapatıldı"}), 200
    except Exception as e:
        print(f"❌ Manuel kapatma hatası: {e}")
        return jsonify({"status":"error","message":str(e)}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.data.decode('utf-8')
    print(f"📡 [SİNYAL] Gelen: {payload}")
    try:
        data = json.loads(payload)
    except:
        print("❌ JSON parse hatası")
        return jsonify({"status":"error","message":"Gecersiz JSON"}), 200

    raw_symbol    = data.get('symbol')
    side          = data.get('side', 'buy')
    step          = int(data.get('step', 1))
    current_price = float(data.get('price', 0))

    if not raw_symbol or not current_price:
        print("❌ Eksik veri")
        return jsonify({"status":"error","message":"Eksik veri"}), 200

    symbol = raw_symbol.replace('.P','').replace('-','').replace('_','').strip()
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
        return jsonify({"status":"error","message":"API anahtarlari eksik"}), 200

    conn   = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT side, last_entry_price, current_step, avg_entry_price, total_contracts FROM active_positions WHERE symbol=?", (symbol,))
    position = cursor.fetchone()

    if position:
        pos_side, last_entry_price, current_step, avg_entry_price, total_contracts = position

        if step == 1 and current_step >= 1:
            print(f"⛔ STEP 1 Engeli: {symbol} zaten aktif (step:{current_step})")
            conn.close()
            return jsonify({"status":"ignored","message":"Pozisyon zaten açık"}), 200

        if step == current_step:
            print(f"⛔ Aynı Step Engeli: step:{step} zaten işlendi")
            conn.close()
            return jsonify({"status":"ignored","message":f"Step {step} zaten işlendi"}), 200

        if step > 1:
            if side == 'buy' and current_price >= last_entry_price:
                conn.close()
                return jsonify({"status":"ignored","message":"LONG DCA engeli"}), 200
            if side == 'sell' and current_price <= last_entry_price:
                conn.close()
                return jsonify({"status":"ignored","message":"SHORT DCA engeli"}), 200
            price_diff_pct = abs(current_price - last_entry_price) / last_entry_price * 100
            if price_diff_pct < min_distance_filter:
                conn.close()
                return jsonify({"status":"ignored","message":"Mesafe engeli"}), 200

    try:
        okx.load_markets()
        try:
            okx.set_margin_mode('cross', symbol)
        except Exception as e:
            print(f"ℹ️ Margin: {e}")
        try:
            okx.set_leverage(10, symbol, {'mgnMode':'cross','posSide':'long'})
            okx.set_leverage(10, symbol, {'mgnMode':'cross','posSide':'short'})
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
            final_qty = math.floor(calculated_qty * (10**d)) / (10**d)
        else:
            final_qty = math.floor(calculated_qty)

        if final_qty <= 0:
            final_qty = min_qty if min_qty else 1

        pos_side = "long" if side == "buy" else "short"
        print(f"🚀 EMİR → {symbol} | {side.upper()} | {final_qty} kontrat | ${allocated_usd}")

        order = okx.create_market_order(
            symbol=symbol, side=side, amount=final_qty,
            params={"posSide": pos_side}
        )
        print(f"✅ İŞLEM AÇILDI! Order ID: {order.get('id','N/A')}")

        if not position:
            cursor.execute(
                "INSERT INTO active_positions (symbol,side,last_entry_price,current_step,avg_entry_price,total_contracts) VALUES (?,?,?,?,?,?)",
                (symbol, side, current_price, step, current_price, final_qty)
            )
        else:
            old_avg   = avg_entry_price or current_price
            old_total = total_contracts or 0
            new_total = old_total + final_qty
            new_avg   = ((old_avg * old_total) + (current_price * final_qty)) / new_total
            cursor.execute(
                "UPDATE active_positions SET last_entry_price=?,current_step=?,avg_entry_price=?,total_contracts=? WHERE symbol=?",
                (current_price, step, new_avg, new_total, symbol)
            )
            print(f"📐 Ort.Fiyat:{new_avg:.4f} | Toplam:{new_total} kontrat")

        conn.commit()
        conn.close()
        return jsonify({"status":"success","order_id":order.get('id')}), 200

    except Exception as e:
        conn.close()
        print(f"❌ BORSA HATASI: {str(e)}")
        return jsonify({"status":"error","okx_error":str(e)}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f"🌐 Sunucu başlatılıyor → port {port}")
    app.run(host='0.0.0.0', port=port)
