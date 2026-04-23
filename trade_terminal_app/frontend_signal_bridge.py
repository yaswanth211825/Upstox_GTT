"""
frontend_signal_bridge.py -- Lightweight HTTP bridge for frontend-originated trade signals.

This service bypasses Telegram and AI parsing by accepting structured signal JSON and
publishing it directly to the existing Redis stream consumed by gtt_strategy.py.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import re

from dotenv import load_dotenv
import redis

# Ensure project-root modules are importable when this file is run directly.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from latency import log_latency, now_ms, now_perf_ns, duration_ms
from settings import LOG_DIR


load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_STREAM_KEY = os.getenv("REDIS_STREAM_KEY", os.getenv("REDIS_STREAM_NAME", "raw_trade_signals"))

FRONTEND_BIND_HOST = os.getenv("FRONTEND_BIND_HOST", "127.0.0.1")
FRONTEND_BIND_PORT = int(os.getenv("FRONTEND_BIND_PORT", "8787"))
FRONTEND_AUTH_TOKEN = os.getenv("FRONTEND_AUTH_TOKEN", "")

logger = logging.getLogger("frontend_signal_bridge")
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _ch = logging.StreamHandler()
    _ch.setFormatter(_fmt)
    _fh = logging.FileHandler(LOG_DIR / "frontend_signal_bridge.log")
    _fh.setFormatter(_fmt)
    logger.addHandler(_ch)
    logger.addHandler(_fh)
logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def _send_json(handler: BaseHTTPRequestHandler, status_code: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.end_headers()
    handler.wfile.write(payload)


def _send_html(handler: BaseHTTPRequestHandler, status_code: int, body: str) -> None:
    payload = body.encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(payload)


def _required_missing(payload: dict[str, Any]) -> list[str]:
    required = [
        "action",
        "instrument",
        "strike",
        "option_type",
        "entry_low",
        "stoploss",
        "targets",
        "expiry",
    ]
    missing = []
    for field in required:
        value = payload.get(field)
        if value is None:
            missing.append(field)
        elif isinstance(value, str) and not value.strip():
            missing.append(field)
        elif field == "targets" and isinstance(value, list) and not value:
            missing.append(field)
    return missing


def _targets_to_str(targets: Any) -> str:
    if isinstance(targets, str):
                normalized = re.sub(r"[\s,]+", "/", targets.strip())
                normalized = re.sub(r"/+", "/", normalized)
                return normalized.strip("/")
    if isinstance(targets, list):
        return "/".join(str(t) for t in targets)
    return str(targets)


def _form_page() -> str:
        return """<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Upstox GTT Signal Entry</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
            background: #f5f5f5;
            color: #111;
            min-height: 100vh;
            padding: 32px 24px;
        }
        .wrap { max-width: 780px; margin: 0 auto; }
        .page-header { margin-bottom: 28px; }
        .page-header h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.02em; }
        .page-header p { font-size: 14px; color: #555; margin-top: 6px; line-height: 1.5; }
        .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
        .tag {
            font-size: 12px;
            padding: 3px 10px;
            border-radius: 999px;
            border: 1px solid #ddd;
            color: #444;
            background: #fff;
        }
        .card {
            background: #fff;
            border: 1px solid #e2e2e2;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 16px;
        }
        .section-title {
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.07em;
            color: #888;
            margin-bottom: 16px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(12, 1fr);
            gap: 16px;
        }
        .field { grid-column: span 6; }
        .field.full { grid-column: span 12; }
        .field.third { grid-column: span 4; }
        .label {
            display: block;
            font-size: 12px;
            font-weight: 500;
            color: #555;
            margin-bottom: 6px;
        }
        input, select, textarea {
            width: 100%;
            border-radius: 8px;
            border: 1px solid #ddd;
            background: #fff;
            color: #111;
            padding: 10px 12px;
            font-size: 14px;
            outline: none;
            transition: border-color 0.12s;
            font-family: inherit;
        }
        input:focus, select:focus, textarea:focus { border-color: #111; }
        textarea { min-height: 80px; resize: vertical; }
        .helper { font-size: 12px; color: #888; margin-top: 5px; line-height: 1.4; }
        .btn-group { display: flex; gap: 8px; flex-wrap: wrap; }
        .btn {
            border: 1px solid #ddd;
            background: #fff;
            color: #333;
            padding: 9px 18px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            font-family: inherit;
            transition: background 0.12s, border-color 0.12s, color 0.12s;
        }
        .btn:hover { border-color: #aaa; }
        .btn.active.buy  { background: #dcfce7; border-color: #16a34a; color: #15803d; }
        .btn.active.sell { background: #fee2e2; border-color: #dc2626; color: #b91c1c; }
        .btn.active.ce   { background: #dbeafe; border-color: #2563eb; color: #1d4ed8; }
        .btn.active.pe   { background: #fef3c7; border-color: #d97706; color: #b45309; }
        .divider { height: 1px; background: #eee; margin: 20px 0; }
        .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 20px; }
        .btn-submit {
            background: #111;
            color: #fff;
            border: 1px solid #111;
            padding: 11px 22px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            font-family: inherit;
            transition: background 0.12s;
        }
        .btn-submit:hover { background: #333; border-color: #333; }
        .btn-ghost {
            background: transparent;
            color: #555;
            border: 1px solid #ddd;
            padding: 11px 18px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            font-family: inherit;
            transition: border-color 0.12s, color 0.12s;
        }
        .btn-ghost:hover { border-color: #aaa; color: #111; }
        .status {
            margin-top: 16px;
            padding: 12px 14px;
            border-radius: 8px;
            background: #f9f9f9;
            color: #666;
            border: 1px solid #e2e2e2;
            font-size: 13px;
            white-space: pre-wrap;
            font-family: ui-monospace, "SF Mono", monospace;
        }
        .status.error   { background: #fff5f5; border-color: #fca5a5; color: #b91c1c; }
        .status.success { background: #f0fdf4; border-color: #86efac; color: #15803d; }
        .info-card {
            background: #fafafa;
            border: 1px solid #e2e2e2;
            border-radius: 8px;
            padding: 14px;
            font-size: 13px;
            color: #444;
            line-height: 1.6;
        }
        @media (max-width: 640px) {
            .field, .field.third { grid-column: span 12; }
        }
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"page-header\">
            <h1>Upstox GTT Direct Entry</h1>
            <p>Enter signal details below. Expiry is auto-calculated. Posts directly to the local bridge which publishes to Redis.</p>
            <div class=\"tags\">
                <span class=\"tag\">No Telegram</span>
                <span class=\"tag\">No AI</span>
                <span class=\"tag\">Direct Redis publish</span>
                <span class=\"tag\">Upstox GTT only</span>
            </div>
        </div>

        <div class=\"card\">
            <p class=\"section-title\">Expiry Rules</p>
            <div class=\"info-card\">
                NIFTY &amp; BANKNIFTY &mdash; first Tuesday after today.<br/>
                SENSEX &mdash; first Thursday after today.<br/>
                <span style=\"color:#888;\">The expiry field is auto-filled but remains editable.</span>
            </div>
        </div>

        <div class=\"card\">
            <form id=\"signalForm\">
                <input type=\"hidden\" name=\"action\" id=\"action\" value=\"BUY\" />
                <input type=\"hidden\" name=\"option_type\" id=\"option_type\" value=\"CE\" />
                <input type=\"hidden\" name=\"trace_id\" id=\"trace_id\" value=\"\" />
                <input type=\"hidden\" name=\"source_message_id\" id=\"source_message_id\" value=\"\" />
                <input type=\"hidden\" name=\"message\" id=\"message\" value=\"\" />

                <p class=\"section-title\">Signal</p>
                <div class=\"grid\">
                    <div class=\"field full\">
                        <span class=\"label\">Action</span>
                        <div class=\"btn-group\" data-toggle=\"action\">
                            <button class=\"btn active buy\" type=\"button\" data-value=\"BUY\">Buy</button>
                            <button class=\"btn sell\" type=\"button\" data-value=\"SELL\">Sell</button>
                        </div>
                    </div>

                    <div class=\"field third\">
                        <span class=\"label\">Index</span>
                        <select id=\"instrument\" name=\"instrument\">
                            <option value=\"NIFTY\">NIFTY</option>
                            <option value=\"SENSEX\">SENSEX</option>
                        </select>
                    </div>

                    <div class=\"field third\">
                        <span class=\"label\">Strike</span>
                        <input id=\"strike\" name=\"strike\" type=\"number\" placeholder=\"22400\" required />
                    </div>

                    <div class=\"field third\">
                        <span class=\"label\">Option Type</span>
                        <div class=\"btn-group\" data-toggle=\"option_type\">
                            <button class=\"btn active ce\" type=\"button\" data-value=\"CE\">CE</button>
                            <button class=\"btn pe\" type=\"button\" data-value=\"PE\">PE</button>
                        </div>
                    </div>

                    <div class=\"field third\">
                        <span class=\"label\">Expiry</span>
                        <input id=\"expiry\" name=\"expiry\" type=\"text\" required />
                        <div class=\"helper\">Auto-calculated from selected index.</div>
                    </div>

                    <div class=\"field third\">
                        <span class=\"label\">Entry Low</span>
                        <input id=\"entry_low\" name=\"entry_low\" type=\"number\" step=\"0.05\" placeholder=\"205\" required />
                    </div>

                    <div class=\"field third\">
                        <span class=\"label\">Stoploss</span>
                        <input id=\"stoploss\" name=\"stoploss\" type=\"number\" step=\"0.05\" placeholder=\"185\" required />
                    </div>

                    <div class=\"field third\">
                        <span class=\"label\">Quantity (lots)</span>
                        <input id=\"quantity\" name=\"quantity\" type=\"number\" min=\"1\" value=\"1\" required />
                    </div>

                    <div class=\"field full\">
                        <span class=\"label\">Targets</span>
                        <textarea id=\"targets\" name=\"targets\" placeholder=\"220/258/300\" required></textarea>
                        <div class=\"helper\">Slash, comma, or space separated. e.g. 220/258/300</div>
                    </div>

                    <div class=\"field\">
                        <span class=\"label\">Product</span>
                        <input id=\"product\" name=\"product\" type=\"text\" value=\"I\" />
                    </div>
                </div>

                <div class=\"divider\"></div>

                <div class=\"actions\">
                    <button class=\"btn-submit\" type=\"submit\">Send to Upstox</button>
                    <button class=\"btn-ghost\" type=\"button\" id=\"fillSample\">Fill sample</button>
                    <button class=\"btn-ghost\" type=\"button\" id=\"resetForm\">Reset</button>
                </div>

                <div id=\"status\" class=\"status\">Ready.</div>
            </form>
        </div>
    </div>

    <script>
        const actionInput = document.getElementById('action');
        const optionTypeInput = document.getElementById('option_type');
        const instrumentInput = document.getElementById('instrument');
        const strikeInput = document.getElementById('strike');
        const expiryInput = document.getElementById('expiry');
        const messageInput = document.getElementById('message');
        const traceInput = document.getElementById('trace_id');
        const sourceMessageInput = document.getElementById('source_message_id');
        const statusEl = document.getElementById('status');
        const form = document.getElementById('signalForm');

        function nth(d) {
            if (d > 3 && d < 21) return d + 'th';
            switch (d % 10) {
                case 1: return d + 'st';
                case 2: return d + 'nd';
                case 3: return d + 'rd';
                default: return d + 'th';
            }
        }

        function weekdayTarget(index) {
            return index === 'SENSEX' ? 4 : 2; // Thursday: 4, Tuesday: 2
        }

        function computeExpiry(index) {
            const targetWeekday = weekdayTarget(index);
            const now = new Date();
            const next = new Date(now);
            const currentDay = now.getDay();
            let diff = (targetWeekday - currentDay + 7) % 7;
            if (diff === 0) diff = 7;
            next.setDate(now.getDate() + diff);
            const day = nth(next.getDate());
            const month = next.toLocaleString('en-US', { month: 'long' }).toUpperCase();
            return `${day} ${month}`;
        }

        function updateExpiry() {
            expiryInput.value = computeExpiry(instrumentInput.value);
            messageInput.value = `${actionInput.value} ${instrumentInput.value} ${strikeInput.value || ''}${optionTypeInput.value}`.trim();
        }

        function setActive(groupName, value) {
            document.querySelectorAll(`[data-toggle="${groupName}"] .btn`).forEach(btn => {
                btn.classList.toggle('active', btn.dataset.value === value);
            });
        }

        document.querySelectorAll('[data-toggle="action"] .btn').forEach(btn => {
            btn.addEventListener('click', () => {
                actionInput.value = btn.dataset.value;
                setActive('action', btn.dataset.value);
                updateExpiry();
            });
        });

        document.querySelectorAll('[data-toggle="option_type"] .btn').forEach(btn => {
            btn.addEventListener('click', () => {
                optionTypeInput.value = btn.dataset.value;
                setActive('option_type', btn.dataset.value);
                updateExpiry();
            });
        });

        instrumentInput.addEventListener('change', updateExpiry);
        strikeInput.addEventListener('input', updateExpiry);

        document.getElementById('fillSample').addEventListener('click', () => {
            actionInput.value = 'BUY';
            optionTypeInput.value = 'CE';
            instrumentInput.value = 'NIFTY';
            strikeInput.value = '22400';
            document.getElementById('entry_low').value = '205';
            document.getElementById('stoploss').value = '185';
            document.getElementById('targets').value = '220/258/300';
            document.getElementById('quantity').value = '1';
            document.getElementById('product').value = 'I';
            setActive('action', 'BUY');
            setActive('option_type', 'CE');
            updateExpiry();
            statusEl.textContent = 'Sample loaded.';
            statusEl.className = 'status';
        });

        document.getElementById('resetForm').addEventListener('click', () => {
            form.reset();
            actionInput.value = 'BUY';
            optionTypeInput.value = 'CE';
            instrumentInput.value = 'NIFTY';
            document.getElementById('quantity').value = '1';
            document.getElementById('product').value = 'I';
            setActive('action', 'BUY');
            setActive('option_type', 'CE');
            updateExpiry();
            statusEl.textContent = 'Ready.';
            statusEl.className = 'status';
        });

        form.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            updateExpiry();
            traceInput.value = `fe:${Date.now()}`;
            sourceMessageInput.value = traceInput.value;

            const payload = {
                action: actionInput.value,
                instrument: instrumentInput.value,
                strike: Number(strikeInput.value),
                option_type: optionTypeInput.value,
                entry_low: Number(document.getElementById('entry_low').value),
                stoploss: Number(document.getElementById('stoploss').value),
                targets: document.getElementById('targets').value,
                expiry: expiryInput.value,
                quantity: Number(document.getElementById('quantity').value || '1'),
                product: document.getElementById('product').value || 'I',
                trace_id: traceInput.value,
                source_message_id: sourceMessageInput.value,
                message: messageInput.value,
            };

            try {
                statusEl.className = 'status';
                statusEl.textContent = 'Sending...';
                const headers = {'Content-Type': 'application/json'};
                const resp = await fetch('/signal', {
                    method: 'POST',
                    headers,
                    body: JSON.stringify(payload),
                });
                const data = await resp.json();
                if (!resp.ok || !data.ok) {
                    throw new Error(data.error || `HTTP ${resp.status}`);
                }
                statusEl.className = 'status success';
                statusEl.textContent = `Published. Redis ID: ${data.redis_message_id}\nLatency: ${JSON.stringify(data.latency_ms)}`;
            } catch (err) {
                statusEl.className = 'status error';
                statusEl.textContent = `Error: ${err.message}`;
            }
        });

        updateExpiry();
    </script>
</body>
</html>"""


class _FrontendSignalHandler(BaseHTTPRequestHandler):
    server_version = "FrontendSignalBridge/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        logger.info("HTTP " + format % args)

    def do_OPTIONS(self) -> None:  # noqa: N802
        _send_json(self, 200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/signal"):
            _send_html(self, 200, _form_page())
            return

        if self.path == "/health":
            _send_json(
                self,
                200,
                {
                    "ok": True,
                    "service": "frontend_signal_bridge",
                    "endpoint": "/signal",
                    "method": "POST",
                    "stream": REDIS_STREAM_KEY,
                },
            )
            return

        if self.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        _send_json(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        request_started_perf = now_perf_ns()

        if self.path != "/signal":
            _send_json(self, 404, {"ok": False, "error": "not_found"})
            return

        if FRONTEND_AUTH_TOKEN:
            auth_header = self.headers.get("Authorization", "")
            expected = f"Bearer {FRONTEND_AUTH_TOKEN}"
            if auth_header != expected:
                _send_json(self, 401, {"ok": False, "error": "unauthorized"})
                return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
            incoming = json.loads(raw.decode("utf-8"))
            if not isinstance(incoming, dict):
                raise ValueError("JSON body must be an object")

            missing = _required_missing(incoming)
            if missing:
                _send_json(self, 400, {"ok": False, "error": "missing_fields", "fields": missing})
                return

            telegram_received_at_ms = int(incoming.get("telegram_received_at_ms") or now_ms())
            listener_published_at_ms = now_ms()
            trace_id = str(incoming.get("trace_id") or f"fe:{listener_published_at_ms}")
            source_message_id = str(incoming.get("source_message_id") or trace_id)

            message_text = incoming.get("message") or (
                f"{incoming.get('action', '').upper()} {incoming.get('instrument', '').upper()} "
                f"{incoming.get('strike')}{str(incoming.get('option_type', '')).upper()}"
            )

            stream_payload = {
                "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                "message": str(message_text),
                "hash": str(incoming.get("hash") or f"frontend:{trace_id}"),
                "event_type": str(incoming.get("event_type") or "ENTRY"),
                "priority": str(incoming.get("priority") or "NORMAL"),
                "action": str(incoming.get("action", "")).upper(),
                "instrument": str(incoming.get("instrument", "")).upper(),
                "strike": str(incoming.get("strike", "")),
                "option_type": str(incoming.get("option_type", "")).upper(),
                "entry_low": str(incoming.get("entry_low", "")),
                "entry_high": str(incoming.get("entry_high", "")),
                "stoploss": str(incoming.get("stoploss", "")),
                "targets": _targets_to_str(incoming.get("targets", "")),
                "expiry": str(incoming.get("expiry", "")),
                "group_id": str(incoming.get("group_id") or "frontend"),
                "message_id": str(incoming.get("message_id") or source_message_id),
                "sender_id": str(incoming.get("sender_id") or "frontend"),
                "product": str(incoming.get("product") or "I").upper(),
                "quantity": str(incoming.get("quantity") or os.getenv("DEFAULT_QUANTITY", "1")),
                "trace_id": trace_id,
                "telegram_received_at_ms": str(telegram_received_at_ms),
                "listener_published_at_ms": str(listener_published_at_ms),
                "ai_latency_ms": "0",
                "source_message_id": source_message_id,
                "source": "frontend",
            }

            redis_started_perf = now_perf_ns()
            redis_message_id = redis_client.xadd(REDIS_STREAM_KEY, stream_payload)
            redis_publish_ms = duration_ms(redis_started_perf)

            total_ms = duration_ms(request_started_perf)
            log_latency(
                logger,
                trace_id,
                "frontend_bridge",
                redis_message_id=redis_message_id,
                redis_publish_ms=redis_publish_ms,
                total_ms=total_ms,
                status="published",
            )

            _send_json(
                self,
                200,
                {
                    "ok": True,
                    "trace_id": trace_id,
                    "redis_message_id": redis_message_id,
                    "stream": REDIS_STREAM_KEY,
                    "latency_ms": {
                        "redis_publish_ms": redis_publish_ms,
                        "total_ms": total_ms,
                    },
                },
            )
        except Exception as exc:
            logger.error(f"❌ frontend bridge error: {exc}")
            logger.error(traceback.format_exc())
            _send_json(self, 500, {"ok": False, "error": str(exc)})


def main() -> int:
    try:
        redis_client.ping()
        logger.info(f"✅ Redis connection established ({REDIS_HOST}:{REDIS_PORT})")
    except Exception as exc:
        logger.critical(f"❌ Failed to connect to Redis: {exc}")
        return 1

    server = ThreadingHTTPServer((FRONTEND_BIND_HOST, FRONTEND_BIND_PORT), _FrontendSignalHandler)
    logger.info(f"🚀 Frontend Signal Bridge running at http://{FRONTEND_BIND_HOST}:{FRONTEND_BIND_PORT}/signal")
    logger.info(f"   Publishing to Redis stream: {REDIS_STREAM_KEY}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("🛑 Frontend Signal Bridge shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
