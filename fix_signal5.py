"""
fix_signal5.py — One-time correction for signal id=5 (GTT-C26240300296022)

Fetches 1-minute historical candles from Upstox to find:
  - Actual price at 12:26 PM IST (entry)
  - First candle after entry where low <= 185.0 (stoploss hit)

Then updates gtt_signals.db with correct data.
"""

import os
import sqlite3
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TOKEN           = os.getenv("UPSTOX_ACCESS_TOKEN", "")
BASE_URL        = os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com")
DB_PATH         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtt_signals.db")

# Both signals share the same instrument and trade — fix both
SIGNAL_IDS      = [5, 6]
INSTRUMENT_KEY  = "BSE_FO|864149"
SL_PRICE        = 185.0
ENTRY_MINUTE    = "2026-03-24T12:26"   # IST prefix to match candle timestamp
DATE            = "2026-03-24"


def fetch_candles() -> list:
    ik_encoded = INSTRUMENT_KEY.replace("|", "%7C")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    }
    # Try historical endpoint first (works for past dates)
    url_hist = f"{BASE_URL}/v2/historical-candle/{ik_encoded}/1minute/{DATE}/{DATE}"
    resp = requests.get(url_hist, headers=headers, timeout=15)
    resp.raise_for_status()
    candles = resp.json()["data"]["candles"]
    if candles:
        return sorted(candles, key=lambda c: c[0])

    # Fall back to intraday endpoint (works for today)
    url_intra = f"{BASE_URL}/v2/historical-candle/intraday/{ik_encoded}/1minute"
    resp = requests.get(url_intra, headers=headers, timeout=15)
    resp.raise_for_status()
    candles = resp.json()["data"]["candles"]
    # Upstox returns newest-first; sort ascending by timestamp
    return sorted(candles, key=lambda c: c[0])


def main():
    if not TOKEN:
        raise SystemExit("UPSTOX_ACCESS_TOKEN not set in .env")

    print("📡 Fetching 1-minute candles for BSE_FO|864149 on 2026-03-24...")
    candles = fetch_candles()
    print(f"   Got {len(candles)} candles")

    # ── Find entry candle at 12:26 PM IST ────────────────────────────────────
    entry_candle = next((c for c in candles if c[0].startswith(ENTRY_MINUTE)), None)
    if not entry_candle:
        raise SystemExit(f"No candle found for {ENTRY_MINUTE}. Available timestamps around that time:")

    # candle format: [timestamp, open, high, low, close, volume, oi]
    entry_ts_ist  = entry_candle[0]
    entry_price   = float(entry_candle[4])   # use close of entry candle
    entry_ts_utc  = datetime.fromisoformat(entry_ts_ist).astimezone(timezone.utc).isoformat()

    print(f"\n✅ Entry candle  @ {entry_ts_ist} IST")
    print(f"   O={entry_candle[1]} H={entry_candle[2]} L={entry_candle[3]} C={entry_candle[4]}")
    print(f"   entry_price (close) = {entry_price}")

    # ── Find first SL candle after entry ─────────────────────────────────────
    candles_from_entry = [c for c in candles if c[0] >= entry_ts_ist]
    sl_candle = next((c for c in candles_from_entry if float(c[3]) <= SL_PRICE), None)

    if not sl_candle:
        raise SystemExit(f"No candle found where low <= {SL_PRICE} after entry. SL was never hit in candle data.")

    sl_ts_ist   = sl_candle[0]
    exit_price  = float(sl_candle[3])   # low of the SL candle
    exit_ts_utc = datetime.fromisoformat(sl_ts_ist).astimezone(timezone.utc).isoformat()

    print(f"\n🛑 SL candle     @ {sl_ts_ist} IST")
    print(f"   O={sl_candle[1]} H={sl_candle[2]} L={sl_candle[3]} C={sl_candle[4]}")
    print(f"   exit_price (low) = {exit_price}")

    pnl = round((exit_price - entry_price) * 4 * 1, 2)   # BUY, qty=4, lot_sign=+1
    print(f"\n💰 PnL = ({exit_price} - {entry_price}) × 4 = {pnl}")

    # ── Update DB for both signals ────────────────────────────────────────────
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys=ON")

    for sid in SIGNAL_IDS:
        print(f"\n📝 Updating DB for signal id={sid}...")
        con.execute("""
            UPDATE signals SET
                status            = 'STOPLOSS_HIT',
                entry_price       = ?,
                exit_price        = ?,
                pnl               = ?,
                activated_at      = ?,
                resolved_at       = ?,
                upstox_gtt_status = 'stoploss_hit_manual_correction'
            WHERE id = ?
        """, (entry_price, exit_price, pnl, entry_ts_utc, exit_ts_utc, sid))

        # Replace price_events with correct records
        con.execute("DELETE FROM price_events WHERE signal_id = ?", (sid,))
        con.execute(
            "INSERT INTO price_events (signal_id, event_type, price, threshold, recorded_at) VALUES (?,?,?,?,?)",
            (sid, "ENTRY_HIT", entry_price, 190.0, entry_ts_utc),
        )
        con.execute(
            "INSERT INTO price_events (signal_id, event_type, price, threshold, recorded_at) VALUES (?,?,?,?,?)",
            (sid, "STOPLOSS_HIT", exit_price, SL_PRICE, exit_ts_utc),
        )

    con.commit()
    con.close()
    print("\n✅ DB updated successfully.\n")

    # ── Print final state ─────────────────────────────────────────────────────
    con2 = sqlite3.connect(DB_PATH)
    con2.row_factory = sqlite3.Row
    for sid in SIGNAL_IDS:
        print(f"── Signal id={sid} ──────────────────────────────────────────")
        row = con2.execute(
            "SELECT id, redis_message_id, status, entry_price, exit_price, pnl, activated_at, resolved_at FROM signals WHERE id=?",
            (sid,),
        ).fetchone()
        for k in row.keys():
            print(f"  {k:22s} = {row[k]}")
        print(f"  price_events:")
        for ev in con2.execute(
            "SELECT event_type, price, threshold, recorded_at FROM price_events WHERE signal_id=? ORDER BY recorded_at",
            (sid,),
        ):
            print(f"    {ev['event_type']:20s} price={ev['price']} threshold={ev['threshold']} @ {ev['recorded_at']}")
    con2.close()


if __name__ == "__main__":
    main()
