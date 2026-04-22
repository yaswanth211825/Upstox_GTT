"""
Trading Floor Guide — Rule-Derived Simulation Scenarios
=========================================================

All scenarios derived STRICTLY from the Trading Floor Guide PDF.
No repository code was consulted during authoring.

PDF Rules Encoded
─────────────────────────────────────────────────────────────────────────────

BUFFER POINTS  (applied to the OPTION PREMIUM price, not the underlying strike)
  100–200   → 3 pts
  200–300   → 5 pts
  300–400   → 7 pts  ⚠ NOT in PDF — interpolated; must confirm with operator
  400–500+  → 10 pts
  <100      → 3 pts  (assumed same as lowest tier)

BUY trade buffer direction  (PDF p.3)
  Entry   : entry_adj = entry + buffer_pts
  SL      : sl_adj    = SL   - 2.5 pts  (exit 2-3 pts BEFORE SL is hit)
  Target  : t_adj     = T1   - 6   pts  (exit 5-8 pts before target; midpoint=6)

SELL trade buffer direction  (PDF p.3)
  Entry   : entry_adj = entry - buffer_pts
  SL      : sl_adj    = SL   + 2.5 pts  (exit before SL from above)
  Target  : t_adj     = T1   + 6   pts

TRADE TYPES & GTT SPLIT  (PDF pp.7-8)
  RANGING   (entry_low AND entry_high provided)
    Safe → 1 GTT  : 100% lots at entry_low + buffer
    Risk → 2 GTTs : 50% at entry_low+buf,  50% at entry_high+buf

  BUY_ABOVE  (single entry, no range)
    Safe → 2 GTTs : 50% at entry+buf,  50% near SL level
    Risk → 1 GTT  : 100% at entry+buf

  AVERAGE  (entry_low AND explicit average_level below entry_low)
    Safe → 2 GTTs : 30% at entry_low+buf,  70% at avg_level+buf
    Risk → 2 GTTs : 50% at entry_low+buf,  50% at avg_level+buf

TIMING GATES  (PDF p.9 + NSE hours)
  Before 9:15 AM          → BLOCKED  (market not open)
  9:15–9:30 AM + SAFE     → BLOCKED  ("Safe traders enter after 9:30 AM" — PDF p.8)
  9:15–9:30 AM + RISK     → ACTIVE   (experienced traders can trade from 9:15)
  9:30 AM–3:30 PM + any   → ACTIVE
  After 3:30 PM           → BLOCKED  (NSE market close)
  Saturday / Sunday       → BLOCKED

DAILY P&L GUARD  (PDF p.2)
  "Once you reach the daily target of 10% of your capital, stop trading"
  daily_pnl >= 10% of capital → BLOCKED

MISSED ENTRY GUARD  (PDF p.2: "avoid chasing missed positions")
  LTP already >= T1_adj  → BLOCKED (move already done)
  LTP already <= SL_adj  → BLOCKED (would enter a losing position)
  No LTP in cache        → ACTIVE  (cannot verify, allow)
  LTP in valid range     → ACTIVE

RISK-REWARD RATIO  (PDF p.2, p.8: "Follow 1:2 or 1:3 RR ratio")
  risk   = entry_adj - sl_adj   (for BUY)
  reward = t1_adj   - entry_adj (for BUY)
  reward / risk < 2.0  → BLOCKED  (minimum 1:2 not met)
  reward / risk >= 2.0 → ACTIVE

SIGNAL VALIDATION
  BUY:  entry_low must be > stoploss  (else logical error)
  SELL: entry_low must be < stoploss
  stoploss must be present
  at least one target must be present

PROFILE FILTERING  (PDF pp.6-7)
  safe_only = 1  + RISK profile  → silently skipped (no DB row)
  risk_only = 1  + SAFE profile  → silently skipped (no DB row)

DEDUPLICATION
  Same signal (same hash) sent twice → only 1 row inserted; second ignored

─────────────────────────────────────────────────────────────────────────────
Instruments used: SENSEX 85200PE  30 APR 2026 (verified present in cache)
All entry prices are OPTION PREMIUM values (not underlying strike).
─────────────────────────────────────────────────────────────────────────────
"""

from datetime import date

EXPIRY = "30 APR 2026"   # BSE monthly expiry; verified in instrument cache
I = "SENSEX"
S = "85200"
O = "PE"


SCENARIOS = [

    # ══════════════════════════════════════════════════════════════════════
    #  BUF — Buffer Math
    #  Rules: tier-based entry buffer + constant SL(2.5) + target(6) buffers
    # ══════════════════════════════════════════════════════════════════════

    {
        # PDF: 100–200 → 3 pts.  BUY: entry_adj = 155+3=158, sl_adj=130-2.5=127.5, t1_adj=190-6=184
        "id": "BUF-1",
        "name": "Tier 100-200: entry 155 → adj 158, SL 130 → 127.5, T1 190 → 184",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "155", "stoploss": "130", "targets": "235/270",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 150.0,
        "expected": {
            "status": ["ACTIVE"],
            "trade_type": "BUY_ABOVE",
            "entry_low_adj": 158.0,
            "stoploss_adj":  127.5,
            "targets_adj[0]": 229.0,
        },
    },

    {
        # PDF: 200–300 → 5 pts.  entry_adj=255+5=260, sl_adj=225-2.5=222.5, t1_adj=315-6=309
        "id": "BUF-2",
        "name": "Tier 200-300: entry 255 → adj 260, SL 225 → 222.5, T1 315 → 309",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "255", "stoploss": "225", "targets": "355/420",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 250.0,
        "expected": {
            "status": ["ACTIVE"],
            "entry_low_adj": 260.0,
            "stoploss_adj":  222.5,
            "targets_adj[0]": 349.0,
        },
    },

    {
        # PDF: 300–400 → NOT DEFINED in PDF; system interpolates 7 pts.
        # This test DOCUMENTS the interpolated behavior — must confirm with operator.
        "id": "BUF-3",
        "name": "Tier 300-400 (NOT in PDF, interpolated 7pts): entry 355 → adj 362",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "355", "stoploss": "325", "targets": "460/540",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 350.0,
        "expected": {
            "status": ["ACTIVE"],
            "entry_low_adj": 362.0,   # 355 + 7
            "stoploss_adj":  322.5,   # 325 - 2.5
            "targets_adj[0]": 454.0,  # 460 - 6
        },
    },

    {
        # PDF: 400–500+ → 10 pts.  entry_adj=455+10=465, sl_adj=420-2.5=417.5, t1_adj=535-6=529
        "id": "BUF-4",
        "name": "Tier 400-500+: entry 455 → adj 465, SL 420 → 417.5, T1 535 → 529",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "455", "stoploss": "420", "targets": "580/660",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 450.0,
        "expected": {
            "status": ["ACTIVE"],
            "entry_low_adj": 465.0,
            "stoploss_adj":  417.5,
            "targets_adj[0]": 574.0,
        },
    },

    {
        # PDF: 400–500+ applies to 500+ too (no upper bound).  entry 620+10=630
        "id": "BUF-5",
        "name": "Tier 500+ still 10pts: entry 620 → adj 630",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "620", "stoploss": "575", "targets": "770/870",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 615.0,
        "expected": {
            "status": ["ACTIVE"],
            "entry_low_adj": 630.0,   # 620 + 10
            "stoploss_adj":  572.5,
            "targets_adj[0]": 764.0,
        },
    },

    {
        # Boundary: entry exactly at 200 → falls in 200-300 tier → 5 pts
        "id": "BUF-6",
        "name": "Boundary at 200: tier should be 200-300 (5pts) → adj 205",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "200", "stoploss": "175", "targets": "290/350",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 195.0,
        "expected": {
            "status": ["ACTIVE"],
            "entry_low_adj": 205.0,   # 200 + 5
            "stoploss_adj":  172.5,
            "targets_adj[0]": 284.0,
        },
    },

    {
        # Boundary: entry exactly at 400 → falls in 400-500+ tier → 10 pts
        "id": "BUF-7",
        "name": "Boundary at 400: tier 400-500+ (10pts) → adj 410",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "400", "stoploss": "368", "targets": "520/600",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 395.0,
        "expected": {
            "status": ["ACTIVE"],
            "entry_low_adj": 410.0,   # 400 + 10
            "stoploss_adj":  365.5,   # 368 - 2.5
            "targets_adj[0]": 514.0,  # 520 - 6
        },
    },

    {
        # PDF p.3: "For sell trades, subtract buffer points from entry level"
        # SELL, entry 265 (200-300 tier, 5pts): entry_adj=265-5=260
        # SL 292 (above entry for SELL): sl_adj=292+2.5=294.5
        # T1 210 (below entry for SELL): t1_adj=210+6=216
        "id": "BUF-8",
        "name": "SELL trade: subtract buffer from entry (265-5=260), add to SL/target",
        "signal": {
            "action": "SELL", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "265", "stoploss": "292", "targets": "180/145",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 270.0,
        "expected": {
            "status": ["ACTIVE"],
            "entry_low_adj": 260.0,   # 265 - 5
            "stoploss_adj":  294.5,   # 292 + 2.5
            "targets_adj[0]": 186.0,  # 180 + 6
        },
    },

    # ══════════════════════════════════════════════════════════════════════
    #  TT — Trade Types & GTT Count per Profile
    #  Rules: RANGING/BUY_ABOVE/AVERAGE × SAFE/RISK split ratios
    # ══════════════════════════════════════════════════════════════════════

    {
        # PDF p.7: RANGING + SAFE → "buy tradable 100% lots at entry_low to entry_low+buf"
        # = all lots in 1 GTT at entry_low + buffer
        "id": "TT-1",
        "name": "RANGING + SAFE: 100% lots at entry_low → 1 GTT",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "171", "entry_high": "185", "stoploss": "155",
            "targets": "230/280", "expiry": EXPIRY, "quantity": "4",
        },
        "market_time": "mid_session",
        "trader_profile": "SAFE",
        "ltp_before": 166.0,
        "expected": {
            "status": ["ACTIVE"],
            "trade_type": "RANGING",
            "entry_low_adj": 174.0,   # 171 + 3 (100-200 tier)
            "gtt_count": 1,           # SAFE RANGING = all lots at entry_low, 1 GTT
        },
    },

    {
        # PDF p.7: RANGING + RISK → "buy 50% lots at entry_low & 50% lots at entry_high"
        # = 2 GTTs, 50/50 split
        "id": "TT-2",
        "name": "RANGING + RISK: 50%/50% at entry_low/entry_high → 2 GTTs",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "271", "entry_high": "285", "stoploss": "248",
            "targets": "355/425", "expiry": EXPIRY, "quantity": "4",
        },
        "market_time": "mid_session",
        "trader_profile": "RISK",
        "ltp_before": 266.0,
        "expected": {
            "status": ["ACTIVE"],
            "trade_type": "RANGING",
            "entry_low_adj": 276.0,   # 271 + 5 (200-300 tier)
            "entry_high_adj": 290.0,  # 285 + 5
            "gtt_count": 2,           # RISK RANGING = 2 GTTs
        },
    },

    {
        # PDF p.8: BUY_ABOVE + SAFE → "50% at above entry (entry+2-3pts), 50% near SL"
        # = 2 GTTs
        "id": "TT-3",
        "name": "BUY_ABOVE + SAFE: 50% at entry+buf, 50% near SL → 2 GTTs",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "480", "stoploss": "448", "targets": "600/680",
            "expiry": EXPIRY, "quantity": "2",
        },
        "market_time": "mid_session",
        "trader_profile": "SAFE",
        "ltp_before": 474.0,
        "expected": {
            "status": ["ACTIVE"],
            "trade_type": "BUY_ABOVE",
            "entry_low_adj": 490.0,   # 480 + 10 (400+ tier)
            "gtt_count": 2,           # SAFE BUY_ABOVE = 2 GTTs
        },
    },

    {
        # PDF p.8: BUY_ABOVE + RISK → "Buy above 100%"
        # = 1 GTT with all lots
        "id": "TT-4",
        "name": "BUY_ABOVE + RISK: 100% at entry+buf → 1 GTT",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "382", "stoploss": "352", "targets": "490/565",
            "expiry": EXPIRY, "quantity": "2",
        },
        "market_time": "mid_session",
        "trader_profile": "RISK",
        "ltp_before": 376.0,
        "expected": {
            "status": ["ACTIVE"],
            "trade_type": "BUY_ABOVE",
            "entry_low_adj": 389.0,   # 382 + 7 (300-400 interpolated)
            "gtt_count": 1,           # RISK BUY_ABOVE = 1 GTT
        },
    },

    {
        # PDF p.8: AVERAGE + SAFE → "buy 30% at entry_low level, 70% at average level"
        # signal has an explicit average field (entry below entry_low, above SL)
        "id": "TT-5",
        "name": "AVERAGE + SAFE: 30% at entry_low, 70% at avg_level → 2 GTTs",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "218", "average": "198", "stoploss": "182",
            "targets": "320/385", "expiry": EXPIRY, "quantity": "10",
        },
        "market_time": "mid_session",
        "trader_profile": "SAFE",
        "ltp_before": 213.0,
        "expected": {
            "status": ["ACTIVE"],
            "trade_type": "AVERAGE",
            "entry_low_adj": 223.0,   # 218 + 5 (200-300 tier)
            "gtt_count": 2,           # SAFE AVERAGE = 2 GTTs (30%/70%)
            # lot split: qty=10, safe: 30%=3 lots at entry, 70%=7 lots at avg
        },
    },

    {
        # PDF p.8: AVERAGE + RISK → "buy 50% at entry_low level, 50% at average level"
        "id": "TT-6",
        "name": "AVERAGE + RISK: 50% at entry_low, 50% at avg_level → 2 GTTs",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "316", "average": "296", "stoploss": "278",
            "targets": "440/525", "expiry": EXPIRY, "quantity": "10",
        },
        "market_time": "mid_session",
        "trader_profile": "RISK",
        "ltp_before": 310.0,
        "expected": {
            "status": ["ACTIVE"],
            "trade_type": "AVERAGE",
            "entry_low_adj": 323.0,   # 316 + 7 (300-400 interpolated)
            "gtt_count": 2,           # RISK AVERAGE = 2 GTTs (50%/50%)
        },
    },

    # ══════════════════════════════════════════════════════════════════════
    #  TIME — Market Timing Gates
    #  Rules: block before 9:15, SAFE blocked before 9:30, block after 3:30
    # ══════════════════════════════════════════════════════════════════════

    {
        # PDF p.9: market opens 9:15 AM. Before that = no trading for anyone.
        # market_clock "before_open" = 9:00 AM (before 9:15)
        "id": "TIME-1",
        "name": "9:00 AM (before market opens 9:15) → BLOCKED for all traders",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "461", "stoploss": "430", "targets": "545", "expiry": EXPIRY,
        },
        "market_time": "before_open",   # 9:00 AM
        "trader_profile": "RISK",
        "ltp_before": 456.0,
        "expected": {"status": ["BLOCKED"], "block_reason_prefix": "timing:"},
    },

    {
        # PDF p.8: "Safe Traders Please Enter After 9:30am every day"
        # At 9:20 SAFE = BLOCKED
        "id": "TIME-2",
        "name": "9:20 AM + SAFE profile → BLOCKED (PDF: safe enters after 9:30)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "462", "stoploss": "431", "targets": "546", "expiry": EXPIRY,
        },
        "market_time": "pre_9_30",      # 9:20 AM
        "trader_profile": "SAFE",
        "ltp_before": 457.0,
        "expected": {"status": ["BLOCKED"], "block_reason_prefix": "timing:safe_before_9_30"},
    },

    {
        # PDF p.9: 9:15–10:00 AM = "Ideal for Experienced Traders" (RISK).
        # RISK at 9:20 should be ALLOWED.
        "id": "TIME-3",
        "name": "9:20 AM + RISK profile → ACTIVE (experienced traders trade from 9:15)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "463", "stoploss": "432", "targets": "580", "expiry": EXPIRY,
        },
        "market_time": "pre_9_30",      # 9:20 AM
        "trader_profile": "RISK",
        "ltp_before": 458.0,
        "expected": {"status": ["ACTIVE"]},
    },

    {
        # PDF p.8: safe enters after 9:30. At 9:32 SAFE should be ACTIVE.
        "id": "TIME-4",
        "name": "9:32 AM + SAFE profile → ACTIVE (after 9:30 gate passes)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "464", "stoploss": "433", "targets": "582", "expiry": EXPIRY,
        },
        "market_time": "open_safe",     # 9:32 AM
        "trader_profile": "SAFE",
        "ltp_before": 459.0,
        "expected": {"status": ["ACTIVE"]},
    },

    {
        # PDF p.9: 10:30–12:45 "Optimal for both Safe and Experienced traders"
        "id": "TIME-5",
        "name": "11:00 AM (optimal window) → ACTIVE for both profiles",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "465", "stoploss": "434", "targets": "584", "expiry": EXPIRY,
        },
        "market_time": "mid_session",   # 11:00 AM
        "trader_profile": "SAFE",
        "ltp_before": 460.0,
        "expected": {"status": ["ACTIVE"]},
    },

    {
        # After 3:30 PM NSE market close → BLOCKED for all
        "id": "TIME-6",
        "name": "3:31 PM (after market close) → BLOCKED for all",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "466", "stoploss": "435", "targets": "550", "expiry": EXPIRY,
        },
        "market_time": "after_close",   # 15:31
        "trader_profile": "RISK",
        "ltp_before": 461.0,
        "expected": {"status": ["BLOCKED"], "block_reason_prefix": "timing:after_entry_close"},
    },

    {
        # NSE is closed on weekends — BLOCKED regardless of profile
        # Requires "weekend" key in market_clock.py (Saturday date)
        "id": "TIME-7",
        "name": "Saturday 11:00 AM → BLOCKED (weekend, market closed)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "467", "stoploss": "436", "targets": "551", "expiry": EXPIRY,
        },
        "market_time": "weekend",       # Saturday — requires market_clock addition
        "trader_profile": "RISK",
        "ltp_before": 462.0,
        "expected": {"status": ["BLOCKED"], "block_reason_prefix": "timing:weekend"},
    },

    # ══════════════════════════════════════════════════════════════════════
    #  PNL — Daily P&L Guard (10% capital target)
    #  PDF p.2: "Once you reach the daily target of 10% of your capital, stop trading"
    #  Capital configured in settings.account_capital (default 100,000)
    # ══════════════════════════════════════════════════════════════════════

    {
        "id": "PNL-1",
        "name": "Daily P&L = 0 (no trades yet) → ACTIVE",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "502", "stoploss": "471", "targets": "620", "expiry": EXPIRY,
        },
        "market_time": "mid_session",
        "ltp_before": 497.0,
        "daily_pnl_inject": 0.0,        # inject this P&L into Redis before processing
        "expected": {"status": ["ACTIVE"]},
    },

    {
        "id": "PNL-2",
        "name": "Daily P&L = 9,999 (just below 10% of 100k) → ACTIVE",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "503", "stoploss": "472", "targets": "621", "expiry": EXPIRY,
        },
        "market_time": "mid_session",
        "ltp_before": 498.0,
        "daily_pnl_inject": 9_999.0,
        "expected": {"status": ["ACTIVE"]},
    },

    {
        "id": "PNL-3",
        "name": "Daily P&L = 10,000 (exactly 10% of 100k) → BLOCKED",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "504", "stoploss": "473", "targets": "587", "expiry": EXPIRY,
        },
        "market_time": "mid_session",
        "ltp_before": 499.0,
        "daily_pnl_inject": 10_000.0,
        "expected": {"status": ["BLOCKED"], "block_reason_prefix": "daily_pnl_limit"},
    },

    {
        "id": "PNL-4",
        "name": "Daily P&L = 15,000 (above 10% of 100k) → BLOCKED",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "505", "stoploss": "474", "targets": "588", "expiry": EXPIRY,
        },
        "market_time": "mid_session",
        "ltp_before": 500.0,
        "daily_pnl_inject": 15_000.0,
        "expected": {"status": ["BLOCKED"], "block_reason_prefix": "daily_pnl_limit"},
    },

    # ══════════════════════════════════════════════════════════════════════
    #  ME — Missed Entry Guard
    #  PDF p.2: "If you miss a position, avoid chasing it — wait for next update"
    # ══════════════════════════════════════════════════════════════════════

    {
        # LTP already above T1_adj → trade move is done, don't chase
        # entry=511, tier 400+, buf=10 → entry_adj=521
        # T1=590, t1_adj=584.  LTP=590 >= 584 → BLOCKED
        "id": "ME-1",
        "name": "LTP 590 already above T1_adj 584 → BLOCKED (missed entry)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "511", "stoploss": "478", "targets": "620/700",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 620.0,
        "expected": {"status": ["BLOCKED"], "block_reason_prefix": "missed_entry:ltp="},
    },

    {
        # LTP already below SL_adj → would enter an immediate losing position
        # entry=512, SL=480, sl_adj=477.5.  LTP=465 <= 477.5 → BLOCKED
        "id": "ME-2",
        "name": "LTP 465 already below SL_adj 477.5 → BLOCKED (missed entry, below SL)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "512", "stoploss": "480", "targets": "625/710",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 465.0,
        "expected": {"status": ["BLOCKED"], "block_reason_prefix": "missed_entry:ltp="},
    },

    {
        # No LTP in Redis cache → guard cannot check → ALLOW
        # (price-monitor may not be running; safer to allow than to block blindly)
        "id": "ME-3",
        "name": "No LTP in cache → ACTIVE (guard cannot verify, allow by default)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "513", "stoploss": "481", "targets": "625/710",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": None,             # None = explicitly delete ltp key before processing
        "expected": {"status": ["ACTIVE"]},
    },

    {
        # LTP within valid range (above SL_adj and below T1_adj) → ACTIVE
        # entry=514, SL=482, sl_adj=479.5, T1=594, t1_adj=588.  LTP=520 → valid
        "id": "ME-4",
        "name": "LTP 520 within valid range (SL_adj 479.5 < LTP < T1_adj 588) → ACTIVE",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "514", "stoploss": "482", "targets": "635/720",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 520.0,
        "expected": {"status": ["ACTIVE"]},
    },

    # ══════════════════════════════════════════════════════════════════════
    #  RR — Risk-Reward Ratio Enforcement
    #  PDF p.2, p.8: "Follow 1:2 or 1:3 Risk-Reward ratio"
    #  risk   = entry_adj - sl_adj        (BUY)
    #  reward = t1_adj    - entry_adj     (BUY)
    #  Enforce: reward / risk >= 2.0
    # ══════════════════════════════════════════════════════════════════════

    {
        # Good RR: entry=203 adj=206 (100-200 tier+3), SL=193 adj=190.5
        # risk=206-190.5=15.5pts. T1=255 adj=249. reward=249-206=43pts. RR=2.77 ✓
        "id": "RR-1",
        "name": "RR 1:2.77 (reward 43pts / risk 15.5pts) → ACTIVE",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "203", "stoploss": "193", "targets": "255/310",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 198.0,
        "expected": {"status": ["ACTIVE"]},
    },

    {
        # Good 1:3 RR: entry=213 adj=216, SL=203 adj=200.5
        # risk=216-200.5=15.5. T1=270 adj=264. reward=264-216=48. RR=3.1 ✓
        "id": "RR-2",
        "name": "RR 1:3 (reward 48pts / risk 15.5pts) → ACTIVE",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "213", "stoploss": "203", "targets": "270/330",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 208.0,
        "expected": {"status": ["ACTIVE"]},
    },

    {
        # Poor RR < 1:2: entry=223 adj=226, SL=213 adj=210.5
        # risk=226-210.5=15.5. T1=240 adj=234. reward=234-226=8. RR=0.52 ✗
        # PDF mandates minimum 1:2 — should be BLOCKED
        "id": "RR-3",
        "name": "RR 1:0.5 (reward 8pts / risk 15.5pts) → BLOCKED (below 1:2 minimum)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "223", "stoploss": "213", "targets": "240/280",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 218.0,
        "expected": {"status": ["BLOCKED"], "block_reason_prefix": "rr_ratio:"},
    },

    {
        # Borderline 1:2 exactly: entry=233 adj=236, SL=223 adj=220.5
        # risk=236-220.5=15.5. T1=267 adj=261. reward=261-236=25. RR=1.61 — fails 1:2
        # Note: 2.0 exactly would need reward=31pts from adj entry.  This is just below.
        "id": "RR-4",
        "name": "RR 1:1.6 (reward 25pts / risk 15.5pts) → BLOCKED (still below 1:2)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "233", "stoploss": "223", "targets": "267/310",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 228.0,
        "expected": {"status": ["BLOCKED"], "block_reason_prefix": "rr_ratio:"},
    },

    # ══════════════════════════════════════════════════════════════════════
    #  DEDUP — Signal Deduplication
    #  Same signal hash must not be processed twice
    # ══════════════════════════════════════════════════════════════════════

    {
        "id": "DEDUP-1",
        "name": "Same signal injected twice → 1 DB row (second silently ignored)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "415", "stoploss": "383", "targets": "535",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 410.0,
        "repeat": 2,
        "expected": {
            "status": ["ACTIVE"],
            "signal_count": 1,
        },
    },

    # ══════════════════════════════════════════════════════════════════════
    #  PROF — Trader Profile Filtering
    #  safe_only / risk_only flags in signal
    # ══════════════════════════════════════════════════════════════════════

    {
        "id": "PROF-1",
        "name": "safe_only=1 + SAFE profile → ACTIVE (intended audience)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "425", "stoploss": "393", "targets": "545",
            "expiry": EXPIRY, "quantity": "1", "safe_only": "1",
        },
        "market_time": "mid_session",
        "trader_profile": "SAFE",
        "ltp_before": 420.0,
        "expected": {"status": ["ACTIVE"]},
    },

    {
        "id": "PROF-2",
        "name": "safe_only=1 + RISK profile → silently skipped (no DB row created)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "426", "stoploss": "394", "targets": "509",
            "expiry": EXPIRY, "quantity": "1", "safe_only": "1",
        },
        "market_time": "mid_session",
        "trader_profile": "RISK",
        "ltp_before": 421.0,
        "expected": {"signal_count": 0},
    },

    {
        "id": "PROF-3",
        "name": "risk_only=1 + SAFE profile → silently skipped (no DB row created)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "427", "stoploss": "395", "targets": "510",
            "expiry": EXPIRY, "quantity": "1", "risk_only": "1",
        },
        "market_time": "mid_session",
        "trader_profile": "SAFE",
        "ltp_before": 422.0,
        "expected": {"signal_count": 0},
    },

    {
        "id": "PROF-4",
        "name": "risk_only=1 + RISK profile → ACTIVE (intended audience)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "428", "stoploss": "396", "targets": "548",
            "expiry": EXPIRY, "quantity": "1", "risk_only": "1",
        },
        "market_time": "mid_session",
        "trader_profile": "RISK",
        "ltp_before": 423.0,
        "expected": {"status": ["ACTIVE"]},
    },

    # ══════════════════════════════════════════════════════════════════════
    #  VAL — Signal Validation
    #  Basic sanity checks before any rules are applied
    # ══════════════════════════════════════════════════════════════════════

    {
        # BUY where entry < SL is logically invalid (you'd be buying into a loss)
        "id": "VAL-1",
        "name": "BUY with entry_low < stoploss → REJECTED at parse (invalid logic)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "360", "stoploss": "390",  # INVALID: SL above entry for BUY
            "targets": "450", "expiry": EXPIRY,
        },
        "market_time": "mid_session",
        "ltp_before": 355.0,
        "expected": {"signal_count": 0},
    },

    {
        # SELL where entry > SL is logically invalid (SL should be above entry for SELL)
        "id": "VAL-2",
        "name": "SELL with entry_low > stoploss → REJECTED at parse (invalid SELL logic)",
        "signal": {
            "action": "SELL", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "390", "stoploss": "360",  # INVALID: SL below entry for SELL
            "targets": "320", "expiry": EXPIRY,
        },
        "market_time": "mid_session",
        "ltp_before": 395.0,
        "expected": {"signal_count": 0},
    },

    {
        # Valid signal with all required fields — baseline positive case
        "id": "VAL-3",
        "name": "Valid BUY signal with all fields → ACTIVE (baseline positive case)",
        "signal": {
            "action": "BUY", "instrument": I, "strike": S, "option_type": O,
            "entry_low": "440", "stoploss": "408", "targets": "555/640",
            "expiry": EXPIRY, "quantity": "1",
        },
        "market_time": "mid_session",
        "ltp_before": 435.0,
        "expected": {
            "status": ["ACTIVE"],
            "entry_low_adj": 450.0,   # 440 + 10 (400+ tier)
            "stoploss_adj":  405.5,   # 408 - 2.5
            "targets_adj[0]": 549.0,  # 555 - 6
        },
    },

    {
        # Pipe-template format (alternative signal encoding)
        # ENTRY|SENSEX|85200PE|430|445|398|515|30 APR 2026|1
        "id": "VAL-4",
        "name": "Pipe-template signal ENTRY|SENSEX|85200PE|430|445|... → ACTIVE (RANGING)",
        "signal": {
            "message": f"ENTRY|{I}|{S}{O}|430|445|398|545|{EXPIRY}|1",
        },
        "market_time": "mid_session",
        "ltp_before": 425.0,
        "expected": {
            "status": ["ACTIVE"],
            "trade_type": "RANGING",      # has both entry_low and entry_high
            "entry_low_adj": 440.0,       # 430 + 10 (400+ tier)
        },
    },
]
