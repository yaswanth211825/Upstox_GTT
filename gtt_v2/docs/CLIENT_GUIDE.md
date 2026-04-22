# GTT Trading System — Client Guide

**Version:** 2.0 &nbsp;|&nbsp; **Last updated:** April 2026  
**Prepared by:** Amiron Technologies &nbsp;|&nbsp; [www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; +91 93912 00553

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [How to Send a Signal](#2-how-to-send-a-signal)
3. [Signal Lifecycle — Full Flow](#3-signal-lifecycle--full-flow)
4. [All Signal Statuses Explained](#4-all-signal-statuses-explained)
5. [Blocking & Rejection Messages — Every Case With Examples](#5-blocking--rejection-messages--every-case-with-examples)
6. [Auto-Cancel & Expiry Scenarios](#6-auto-cancel--expiry-scenarios)
7. [Manual Actions from the Dashboard](#7-manual-actions-from-the-dashboard)
8. [Price Adjustment (Buffer) Rules](#8-price-adjustment-buffer-rules)
9. [Dashboard Reference](#9-dashboard-reference)
10. [Environment Limits Quick Reference](#10-environment-limits-quick-reference)

---

## 1. System Overview

The GTT Trading System receives trade signals (via Telegram or admin API), validates them through multiple safety gates, and automatically places **Good-Till-Triggered (GTT)** orders on Upstox. The system manages the full lifecycle — from signal receipt to position exit — and tracks all P&L in a PostgreSQL database.

**Services running continuously:**

| Service | What it does |
|---|---|
| **Trading Engine** | Reads signals from Redis queue → validates → places GTT on Upstox |
| **Price Monitor** | Watches live market prices → detects if entry was never filled → auto-cancels stale GTTs |
| **Order Tracker** | Listens to Upstox portfolio WebSocket → updates signal status when orders fill |
| **Admin API** | REST API + web dashboard for monitoring and manual control |

<br>

> *Amiron Technologies &nbsp;|&nbsp; [www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; Contact: +91 93912 00553*

---

## 2. How to Send a Signal

Signals can be sent in two ways:

### Method 1 — Telegram (Pipe-Delimited Template)

Send a message to the configured Telegram group in this exact format:

```
ENTRY|UNDERLYING|STRIKEOPT|ENTRY_LOW|ENTRY_HIGH|STOPLOSS|TARGET|EXPIRY|QUANTITY
```

**Field definitions:**

| Position | Field | Required | Description |
|---|---|---|---|
| 1 | Verb | Yes | `ENTRY`, `SIGNAL`, `BUY`, or `SELL` |
| 2 | Underlying | Yes | `NIFTY`, `BANKNIFTY`, or `SENSEX` |
| 3 | Strike + Type | Yes | e.g. `23500CE` or `48000PE` |
| 4 | Entry Low | Yes | Minimum entry price (option premium) |
| 5 | Entry High | Yes | Maximum entry price — same as entry low for exact entry; different for a ranging zone |
| 6 | Stop Loss | Yes | Stop loss price |
| 7 | Target | Yes | Target price (T1) |
| 8 | Expiry | Yes | Date in `YYYY-MM-DD` format |
| 9 | Quantity | No | Number of lots (default: 1) |

**Rules:**
- If Entry High = Entry Low → **exact entry** (BUY ABOVE mode, GTT triggers at that exact price)
- If Entry High > Entry Low → **ranging entry** (GTT triggers at the midpoint of the range)
- Verb `ENTRY` or `SIGNAL` → BUY direction
- Verb `SELL` → SELL direction

---

**Example 1 — Exact BUY entry on NIFTY CE:**
```
ENTRY|NIFTY|23500CE|145|145|120|210|2026-04-24|1
```
Buy 1 lot of NIFTY 23500 CE. Entry at exactly ₹145. SL at ₹120. Target at ₹210. Expires 24 Apr 2026.

---

**Example 2 — Ranging entry (entry zone ₹220–₹240) on BANKNIFTY PE:**
```
ENTRY|BANKNIFTY|51000PE|220|240|190|310|2026-04-24|1
```
Buy 1 lot of BANKNIFTY 51000 PE. Entry zone ₹220–₹240 (GTT triggers at midpoint ₹230). SL at ₹190. Target at ₹310.

---

**Example 3 — SELL signal on SENSEX CE:**
```
SELL|SENSEX|72000CE|350|350|385|280|2026-04-24|1
```
Sell 1 lot of SENSEX 72000 CE at ₹350. SL at ₹385 (above entry for SELL). Target at ₹280 (below entry).

---

### Method 2 — Admin API (Manual Signal)

Use the dashboard or call the REST endpoint directly:

```
POST /signals/manual
Authorization: Bearer <your-admin-token>
Content-Type: application/json
```

```json
{
  "action": "BUY",
  "underlying": "NIFTY",
  "strike": 23500,
  "option_type": "CE",
  "entry_low": 145.0,
  "entry_high": 145.0,
  "stoploss": 120.0,
  "targets": [210.0],
  "expiry": "2026-04-24",
  "quantity_lots": 1,
  "product": "D"
}
```

**`product` values:**

| Value | Meaning |
|---|---|
| `D` | Delivery (carry overnight) |
| `I` | Intraday |

Multiple targets are supported — pass an array:
```json
"targets": [210.0, 250.0, 300.0]
```

<br>

> *Amiron Technologies &nbsp;|&nbsp; [www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; Contact: +91 93912 00553*

---

## 3. Signal Lifecycle — Full Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          SIGNAL RECEIVED                                    │
│                   (Telegram or /signals/manual API)                         │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  1. PARSE       │  Missing fields? malformed?
                    │  & VALIDATE     │──────────────────────────► Silently dropped
                    └────────┬────────┘                            (not saved to DB)
                             │
                             ▼
                    ┌─────────────────┐
                    │  2. DEDUP       │  Same instrument/strike/
                    │  CHECK          │  entry/SL/targets seen     ► [SKIPPED]
                    │  (Redis + DB)   │  in last 24 hours?          not saved again
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  3. TARGETS     │  Signal has no targets?
                    │  REQUIRED       │──────────────────────────► [BLOCKED]
                    └────────┬────────┘                            reason: no_targets
                             │
                             ▼
                    ┌─────────────────┐
                    │  4. TIMING      │  Before 9:15 AM / weekend /
                    │  GATE           │  SAFE profile before 9:30 /──► [BLOCKED]
                    │                 │  after 3:30 PM?               reason: timing:*
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  5. DAILY P&L   │  Profit OR loss already
                    │  GUARD          │  hit ±10% of capital?────────► [BLOCKED]
                    │                 │                                reason: daily_pnl_limit
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  6. R:R CHECK   │  Reward ÷ Risk < 2.0?
                    │  (raw prices)   │──────────────────────────────► [BLOCKED]
                    │                 │                                reason: rr_ratio:X.XX
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  7. INSTRUMENT  │  Strike or expiry not in
                    │  LOOKUP         │  Upstox instrument master?───► [BLOCKED]
                    └────────┬────────┘                               reason: instrument_not_found
                             │
                             ▼
                    ┌─────────────────┐
                    │  8. MISSED      │  Current LTP already past
                    │  ENTRY GUARD    │  entry zone?─────────────────► [BLOCKED]
                    └────────┬────────┘                               reason: missed_entry:*
                             │
                             ▼
                    ┌─────────────────┐
                    │  9. BUFFER      │  Apply buffer to SL and
                    │  APPLIED        │  Target (NOT to entry).
                    └────────┬────────┘  RANGING → entry = midpoint
                             │
                             ▼
                    ┌─────────────────┐
                    │ 10. RATE LIMIT  │  Too many GTT calls in
                    │  CHECK          │  last time window?───────────► [FAILED]
                    └────────┬────────┘                               reason: rate_limited
                             │
                             ▼
                    ┌─────────────────┐
                    │ 11. GTT PLACED  │  Upstox rejects order?───────► [FAILED]
                    │  ON UPSTOX      │  No GTT IDs returned?─────────► [FAILED]
                    └────────┬────────┘
                             │ GTT IDs received
                             ▼
                         [PENDING]
               GTT is live on Upstox.
               Entry leg not yet triggered.
                             │
                             │  [Portfolio WebSocket: entry leg fills]
                             ▼
                          [ACTIVE]
               Position is open.
               SL + Target GTT now monitoring.
                             │
              ┌──────────────┼──────────────────┐
              │              │                  │
    [Portfolio WS:   [Portfolio WS:    [Manual from dashboard]
    target fills]    SL fills]
              │              │                  │
              ▼              ▼                  ▼
      [TARGET1_HIT]   [STOPLOSS_HIT]       [CANCELLED]
      Trade closed     Trade closed       GTT cancelled +
      at profit.       at loss.           exit order placed
```

<br>

> *Amiron Technologies &nbsp;|&nbsp; [www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; Contact: +91 93912 00553*

---

## 4. All Signal Statuses Explained

| Status | Colour | What it means |
|---|---|---|
| **PENDING** | Yellow | GTT is live on Upstox. Entry leg has not triggered yet. The system is waiting for the market price to reach the entry trigger. |
| **ACTIVE** | Green | Entry leg triggered — a position is open. The SL and Target GTT legs are now active and will execute automatically. |
| **TARGET1_HIT** | Dark green | First target was hit. The system exited the trade at a profit. |
| **TARGET2_HIT** | Dark green | Second target hit (if multiple targets were set). |
| **TARGET3_HIT** | Dark green | Third target hit. |
| **STOPLOSS_HIT** | Red | Stop loss triggered. The trade was exited at a loss. |
| **BLOCKED** | Light red | The signal was rejected before any GTT was placed on Upstox. No order was created. The reason column tells you exactly why. |
| **FAILED** | Dark red | The system tried to place a GTT but it failed (Upstox API error or empty response). No live order exists. |
| **CANCELLED** | Grey | Manually cancelled from the dashboard. GTT was deleted from Upstox. |
| **EXPIRED** | Light grey | GTT was never triggered within the allowed time window and was automatically removed — OR the price hit the target before the entry ever filled. |

**Who sets each status:**

| Status | Set by |
|---|---|
| `PENDING` | Trading Engine — after successful GTT placement on Upstox |
| `BLOCKED` | Trading Engine — validation failed before GTT was placed |
| `FAILED` | Trading Engine — GTT placement returned an error or empty IDs |
| `ACTIVE` | Order Tracker — Upstox Portfolio WebSocket detects entry leg filled |
| `TARGET_HIT` / `STOPLOSS_HIT` | Order Tracker — Upstox Portfolio WebSocket detects exit leg filled |
| `CANCELLED` | Admin API — triggered by the Cancel or Exit button in the dashboard |
| `EXPIRED` | Trading Engine reconcile loop (every 2 min) OR Price Monitor auto-cancel |

<br>

> *Amiron Technologies &nbsp;|&nbsp; [www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; Contact: +91 93912 00553*

---

## 5. Blocking & Rejection Messages — Every Case With Examples

Every time a signal is blocked or rejected, the **reason is stored in the database** and shown on the dashboard under the signal row. Below is every possible message with a worked example.

---

### BLOCK 1 — No Targets Defined

**When it happens:** Signal was sent without any target price.

**Dashboard message:**
> Signal has no targets defined — GTT requires at least one target

**Why blocked:** A GTT order requires 3 legs: Entry + Target + Stoploss. Without a target, the order structure is incomplete and cannot be built.

**Example signal that triggers this block:**
```
ENTRY|NIFTY|23500CE|145|145|120||2026-04-24|1
```
The target field (position 7) is empty.

**Fix:** Always include at least one target price.
```
ENTRY|NIFTY|23500CE|145|145|120|210|2026-04-24|1
```

---

### BLOCK 2 — Timing Gate: Before Market Open

**When it happens:** Signal received before 9:15 AM IST on a weekday.

**Dashboard message:**
> Market not open yet — signal received before 9:15 AM IST

**Example:** Signal sent at 8:45 AM IST:
```
ENTRY|NIFTY|23500CE|145|145|120|210|2026-04-24|1
```
Blocked. The market opens at 9:15 AM. Signals received before this time are blocked and not queued.

**Fix:** Send signals only after 9:15 AM IST on trading days.

---

### BLOCK 3 — Timing Gate: SAFE Profile Before 9:30 AM

**When it happens:** The account is configured as `SAFE` profile and the signal arrives between 9:15 AM and 9:30 AM IST.

**Dashboard message:**
> SAFE profile cannot trade before 9:30 AM IST

**Example:** Signal sent at 9:22 AM IST on a SAFE profile account:
```
ENTRY|BANKNIFTY|51000PE|220|220|190|310|2026-04-24|1
```
Blocked. SAFE accounts wait for initial opening volatility to settle before entering trades.

**Fix:** Wait until 9:30 AM IST before sending signals on SAFE profile accounts.

---

### BLOCK 4 — Timing Gate: After Entry Close

**When it happens:** Signal received after 3:30 PM IST. New entries are not accepted after this time because positions would carry overnight risk.

**Dashboard message:**
> Entry window closed — no new GTTs after 3:30 PM IST

**Example:** Signal sent at 3:45 PM IST:
```
ENTRY|SENSEX|72000CE|350|350|310|430|2026-04-24|1
```
Blocked.

**Fix:** All entry signals must be sent before 3:30 PM IST.

---

### BLOCK 5 — Timing Gate: Weekend

**When it happens:** Signal received on Saturday or Sunday.

**Dashboard message:**
> Market closed on weekends

**Example:** Signal sent on Saturday at 11:00 AM:
```
ENTRY|NIFTY|23500CE|145|145|120|210|2026-04-27|1
```
Blocked. NSE and BSE are closed on weekends.

---

### BLOCK 6 — Daily P&L Limit Reached

**When it happens:** The account's total P&L (realized + unrealized) for the day has crossed ±10% of available capital. This protects against runaway losses and over-trading on winning days.

**Dashboard message:**
> Daily P&L limit reached (±10% of capital). No more trades today.

**How the limit is calculated:**
- Capital = Available equity margin fetched from Upstox at 9:15 AM each day
- Limit threshold = ±10% of that capital
- Example: ₹1,00,000 capital → limit = ₹10,000 (loss) or +₹10,000 (profit cap)

**Example scenario — loss limit:**
- Capital: ₹1,00,000
- Three trades closed with a combined loss of −₹10,500
- Fourth signal arrives:
```
ENTRY|NIFTY|23500CE|145|145|120|210|2026-04-24|1
```
Blocked. Daily loss limit exceeded.

**Example scenario — profit cap:**
- Capital: ₹1,00,000
- Profitable day, combined P&L reaches +₹10,200
- Next signal arrives → Blocked. Profit cap reached.

**Fix:** This is an automatic safety feature. The limit resets the next trading day at 9:15 AM.

---

### BLOCK 7 — Risk:Reward Ratio Too Low

**When it happens:** The potential reward divided by the potential risk is less than 2.0 (minimum 1:2 R:R required).

**Dashboard message:**
> Risk:Reward = 1.45 (minimum 2.0 required). Target is too close to entry.

**Formula for BUY signals:**
```
Risk   = Entry Low − Stoploss
Reward = Target − Entry Low
R:R    = Reward ÷ Risk   →  must be ≥ 2.0
```

**Formula for SELL signals:**
```
Risk   = Stoploss − Entry Low
Reward = Entry Low − Target
R:R    = Reward ÷ Risk   →  must be ≥ 2.0
```

**Note:** R:R is always calculated using the raw signal prices — never the buffer-adjusted prices.

**Example — blocked (R:R = 1.0):**
```
ENTRY|NIFTY|23500CE|145|145|120|170|2026-04-24|1
```
- Risk = 145 − 120 = 25 pts
- Reward = 170 − 145 = 25 pts
- R:R = 1.0 → **BLOCKED**

**Example — passes (R:R = 2.0):**
```
ENTRY|NIFTY|23500CE|145|145|120|195|2026-04-24|1
```
- Risk = 145 − 120 = 25 pts
- Reward = 195 − 145 = 50 pts
- R:R = 2.0 → **PASSES**

**Fix:** If your stoploss is N points below entry, your target must be at least 2×N points above entry.

---

### BLOCK 8 — Instrument Not Found

**When it happens:** The strike price or expiry date does not exist in Upstox's instrument master file. The instrument master is downloaded fresh every day at startup.

**Dashboard message:**
> Strike or expiry not found in Upstox instrument master

**Example — wrong expiry date:**
```
ENTRY|NIFTY|23500CE|145|145|120|210|2026-04-30|1
```
Blocked. NIFTY weekly expiry for that week is 2026-04-24 (Thursday), not April 30.

**Example — strike not in standard interval:**
```
ENTRY|NIFTY|23501CE|145|145|120|210|2026-04-24|1
```
Blocked. NIFTY strikes are in multiples of 50. Strike 23501 does not exist.

**Fix:** Use the exact contract expiry date (NIFTY/BANKNIFTY expire on Thursdays, SENSEX on Fridays) and use standard strike intervals (NIFTY: multiples of 50, BANKNIFTY: multiples of 100, SENSEX: multiples of 100).

---

### BLOCK 9 — Missed Entry (Price Already Moved)

**When it happens:** By the time the signal is processed, the current live market price has already moved past the entry zone. Placing a GTT at that price would trigger immediately — not wait — which defeats the purpose of the order.

**Dashboard message:**
> Missed entry — price already moved past entry zone: ltp=162.5 > entry_high=145.0

**Example:** Signal specifies entry at ₹145, but current LTP is ₹163:
```
ENTRY|NIFTY|23500CE|145|145|120|210|2026-04-24|1
```
Blocked. The entry opportunity has already passed.

**Fix:** Re-evaluate current market conditions. If the trade is still valid at the new price level, send a fresh signal with updated entry, SL, and target.

---

### FAIL 1 — Upstox Rate Limit Hit

**When it happens:** Too many GTT API calls have been made in a short window. The system has a built-in rate limiter to stay within Upstox API quotas.

**Dashboard message:**
> GTT rate limit hit — too many orders in short time window

**Status shown:** `FAILED`

**Fix:** The rate limit clears automatically after a short period. If a burst of signals was sent, re-send the ones marked FAILED after a minute.

---

### FAIL 2 — Upstox Rejected the GTT

**When it happens:** Upstox API returned an explicit error when the system attempted to place the GTT (e.g. insufficient margin, contract already expired, market closed for that instrument).

**Dashboard message:**
> Upstox rejected GTT: [exact error text returned by Upstox]

**Status shown:** `FAILED`

**Common Upstox rejection causes:**
- Insufficient margin in the trading account
- The option contract's expiry has already passed
- A trading halt is in effect for that instrument
- The Upstox access token has expired (re-login required)

**Fix:** Read the exact Upstox message displayed on the dashboard. For margin issues, add funds to your Upstox account. For an expired token, log in to Upstox again and update the access token in the system configuration.

---

### FAIL 3 — GTT Placed But No IDs Returned

**When it happens:** The system called the Upstox GTT API and received a successful (HTTP 200) response, but the response body contained no GTT order IDs. This is an unusual Upstox API edge case.

**Dashboard message:**
> Upstox returned no GTT IDs after placement — order may not have been created

**Status shown:** `FAILED`

**Fix:** Log in to the Upstox portal directly and check whether the GTT order was actually created. If it was created, contact support to reconcile the IDs. If it was not created, re-send the signal.

<br>

> *Amiron Technologies &nbsp;|&nbsp; [www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; Contact: +91 93912 00553*

---

## 6. Auto-Cancel & Expiry Scenarios

These scenarios are handled **automatically by the system** — no manual action is required.

---

### AUTO-CANCEL 1 — Entry Not Filled, Price Hit Target

**What happens:** The signal is PENDING (GTT placed, waiting for entry). The live price moves all the way to the first target without ever touching the entry price. The entry leg was never triggered — the trade opportunity was missed.

**System actions (automatic):**
1. Price Monitor detects: LTP ≥ T1 (for BUY) or LTP ≤ T1 (for SELL), and signal status is still PENDING
2. System cancels all GTT order IDs on Upstox
3. Signal is marked **EXPIRED**
4. Reason stored: `entry_not_filled_target_hit:ltp=210.50:target=210.0`

**Dashboard message:**
> Auto-cancelled: price hit target before entry leg was triggered. ltp=210.50:target=210.0

**Example scenario:**
- Signal placed: Buy NIFTY 23500 CE, Entry = ₹145, Target = ₹210
- Market opens gap-up — the option opens directly at ₹225
- The GTT entry at ₹145 was never triggered
- System automatically cancels the GTT and marks the signal EXPIRED

---

### AUTO-CANCEL 2 — GTT Stale (Not Triggered Within Time Limit)

**What happens:** A GTT has been PENDING for longer than the configured expiry window (default: 6 hours) without the entry ever triggering. The market moved away from the entry zone.

**System actions (automatic):**
1. Reconcile loop runs every 2 minutes
2. Any PENDING signal older than 6 hours is cancelled on Upstox
3. Signal is marked **EXPIRED**
4. Reason stored: `stale_expiry`

**Dashboard message:**
> GTT auto-expired — entry was never triggered within the allowed time window

**Example scenario:**
- Signal sent at 10:00 AM, entry set at ₹145
- By 4:00 PM (6 hours later) the option price never reached ₹145
- System auto-cancels and marks EXPIRED

<br>

> *Amiron Technologies &nbsp;|&nbsp; [www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; Contact: +91 93912 00553*

---

## 7. Manual Actions from the Dashboard

### Cancel GTT

Available for signals in **PENDING** or **ACTIVE** status.

**What it does:**
1. Calls Upstox API to cancel all GTT order IDs linked to the signal
2. Marks the signal **CANCELLED** with reason `manual_cancel`

**When to use:** You change your market view and want to remove the pending order before it triggers.

---

### Exit Trade

Available **only** for signals in **ACTIVE** status (position is already open).

**What it does:**
1. Cancels the remaining GTT legs (SL + Target) on Upstox
2. Immediately places a **MARKET order** in the opposite direction to close the open position:
   - Open BUY position → places MARKET SELL
   - Open SELL position → places MARKET BUY
3. Marks signal **CANCELLED** with reason `manual_exit`

**When to use:** You want to close an open position immediately at the current market price — for example if breaking news changes the outlook.

> ⚠️ **Warning:** Market orders execute at the best available price at that moment. In a fast-moving or illiquid market this can be significantly different from the last traded price shown on screen.

<br>

> *Amiron Technologies &nbsp;|&nbsp; [www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; Contact: +91 93912 00553*

---

## 8. Price Adjustment (Buffer) Rules

The system applies a small **buffer** to the Stop Loss and Target prices before placing the GTT on Upstox. The entry price is **never changed**.

**Why:** GTT orders trigger when the price is reached but execute at the next available market price. The buffer creates a small safety margin to improve fill probability before the move reverses.

**Buffer is based on the option's premium price (entry_low of the signal):**

| Option Premium Range | Buffer Points Applied |
|---|---|
| ₹100 – ₹200 | 3 points |
| ₹200 – ₹300 | 5 points |
| ₹300 – ₹400 | 7 points |
| ₹400 and above | 10 points |

**How buffer is applied:**

For a **BUY** signal (buffer moves prices down, creating tighter safety margins):
- `SL adjusted   = SL raw − buffer`
- `Target adjusted = Target raw − buffer`

For a **SELL** signal (buffer moves prices up):
- `SL adjusted   = SL raw + buffer`
- `Target adjusted = Target raw + buffer`

**Entry price rules:**
- Exact entry (BUY ABOVE): GTT triggers at exactly `entry_low` — no change
- Ranging entry: GTT triggers at midpoint `(entry_low + entry_high) / 2` — no buffer added
- Buffer is **never** applied to entry under any condition

**Worked example — BUY signal, premium = ₹145 → buffer = 3 pts:**

| Price | Raw (from signal) | Adjusted (placed on Upstox) |
|---|---|---|
| Entry | ₹145.0 | ₹145.0 (unchanged) |
| Stop Loss | ₹120.0 | **₹117.0** (120 − 3) |
| Target 1 | ₹210.0 | **₹207.0** (210 − 3) |

**Important:** The R:R ratio check (Section 5, Block 7) always uses the **raw** prices from the signal, never the buffer-adjusted prices. Buffer is purely an execution detail.

<br>

> *Amiron Technologies &nbsp;|&nbsp; [www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; Contact: +91 93912 00553*

---

## 9. Dashboard Reference

Access the dashboard at: `http://<your-server>/`

The dashboard auto-refreshes every **15 seconds**.

| Section | What it shows |
|---|---|
| **Profile badge** (header) | Account profile — SAFE or RISK |
| **Active GTTs** | Count of signals currently in ACTIVE status (position open) |
| **Pending** | Count of signals in PENDING status (GTT placed, entry not yet triggered) |
| **Targets Hit** | Count of TARGET_HIT exits today |
| **SL Hit** | Count of STOPLOSS_HIT exits today |
| **Today P&L** | Live total P&L = unrealized (from Upstox positions API) + realized (from DB). Sub-label shows the breakdown as `unr ₹X + rel ₹Y`. |
| **Signals table** | Last 50 signals — instrument, action, raw prices, adjusted prices, trade type, status, block reason, GTT IDs, action buttons |
| **Live Positions** | Real-time open positions from Upstox with quantity, average price, LTP, unrealized and realized P&L |

**Status legend** (shown at the top of the dashboard for quick reference):

| Colour | Status | Meaning |
|---|---|---|
| Yellow | PENDING | GTT placed, waiting for entry |
| Green | ACTIVE | Position open |
| Dark green | TARGET_HIT | Closed at profit |
| Red | STOPLOSS_HIT | Closed at loss |
| Light red | BLOCKED | Rejected before placement |
| Dark red | FAILED | Placement error |
| Grey | CANCELLED | Manually cancelled |
| Light grey | EXPIRED | Auto-expired or entry missed |

**Error banner:** If the server is unreachable, a red banner appears at the top:
> Dashboard failed to load data: Failed to fetch. Check if the admin API server is running.

<br>

> *Amiron Technologies &nbsp;|&nbsp; [www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; Contact: +91 93912 00553*

---

## 10. Environment Limits Quick Reference

| Setting | Default | What it controls |
|---|---|---|
| `DAILY_PNL_LIMIT_PCT` | 10% | Block new trades when P&L crosses ±10% of capital |
| `GTT_EXPIRY_HOURS` | 6 hours | Auto-cancel PENDING GTTs older than this |
| `DEFAULT_QUANTITY` | 1 lot | Lots per trade (one order per signal) |
| `LOT_SIZE_NIFTY` | 75 | Shares per lot for NIFTY options |
| `LOT_SIZE_BANKNIFTY` | 15 | Shares per lot for BANKNIFTY options |
| `LOT_SIZE_SENSEX` | 20 | Shares per lot for SENSEX options |
| `BUFFER_TABLE` | `100:200:3,200:300:5,300:400:7,400:inf:10` | Buffer points per premium price tier |
| `TRADER_PROFILE` | SAFE | SAFE = no trades before 9:30 AM; RISK = trades from 9:15 AM |
| `DRY_RUN` | false | When true, signals are processed and logged but no real GTTs are placed on Upstox |

<br>

---

<div align="center">

**Amiron Technologies**

[www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; Contact: +91 93912 00553 &nbsp;|&nbsp; adminmanager@amirontech.com

*For support, queries, and assistance — reach out to us anytime.*

*© 2026 Amiron Technologies. All rights reserved.*

</div>
