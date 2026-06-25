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
from datetime import datetime, timedelta

app = Flask(__name__)

FIXED_API_KEY    = os.environ.get('OKX_API_KEY',    '733c2c4a-1929-4817-b79a-345cf9deab0a')
FIXED_SECRET     = os.environ.get('OKX_SECRET',     'CACD00C63057E9AE722A007F00FCF03D')
FIXED_PASSPHRASE = os.environ.get('OKX_PASS',       'Caner157344.')
RENDER_URL       = os.environ.get('RENDER_URL',      'https://okx-dca-bot.onrender.com')
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN',  '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID','')

DB_PATH = 'bot_settings.db'

def get_db():
    """Tüm DB bağlantıları buradan geçer: WAL modu + uzun timeout ile kilitlenme sorununu engeller."""
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"⚠️ Telegram hatası: {e}")

def init_db():
    conn   = get_db()
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
            tp2_done          INTEGER DEFAULT 0,
            sl_active         INTEGER DEFAULT 0
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
        ('sl_active',       'INTEGER DEFAULT 0'),
        ('realized_pnl',    'REAL DEFAULT 0'),
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
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_setting(key, default):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cursor.fetchone()
    conn.close()
    val = row[0] if row else None
    if val is None or val == '':
        return default
    return val

def get_stats():
    conn = get_db()
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

def get_daily_stats():
    conn = get_db()
    cursor = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        cursor.execute("SELECT COUNT(*) FROM trades WHERE closed_at LIKE ?", (today + '%',))
        kapanan = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM trades WHERE status='TP1' AND closed_at LIKE ?", (today + '%',))
        tp1 = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM trades WHERE status='TP2' AND closed_at LIKE ?", (today + '%',))
        tp2 = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM trades WHERE status='SL_BREAKEVEN' AND closed_at LIKE ?", (today + '%',))
        sl = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM active_positions WHERE tp1_done=1 AND tp2_done=0")
        tp2_bekliyor = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM active_positions")
        acik = cursor.fetchone()[0] or 0
        acilan = kapanan + acik
    except:
        acilan, kapanan, acik, tp1, tp2, sl, tp2_bekliyor = 0, 0, 0, 0, 0, 0, 0
    finally:
        conn.close()
    return acilan, kapanan, acik, tp1, tp2, sl, tp2_bekliyor

def get_pnl_analytics(range_key):
    """
    trades tablosundan range_key'e göre gruplanmış PnL analitiği döndürür.
    24h -> saatlik noktalar (son 24 saat)
    diğerleri -> günlük satırlar (tarih, işlem sayısı, kazanan, kaybeden, pnl, kümülatif)
    """
    conn = get_db()
    cursor = conn.cursor()

    range_days_map = {
        '24h': 1, '7d': 7, '14d': 14, '30d': 30,
        '90d': 90, '180d': 180, '365d': 365, 'all': None
    }
    days = range_days_map.get(range_key, 7)

    try:
        if range_key == '24h':
            since = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M')
            cursor.execute("""
                SELECT strftime('%Y-%m-%d %H:00', closed_at) as bucket,
                       COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), SUM(pnl)
                FROM trades
                WHERE closed_at >= ?
                GROUP BY bucket
                ORDER BY bucket ASC
            """, (since,))
            raw = cursor.fetchall()
            rows = []
            cum = 0.0
            for bucket, cnt, wins, losses, pnl in raw:
                pnl = pnl or 0.0
                cum += pnl
                try:
                    dt = datetime.strptime(bucket, '%Y-%m-%d %H:00')
                    label = dt.strftime('%H:00')
                    sub = dt.strftime('%d %b')
                except:
                    label, sub = bucket, ''
                rows.append({
                    'label': label, 'sub': sub, 'trades': cnt,
                    'wins': wins or 0, 'losses': losses or 0,
                    'pnl': round(pnl, 2), 'cum': round(cum, 2),
                    'is_today': False
                })
        else:
            if days:
                since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
                cursor.execute("""
                    SELECT substr(closed_at,1,10) as d,
                           COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                           SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), SUM(pnl)
                    FROM trades
                    WHERE closed_at >= ?
                    GROUP BY d
                    ORDER BY d ASC
                """, (since,))
            else:
                cursor.execute("""
                    SELECT substr(closed_at,1,10) as d,
                           COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                           SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), SUM(pnl)
                    FROM trades
                    WHERE closed_at IS NOT NULL
                    GROUP BY d
                    ORDER BY d ASC
                """)
            raw = cursor.fetchall()
            rows = []
            cum = 0.0
            gun_isimleri = ['Pazartesi','Salı','Çarşamba','Perşembe','Cuma','Cumartesi','Pazar']
            today_str = datetime.now().strftime('%Y-%m-%d')
            for d, cnt, wins, losses, pnl in raw:
                if not d:
                    continue
                pnl = pnl or 0.0
                cum += pnl
                try:
                    dt = datetime.strptime(d, '%Y-%m-%d')
                    label = dt.strftime('%d %b')
                    sub = gun_isimleri[dt.weekday()]
                except:
                    label, sub = d, ''
                rows.append({
                    'label': label, 'sub': sub, 'trades': cnt,
                    'wins': wins or 0, 'losses': losses or 0,
                    'pnl': round(pnl, 2), 'cum': round(cum, 2),
                    'is_today': (d == today_str)
                })

        total_pnl    = rows[-1]['cum'] if rows else 0.0
        total_trades = sum(r['trades'] for r in rows)
        total_wins   = sum(r['wins'] for r in rows)
        win_rate     = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
        best_row     = max(rows, key=lambda r: r['pnl']) if rows else None
        worst_row    = min(rows, key=lambda r: r['pnl']) if rows else None

        return {
            'rows': list(reversed(rows)),
            'chart_labels': [r['label'] for r in rows],
            'chart_cum': [r['cum'] for r in rows],
            'total_pnl': round(total_pnl, 2),
            'total_trades': total_trades,
            'win_rate': round(win_rate, 1),
            'best': {'label': best_row['label'], 'pnl': best_row['pnl']} if best_row else None,
            'worst': {'label': worst_row['label'], 'pnl': worst_row['pnl']} if worst_row else None,
        }
    except Exception as e:
        print(f"⚠️ PnL analitik hatası: {e}")
        return {
            'rows': [], 'chart_labels': [], 'chart_cum': [],
            'total_pnl': 0, 'total_trades': 0, 'win_rate': 0,
            'best': None, 'worst': None
        }
    finally:
        conn.close()

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

def get_okx_positions():
    """OKX'ten gerçek pozisyon verilerini çeker"""
    try:
        okx = get_okx()
        if not okx:
            return {}
        positions = okx.fetch_positions()
        result = {}
        for pos in positions:
            if not pos or float(pos.get('contracts', 0) or 0) == 0:
                continue
            symbol     = pos.get('symbol', '')
            side       = pos.get('side', '')
            entry      = float(pos.get('entryPrice', 0) or 0)
            mark       = float(pos.get('markPrice', 0) or 0)
            pnl_usd    = float(pos.get('unrealizedPnl', 0) or 0)
            size_usd   = float(pos.get('notional', 0) or 0)
            pnl_pct    = float(pos.get('percentage', 0) or 0)
            contracts  = float(pos.get('contracts', 0) or 0)
            result[symbol] = {
                'entryPrice': entry,
                'markPrice':  mark,
                'pnlUsd':     round(pnl_usd, 2),
                'pnlPct':     round(pnl_pct, 2),
                'sizeUsd':    round(abs(size_usd), 2),
                'contracts':  contracts,
                'side':       side,
            }
        return result
    except Exception as e:
        print(f"⚠️ OKX pozisyon çekme hatası: {e}")
        return {}

def get_okx_position_for_symbol(okx, symbol):
    """Belirli bir sembol için OKX'teki gerçek pozisyonu döndürür (varsa), yoksa None."""
    try:
        positions = okx.fetch_positions([symbol])
        for p in positions:
            if p and float(p.get('contracts', 0) or 0) > 0:
                return p
    except Exception as e:
        print(f"⚠️ OKX pozisyon kontrol hatası {symbol}: {e}")
    return None

def get_previous_day_hl(okx, symbol):
    try:
        ohlcv = okx.fetch_ohlcv(symbol, timeframe='1d', limit=2)
        if not ohlcv or len(ohlcv) < 2:
            return None, None
        prev  = ohlcv[-2]
        fhigh = prev[2]
        flow  = prev[3]
        print(f"📊 Önceki gün H:{fhigh} L:{flow} ({symbol})")
        return fhigh, flow
    except Exception as e:
        print(f"⚠️ Günlük mum alınamadı {symbol}: {e}")
        return None, None

def calc_fibo_levels(side, fhigh, flow):
    diff = fhigh - flow
    if side == 'buy':
        return [
            flow + diff * 0.618,
            fhigh,
            flow + diff * 1.618,
            flow + diff * 2.000,
            flow + diff * 2.618,
        ]
    else:
        return [
            flow + diff * 2.000,
            flow + diff * 2.618,
            flow + diff * 3.000,
            flow + diff * 3.618,
        ]

def check_fibo_distance(side, fhigh, flow, min_dist_pct):
    levels = calc_fibo_levels(side, fhigh, flow)
    for i in range(len(levels) - 1):
        a    = levels[i]
        b    = levels[i + 1]
        dist = abs(b - a) / a * 100
        print(f"  Step{i+1}→Step{i+2}: {a:.6f} → {b:.6f} = %{dist:.2f}")
        if dist < min_dist_pct:
            print(f"  ❌ Fibo mesafe engeli: %{dist:.2f} < min %{min_dist_pct}")
            return False, i + 1, dist
    print(f"  ✅ Tüm fibo adımları min %{min_dist_pct} üstünde")
    return True, None, None

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
            conn   = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(pnl) FROM trades")
            row       = cursor.fetchone()
            total_pnl = row[0] if row[0] else 0.0
            cursor.execute("INSERT INTO pnl_history (pnl, recorded_at) VALUES (?, ?)",
                           (total_pnl, datetime.now().strftime('%Y-%m-%d %H:%M')))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"⚠️ PnL snapshot hatası: {e}")

def close_full_position_in_db(symbol, side, status, pnl_usd):
    """
    Bir pozisyonu trades tablosuna kaydeder ve active_positions'tan siler.
    Başarılı olursa True, DB hatası olursa False döner (asla sessiz geçmez).
    """
    try:
        conn2 = get_db()
        cur2  = conn2.cursor()
        cur2.execute("INSERT INTO trades (symbol,side,status,pnl,closed_at) VALUES (?,?,?,?,?)",
                     (symbol, side, status, round(pnl_usd, 2), datetime.now().strftime('%Y-%m-%d %H:%M')))
        cur2.execute("DELETE FROM active_positions WHERE symbol=?", (symbol,))
        conn2.commit()
        conn2.close()
        return True
    except Exception as e:
        print(f"❌ DB GÜNCELLEME HATASI ({symbol}, {status}): {e}")
        return False

def update_partial_position_in_db(symbol, **fields):
    """active_positions tablosunda kısmi güncelleme (TP1 sonrası kalan miktar ve gerçekleşen kâr gibi)."""
    try:
        conn2 = get_db()
        cur2  = conn2.cursor()
        set_clause = ", ".join(f"{k}=?" for k in fields.keys())
        values = list(fields.values()) + [symbol]
        cur2.execute(f"UPDATE active_positions SET {set_clause} WHERE symbol=?", values)
        conn2.commit()
        conn2.close()
        return True
    except Exception as e:
        print(f"❌ DB GÜNCELLEME HATASI (partial, {symbol}): {e}")
        return False

def tp_monitor():
    while True:
        try:
            conn   = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, side, avg_entry_price, total_contracts, tp1_done, tp2_done, sl_active, realized_pnl FROM active_positions")
            positions = cursor.fetchall()
            conn.close()

            for pos in positions:
                symbol, side, avg_price, total_contracts, tp1_done, tp2_done, sl_active, realized_pnl = pos
                if not avg_price or avg_price == 0:
                    continue
                if not total_contracts or total_contracts == 0:
                    continue
                try:
                    okx = get_okx()
                    if not okx:
                        continue
                    okx.load_markets()

                    # OKX'te bu sembol için gerçekten açık pozisyon var mı? (manuel kapatılmış olabilir)
                    real_pos = get_okx_position_for_symbol(okx, symbol)
                    if not real_pos:
                        # Borsada pozisyon yok ama DB'de hâlâ "açık" görünüyor demek ki manuel/başka yoldan
                        # kapanmış ve DB güncellenememiş. DB'yi senkronize et, sessizce kayıp PnL bırakma.
                        print(f"ℹ️ {symbol} borsada bulunamadı, DB'den temizleniyor (muhtemelen manuel kapatılmış)")
                        # TP1 sonrası gelen manuel kapamada da realized_pnl'i koru
                        fallback_pnl = realized_pnl or 0.0
                        status = 'SL_BREAKEVEN' if tp1_done else 'MANUEL_SENKRON'
                        close_full_position_in_db(symbol, side, status, fallback_pnl)
                        continue

                    cur_price    = float(real_pos.get('markPrice', 0) or 0)
                    real_pnl_usd = float(real_pos.get('unrealizedPnl', 0) or 0)
                    real_pnl_pct = float(real_pos.get('percentage', 0) or 0)

                    if not cur_price:
                        ticker    = okx.fetch_ticker(symbol)
                        cur_price = ticker['last']

                    tp1_pct = float(get_setting('tp1_pct', 1.5)) / 100
                    tp2_pct = float(get_setting('tp2_pct', 3.0)) / 100
                    tp1_qty = float(get_setting('tp1_qty', 50)) / 100

                    if side == 'buy':
                        tp1_price = avg_price * (1 + tp1_pct)
                        tp2_price = avg_price * (1 + tp2_pct)
                        hit_tp1   = cur_price >= tp1_price
                        hit_tp2   = cur_price >= tp2_price
                        hit_sl    = sl_active and (cur_price <= avg_price)
                    else:
                        tp1_price = avg_price * (1 - tp1_pct)
                        tp2_price = avg_price * (1 - tp2_pct)
                        hit_tp1   = cur_price <= tp1_price
                        hit_tp2   = cur_price <= tp2_price
                        hit_sl    = sl_active and (cur_price >= avg_price)

                    close_side  = 'sell' if side == 'buy' else 'buy'
                    pos_side    = 'long' if side == 'buy' else 'short'
                    pnl_usd     = real_pnl_usd if real_pnl_usd is not None else 0
                    pnl_pct_val = real_pnl_pct if real_pnl_pct is not None else 0

                    print(f"👁 {symbol} | Fiyat:{cur_price} | Ort:{avg_price:.4f} | PnL:{pnl_usd:.2f} | SL_Aktif:{bool(sl_active)} | Birikmiş:{realized_pnl or 0:.2f}")

                    # --- SL (BREAKEVEN): TP1 sonrası geldiği için her zaman WIN sayılır ---
                    if hit_sl and not tp2_done:
                        okx.create_market_order(
                            symbol=symbol, side=close_side, amount=total_contracts,
                            params={"posSide": pos_side, "reduceOnly": True}
                        )
                        sym_short = symbol.replace('/USDT:USDT', '')
                        total_realized = (realized_pnl or 0) + pnl_usd
                        # TP1 kârı gerçekleşmiş olduğu için bu satış WIN olarak işaretlenir (en az +0.01)
                        final_pnl = total_realized if total_realized > 0 else 0.01
                        ok = close_full_position_in_db(symbol, side, 'SL_BREAKEVEN', final_pnl)
                        if ok:
                            send_telegram(
                                f"🛡 <b>SL (Breakeven) Tetiklendi!</b>\n"
                                f"Sembol: <b>{sym_short}</b>\n"
                                f"Yön: {'LONG' if side=='buy' else 'SHORT'}\n"
                                f"Fiyat: ${cur_price:.4f}\n"
                                f"Entry: ${avg_price:.4f}\n"
                                f"Toplam PnL: +${final_pnl:.2f} (TP1 kârı korundu)"
                            )
                        else:
                            send_telegram(f"⚠️ {sym_short} SL'de kapandı ama panel kaydı başarısız oldu, kontrol edin!")
                        continue

                    # --- TP2: kalan pozisyonun TAMAMI kapanır ---
                    if hit_tp2 and not tp2_done:
                        qty = total_contracts  # kalan = TP2 payının tamamı, yüzde uygulanmaz
                        okx.create_market_order(
                            symbol=symbol, side=close_side, amount=qty,
                            params={"posSide": pos_side, "reduceOnly": True}
                        )
                        sym_short = symbol.replace('/USDT:USDT', '')
                        total_realized = (realized_pnl or 0) + pnl_usd
                        ok = close_full_position_in_db(symbol, side, 'TP2', total_realized)
                        if ok:
                            send_telegram(
                                f"✅ <b>TP2 HIT! Pozisyon tamamen kapandı</b>\n"
                                f"Sembol: <b>{sym_short}</b>\n"
                                f"Yön: {'LONG' if side=='buy' else 'SHORT'}\n"
                                f"Fiyat: ${cur_price:.4f}\n"
                                f"Toplam PnL: +${total_realized:.2f} USDT"
                            )
                        else:
                            send_telegram(f"⚠️ {sym_short} TP2'de kapandı ama panel kaydı başarısız oldu, kontrol edin!")
                        continue

                    # --- TP1: kısmi kapama + breakeven SL aktif et + gerçekleşen kârı biriktir ---
                    elif hit_tp1 and not tp1_done:
                        qty = math.floor(total_contracts * tp1_qty * 10) / 10
                        if qty <= 0 or qty >= total_contracts:
                            qty = total_contracts * 0.5
                            qty = math.floor(qty * 10) / 10
                        if qty <= 0:
                            qty = total_contracts

                        # TP1'de kapanan dilimin gerçekleşen kârını oransal hesapla
                        portion      = qty / total_contracts if total_contracts else 0
                        tp1_realized = pnl_usd * portion

                        okx.create_market_order(
                            symbol=symbol, side=close_side, amount=qty,
                            params={"posSide": pos_side, "reduceOnly": True}
                        )
                        sym_short = symbol.replace('/USDT:USDT', '')
                        remaining = max(0, round(total_contracts - qty, 6))
                        ok = update_partial_position_in_db(
                            symbol, tp1_done=1, sl_active=1,
                            total_contracts=remaining, realized_pnl=tp1_realized
                        )
                        if ok:
                            send_telegram(
                                f"🎯 <b>TP1 HIT!</b>\n"
                                f"Sembol: <b>{sym_short}</b>\n"
                                f"Yön: {'LONG' if side=='buy' else 'SHORT'}\n"
                                f"Fiyat: ${cur_price:.4f}\n"
                                f"Gerçekleşen kâr: +${tp1_realized:.2f} USDT\n"
                                f"Kalan pozisyon TP2'de tamamen kapanacak\n"
                                f"🛡 SL Breakeven aktif @ ${avg_price:.4f}"
                            )
                        else:
                            send_telegram(f"⚠️ {sym_short} TP1'de kapandı ama panel kaydı başarısız oldu, kontrol edin!")

                except Exception as e:
                    print(f"⚠️ TP kontrol hatası {symbol}: {e}")

        except Exception as e:
            print(f"⚠️ TP monitor hatası: {e}")

        time.sleep(10)

threading.Thread(target=tp_monitor,          daemon=True).start()
threading.Thread(target=self_ping,           daemon=True).start()
threading.Thread(target=record_pnl_snapshot, daemon=True).start()

@app.route('/dashboard_data', methods=['GET'])
def dashboard_data():
    total, win_rate, total_pnl = get_stats()
    acilan, kapanan, acik, tp1, tp2, sl, tp2_bekliyor = get_daily_stats()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, side, avg_entry_price, total_contracts, current_step, tp1_done, tp2_done, sl_active FROM active_positions")
    active_pos = cursor.fetchall()
    conn.close()

    okx_pos = get_okx_positions()

    positions = []
    stale_symbols = []
    for p in active_pos:
        symbol, side, avg_price, contracts, step, tp1_done, tp2_done, sl_active = p
        okx = okx_pos.get(symbol, {})

        if not okx:
            # Borsada karşılığı yok -> manuel kapatılmış ama DB'den silinememiş demektir.
            stale_symbols.append((symbol, side, bool(tp1_done)))
            continue

        real_entry   = okx.get('entryPrice', avg_price) or avg_price
        real_mark    = okx.get('markPrice',  avg_price) or avg_price
        real_pnl_usd = okx.get('pnlUsd',     0.0)
        real_pnl_pct = okx.get('pnlPct',     0.0)
        real_size    = okx.get('sizeUsd',     0.0)

        positions.append({
            "symbol":    symbol,
            "side":      side,
            "avgPrice":  real_entry,
            "markPrice": real_mark,
            "pnlUsd":    real_pnl_usd,
            "pnlPct":    real_pnl_pct,
            "sizeUsd":   real_size,
            "contracts": contracts,
            "step":      step,
            "tp1Done":   bool(tp1_done),
            "tp2Done":   bool(tp2_done),
            "slActive":  bool(sl_active),
            "slPrice":   real_entry,
        })

    # Borsada artık olmayan ama DB'de kalmış pozisyonları burada da temizle
    # (panel her açıldığında ekstra bir güvenlik katmanı, tp_monitor'u 10sn beklemeden)
    if stale_symbols:
        conn3 = get_db()
        cur3  = conn3.cursor()
        for sym, sd, had_tp1 in stale_symbols:
            cur3.execute("SELECT realized_pnl FROM active_positions WHERE symbol=?", (sym,))
            r = cur3.fetchone()
            fallback_pnl = (r[0] if r and r[0] else 0.0)
            status = 'SL_BREAKEVEN' if had_tp1 else 'MANUEL_SENKRON'
            print(f"ℹ️ Panel: {sym} borsada yok, DB temizleniyor ({status})")
            close_full_position_in_db(sym, sd, status, fallback_pnl)
        conn3.close()

    return jsonify({
        "total":        total,
        "win_rate":     round(win_rate, 1),
        "total_pnl":    round(total_pnl, 2),
        "acilan":       acilan,
        "kapanan":      kapanan,
        "acik":         acik,
        "tp1":          tp1,
        "tp2":          tp2,
        "sl":           sl,
        "tp2_bekliyor": tp2_bekliyor,
        "positions":    positions,
        "today":        datetime.now().strftime('%d %b %Y'),
    })

@app.route('/pnl_analytics', methods=['GET'])
def pnl_analytics():
    range_key = request.args.get('range', '7d')
    valid_ranges = ['24h', '7d', '14d', '30d', '90d', '180d', '365d', 'all']
    if range_key not in valid_ranges:
        range_key = '7d'
    data = get_pnl_analytics(range_key)
    return jsonify(data), 200

@app.route('/', methods=['GET'])
def dashboard():
    api_status    = "✅ Bağlı" if get_okx() else "❌ API Eksik"
    tg_configured = "✅ Aktif" if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else "❌ Ayarlanmadı"

    context = {
        'api_status': api_status,
        'tg_status':  tg_configured,
        'l1_usd':     get_setting('l1_usd',  '40'),
        'd1_usd':     get_setting('d1_usd',  '60'),
        'd2_usd':     get_setting('d2_usd',  '90'),
        'd3_usd':     get_setting('d3_usd',  '135'),
        'd4_usd':     get_setting('d4_usd',  '202.5'),
        'min_dist':   get_setting('min_dist', '2.0'),
        'tp1_pct':    get_setting('tp1_pct',  '1.5'),
        'tp1_qty':    get_setting('tp1_qty',  '50'),
        'tp2_pct':    get_setting('tp2_pct',  '3.0'),
        'tg_token':   get_setting('tg_token', ''),
        'tg_chat_id': get_setting('tg_chat_id', ''),
    }

    html = '''<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>OKX DCA Bot</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0d0d0f;color:#e0e0e0;padding:12px;font-variant-numeric:tabular-nums}
.header{text-align:center;padding:16px 0 20px;font-size:1.05rem;font-weight:700;color:#4caf50;letter-spacing:1px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.stat{background:#16161a;border:1px solid #222;border-radius:10px;padding:12px 8px;text-align:center}
.stat .val{font-size:1.15rem;font-weight:700;color:#4caf50}
.stat .lbl{font-size:0.65rem;color:#555;margin-top:3px;text-transform:uppercase;letter-spacing:.5px}
.card{background:#16161a;border:1px solid #222;border-radius:10px;padding:14px;margin-bottom:12px}
.card h2{font-size:0.78rem;color:#4caf50;text-transform:uppercase;letter-spacing:.8px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #222;display:flex;align-items:center;justify-content:space-between}
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
.neg-val{color:#ff5252}
.neu-val{color:#888}
.pnl-bar-wrap{padding:8px 14px 10px}
.pnl-bar-label{display:flex;justify-content:space-between;font-size:9px;color:#555;margin-bottom:4px}
.pnl-bar-bg{height:3px;background:#222;border-radius:2px;overflow:hidden}
.pnl-bar-fill{height:100%;border-radius:2px}
.pos-footer{display:flex;justify-content:space-between;align-items:center;padding:8px 14px 10px}
.tp-pills{display:flex;gap:5px;flex-wrap:wrap}
.tp-pill{font-size:9px;font-weight:600;padding:2px 7px;border-radius:10px}
.tp-wait{background:#1e1e22;color:#555;border:1px solid #2a2a2e}
.tp-hit{background:#1a3a1a;color:#4caf50}
.sl-pill{font-size:9px;font-weight:600;padding:2px 7px;border-radius:10px}
.sl-wait{background:#1e1e22;color:#555;border:1px solid #2a2a2e}
.sl-active-pill{background:#1a1a3a;color:#ff9800;border:1px solid #ff9800}
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
.daily-inner{background:#111;border-radius:8px;padding:10px 14px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}
.daily-big{font-size:28px;font-weight:700;color:#e0e0e0}
.daily-sub{font-size:11px;color:#555;margin-top:2px}
.daily-row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1a1a1e}
.daily-row:last-child{border-bottom:none}
.daily-label{font-size:12px;color:#666}
.daily-val{font-size:18px;font-weight:700}
.note{font-size:0.66rem;color:#555;margin-top:8px;line-height:1.4}

.range-select{position:relative}
.range-select select{
  background:#0d0d0f;border:1px solid #2a2a2e;color:#e0e0e0;font-size:0.68rem;
  padding:6px 26px 6px 10px;border-radius:7px;text-transform:none;letter-spacing:0;
  font-weight:600;appearance:none;cursor:pointer;
}
.range-select::after{
  content:'▾';position:absolute;right:9px;top:50%;transform:translateY(-50%);
  color:#4caf50;font-size:9px;pointer-events:none;
}
.summary-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
.sum-cell{background:#1a1a1e;border:1px solid #2a2a2e;border-radius:10px;padding:11px 10px}
.sum-cell .lbl{font-size:0.6rem;color:#555;text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}
.sum-cell .val{font-size:1.0rem;font-weight:700;letter-spacing:-.2px}
.sum-cell .sub{font-size:0.6rem;color:#444;margin-top:3px}
.chart-wrap{position:relative;height:200px;margin-bottom:14px}
.tbl-scroll{max-height:420px;overflow-y:auto;border-radius:8px}
.tbl-scroll::-webkit-scrollbar{width:6px}
.tbl-scroll::-webkit-scrollbar-thumb{background:#2a2a2e;border-radius:3px}
.pnl-table{width:100%;border-collapse:collapse;font-size:0.74rem}
.pnl-table thead th{
  position:sticky;top:0;background:#1a1a1e;color:#666;text-transform:uppercase;
  font-size:0.58rem;letter-spacing:.5px;font-weight:600;text-align:right;padding:8px 6px;
  border-bottom:1px solid #2a2a2e;z-index:1;
}
.pnl-table thead th:first-child{text-align:left;padding-left:14px}
.pnl-table tbody td{padding:8px 6px;text-align:right;border-bottom:1px solid #1c1c20;white-space:nowrap;font-size:0.74rem}
.pnl-table tbody td:first-child{text-align:left;padding-left:14px;position:relative}
.pnl-table tbody tr:hover{background:#1a1a1e}
.pnl-table tbody tr:last-child td{border-bottom:none}
.pulse-bar{position:absolute;left:0;top:6px;bottom:6px;width:3px;border-radius:2px}
.date-main{font-weight:600;color:#ddd;display:block;font-size:0.76rem}
.date-sub{font-size:0.58rem;color:#555;display:block;margin-top:1px}
.wr-pill{display:inline-block;padding:2px 6px;border-radius:20px;font-size:0.62rem;font-weight:600}
.wr-high{background:#0a2a0a;color:#4caf50}
.wr-mid{background:#2a2a0a;color:#ffc107}
.wr-low{background:#2a0a0a;color:#ff5252}
.mini-bar-bg{width:38px;height:4px;background:#222;border-radius:3px;overflow:hidden;display:inline-block;vertical-align:middle;margin-left:5px}
.mini-bar-fill{height:100%;border-radius:3px}
.today-row td:first-child .date-main{color:#4caf50}
.today-tag{font-size:0.52rem;background:#0a2a0a;color:#4caf50;padding:1px 5px;border-radius:4px;margin-left:5px;font-weight:700;letter-spacing:.3px}
.footnote{font-size:0.62rem;color:#444;text-align:center;padding-top:10px}
</style>
</head><body>

<div class="header">🤖 S-DCA KONTROL PANELİ</div>

<div class="stats">
  <div class="stat"><div class="val" id="s-total">-</div><div class="lbl">İşlem</div></div>
  <div class="stat"><div class="val" id="s-winrate">-</div><div class="lbl">Kazanma</div></div>
  <div class="stat"><div class="val" id="s-pnl">-</div><div class="lbl">Toplam PnL</div></div>
</div>

<div class="card">
  <h2><span id="live-indicator"></span>Aktif Pozisyonlar <span id="last-update" style="float:right;color:#333;font-size:0.65rem;font-weight:400;text-transform:none"></span></h2>
  <div id="positions"><div class="no-pos">Yükleniyor...</div></div>
</div>

<div class="card">
  <h2>📅 Bugünün Özeti <span id="today-str" style="float:right;color:#444;font-size:0.65rem;font-weight:400;text-transform:none"></span></h2>
  <div class="daily-inner">
    <div>
      <div class="daily-big" id="d-acilan">-</div>
      <div class="daily-sub">toplam açılan işlem</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:13px;color:#4caf50" id="d-kapanan">-</div>
      <div style="font-size:13px;color:#555;margin-top:2px" id="d-acik">-</div>
    </div>
  </div>
  <div class="daily-row"><span class="daily-label">TP1 oldu</span><span class="daily-val" style="color:#4caf50" id="d-tp1">-</span></div>
  <div class="daily-row"><span class="daily-label">TP2 oldu</span><span class="daily-val" style="color:#42a5f5" id="d-tp2">-</span></div>
  <div class="daily-row"><span class="daily-label">TP1 → SL yedi</span><span class="daily-val" style="color:#ffa726" id="d-sl">-</span></div>
  <div class="daily-row"><span class="daily-label">TP2 bekliyor</span><span class="daily-val" style="color:#888" id="d-tp2bek">-</span></div>
</div>

<div class="card">
  <h2>
    <span>📊 PnL Analitiği</span>
    <span class="range-select">
      <select id="rangeSel">
        <option value="24h">Son 24 Saat</option>
        <option value="7d" selected>Son 7 Gün</option>
        <option value="14d">Son 14 Gün</option>
        <option value="30d">Son 30 Gün</option>
        <option value="90d">Son 90 Gün</option>
        <option value="180d">Son 6 Ay</option>
        <option value="365d">Son 1 Yıl</option>
        <option value="all">Tüm Zamanlar</option>
      </select>
    </span>
  </h2>

  <div class="summary-grid">
    <div class="sum-cell">
      <div class="lbl">Dönem PnL</div>
      <div class="val" id="pa-total">-</div>
      <div class="sub" id="pa-total-sub">-</div>
    </div>
    <div class="sum-cell">
      <div class="lbl">Win Rate</div>
      <div class="val" id="pa-winrate" style="color:#e0e0e0">-</div>
      <div class="sub" id="pa-winrate-sub">-</div>
    </div>
    <div class="sum-cell">
      <div class="lbl">En İyi Gün</div>
      <div class="val pos-val" id="pa-best">-</div>
      <div class="sub" id="pa-best-sub">-</div>
    </div>
    <div class="sum-cell">
      <div class="lbl">En Kötü Gün</div>
      <div class="val neg-val" id="pa-worst">-</div>
      <div class="sub" id="pa-worst-sub">-</div>
    </div>
  </div>

  <div class="chart-wrap">
    <canvas id="cumChart"></canvas>
  </div>

  <div class="tbl-scroll">
  <table class="pnl-table">
    <thead>
      <tr>
        <th>Tarih</th>
        <th>İşlem</th>
        <th>K/Z</th>
        <th>Win%</th>
        <th>PnL</th>
        <th>Kümülatif</th>
      </tr>
    </thead>
    <tbody id="paTblBody"><tr><td colspan="6" class="no-pos">Yükleniyor...</td></tr></tbody>
  </table>
  </div>
  <div class="footnote" id="pa-footnote">veriler her saat güncellenir</div>
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
  <label>Min. Fibo Mesafesi (%) — İlk girişte kontrol edilir</label>
  <input type="number" step="0.1" name="min_dist" value="{{ min_dist }}">
  <label>TP1 Oranı (%)</label><input type="number" step="0.1" name="tp1_pct" value="{{ tp1_pct }}">
  <label>TP1 Satış (%) — kalanın tamamı TP2'de kapanır</label><input type="number" name="tp1_qty" value="{{ tp1_qty }}">
  <label>TP2 Oranı (%)</label><input type="number" step="0.1" name="tp2_pct" value="{{ tp2_pct }}">
  <div class="note">ℹ️ TP2 tetiklendiğinde kalan pozisyonun tamamı kapanır. TP1 sonrası gelen breakeven SL de artık TP1'de gerçekleşen kârı koruyarak WIN olarak sayılır.</div>
</div>
<div class="card">
  <h2>📱 Telegram Bildirimleri</h2>
  <label>Bot Token</label><input type="text" name="tg_token" value="{{ tg_token }}" placeholder="1234567890:ABC...">
  <label>Chat ID</label><input type="text" name="tg_chat_id" value="{{ tg_chat_id }}" placeholder="-100xxxxxxxxx">
</div>
<button class="btn-save" type="submit">💾 Ayarları Kaydet</button>
</form>

<script>
let cumChart = null;

function fmtU(n){
  const abs = Math.abs(n).toFixed(2);
  return (n >= 0 ? '+$' : '−$') + abs;
}
function fmt(n, d=2){
  return (n >= 0 ? '+' : '') + n.toFixed(d);
}

function renderPositions(positions){
  const container = document.getElementById('positions');
  if(!positions.length){
    container.innerHTML = '<div class="no-pos">Aktif pozisyon yok</div>';
    return;
  }
  let html = '';
  positions.forEach(p => {
    const isPos  = p.pnlUsd >= 0;
    const cls    = isPos ? 'pos-val' : 'neg-val';
    const barW   = Math.min(Math.abs(p.pnlPct) * 5, 100);
    const barClr = isPos ? '#4caf50' : '#ff5252';
    const sym    = p.symbol.replace('/USDT:USDT','');

    const tp1_pct  = p.side === 'buy' ? 1.015 : 0.985;
    const tp2_pct  = p.side === 'buy' ? 1.030 : 0.970;
    const tp1Price = (p.avgPrice * tp1_pct).toFixed(4);
    const tp2Price = (p.avgPrice * tp2_pct).toFixed(4);

    const tp1Target = p.side === 'buy' ? p.avgPrice * 1.015 : p.avgPrice * 0.985;
    const distToTp1 = p.side === 'buy'
      ? (tp1Target - p.markPrice) / p.markPrice * 100
      : (p.markPrice - tp1Target) / p.markPrice * 100;

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
        <div class="cell"><div class="clabel">Mark Fiyat</div><div class="cval ${cls}">$${p.markPrice.toFixed(4)}</div></div>
        <div class="cell"><div class="clabel">Pozisyon ($)</div><div class="cval">$${p.sizeUsd.toFixed(2)}</div></div>
        <div class="cell"><div class="clabel">PnL (USDT)</div><div class="cval ${cls}">${fmtU(p.pnlUsd)}</div></div>
        <div class="cell"><div class="clabel">PnL (%)</div><div class="cval ${cls}">${fmt(p.pnlPct)}%</div></div>
        <div class="cell"><div class="clabel">TP1'e Uzaklık</div><div class="cval neu-val">${fmt(distToTp1,2)}%</div></div>
      </div>
      <div class="pnl-bar-wrap">
        <div class="pnl-bar-label">
          <span>SL $${p.slPrice.toFixed(4)}</span>
          <span>TP1 $${tp1Price} | TP2 $${tp2Price}</span>
        </div>
        <div class="pnl-bar-bg"><div class="pnl-bar-fill" style="width:${barW}%;background:${barClr}"></div></div>
      </div>
      <div class="divider"></div>
      <div class="pos-footer">
        <div class="tp-pills">
          <span class="tp-pill ${p.tp1Done?'tp-hit':'tp-wait'}">TP1 ${p.tp1Done?'✓':'bekliyor'}</span>
          <span class="tp-pill ${p.tp2Done?'tp-hit':'tp-wait'}">TP2 ${p.tp2Done?'✓':'bekliyor'}</span>
          <span class="sl-pill ${p.slActive?'sl-active-pill':'sl-wait'}">🛡 SL ${p.slActive?'AKTİF':'bekliyor'}</span>
        </div>
        <button class="close-btn" onclick="closePos('${p.symbol}','${p.side}')">✖ Kapat</button>
      </div>
    </div>`;
  });
  container.innerHTML = html;
}

function winRateClass(wr){
  if(wr >= 60) return 'wr-high';
  if(wr >= 45) return 'wr-mid';
  return 'wr-low';
}

function renderPnlAnalytics(data){
  const totalEl = document.getElementById('pa-total');
  totalEl.textContent = fmtU(data.total_pnl);
  totalEl.className = 'val ' + (data.total_pnl >= 0 ? 'pos-val' : 'neg-val');
  document.getElementById('pa-total-sub').textContent = data.total_trades + ' işlemde';

  document.getElementById('pa-winrate').textContent = data.win_rate.toFixed(1) + '%';
  document.getElementById('pa-winrate-sub').textContent = data.total_trades + ' işlem';

  if(data.best){
    document.getElementById('pa-best').textContent = fmtU(data.best.pnl);
    document.getElementById('pa-best-sub').textContent = data.best.label;
  } else {
    document.getElementById('pa-best').textContent = '—';
    document.getElementById('pa-best-sub').textContent = 'veri yok';
  }
  if(data.worst){
    document.getElementById('pa-worst').textContent = fmtU(data.worst.pnl);
    document.getElementById('pa-worst-sub').textContent = data.worst.label;
  } else {
    document.getElementById('pa-worst').textContent = '—';
    document.getElementById('pa-worst-sub').textContent = 'veri yok';
  }

  const tbody = document.getElementById('paTblBody');
  if(!data.rows.length){
    tbody.innerHTML = '<tr><td colspan="6" class="no-pos">Bu aralıkta kapanmış işlem yok</td></tr>';
  } else {
    const maxAbs = Math.max(...data.rows.map(r => Math.abs(r.pnl)), 0.01);
    let html = '';
    data.rows.forEach(r => {
      const isPos = r.pnl >= 0;
      const barColor = isPos ? '#4caf50' : '#ff5252';
      const barWidth = Math.abs(r.pnl) / maxAbs * 100;
      const wr = r.trades ? (r.wins / r.trades * 100) : 0;
      html += `
      <tr class="${r.is_today ? 'today-row' : ''}">
        <td>
          <div class="pulse-bar" style="background:${barColor};opacity:${0.35 + (barWidth/100)*0.65}"></div>
          <span class="date-main">${r.label}${r.is_today ? '<span class="today-tag">CANLI</span>' : ''}</span>
          <span class="date-sub">${r.sub}</span>
        </td>
        <td>${r.trades}</td>
        <td><span class="pos-val">${r.wins}W</span>/<span class="neg-val">${r.losses}L</span></td>
        <td><span class="wr-pill ${winRateClass(wr)}">${wr.toFixed(0)}%</span></td>
        <td class="${isPos ? 'pos-val' : 'neg-val'}" style="font-weight:700">
          ${fmtU(r.pnl)}
          <span class="mini-bar-bg"><span class="mini-bar-fill" style="width:${barWidth}%;background:${barColor}"></span></span>
        </td>
        <td class="${r.cum >= 0 ? 'pos-val' : 'neg-val'}">${fmtU(r.cum)}</td>
      </tr>`;
    });
    tbody.innerHTML = html;
  }

  const ctx = document.getElementById('cumChart');
  const gradient = ctx.getContext('2d').createLinearGradient(0,0,0,200);
  const isOverallPos = data.total_pnl >= 0;
  if(isOverallPos){
    gradient.addColorStop(0, 'rgba(76,175,80,0.35)');
    gradient.addColorStop(1, 'rgba(76,175,80,0.0)');
  } else {
    gradient.addColorStop(0, 'rgba(255,82,82,0.35)');
    gradient.addColorStop(1, 'rgba(255,82,82,0.0)');
  }
  const lineColor = isOverallPos ? '#4caf50' : '#ff5252';

  if(!data.chart_cum.length){
    if(cumChart){ cumChart.destroy(); cumChart = null; }
    ctx.getContext('2d').clearRect(0,0,ctx.width,ctx.height);
    return;
  }

  if(cumChart){
    cumChart.data.labels = data.chart_labels;
    cumChart.data.datasets[0].data = data.chart_cum;
    cumChart.data.datasets[0].borderColor = lineColor;
    cumChart.data.datasets[0].pointBorderColor = lineColor;
    cumChart.data.datasets[0].backgroundColor = gradient;
    cumChart.update();
  } else {
    cumChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: data.chart_labels,
        datasets: [{
          label: 'Kümülatif PnL',
          data: data.chart_cum,
          borderColor: lineColor,
          backgroundColor: gradient,
          borderWidth: 2.5,
          pointRadius: 3,
          pointBackgroundColor: '#0d0d0f',
          pointBorderColor: lineColor,
          pointBorderWidth: 2,
          pointHoverRadius: 6,
          fill: true,
          tension: 0.35,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { intersect: false, mode: 'index' },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#1a1a1e',
            borderColor: '#2a2a2e',
            borderWidth: 1,
            titleColor: '#888',
            bodyColor: lineColor,
            bodyFont: { weight: '700' },
            padding: 10,
            callbacks: {
              label: (ctx) => 'Kümülatif: ' + fmtU(ctx.parsed.y)
            }
          }
        },
        scales: {
          x: { ticks: { color: '#555', font: { size: 9 } }, grid: { color: '#1a1a1e' } },
          y: { ticks: { color: '#555', font: { size: 9 }, callback: v => '$'+v }, grid: { color: '#1a1a1e' } }
        }
      }
    });
  }
}

async function loadPnlAnalytics(range){
  try{
    const r = await fetch('/pnl_analytics?range=' + range);
    const d = await r.json();
    renderPnlAnalytics(d);
  } catch(e){
    console.error('PnL analitik hatası:', e);
  }
}

document.getElementById('rangeSel').addEventListener('change', (e) => {
  loadPnlAnalytics(e.target.value);
});

async function refreshAll(){
  try{
    const r = await fetch('/dashboard_data');
    const d = await r.json();

    document.getElementById('s-total').textContent   = d.total;
    document.getElementById('s-winrate').textContent = d.win_rate.toFixed(1) + '%';
    document.getElementById('s-pnl').textContent     = d.total_pnl.toFixed(2) + ' USDT';
    document.getElementById('today-str').textContent  = d.today;
    document.getElementById('d-acilan').textContent   = d.acilan;
    document.getElementById('d-kapanan').textContent  = d.kapanan + ' kapandı';
    document.getElementById('d-acik').textContent     = d.acik + ' açık';
    document.getElementById('d-tp1').textContent      = d.tp1;
    document.getElementById('d-tp2').textContent      = d.tp2;
    document.getElementById('d-sl').textContent       = d.sl;
    document.getElementById('d-tp2bek').textContent   = d.tp2_bekliyor;

    renderPositions(d.positions);

    document.getElementById('last-update').textContent =
      'Güncellendi: ' + new Date().toLocaleTimeString('tr-TR');

  } catch(e){
    console.error('Güncelleme hatası:', e);
  }
}

refreshAll();
loadPnlAnalytics(document.getElementById('rangeSel').value);
setInterval(refreshAll, 30000);
setInterval(() => loadPnlAnalytics(document.getElementById('rangeSel').value), 60000);

function closePos(symbol, side){
  if(!confirm(symbol.replace('/USDT:USDT','')+' pozisyonunu kapatmak istediğine emin misin?')) return;
  fetch('/close', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({symbol, side})
  })
  .then(r => r.json())
  .then(d => { alert(d.message || 'Tamamlandı'); refreshAll(); })
  .catch(e => alert('Hata: ' + e));
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
        conn   = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT total_contracts, avg_entry_price, tp1_done, realized_pnl FROM active_positions WHERE symbol=?", (symbol,))
        row = cursor.fetchone()
        conn.close()
        if not row or not row[0]:
            return jsonify({"status": "error", "message": "Pozisyon bulunamadı"}), 200
        total_contracts, avg_price, tp1_done, realized_pnl = row
        close_side  = 'sell' if side == 'buy' else 'buy'
        pos_side    = 'long' if side == 'buy' else 'short'

        okx_positions = okx.fetch_positions([symbol])
        real_pnl_usd  = 0.0
        real_pnl_pct  = 0.0
        cur_price     = None
        for p in okx_positions:
            if p and float(p.get('contracts', 0) or 0) > 0:
                cur_price    = float(p.get('markPrice', 0) or 0)
                real_pnl_usd = float(p.get('unrealizedPnl', 0) or 0)
                real_pnl_pct = float(p.get('percentage', 0) or 0)
                break

        # OKX'te zaten kapalıysa (kullanıcı borsadan elle kapatmış olabilir) -> sadece DB'yi temizle
        if cur_price is None:
            ticker    = okx.fetch_ticker(symbol)
            cur_price = ticker['last']
            fallback_pnl = realized_pnl or 0.0
            status = 'SL_BREAKEVEN' if tp1_done else 'MANUEL_SENKRON'
            ok = close_full_position_in_db(symbol, side, status, fallback_pnl)
            if ok:
                return jsonify({"status": "success", "message": f"{symbol.replace('/USDT:USDT','')} borsada zaten kapalıydı, panel senkronize edildi"}), 200
            else:
                return jsonify({"status": "error", "message": "Borsada pozisyon yok ama panel güncellenemedi (DB hatası), tekrar deneyin"}), 200

        okx.create_market_order(
            symbol=symbol, side=close_side, amount=total_contracts,
            params={"posSide": pos_side, "reduceOnly": True}
        )
        sym_short = symbol.replace('/USDT:USDT', '')

        # TP1 sonrası gelen manuel kapamada da biriken kâr dahil edilir, win/lose doğru hesaplanır
        total_realized = (realized_pnl or 0) + real_pnl_usd
        status = 'SL_BREAKEVEN' if tp1_done else 'MANUEL'
        if tp1_done and total_realized <= 0:
            total_realized = 0.01  # TP1 kârı gerçekleşmiş, win sayılmalı

        ok = close_full_position_in_db(symbol, side, status, total_realized)
        if not ok:
            # OKX'te kapandı ama DB güncellenemedi -> kullanıcıyı net şekilde uyar, sessiz kalma
            return jsonify({
                "status": "error",
                "message": f"{sym_short} borsada kapatıldı (PnL: ${total_realized:.2f}) AMA panel güncellenemedi! Sayfayı yenileyip tekrar dener misin?"
            }), 200

        send_telegram(
            f"🛑 <b>Manuel Kapatma</b>\n"
            f"Sembol: <b>{sym_short}</b>\n"
            f"Fiyat: ${cur_price:.4f}\n"
            f"Toplam PnL: {'+' if total_realized>=0 else ''}${total_realized:.2f} USDT"
        )
        return jsonify({"status": "success", "message": f"{sym_short} kapatıldı | PnL: ${total_realized:.2f}"}), 200
    except Exception as e:
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
        return jsonify({"status": "error", "message": "Gecersiz JSON"}), 200

    raw_symbol    = data.get('symbol')
    side          = data.get('side', 'buy')
    step          = int(data.get('step', 1))
    current_price = float(data.get('price', 0))

    if not raw_symbol or not current_price:
        return jsonify({"status": "error", "message": "Eksik veri"}), 200

    symbol = raw_symbol.replace('.P', '').replace('-', '').replace('_', '').strip()
    if "USDT" in symbol and ":" not in symbol:
        symbol = symbol.replace("USDT", "/USDT:USDT")

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
        return jsonify({"status": "error", "message": "API anahtarlari eksik"}), 200

    try:
        okx.load_markets()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Market yüklenemedi: {e}"}), 200

    if step == 1:
        try:
            fhigh, flow = get_previous_day_hl(okx, symbol)
            if fhigh is None or flow is None or fhigh == flow:
                print(f"⚠️ Fibo verisi alınamadı, filtre atlanıyor ({symbol})")
            else:
                passed, fail_step, fail_dist = check_fibo_distance(side, fhigh, flow, min_distance_filter)
                if not passed:
                    sym_short = symbol.replace('/USDT:USDT', '')
                    send_telegram(
                        f"🚫 <b>Fibo Mesafe Engeli</b>\n"
                        f"Sembol: <b>{sym_short}</b>\n"
                        f"Yön: {'LONG' if side=='buy' else 'SHORT'}\n"
                        f"Step{fail_step}→Step{fail_step+1} arası: %{fail_dist:.2f}\n"
                        f"Min gereken: %{min_distance_filter}\n"
                        f"❌ İşleme girilmedi"
                    )
                    return jsonify({
                        "status":   "ignored",
                        "message":  f"Fibo mesafe engeli: Step{fail_step} arası %{fail_dist:.2f} < min %{min_distance_filter}",
                        "fibo_h":   fhigh,
                        "fibo_l":   flow,
                        "fail_pct": round(fail_dist, 2)
                    }), 200
        except Exception as e:
            print(f"⚠️ Fibo kontrol hatası, devam ediliyor: {e}")

    conn   = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT side, last_entry_price, current_step, avg_entry_price, total_contracts FROM active_positions WHERE symbol=?", (symbol,))
    position = cursor.fetchone()

    if position:
        pos_side, last_entry_price, current_step, avg_entry_price, total_contracts = position
        if step == 1 and current_step >= 1:
            conn.close()
            return jsonify({"status": "ignored", "message": "Pozisyon zaten açık"}), 200
        if step == current_step:
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
        order    = okx.create_market_order(
            symbol=symbol, side=side, amount=final_qty,
            params={"posSide": pos_side}
        )
        order_id = order.get('id', 'N/A')

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
                "INSERT INTO active_positions (symbol,side,last_entry_price,current_step,avg_entry_price,total_contracts,sl_active) VALUES (?,?,?,?,?,?,0)",
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

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "order_id": order_id}), 200

    except Exception as e:
        conn.close()
        return jsonify({"status": "error", "okx_error": str(e)}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f"🌐 Sunucu başlatılıyor → port {port}")
    app.run(host='0.0.0.0', port=port)
