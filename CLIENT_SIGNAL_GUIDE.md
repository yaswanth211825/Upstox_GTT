# Signal Format Guide for Telegram Group

This document explains how to send trading signals so the automated GTT system picks them up correctly and places orders on Upstox.

---

## Supported Instruments

| Name in message | What system understands |
|---|---|
| `NIFTY` or `NF` | NIFTY |
| `BANKNIFTY` or `BNF` | BANKNIFTY |
| `SENSEX` | SENSEX |

---

## Required Fields in Every Signal

Every signal must have all of these:

| Field | Example | Notes |
|---|---|---|
| **Instrument** | `NIFTY`, `BANKNIFTY`, `SENSEX` | Must be present |
| **Strike** | `24000`, `78500` | 4–6 digit number |
| **Option Type** | `CE` or `PE` | Also accepts `CALL` / `PUT` |
| **Action** | `BUY` or `SELL` | Defaults to BUY if not mentioned |
| **Entry Price** | `200` or `200-210` | Single value or range |
| **Stoploss** | `SL 185` or `STOPLOSS 185` | Required — signal is rejected without it |
| **Target 1** | `TGT 250` or `TARGET 250` | Required — must be at least 25 pts from entry |
| **Target 2, 3** | Optional | Additional targets are accepted |

---

## Signal Format (Recommended)

```
BUY NIFTY 24000CE
Entry: 200-210
SL: 175
Targets: 250 / 320 / 400
Expiry: 24th APRIL
```

```
BUY SENSEX 79000PE
Entry: 350
SL: 320
TGT1: 420  TGT2: 500  TGT3: 600
```

```
BANKNIFTY 52000CE BUY
Entry 180 to 190
Stoploss 155
Targets 240/310/390
```

All three formats above are correctly parsed by the AI.

---

## The 25-Point Rule for Target 1

> **Target 1 must be at least 25 points above the entry price.**

The system adds a buffer to every price before placing the order on Upstox (to ensure fills). This buffer is:
- **Entry:** +5 pts added
- **Target:** −6 pts subtracted

So if your entry is `200` and T1 is `210`:
- Adjusted entry = 205
- Adjusted T1 = 204
- T1 is now **below entry** → order is rejected

**Safe minimum gap: 25 pts between entry and T1.**

| Entry | Minimum T1 |
|---|---|
| 200 | 225 |
| 340 | 365 |
| 500 | 525 |

T2 and T3 can be anything — only T1 is checked.

---

## Expiry Date

- If you include an expiry, write it as: `24th APRIL` or `1st MAY`
- If no expiry is mentioned, the system auto-calculates:
  - NIFTY / BANKNIFTY → nearest **Tuesday**
  - SENSEX → nearest **Thursday**

---

## Re-entry Signals

Add any of these keywords and the system marks it as a re-entry:

```
RE-ENTRY BUY NIFTY 24000CE
Entry: 195
SL: 170
Targets: 240/310
```

Keywords recognised: `RE-ENTRY`, `RE-ENTER`, `REBUY`, `RE BUY`, `ADD MORE`

---

## Safe / Risk Profile Tags

If a signal is only for one type of trader, add the tag:

```
BUY NIFTY 24000CE Entry 200 SL 175 TGT 250/320
(SAFE TRADERS ONLY)
```

```
SENSEX 79000PE BUY Entry 350 SL 320 TGT 420/500
RISK ONLY
```

| Tag in message | Effect |
|---|---|
| `SAFE TRADERS ONLY` / `FOR SAFE TRADERS` | Only placed if account is set to SAFE profile |
| `RISK ONLY` / `JACKPOT` / `RISKY TRADERS` | Only placed if account is set to RISK profile |

---

## Messages That Are Automatically Ignored

These message types are filtered out — no order is placed:

| Message type | Example |
|---|---|
| Order activation confirmations | `ORDER ACTIVATED 👍 SENSEX 79000PE` |
| Target hit updates | `TARGET 1 DONE ✅` / `TGT DONE` |
| Stoploss hit updates | `SL HIT` / `STOP LOSS TRIGGERED` |
| Profit booking | `BOOK PROFIT` / `PROFIT BOOK` |
| General commentary | Any message without instrument + strike |

You do **not** need to avoid sending these — the system ignores them automatically.

---

## What Happens After You Send a Signal

```
Telegram message
    ↓
AI reads the message (2–8 seconds)
    ↓
Checks: valid instrument? stoploss present? T1 ≥ 25 pts from entry?
    ↓
Buffer applied to all prices
    ↓
GTT placed on Upstox
    ↓
Visible in dashboard: http://3.111.123.148:8080
```

---

## Why a Signal Might Be Rejected

| Reason shown in dashboard | Fix |
|---|---|
| `R:R ratio = X (min 2.0). T1 too close to entry.` | Increase T1 by at least 25 pts from entry |
| `Strike/expiry not found in Upstox` | Check strike exists for that expiry on Upstox |
| `Timing: Before market open (9:15 AM)` | Signal arrived before market hours |
| `Timing: SAFE profile — before 9:30 AM` | SAFE account waits until 9:30 AM |
| `Timing: After entry close (3:30 PM)` | Too late in the day to enter |
| `Daily P&L limit hit` | 10% daily loss limit reached — no more trades today |
| `Missed entry` | Price already moved past T1 before order was placed |

---

## Quick Checklist Before Sending a Signal

- [ ] Instrument name correct (NIFTY / BANKNIFTY / SENSEX)
- [ ] Strike is a 4–6 digit number
- [ ] CE or PE clearly mentioned
- [ ] Entry price included
- [ ] Stoploss included (SL / STOPLOSS)
- [ ] At least one target included
- [ ] T1 is at least 25 pts above entry
- [ ] Sent between 9:15 AM and 3:30 PM IST on weekdays
