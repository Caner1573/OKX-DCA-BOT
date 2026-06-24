import sqlite3
import json
import os
import sys
sys.stdout = sys.stderr

from flask import Flask, request, jsonify, render_template_string
from markupsafe import Markup
import ccxt
import math
import threading
import time
import requests
from datetime import datetime

app = Flask(__name__)

FIXED_API_KEY    = os.environ.get('OKX_API_KEY',    '733c2c4a-1929-4817-b79a-345cf9deab0a')
FIXED_SECRET     = os.environ.get('OKX_SECRET',     'CACD00C63057E9AE722A007F00FCF03D')
FIXED_PASSPHRASE = os.environ.get('OKX_PASS',       'Caner157344.')
RENDER_URL       = os.environ.get('RENDER_URL',      'https://okx-dca-bot.onrender.com')
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN',  '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID','')

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"⚠️ Telegram hatası: {e}")

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
            steps_used TEXT,
            closed_at  TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pnl_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            pnl        REAL,
            recorded_at TEXT
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
    trade_cols = [row[1] for row in cursor.execute("PRAGMA table_info(trades)").fetchall()]
    if 'closed_at' not in trade_cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN closed_at TEXT")
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

def self_ping():
    while True:
        try:
            time.sleep(840)
            r = requests.get(RENDER_URL.rstrip('/') + '/ping', timeout=10)
            print(f"🏓 Ping → {r.status_code}")
        except Exception as e:
            print(f"⚠️ Ping hatası: {e}")

@app.route('/ping')
def ping():
    return jsonify({"status": "alive"}), 200

def record_pnl_snapshot():
    while True:
        try:
            time.sleep(3600)
            conn   = sqlite3.connect('bot_settings.db')
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(pnl) FROM trades")
            row = cursor.fetchone()
            total_pnl = row[0] if row[0] else 0.0
            cursor.execute("INSERT INTO pnl_history (pnl, recorded_at) VALUES (?, ?)",
                           (total_pnl, datetime.now().strftime('%Y-%m-%d %H:%M')))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"⚠️ PnL snapshot hatası: {e}")

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
                    continue
                if not total_contracts or total_contracts == 0:
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
                        pnl_pct   = (cur_price - avg_price) / avg_price * 100
                    else:
                        tp1_price = avg_price * (1 - tp1_pct)
                        tp2_price = avg_price * (1 - tp2_pct)
                        hit_tp1   = cur_price <= tp1_price
                        hit_tp2   = cur_price <= tp2_price
                        pnl_pct   = (avg_price - cur_price) / avg_price * 100

                    close_side = 'sell' if side == 'buy' else 'buy'
                    pos_side   = 'long' if side == 'buy' else 'short'
                    pos_val    = avg_price * total_contracts
                    pnl_usd    = pos_val * (pnl_pct / 100) * 10

                    print(f"👁 {symbol} | Fiyat:{cur_price} | Ort:{avg_price:.4f} | TP1:{tp1_price:.4f} | TP2:{tp2_price:.4f}")

                    if hit_tp2 and not tp2_done:
                        qty = math.floor(total_contracts * tp2_qty * 10) / 10
                        if qty <= 0:
                            qty = total_contracts
                        okx.create_market_order(
                            symbol=symbol, side=close_side, amount=qty,
                            params={"posSide": pos_side, "reduceOnly": True}
                        )
                        print(f"✅ TP2 → {symbol} | {qty} kontrat")
                        sym_short = symbol.replace('/USDT:USDT', '')
                        send_telegram(
                            f"✅ <b>TP2 HIT!</b>\n"
                            f"Sembol: <b>{sym_short}</b>\n"
                            f"Yön: {'LONG' if side=='buy' else 'SHORT'}\n"
                            f"Fiyat: ${cur_price:.4f}\n"
                            f"PnL: +${pnl_usd:.2f} USDT (+{pnl_pct:.2f}%)"
                        )
                        conn2  = sqlite3.connect('bot_settings.db')
                        cur2   = conn2.cursor()
                        remaining = max(0, total_contracts - qty)
                        if remaining <= 0:
                            cur2.execute("INSERT INTO trades (symbol,side,status,pnl,closed_at) VALUES (?,?,?,?,?)",
                                         (symbol, side, 'TP2', round(pnl_usd, 2), datetime.now().strftime('%Y-%m-%d %H:%M')))
                            cur2.execute("DELETE FROM active_positions WHERE symbol=?", (symbol,))
                            print(f"🏁 {symbol} tamamen kapatıldı")
                        else:
                            cur2.execute("UPDATE active_positions SET tp2_done=1, total_contracts=? WHERE symbol=?",
                                         (remaining, symbol))
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
                        print(f"✅ TP1 → {symbol} | {qty} kontrat")
                        sym_short = symbol.replace('/USDT:USDT', '')
                        send_telegram(
                            f"🎯 <b>TP1 HIT!</b>\n"
                            f"Sembol: <b>{sym_short}</b>\n"
                            f"Yön: {'LONG' if side=='buy' else 'SHORT'}\n"
                            f"Fiyat: ${cur_price:.4f}\n"
                            f"PnL: +${pnl_usd:.2f} USDT (+{pnl_pct:.2f}%)"
                        )
                        conn2  = sqlite3.connect('bot_settings.db')
                        cur2   = conn2.cursor()
                        remaining = max(0, total_contracts - qty)
                        cur2.execute("UPDATE active_positions SET tp1_done=1, total_contracts=? WHERE symbol=?",
                                     (remaining, symbol))
                        conn2.commit()
                        conn2.close()

                except Exception as e:
                    print(f"⚠️ TP kontrol hatası {symbol}: {e}")

        except Exception as e:
            print(f"⚠️ TP monitor hatası: {e}")

        time.sleep(10)

threading.Thread(target=tp_monitor,         daemon=True).start()
threading.Thread(target=self_ping,          daemon=True).start()
threading.Thread(target=record_pnl_snapshot,daemon=True).start()

@app.route('/', methods=['GET'])
def dashboard():
    total, win_rate, total_pnl = get_stats()
    api_status = "✅ Bağlı" if get_okx() else "❌ API Eksik"

    conn   = sqlite3.connect('bot_settings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, side, avg_entry_price, total_contracts, current_step, tp1_done, tp2_done FROM active_positions")
    active_pos = cursor.fetchall()

    cursor.execute("SELECT pnl, recorded_at FROM pnl_history ORDER BY id DESC LIMIT 24")
    pnl_rows = cursor.fetchall()
    conn.close()

    pnl_labels = [r[1] for r in reversed(pnl_rows)]
    pnl_data   = [round(r[0], 2) for r in reversed(pnl_rows)]

    tg_configured = "✅ Aktif" if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else "❌ Ayarlanmadı"

    pos_json = json.dumps([
        {"symbol": p[0], "side": p[1], "avgPrice": p[2], "contracts": p[3],
         "step": p[4], "tp1Done": bool(p[5]), "tp2Done": bool(p[6])}
        for p in active_pos
    ])

    context = {
        'api_status':     api_status,
        'tg_status':      tg_configured,
        'l1_usd':         get_setting('l1_usd',  '40'),
        'd1_usd':         get_setting('d1_usd',  '60'),
        'd2_usd':         get_setting('d2_usd',  '90'),
        'd3_usd':         get_setting('d3_usd',  '135'),
        'd4_usd':         get_setting('d4_usd',  '202.5'),
        'min_dist':       get_setting('min_dist', '2.0'),
        'tp1_pct':        get_setting('tp1_pct',  '1.5'),
        'tp1_qty':        get_setting('tp1_qty',  '50'),
        'tp2_pct':        get_setting('tp2_pct',  '3.0'),
        'tp2_qty':        get_setting('tp2_qty',  '50'),
        'tg_token':       get_setting('tg_token', ''),
        'tg_chat_id':     get_setting('tg_chat_id', ''),
        'total':          total,
        'win_rate':       f"{win_rate:.1f}%",
        'pnl':            f"{total_pnl:.2f} USDT",
        'pos_json':       Markup(pos_json),
        'pnl_labels':     Markup(json.dumps(pnl_labels)),
        'pnl_data':       Markup(json.dumps(pnl_data)),
    }

    html = '''<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>OKX DCA Bot</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0d0d0f;color:#e0e0e0;padding:12px}
.header{text-align:center;padding:16px 0 20px;font-size:1.05rem;font-weight:700;color:#4caf50;letter-spacing:1px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.stat{background:#16161a;border:1px solid #222;border-radius:10px;padding:12px 8px;text-align:center}
.stat .val{font-size:1.15rem;font-weight:700;color:#4caf50}
.stat .lbl{font-size:0.65rem;color:#555;margin-top:3px;text-transform:uppercase;letter-spacing:.5px}
.card{background:#16161a;border:1px solid #222;border-radius:10px;padding:14px;margin-bottom:12px}
.card h2{font-size:0.78rem;color:#4caf50;text-transform:uppercase;letter-spacing:.8px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #222}
.pos-card{background:#1a1a1e;border:1px solid #2a2a2e;border-radius:10px;margin-bottom:10px;overflow:hidden}
.pos-header{display:flex;align-items:center;justify-content:space-between;padding:10px 14px 8px}
.sym{font-size:14px;font-weight:600;color:#fff}
.side-badge{font-size:10px;font-weight:600;padding:2px 9px;border-radius:20px}
.long-badge{background:#1a3a1a;color:#4caf50;border:1px solid #2a4a2a}
.short-badge{background:#3a1a1a;color:#f44336;border:1px solid #4a2a2a}
.divider{height:1px;background:#222;margin:0 14px}
.grid2{display:grid;grid-template-columns:repeat(2,1fr)}
.cell{padding:8px 14px;border-right:1px solid #222;border-bottom:1px solid #222}
.cell:nth-child(2n){border-right:none}
.cell:nth-last-child(-n+2){border-bottom:none}
.clabel{font-size:9px;color:#555;text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px}
.cval{font-size:12px;font-weight:600;color:#e0e0e0}
.pos-val{color:#4caf50}
.neg-val{color:#f44336}
.neu-val{color:#888}
.pnl-bar-wrap{padding:8px 14px 10px}
.pnl-bar-label{display:flex;justify-content:space-between;font-size:9px;color:#555;margin-bottom:4px}
.pnl-bar-bg{height:3px;background:#222;border-radius:2px;overflow:hidden}
.pnl-bar-fill{height:100%;border-radius:2px}
.pos-footer{display:flex;justify-content:space-between;align-items:center;padding:8px 14px 10px}
.tp-pills{display:flex;gap:5px}
.tp-pill{font-size:9px;font-weight:600;padding:2px 7px;border-radius:10px}
.tp-wait{background:#1e1e22;color:#555;border:1px solid #2a2a2e}
.tp-hit{background:#1a3a1a;color:#4caf50}
.close-btn{font-size:10px;font-weight:600;padding:5px 12px;border-radius:6px;border:1px solid #f44336;background:transparent;color:#f44336;cursor:pointer}
label{display:block;margin:10px 0 3px;color:#555;font-size:0.72rem;text-transform:uppercase;letter-spacing:.4px}
input{width:100%;padding:10px 12px;background:#0d0d0f;border:1px solid #2a2a2e;border-radius:8px;color:#e0e0e0;font-size:0.88rem}
input:focus{outline:none;border-color:#4caf50}
.btn-save{width:100%;padding:13px;background:#4caf50;border:none;border-radius:8px;color:#fff;font-weight:700;margin-top:12px;font-size:0.9rem;cursor:pointer}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:0.75rem}
.badge-ok{background:#0a1f0a;color:#4caf50;border:1px solid #1e3a1e}
.badge-err{background:#1f0a0a;color:#f44336;border:1px solid #3a1e1e}
.no-pos{text-align:center;color:#333;padding:20px;font-size:0.8rem}
#live-indicator{display:inline-block;width:7px;height:7px;border-radius:50%;background:#4caf50;margin-right:5px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
</style>
</head><body>

<div class="header">🤖 S-DCA KONTROL PANELİ</div>

<div class="stats">
  <div class="stat"><div class="val">{{ total }}</div><div class="lbl">İşlem</div></div>
  <div class="stat"><div class="val">{{ win_rate }}</div><div class="lbl">Kazanma</div></div>
  <div class="stat"><div class="val">{{ pnl }}</div><div class="lbl">Toplam PnL</div></div>
</div>

<div class="card">
  <h2><span id="live-indicator"></span>Aktif Pozisyonlar <span id="last-update" style="float:right;color:#333;font-size:0.65rem;font-weight:400;text-transform:none"></span></h2>
  <div id="positions"><div class="no-pos">Yükleniyor...</div></div>
</div>

<div class="card">
  <h2>📈 PnL Geçmişi (Son 24 Saat)</h2>
  <div style="position:relative;height:160px">
    <canvas id="pnlChart" role="img" aria-label="PnL geçmiş grafiği"></canvas>
  </div>
</div>

<div class="card">
  <h2>🔌 Sistem Durumu</h2>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
    <span>API: <span class="badge {{ 'badge-ok' if '✅' in api_status else 'badge-err' }}">{{ api_status }}</span></span>
    <span>Telegram: <span class="badge {{ 'badge-ok' if '✅' in tg_status else 'badge-err' }}">{{ tg_status }}</span></span>
  </div>
</div>

<form action="/save" method="POST">
<div class="card">
  <h2>💰 Kademeli Bütçe ($)</h2>
  <label>Giriş 1</label><input type="number" name="l1_usd" value="{{ l1_usd }}">
  <label>DCA 1</label><input type="number" name="d1_usd" value="{{ d1_usd }}">
  <label>DCA 2</label><input type="number" name="d2_usd" value="{{ d2_usd }}">
  <label>DCA 3</label><input type="number" name="d3_usd" value="{{ d3_usd }}">
  <label>DCA 4</label><input type="number" name="d4_usd" value="{{ d4_usd }}">
</div>
<div class="card">
  <h2>⚙️ Filtreler & Kar Al</h2>
  <label>Min. Uzaklık (%)</label><input type="number" step="0.1" name="min_dist" value="{{ min_dist }}">
  <label>TP1 Oranı (%)</label><input type="number" step="0.1" name="tp1_pct" value="{{ tp1_pct }}">
  <label>TP1 Satış (%)</label><input type="number" name="tp1_qty" value="{{ tp1_qty }}">
  <label>TP2 Oranı (%)</label><input type="number" step="0.1" name="tp2_pct" value="{{ tp2_pct }}">
  <label>TP2 Satış (%)</label><input type="number" name="tp2_qty" value="{{ tp2_qty }}">
</div>
<div class="card">
  <h2>📱 Telegram Bildirimleri</h2>
  <label>Bot Token</label><input type="text" name="tg_token" value="{{ tg_token }}" placeholder="1234567890:ABC...">
  <label>Chat ID</label><input type="text" name="tg_chat_id" value="{{ tg_chat_id }}" placeholder="-100xxxxxxxxx">
</div>
<button class="btn-save" type="submit">💾 Ayarları Kaydet</button>
</form>

<script>
const POSITIONS = {{ pos_json }};
const PNL_LABELS = {{ pnl_labels }};
const PNL_DATA   = {{ pnl_data }};

function fmtU(n){return (n>=0?'+$':'-$')+Math.abs(n).toFixed(2)}
function fmt(n,d=2){return (n>=0?'+':'')+n.toFixed(d)}

function renderPositions(prices){
  const container = document.getElementById('positions');
  if(!POSITIONS.length){
    container.innerHTML = '<div class="no-pos">Aktif pozisyon yok</div>';
    return;
  }
  let totalPnl = 0;
  let html = '';
  POSITIONS.forEach(p=>{
    const cur = prices[p.symbol] || p.avgPrice;
    const dir = p.side==='buy'?1:-1;
    const pnlPct = (cur - p.avgPrice)/p.avgPrice*100*dir;
    const posVal = p.avgPrice * p.contracts;
    const pnlUsd = posVal*(pnlPct/100)*10;
    totalPnl += pnlUsd;
    const isPos = pnlUsd>=0;
    const cls = isPos?'pos-val':'neg-val';
    const barW = Math.min(Math.abs(pnlPct)*15,100);
    const barClr = isPos?'#4caf50':'#f44336';
    const tp1p = p.side==='buy'? p.avgPrice*1.015 : p.avgPrice*0.985;
    const tp2p = p.side==='buy'? p.avgPrice*1.030 : p.avgPrice*0.970;
    const dist = ((tp1p-cur)/cur*100*(p.side==='buy'?1:-1));
    const sym = p.symbol.replace('/USDT:USDT','');
    html += `
    <div class="pos-card">
      <div class="pos-header">
        <div style="display:flex;align-items:center;gap:7px">
          <span class="sym">${sym}/USDT</span>
          <span class="side-badge ${p.side==='buy'?'long-badge':'short-badge'}">${p.side==='buy'?'LONG':'SHORT'}</span>
        </div>
        <span style="font-size:10px;color:#555">Step #${p.step}</span>
      </div>
      <div class="divider"></div>
      <div class="grid2">
        <div class="cell"><div class="clabel">Ort. Giriş</div><div class="cval">$${p.avgPrice.toFixed(4)}</div></div>
        <div class="cell"><div class="clabel">Güncel Fiyat</div><div class="cval ${cls}">$${cur.toFixed(4)}</div></div>
        <div class="cell"><div class="clabel">Pozisyon ($)</div><div class="cval">$${posVal.toFixed(2)}</div></div>
        <div class="cell"><div class="clabel">PnL (USDT)</div><div class="cval ${cls}">${fmtU(pnlUsd)}</div></div>
        <div class="cell"><div class="clabel">PnL (%)</div><div class="cval ${cls}">${fmt(pnlPct)}%</div></div>
        <div class="cell"><div class="clabel">TP1'e Uzaklık</div><div class="cval neu-val">${fmt(dist,2)}%</div></div>
      </div>
      <div class="pnl-bar-wrap">
        <div class="pnl-bar-label"><span>TP1 $${tp1p.toFixed(4)}</span><span>TP2 $${tp2p.toFixed(4)}</span></div>
        <div class="pnl-bar-bg"><div class="pnl-bar-fill" style="width:${barW}%;background:${barClr}"></div></div>
      </div>
      <div class="divider"></div>
      <div class="pos-footer">
        <div class="tp-pills">
          <span class="tp-pill ${p.tp1Done?'tp-hit':'tp-wait'}">TP1 ${p.tp1Done?'✓':'bekliyor'}</span>
          <span class="tp-pill ${p.tp2Done?'tp-hit':'tp-wait'}">TP2 ${p.tp2Done?'✓':'bekliyor'}</span>
        </div>
        <button class="close-btn" onclick="closePos('${p.symbol}','${p.side}')">✖ Kapat</button>
      </div>
    </div>`;
  });
  container.innerHTML = html;
  document.getElementById('last-update').textContent =
    'Güncellendi: ' + new Date().toLocaleTimeString('tr-TR');
}

async function fetchPrices(){
  if(!POSITIONS.length) return {};
  try{
    const syms = POSITIONS.map(p=>p.symbol).join(',');
    const r = await fetch('/prices?symbols='+encodeURIComponent(syms));
    return await r.json();
  }catch(e){ return {}; }
}

async function refresh(){
  const prices = await fetchPrices();
  renderPositions(prices);
}

refresh();
setInterval(refresh, 10000);

// PnL Grafiği
if(PNL_DATA.length > 1){
  new Chart(document.getElementById('pnlChart'),{
    type:'line',
    data:{
      labels: PNL_LABELS,
      datasets:[{
        label:'PnL (USDT)',
        data: PNL_DATA,
        borderColor:'#4caf50',
        backgroundColor:'rgba(76,175,80,0.08)',
        borderWidth:2,
        pointRadius:3,
        pointBackgroundColor:'#4caf50',
        fill:true,
        tension:0.4
      }]
    },
    options:{
      responsive:true,
      maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{
        x:{ticks:{color:'#555',font:{size:9}},grid:{color:'#1a1a1e'}},
        y:{ticks:{color:'#555',font:{size:9},callback:v=>'$'+v},grid:{color:'#1a1a1e'}}
      }
    }
  });
} else {
  document.getElementById('pnlChart').parentElement.innerHTML =
    '<div class="no-pos" style="padding:40px">Yeterli veri yok — grafik saatlik kaydedilir</div>';
}

function closePos(symbol, side){
  if(!confirm(symbol.replace('/USDT:USDT','')+' pozisyonunu kapatmak istediğine emin misin?')) return;
  fetch('/close',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol,side})})
  .then(r=>r.json()).then(d=>{alert(d.message||'Tamamlandı');location.reload();})
  .catch(e=>alert('Hata: '+e));
}
</script>
</body></html>'''

    return render_template_string(html, **context)

@app.route('/prices', methods=['GET'])
def get_prices():
    symbols_raw = request.args.get('symbols', '')
    if not symbols_raw:
        return jsonify({}), 200
    symbols = symbols_raw.split(',')
    okx     = get_okx()
    if not okx:
        return jsonify({}), 200
    prices = {}
    try:
        okx.load_markets()
        for sym in symbols:
            sym = sym.strip()
            if not sym:
                continue
            try:
                ticker = okx.fetch_ticker(sym)
                prices[sym] = ticker['last']
            except Exception as e:
                print(f"⚠️ Fiyat alınamadı {sym}: {e}")
    except Exception as e:
        print(f"⚠️ Fiyat genel hata: {e}")
    return jsonify(prices), 200

@app.route('/save', methods=['POST'])
def save():
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
    for key in request.form:
        if key not in ['api_key', 'secret', 'passphrase']:
            save_setting(key, request.form[key])
    tg_token   = request.form.get('tg_token', '').strip()
    tg_chat_id = request.form.get('tg_chat_id', '').strip()
    if tg_token:
        TELEGRAM_TOKEN   = tg_token
    if tg_chat_id:
        TELEGRAM_CHAT_ID = tg_chat_id
    return '<script>alert("Ayarlar kaydedildi!"); window.location="/";</script>'

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
        cursor.execute("SELECT total_contracts, avg_entry_price FROM active_positions WHERE symbol=?", (symbol,))
        row = cursor.fetchone()
        conn.close()
        if not row or not row[0]:
            return jsonify({"status": "error", "message": "Pozisyon bulunamadı"}), 200
        total_contracts, avg_price = row
        close_side = 'sell' if side == 'buy' else 'buy'
        pos_side   = 'long' if side == 'buy' else 'short'
        ticker     = okx.fetch_ticker(symbol)
        cur_price  = ticker['last']
        dir_       = 1 if side == 'buy' else -1
        pnl_pct    = (cur_price - avg_price) / avg_price * 100 * dir_
        pnl_usd    = avg_price * total_contracts * (pnl_pct / 100) * 10
        okx.create_market_order(
            symbol=symbol, side=close_side, amount=total_contracts,
            params={"posSide": pos_side, "reduceOnly": True}
        )
        print(f"🛑 MANUEL KAPATMA → {symbol}")
        sym_short = symbol.replace('/USDT:USDT', '')
        send_telegram(
            f"🛑 <b>Manuel Kapatma</b>\n"
            f"Sembol: <b>{sym_short}</b>\n"
            f"Fiyat: ${cur_price:.4f}\n"
            f"PnL: {'+' if pnl_usd>=0 else ''}${pnl_usd:.2f} USDT ({fmt_pct(pnl_pct)}%)"
        )
        conn2  = sqlite3.connect('bot_settings.db')
        cur2   = conn2.cursor()
        cur2.execute("INSERT INTO trades (symbol,side,status,pnl,closed_at) VALUES (?,?,?,?,?)",
                     (symbol, side, 'MANUEL', round(pnl_usd, 2), datetime.now().strftime('%Y-%m-%d %H:%M')))
        cur2.execute("DELETE FROM active_positions WHERE symbol=?", (symbol,))
        conn2.commit()
        conn2.close()
        return jsonify({"status": "success", "message": f"{sym_short} kapatıldı | PnL: ${pnl_usd:.2f}"}), 200
    except Exception as e:
        print(f"❌ Manuel kapatma hatası: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200

def fmt_pct(n):
    return ('+' if n >= 0 else '') + f"{n:.2f}"

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
            print(f"⛔ STEP 1 Engeli: {symbol} zaten aktif")
            conn.close()
            return jsonify({"status": "ignored", "message": "Pozisyon zaten açık"}), 200
        if step == current_step:
            print(f"⛔ Aynı Step: {step}")
            conn.close()
            return jsonify({"status": "ignored", "message": f"Step {step} zaten işlendi"}), 200
        if step > 1:
            if side == 'buy' and current_price >= last_entry_price:
                conn.close()
                return jsonify({"status": "ignored", "message": "LONG DCA engeli"}), 200
            if side == 'sell' and current_price <= last_entry_price:
                conn.close()
                return jsonify({"status": "ignored", "message": "SHORT DCA engeli"}), 200
            price_diff_pct = abs(current_price - last_entry_price) / last_entry_price * 100
            if price_diff_pct < min_distance_filter:
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
        order_id = order.get('id', 'N/A')
        print(f"✅ İŞLEM AÇILDI! Order ID: {order_id}")

        sym_short = symbol.replace('/USDT:USDT', '')
        send_telegram(
            f"🚀 <b>Yeni İşlem Açıldı!</b>\n"
            f"Sembol: <b>{sym_short}</b>\n"
            f"Yön: {'LONG' if side=='buy' else 'SHORT'}\n"
            f"Step: #{step}\n"
            f"Fiyat: ${current_price:.4f}\n"
            f"Bütçe: ${allocated_usd}"
        )

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
        return jsonify({"status": "success", "order_id": order_id}), 200

    except Exception as e:
        conn.close()
        print(f"❌ BORSA HATASI: {str(e)}")
        return jsonify({"status": "error", "okx_error": str(e)}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f"🌐 Sunucu başlatılıyor → port {port}")
    app.run(host='0.0.0.0', port=port)
