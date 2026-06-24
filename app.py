import sqlite3
import json
from flask import Flask, request, jsonify, render_template_string
import ccxt
import traceback
import math

app = Flask(__name__)

# --- VERİTABANI ALTYAPISI ---
def init_db():
    conn = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_positions (
            symbol TEXT PRIMARY KEY,
            highest_step INTEGER,
            lowest_step INTEGER,
            entry_price REAL,
            tp1_hit INTEGER DEFAULT 0,
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
        total = row[0] if row[0] else 0
        wins = row[1] if row[1] else 0
        total_pnl = row[2] if row[2] else 0.0
        win_rate = (wins / total * 100) if total > 0 else 0
    except:
        total, win_rate, total_pnl = 0, 0, 0.0
    finally:
        conn.close()
    return total, win_rate, total_pnl

init_db()

def get_okx():
    api_key = get_setting('api_key', '').strip()
    secret = get_setting('secret', '').strip()
    passphrase = get_setting('passphrase', '').strip()
    if not api_key or not secret or not passphrase:
        return None
    return ccxt.okx({
        'apiKey': api_key,
        'secret': secret,
        'password': passphrase,
        'options': {'defaultType': 'swap'},
        'enableRateLimit': True
    })

@app.route('/', methods=['GET'])
def dashboard():
    total, win_rate, total_pnl = get_stats()
    context = {
        'api_key': get_setting('api_key', ''),
        'secret': get_setting('secret', ''),
        'passphrase': get_setting('passphrase', ''),
        'l1_usd': get_setting('l1_usd', '40'),
        'd1_usd': get_setting('d1_usd', '60'),
        'd2_usd': get_setting('d2_usd', '90'),
        'd3_usd': get_setting('d3_usd', '135'),
        'd4_usd': get_setting('d4_usd', '202.5'),
        'min_dist': get_setting('min_dist', '2.0'),
        'tp1_pct': get_setting('tp1_pct', '1.5'),
        'tp1_qty': get_setting('tp1_qty', '50'),
        'tp2_pct': get_setting('tp2_pct', '3.0'),
        'tp2_qty': get_setting('tp2_qty', '50'),
        'total': total,
        'win_rate': f"{win_rate:.1f}%",
        'pnl': f"{total_pnl:.2f} USDT"
    }
    # Basit bir HTML şablonu (panel tasarımı bozulmasın diye)
    html_template = '''
    <!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>OKX DCA Bot</title>
    <style>body { font-family: sans-serif; background: #121214; color: #fff; padding: 10px; }</style></head>
    <body><h3>🤖 TEST MODU AKTİF</h3><form action="/save" method="POST">
    <label>API Key:</label><input type="text" name="api_key" value="{{api_key}}"><br>
    <label>Secret:</label><input type="password" name="secret" value="{{secret}}"><br>
    <label>Passphrase:</label><input type="password" name="passphrase" value="{{passphrase}}"><br>
    <button type="submit">GÜNCELLE</button></form></body></html>
    '''
    return render_template_string(html_template, **context)

@app.route('/save', methods=['POST'])
def save():
    for key in request.form:
        save_setting(key, request.form[key])
    return '<script>alert("Ayarlar basariyla islendi!"); window.location="/";</script>'

# --- WEBHOOK KAPISI (KORUMALAR GEÇİCİ OLARAK DEVRE DIŞI) ---
@app.route('/webhook', methods=['POST'])
def webhook():
    print(f"--- YENİ SİNYAL GELDİ: {request.data.decode('utf-8')} ---")
    try:
        data = json.loads(request.data)
    except:
        return jsonify({"status": "error", "message": "Gecersiz JSON"}), 200

    raw_symbol = data.get('symbol')
    side = data.get('side', 'buy')
    step = int(data.get('step', 1))
    current_price = float(data.get('price', 0))

    if not raw_symbol or not current_price:
        return jsonify({"status": "error", "message": "Eksik veri"}), 200

    symbol = raw_symbol.replace('.P', '').replace('-','').replace('_','').strip()
    if "USDT" in symbol and not ":" in symbol:
        symbol = symbol.replace("USDT", "/USDT:USDT")

    budgets = {1: 40.0, 2: 60.0, 3: 90.0, 4: 135.0, 5: 202.5}
    allocated_usd = budgets.get(step, 40.0)

    okx = get_okx()
    if not okx:
        print("API ANAHTARLARI VERİTABANINDA BULUNAMADI!")
        return jsonify({"status": "error", "message": "API eksik"}), 200

    try:
        okx.load_markets()
        
        try:
            okx.set_margin_mode('cross', symbol)
        except Exception as e:
            print(f"Margin modu ayarlanamadi: {e}")
        try:
            okx.set_leverage(10, symbol, {'mgnMode': 'cross'})
        except Exception as e:
            print(f"Leverage ayarlanamadi: {e}")

        market = okx.market(symbol)
        total_position_value = allocated_usd * 10
        contract_size = market['contractSize']
        
        calculated_qty = total_position_value / (current_price * contract_size)
        min_qty = market['limits']['amount']['min']
        
        if calculated_qty < min_qty:
            calculated_qty = min_qty
            
        precision = market['precision']['amount']
        if precision is not None:
            d = int(math.log10(1/precision)) if precision > 0 else 0
            final_qty = math.floor(calculated_qty * (10 ** d)) / (10 ** d)
        else:
            final_qty = math.floor(calculated_qty)

        if final_qty <= 0:
            final_qty = 1

        print(f"ZORLU EMİR GÖNDERİLİYOR -> {symbol} | Miktar: {final_qty} kontrat")
        
        order = okx.create_market_order(symbol=symbol, side=side, amount=final_qty)
        print("BORSADA İŞLEM BAŞARIYLA AÇILDI!")
        return jsonify({"status": "success", "message": "Islem iletildi"}), 200

    except Exception as e:
        print(f"OKX EMİR HATASI GERÇEK DETAY: {str(e)}")
        return jsonify({"status": "error", "okx_error": str(e)}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
