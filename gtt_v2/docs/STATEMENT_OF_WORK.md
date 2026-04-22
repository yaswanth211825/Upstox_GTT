# Statement of Work
## Automated GTT Options Trading System

**Prepared by:** Amiron Technologies  
**Website:** [www.amirontech.com](https://www.amirontech.com/)  
**Contact:** +91 93912 00553 | adminmanager@amirontech.com  
**Document Date:** April 2026  
**Version:** 1.0

---

---

## 1. Project Overview

This Statement of Work covers the development, delivery, and future roadmap of an **Automated GTT (Good-Till-Triggered) Options Trading System** built for the Indian derivatives market (NIFTY, BANKNIFTY, SENSEX options on Upstox).

The system receives trade signals via Telegram or a web admin panel, automatically validates them through multiple safety layers, and places GTT orders directly on the client's Upstox brokerage account — without any manual intervention. It monitors live market prices, tracks positions in real-time, manages P&L daily, and provides full visibility through a web dashboard.

**The core platform (Phase 1) is fully built and ready for deployment.** Phase 2 outlines advanced features available to clients who subscribe to the extended package.

---

---

## 2. Scope of Work

### 2.1 Core Trading Engine (Phase 1 — Delivered)

- **Signal Reception:** Accepts trade signals via a configured Telegram group (pipe-delimited template) or directly via the Admin API. Signals are parsed, validated, and queued for processing automatically.
- **Multi-Layer Validation:** Every signal passes through 9 independent safety gates before any order is placed — covering timing, risk:reward ratio, daily P&L limits, instrument verification, and missed-entry detection.
- **GTT Order Placement:** Automatically builds and places a 3-leg GTT order on Upstox (Entry + Target + Stoploss) for each valid signal. One order per signal with configurable lot sizing.
- **Buffer Price Adjustment:** Applies configurable buffer points to Stop Loss and Target prices (based on the option's premium tier) to improve fill probability on Upstox GTT execution.
- **Duplicate Prevention:** Two-level deduplication (Redis cache + PostgreSQL hash) prevents the same signal from being placed more than once in a 24-hour window.

---

### 2.2 Live Market Monitoring (Phase 1 — Delivered)

- **Real-Time LTP Feed:** Connects to Upstox Market Data WebSocket to receive live Last Traded Price (LTP) for all active instruments.
- **Auto-Cancel on Missed Entry:** If a PENDING signal's price reaches the first target without the entry leg ever triggering (price gapped past entry), the system automatically cancels the GTT on Upstox and marks the signal EXPIRED.
- **Stale GTT Cleanup:** Any PENDING GTT older than the configured expiry window (default: 6 hours) is automatically cancelled and expired — runs every 2 minutes.
- **Near-SL Alert Logic:** Configurable buffer to detect when live price is approaching the stop loss zone.

---

### 2.3 Order & Position Tracking (Phase 1 — Delivered)

- **Portfolio WebSocket Integration:** Connects to Upstox's Portfolio Stream WebSocket to receive real-time order fill notifications.
- **Automatic Status Updates:** When the entry leg fills → status moves to ACTIVE. When the target or SL leg fills → status moves to TARGET_HIT or STOPLOSS_HIT automatically, with no manual action needed.
- **P&L Recording:** Fill prices are captured from actual Upstox trade data (weighted average fill price), and P&L is computed and stored per signal.

---

### 2.4 Daily Capital & P&L Guard (Phase 1 — Delivered)

- **Live Capital Fetch:** Available margin is fetched from Upstox at 9:15 AM IST every trading day and cached. This is used as the base for P&L limit calculations.
- **Daily P&L Guard:** Lua-atomic Redis script blocks new trades if the combined realized + unrealized P&L crosses ±10% of capital (configurable). Prevents runaway losses and over-trading on winning days.
- **Limit Reset:** Guard resets automatically the next trading day.

---

### 2.5 Admin Web Dashboard (Phase 1 — Delivered)

- **Live Overview Cards:** Active GTTs, Pending signals, Targets hit, SL hit, and Today's P&L — live combined (unrealized + realized) with breakdown.
- **Signals Table:** Full table of the last 50 signals with raw vs adjusted prices, trade type, status, block reason, and GTT IDs.
- **Status Legend:** Colour-coded legend explaining every status at a glance.
- **Live Positions Panel:** Real-time open positions from Upstox showing quantity, average price, LTP, and unrealized P&L.
- **Manual Controls:** Cancel GTT button (removes GTT from Upstox) and Exit Trade button (cancels GTT + places immediate market order to close position).
- **Error Banner:** Visible red alert if the server is unreachable.
- **Auto-Refresh:** Dashboard refreshes every 15 seconds automatically.
- **Manual Signal Posting:** Admin can post a signal directly from the dashboard without using Telegram.

---

### 2.6 Telegram Signal Broadcasting & Child Account Management (Phase 2 — Future Scope)

The following features are available as an extended package for clients who subscribe to Phase 2:

#### 2.6.1 Signal Broadcasting from Dashboard to Telegram

- Admin composes a trade signal directly from the web dashboard (instrument, entry, SL, targets, quantity).
- With a single click, the signal is:
  1. Formatted and sent automatically to one or more configured Telegram groups/channels.
  2. Simultaneously processed by the trading engine to place the GTT on the broker account.
- Admin can select which Telegram groups to broadcast to (e.g., Premium group, Free group, internal group — separately).
- Message formatting is professional and readable for group members.

#### 2.6.2 Child Account Management

- Admin can add and manage multiple child/sub broker accounts from the same dashboard.
- Each child account is linked to its own Upstox access token and tracked independently.
- When a signal is sent, the admin can select which accounts the trade should be placed on — all at once or individually.
- The dashboard shows each account's positions, P&L, and active GTTs separately.

#### 2.6.3 Unified Child Account Monitoring

- A consolidated view showing all child accounts in one screen.
- Per-account breakdown: Active GTTs, Pending orders, Today's P&L, Open positions.
- Admin can cancel or exit trades on any child account directly from the monitoring panel.

#### 2.6.4 One-Click: Trade + Signal Broadcast

- Single action from the dashboard that simultaneously:
  - Places the GTT on the broker (for all selected accounts), AND
  - Sends the signal message to all selected Telegram groups.
- No need to do these steps separately. Admin controls the entire flow from one screen.
- Option to send to Telegram only (announcement without placing a trade) or to broker only, or both.

---

---

## 3. Technical Deliverables

| Deliverable | Description |
|---|---|
| **Trading Engine Service** | Python/FastAPI + asyncio service. Reads signals from Redis Stream, validates through 9 safety gates, places GTT on Upstox. Runs as a background daemon. |
| **Price Monitor Service** | Upstox Market Data WebSocket consumer. Caches LTP in Redis. Auto-cancels missed-entry and stale GTTs. |
| **Order Tracker Service** | Upstox Portfolio WebSocket consumer. Receives real-time fill notifications. Updates signal status and records P&L. |
| **Admin REST API** | FastAPI-based REST API for signal management, account monitoring, and manual trade control. Bearer token authenticated. |
| **Admin Web Dashboard** | Single-page web application (HTML/JS) served by the Admin API. Live signal table, P&L cards, positions panel, manual signal entry, Cancel/Exit controls. |
| **PostgreSQL Database** | Stores all signals, statuses, GTT rules, price events, and daily P&L. Includes migrations. |
| **Redis Integration** | Used for signal queuing (Redis Stream), LTP cache, daily P&L guard (Lua atomic scripts), rate limiting, and dedup cache. |
| **Shared Library** | Common Python package: config, Upstox client, signal parser, buffer rules, timing gate, P&L guard, DB helpers. Used by all services. |
| **Docker Deployment** | All services containerised and managed via Docker Compose for single-host deployment. |
| **Client Documentation** | Full client guide covering signal format, all error messages, status definitions, flow diagrams, and buffer rules. |
| *(Phase 2)* **Telegram Broadcaster** | Dashboard-integrated Telegram sender. Compose and push signals to multiple groups in one click. |
| *(Phase 2)* **Child Account Module** | Multi-account manager. Linked accounts, per-account monitoring, consolidated P&L view. |

---

---

## 4. Project Phases

### Phase 1 — Core Platform (Completed)

| Step | Activity | Status |
|---|---|---|
| 1 | Upstox API integration (GTT placement, positions, funds, order tracking) | ✅ Done |
| 2 | Signal parsing engine (Telegram template + structured API) | ✅ Done |
| 3 | 9-gate validation pipeline (timing, R:R, P&L guard, instrument lookup, missed entry, etc.) | ✅ Done |
| 4 | Buffer rules engine with configurable price-tier table | ✅ Done |
| 5 | Redis Stream consumer with PEL reclaim and dedup | ✅ Done |
| 6 | Price Monitor + auto-cancel (stale GTT + missed entry) | ✅ Done |
| 7 | Portfolio WebSocket order tracker with status + P&L recording | ✅ Done |
| 8 | Daily capital refresh (Upstox API at 9:15 AM IST) | ✅ Done |
| 9 | Admin Dashboard (signals table, P&L cards, live positions, manual controls) | ✅ Done |
| 10 | Docker Compose single-host deployment setup | ✅ Done |
| 11 | Client documentation | ✅ Done |

---

### Phase 2 — Extended Features (On Subscription)

| Step | Activity | Estimated Timeline |
|---|---|---|
| 1 | Telegram bot setup and group broadcasting from dashboard | 1–2 weeks |
| 2 | Child account configuration and token management | 1 week |
| 3 | Multi-account GTT placement and per-account monitoring panel | 2 weeks |
| 4 | One-click Trade + Signal broadcast interface | 1 week |
| 5 | Testing and delivery | 1 week |

**Total Phase 2 Estimated Timeline: 5–7 weeks from engagement.**

---

---

## 5. Terms and Conditions

- **Hosting:** Infrastructure (VPS/Cloud server) costs are to be borne by the client. A minimum of a 2-core / 4 GB RAM VPS is recommended for running all services.
- **API Costs:** Upstox API access and any third-party API costs (AI providers, etc.) are separate from development fees and are the client's responsibility.
- **Access Token Management:** The Upstox access token must be refreshed by the client as required by Upstox's authentication policy. Amiron Technologies will provide documentation for token renewal.
- **Market Risk:** The system places orders on financial instruments. Amiron Technologies is not responsible for trading losses, missed trades due to broker API downtime, or Upstox platform outages.
- **Support:** Post-delivery support covers bug fixes, configuration changes, and retraining/reconfiguration. Support window is specified per plan below.
- **Source Code:** Source code is the intellectual property of Amiron Technologies unless a separate buyout agreement is executed.
- **Confidentiality:** Both parties agree to keep the terms of this engagement and all shared technical details confidential.

---

---

## 6. Pricing and Costs

### Phase 1 — Core Platform License

| Item | Cost |
|---|---|
| One-time setup, deployment, and license fee | **₹1,85,000** |
| Includes: Trading Engine, Price Monitor, Order Tracker, Admin Dashboard, all documentation | ✅ Included |
| Deployment assistance (Docker setup on client VPS) | ✅ Included |
| 3 months post-delivery bug fix support | ✅ Included |

---

### Monthly Operational Costs (Recurring — Paid by Client)

| Item | Estimated Cost |
|---|---|
| VPS / Cloud Server (2 core, 4 GB RAM minimum) | ₹2,500 – ₹4,000 / month |
| Upstox API access | As per Upstox's broker pricing |
| Amiron Technologies monthly maintenance & support | **₹7,000 / month** |

---

### Phase 2 — Extended Features (Telegram + Child Accounts)

| Item | Cost |
|---|---|
| Phase 2 development fee (Telegram broadcaster + child account module) | **₹1,23,000** |
| Estimated development time | 5–7 weeks |

---

### Complete Package Cost Summary

| Item | Amount |
|---|---|
| Phase 1 — Core Trading Platform (one-time) | ₹1,85,000 |
| Phase 2 — Telegram + Child Accounts (one-time) | ₹1,23,000 |
| **Total One-Time Investment (Phase 1 + Phase 2)** | **₹3,08,000** |
| Monthly Maintenance & Support | ₹7,000 / month |

> **Full platform, fully loaded — ₹3,08,000 one-time + ₹7,000/month.**

---

### Advance Payment Discount

Clients who pay 6 months of monthly maintenance in advance receive a **20% discount** on the maintenance fee.

| Payment Mode | Amount |
|---|---|
| Monthly (₹7,000 × 6) | ₹42,000 |
| After 20% advance discount | **₹33,600** |
| Saving | ₹8,400 |

> **Pay 6 months upfront and save ₹8,400.**

---

### Special Consideration — Subscription-Based Pricing Model

*We recognise that you are a content creator and YouTuber with an active audience, and we value the potential long-term relationship this partnership can bring. As a gesture of goodwill and mutual growth, we are open to a flexible pricing arrangement.*

**Prices and features listed in this document are open to negotiation.** We are willing to discuss scope, payment timelines, and feature priorities based on your specific requirements.

---

**As an alternative to the full one-time fee, we offer a Subscription-Based Pricing Model:**

| Component | Details |
|---|---|
| Per-Trade Fee | ₹25 per executed trade **OR** 0.05% of the traded value — **whichever is lower** |
| Development Fee (mandatory, non-negotiable) | **₹1,25,000 one-time** — covers system setup, deployment, and configuration |
| Monthly Maintenance | **₹7,000 / month** — mandatory regardless of pricing model chosen |

**How the per-trade fee works:**

> Example 1: Trade value = ₹20,000 → 0.05% = ₹10 → Per-trade cap = ₹25 → **Charged: ₹10** (lower of the two)
>
> Example 2: Trade value = ₹80,000 → 0.05% = ₹40 → Per-trade cap = ₹25 → **Charged: ₹25** (lower of the two)

This model is ideal if trade volumes are lower in the beginning and you prefer to keep upfront costs minimal.

---

**Pricing Model Comparison:**

| | One-Time Model | Subscription Model |
|---|---|---|
| Upfront development cost | ₹3,08,000 (Phase 1 + 2) | ₹1,25,000 (mandatory) |
| Per-trade charge | None | ₹25 or 0.05% of trade value (lower) |
| Monthly maintenance | ₹7,000 | ₹7,000 |
| Best suited for | High-volume traders | Growing / early-stage setup |

> *Note: The mandatory development fee of ₹1,25,000 and monthly maintenance of ₹7,000 apply under the subscription model regardless of trade volume. The per-trade fee is billed monthly based on the total number of trades executed that month.*

---

---

## 7. What Is Included vs. Not Included

### Included in Phase 1

- Full GTT auto-trading system (all 9 services and shared library)
- Telegram signal listener (parses signals sent to your Telegram group)
- Admin web dashboard with manual signal posting
- All safety gates (timing, R:R, P&L limit, instrument lookup, missed entry)
- Auto-cancel of unfilled GTTs
- Daily capital fetch from Upstox
- P&L tracking per signal and daily summary
- Docker deployment setup
- Client documentation (full guide)
- 3 months bug fix support

### Not Included in Phase 1 (Available in Phase 2)

- Sending signals **from dashboard to Telegram** (Phase 1 only listens — does not broadcast)
- Child/sub account management and monitoring
- One-click Trade + Telegram broadcast
- Mobile app

### Never Included

- Trading strategy or advice (this is an execution system, not a signal advisory service)
- Upstox account setup
- Server procurement
- Third-party API fees

---

---

## 8. Approvals

This document constitutes the agreed scope, deliverables, and terms between Amiron Technologies and the Client.

| | |
|---|---|
| **Client Name:** | ___________________________ |
| **Client Signature:** | ___________________________ |
| **Date:** | ___________________________ |

&nbsp;

| | |
|---|---|
| **Amiron Technologies Representative:** | B. Chandra Mouli |
| **Designation:** | Developer / Project Lead |
| **Signature:** | ___________________________ |
| **Date:** | April 2026 |

&nbsp;

---

<div align="center">

**Amiron Technologies**

[www.amirontech.com](https://www.amirontech.com/) &nbsp;|&nbsp; +91 93912 00553 &nbsp;|&nbsp; adminmanager@amirontech.com

*Building intelligent automation for modern businesses.*

*© 2026 Amiron Technologies. All rights reserved.*

</div>
