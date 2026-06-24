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

init_db()

# --- OKX API BAĞLANTI MOTORU ---
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

# --- MOBİL PANEL ---
@app.route('/', methods=['GET'])
def dashboard():
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
    }
    html_template = '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>OKX Kontrol Paneli</title>
        <style>
            body { font-family: sans-serif; background: #121214; color: #fff; padding: 10px; margin: 0; }
            .card { background: #1a1a1e; padding: 15px; border-radius: 8px; margin-bottom: 15px; }
            label { display: block; margin: 8px 0 2px; color: #aaa; font-size: 0.8rem; }
            input { width: 100%; padding: 10px; background: #26262b; border: 1px solid #3a3a42; border-radius: 4px; color: #fff; box-sizing: border-box; }
            button { width: 100%; padding: 14px; background: #4caf50; border: none; border-radius: 4px; color: #fff; font-weight: bold; margin-top: 10px; }
        </style>
    </head>
    <body>
        <h3 style="text-align: center; color: #4caf50;">🤖 DCA BOT SİSTEMİ</h3>
        <form action="/save" method="POST">
            <div class="card">
                <label>API Key:</label><input type="text" name="api_key" value="{{api_key}}">
                <label>Secret Key:</label><input type="password" name="secret" value="{{secret}}">
                <label>Passphrase:</label><input type="password" name="passphrase" value="{{passphrase}}">
            </div>
            <div class="card">
                <label>LONG 1 ($):</label><input type="number" name="l1_usd" value="{{l1_usd}}">
                <label>DCA 1 ($):</label><input type="number" name="d1_usd" value="{{d1_usd}}">
                <label>DCA 2 ($):</label><input type="number" name="d2_usd" value="{{d2_usd}}">
                <label>DCA 3 ($):</label><input type="number" name="d3_usd" value="{{d3_usd}}">
                <label>DCA 4 ($):</label><input type="number" name="d4_usd" value="{{d4_usd}}">
                <label>Min Mesafe (%):</label><input type="number" step="0.1" name="min_dist" value="{{min_dist}}">
            </div>
            <button type="submit">AYARLARI KAYDET</button>
        </form>
    </body>
    </html>
    '''
    return render_template_string(html_template, **context)

@app.route('/save', methods=['POST'])
def save():
    for key in request.form:
        save_setting(key, request.form[key])
    return '<script>alert("Ayarlar Basariyla Kaydedildi!"); window.location="/";</script>'

# --- WEBHOOK KAPISI ---
@app.route('/webhook', methods=['POST'])
def webhook():
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

    budgets = {
        1: float(get_setting('l1_usd', 40)),
        2: float(get_setting('d1_usd', 60)),
        3: float(get_setting('d2_usd', 90)),
        4: float(get_setting('d3_usd', 135)),
        5: float(get_setting('d4_usd', 202.5))
    }
    allocated_usd = budgets.get(step, 40.0)

    okx = get_okx()
    if not okx:
        return jsonify({"status": "error", "message": "API eksik"}), 200

    try:
        okx.load_markets()
        
        # 1. Kaldıraç ve Mod Dayatma
        try:
            okx.set_margin_mode('cross', symbol)
        except:
            pass
        try:
            okx.set_leverage(10, symbol, {'mgnMode': 'cross'})
        except:
            pass

        # 2. BORSANIN KURALLARINI ÖĞRENME SEKANSI
        market = okx.market(symbol)
        
        # OKX vadeli işlemlerde emir adet bazında değil "kontrat (contract)" bazında gönderilir.
        # 10x kaldıraç dahil toplam pozisyon büyüklüğü hesaplanır:
        total_position_value = allocated_usd * 10
        
        # Borsanın 1 kontratının kaç adet coine denk geldiğini buluyoruz (contractSize)
        contract_size = market['contractSize']
        
        # Kaç adet kontrat almamız gerektiğini hesaplıyoruz:
        calculated_qty = total_position_value / (current_price * contract_size)
        
        # Borsanın izin verdiği minimum kontrat sınırını kontrol ediyoruz
        min_qty = market['limits']['amount']['min']
        
        # Hesaplanan kontrat adedi, borsanın minimum sınırından küçükse zorunlu olarak minimum sınıra eşitliyoruz
        if calculated_qty < min_qty:
            calculated_qty = min_qty
            
        # Kontrat adedini borsanın basamak hassasiyetine göre aşağı yuvarlıyoruz (Örn: 1.23 -> 1)
        precision = market['precision']['amount']
        
        if precision is not None:
            # OKX kontratları genelde tam sayıdır (0 hassasiyet), hassasiyete göre güvenli yuvarlama:
            d = int(math.log10(1/precision)) if precision > 0 else 0
            final_qty = math.floor(calculated_qty * (10 ** d)) / (10 ** d)
        else:
            final_qty = math.floor(calculated_qty)

        if final_qty <= 0:
            final_qty = 1 # Güvenlik sınırı

        print(f"EMİR HAZIRLANDI -> Sembol: {symbol}, Hesaplanan Kontrat: {final_qty}")

        # 3. Kusursuz Formatlanmış Emri Gönderme
        order = okx.create_market_order(
            symbol=symbol,
            side=side,
            amount=final_qty
        )
        
        print("BORSADA İŞLEM BAŞARIYLA AÇILDI!")
        return jsonify({"status": "success", "message": f"Islem {final_qty} kontrat olarak acildi!"}), 200

    except Exception as e:
        print(f"KOD İÇİ HATA DETAYI: {str(e)}")
        return jsonify({"status": "error", "okx_error": str(e)}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
