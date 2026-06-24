import sqlite3
import json
from flask import Flask, request, jsonify, render_template_string
import ccxt

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

# --- OKX API BAĞLANTI MOTORU ---
def get_okx():
    api_key = get_setting('api_key', '')
    secret = get_setting('secret', '')
    passphrase = get_setting('passphrase', '')
    if not api_key or not secret or not passphrase:
        return None
    return ccxt.okx({
        'apiKey': api_key,
        'secret': secret,
        'password': passphrase,
        'options': {'defaultType': 'swap'}
    })

# --- MOBİL PANEL TASARIMI (ANA SAYFA) ---
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

    html_template = '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>OKX Algoritmik Mükemmel Strateji</title>
        <style>
            body { font-family: sans-serif; background: #121214; color: #fff; padding: 10px; margin: 0; }
            .card { background: #1a1a1e; padding: 15px; border-radius: 8px; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.2); }
            h2 { color: #4caf50; font-size: 1rem; margin-top: 0; border-bottom: 1px solid #26262b; padding-bottom: 5px;}
            label { display: block; margin: 8px 0 2px; color: #aaa; font-size: 0.8rem; }
            input { width: 100%; padding: 10px; background: #26262b; border: 1px solid #3a3a42; border-radius: 4px; color: #fff; box-sizing: border-box; font-size: 0.9rem; }
            button { width: 100%; padding: 14px; background: #4caf50; border: none; border-radius: 4px; color: #fff; font-weight: bold; font-size: 1rem; margin-top: 10px; }
            .stat-box { display: flex; justify-content: space-between; background: #26262b; padding: 10px; border-radius: 4px; margin-bottom: 5px; font-size: 0.9rem;}
        </style>
    </head>
    <body>
        <h3 style="text-align: center; color: #4caf50;">🤖 S-DCA ÖZEL KONTROL PANELİ</h3>
        
        <form action="/save" method="POST">
            <div class="card">
                <h2>1. OKX API BAĞLANTISI</h2>
                <label>API Key:</label><input type="text" name="api_key" value="{{api_key}}">
                <label>Secret Key:</label><input type="password" name="secret" value="{{secret}}">
                <label>Passphrase:</label><input type="password" name="passphrase" value="{{passphrase}}">
            </div>

            <div class="card">
                <h2>2. KADEMELİ BÜTÇE AYARLARI ($)</h2>
                <label>LONG 1:</label><input type="number" name="l1_usd" value="{{l1_usd}}">
                <label>DCA 1 (1.5x):</label><input type="number" name="d1_usd" value="{{d1_usd}}">
                <label>DCA 2:</label><input type="number" name="d2_usd" value="{{d2_usd}}">
                <label>DCA 3:</label><input type="number" name="d3_usd" value="{{d3_usd}}">
                <label>DCA 4:</label><input type="number" name="d4_usd" value="{{d4_usd}}">
            </div>

            <div class="card">
                <h2>3. SPESİFİK AYARLAR & FILTRELER</h2>
                <label>Min. Fibo Uzaklık Filtresi (%):</label><input type="number" step="0.1" name="min_dist" value="{{min_dist}}">
                <label>TP 1 Oranı (%):</label><input type="number" step="0.1" name="tp1_pct" value="{{tp1_pct}}">
                <label>TP 1 Satış Oranı (%):</label><input type="number" name="tp1_qty" value="{{tp1_qty}}">
                <label>TP 2 Oranı (%):</label><input type="number" step="0.1" name="tp2_pct" value="{{tp2_pct}}">
                <label>TP 2 Satış Oranı (%):</label><input type="number" name="tp2_qty" value="{{tp2_qty}}">
            </div>
            <button type="submit">TÜM AYARLARI GÜNCELLE</button>
        </form>

        <div class="card" style="margin-top: 15px;">
            <h2>📊 PERFORMANS VE ANALİTİK TABLOSU</h2>
            <div class="stat-box"><span>Toplam İşlem:</span><strong>{{total}}</strong></div>
            <div class="stat-box"><span>Win Rate (Kazanma Oranı):</span><span style="color:#4caf50;">{{win_rate}}</span></div>
            <div class="stat-box"><span>Net Kazanç (PNL):</span><span style="color:#4caf50;">{{pnl}}</span></div>
        </div>
    </body>
    </html>
    '''
    return render_template_string(html_template, **context)

@app.route('/save', methods=['POST'])
def save():
    for key in request.form:
        save_setting(key, request.form[key])
    return '<script>alert("Tüm spesifik ayarlar başarıyla veritabanına işlendi!"); window.location="/";</script>'


# --- TRADINGVIEW ALARMLARINI KARŞILAYAN GÜVENLİ WEBHOOK KAPISI ---
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
    except:
        return jsonify({"status": "error", "message": "Gecersiz JSON paketi"}), 400

    symbol = data.get('symbol')
    side = data.get('side', 'buy')
    step = int(data.get('step', 1))
    current_price = float(data.get('price', 0))

    if not symbol or not current_price:
        return jsonify({"status": "error", "message": "Eksik veri (Symbol veya Price yok)"}), 400

    budgets = {
        1: float(get_setting('l1_usd', 40)),
        2: float(get_setting('d1_usd', 60)),
        3: float(get_setting('d2_usd', 90)),
        4: float(get_setting('d3_usd', 135)),
        5: float(get_setting('d4_usd', 202.5))
    }
    allocated_usd = budgets.get(step, 40.0)
    min_distance_filter = float(get_setting('min_dist', 2.0))

    okx = get_okx()
    if not okx:
        return jsonify({"status": "error", "message": "OKX API anahtarlari panelde eksik!"}), 400

    conn = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT highest_step, lowest_step, entry_price FROM active_positions WHERE symbol=?", (symbol,))
    position = cursor.fetchone()

    if position:
        highest_step, lowest_step, last_entry_price = position
        if side == 'buy' and step < lowest_step:
            conn.close()
            return jsonify({"status": "ignored", "message": "Kural 4 engeli aktif."})
        
        price_diff_pct = abs(current_price - last_entry_price) / last_entry_price * 100
        if price_diff_pct < min_distance_filter and step > 1:
            conn.close()
            return jsonify({"status": "ignored", "message": f"Mesafe engeli: %{price_diff_pct:.2f}"})

    try:
        okx.load_markets()
        order_qty = allocated_usd / current_price
        
        # OKX Piyasasina Göre Alim/Satim Emri Gönderimi
        order = okx.create_market_order(
            symbol=symbol,
            side=side,
            amount=order_qty
        )
        
        if not position:
            cursor.execute("INSERT INTO active_positions (symbol, highest_step, lowest_step, entry_price) VALUES (?, ?, ?, ?)",
                           (symbol, step, step, current_price))
        else:
            new_high = max(position[0], step)
            new_low = max(position[1], step)
            cursor.execute("UPDATE active_positions SET highest_step=?, lowest_step=?, entry_price=? WHERE symbol=?",
                           (new_high, new_low, current_price, symbol))
        
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Islem OKX borsasina iletildi."})

    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
