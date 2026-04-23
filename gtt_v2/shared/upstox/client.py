"""Upstox async HTTP client — httpx + exponential backoff + circuit breaker."""
import asyncio
import random
import structlog
import httpx
from typing import Any

log = structlog.get_logger()

RETRY_ON = {429, 500, 502, 503, 504}
NO_RETRY  = {400, 401, 403, 404, 422}
MAX_RETRY = 3


class CircuitOpenError(Exception):
    pass


class MaxRetriesError(Exception):
    pass


class UpstoxRejectError(Exception):
    """Upstox returned HTTP 200 but body contains status='error' (soft rejection)."""
    def __init__(self, message: str, body: dict = None):
        super().__init__(message)
        self.body = body or {}


class _CircuitBreaker:
    FAILURE_THRESHOLD = 5
    RECOVERY_SECONDS  = 60

    def __init__(self):
        self._failures = 0
        self._opened_at: float | None = None

    def can_attempt(self) -> bool:
        if self._opened_at is None:
            return True
        import time
        if time.monotonic() - self._opened_at >= self.RECOVERY_SECONDS:
            # half-open: allow one attempt
            return True
        return False

    def record_success(self):
        self._failures = 0
        self._opened_at = None

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.FAILURE_THRESHOLD:
            import time
            self._opened_at = time.monotonic()
            log.error("circuit_breaker_opened", failures=self._failures)


class UpstoxClient:
    def __init__(self, access_token: str, base_url: str = "https://api.upstox.com"):
        self._token = access_token
        self._base = base_url.rstrip("/")
        self._circuit = _CircuitBreaker()
        self._http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=15.0,
        )

    async def aclose(self):
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self._base}{path}"
        for attempt in range(MAX_RETRY):
            if not self._circuit.can_attempt():
                raise CircuitOpenError("Circuit breaker is open")
            try:
                r = await self._http.request(method, url, **kwargs)
                if r.status_code in NO_RETRY:
                    self._circuit.record_success()
                    try:
                        err_body = r.json()
                    except Exception:
                        err_body = r.text
                    log.error("upstox_api_error", status=r.status_code, path=path, body=err_body)
                    print(f"[UPSTOX ERROR] {r.status_code} on {path} — {err_body}")
                    r.raise_for_status()
                if r.status_code in RETRY_ON and attempt < MAX_RETRY - 1:
                    self._circuit.record_failure()
                    wait = min(2 ** attempt + random.uniform(0, 1), 60)
                    log.warning("upstox_retry", status=r.status_code, attempt=attempt, wait=wait)
                    await asyncio.sleep(wait)
                    continue
                self._circuit.record_success()
                body = r.json()
                # Upstox sometimes returns HTTP 200 with status:"error" in the body
                # (e.g. insufficient funds, intraday window closed, invalid strike)
                if isinstance(body, dict) and body.get("status") == "error":
                    errors = body.get("errors") or []
                    msg = "; ".join(
                        e.get("message", str(e)) for e in errors
                    ) if errors else str(body)
                    log.error("upstox_soft_error", path=path, errors=errors, msg=msg)
                    print(f"[UPSTOX REJECTED] {path} — {msg}")
                    raise UpstoxRejectError(msg, body)
                return body
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                self._circuit.record_failure()
                if attempt < MAX_RETRY - 1:
                    wait = min(2 ** attempt + random.uniform(0, 1), 60)
                    log.warning("upstox_timeout_retry", error=str(e), attempt=attempt, wait=wait)
                    await asyncio.sleep(wait)
                else:
                    raise MaxRetriesError(f"Max retries reached: {e}") from e
        raise MaxRetriesError("Max retries exhausted")

    # ── GTT endpoints ────────────────────────────────────────────────────────
    # Docs: https://upstox.com/developer/api-documentation/place-gtt-order
    #       https://upstox.com/developer/api-documentation/cancel-gtt-order
    #       https://upstox.com/developer/api-documentation/get-gtt-order-details

    async def place_gtt(self, payload: dict) -> dict:
        # POST /v3/order/gtt/place
        return await self._request("POST", "/v3/order/gtt/place", json=payload)

    async def get_gtt(self, gtt_order_id: str) -> dict:
        # GET /v3/order/gtt?gtt_order_id=xxx  (query param, not path param)
        return await self._request("GET", "/v3/order/gtt", params={"gtt_order_id": gtt_order_id})

    async def get_all_gtts(self) -> dict:
        # No list endpoint exists in Upstox v3 — returns empty data structure
        return {"data": []}

    async def cancel_gtt(self, gtt_order_id: str) -> dict:
        # DELETE /v3/order/gtt/cancel  with JSON body {"gtt_order_id": "GTT-xxx"}
        return await self._request("DELETE", "/v3/order/gtt/cancel", json={"gtt_order_id": gtt_order_id})

    # ── Funds & Margin ───────────────────────────────────────────────────────
    # Docs: https://upstox.com/developer/api-documentation/get-fund-and-margin

    async def get_funds_and_margin(self, segment: str = "SEC") -> dict:
        # GET /v2/user/get-funds-and-margin?segment=SEC
        # Returns list of segment objects; each has available_margin, used_margin, net, etc.
        resp = await self._request(
            "GET", "/v2/user/get-funds-and-margin", params={"segment": segment}
        )
        return resp.get("data") or {}

    # ── Market quote ─────────────────────────────────────────────────────────
    # Docs: https://upstox.com/developer/api-documentation/ltp

    async def get_ltp(self, instrument_key: str) -> float | None:
        result = await self.get_ltp_multi([instrument_key])
        return result.get(instrument_key)

    async def get_ltp_multi(self, instrument_keys: list[str]) -> dict[str, float]:
        """Fetch LTP for multiple instrument keys via v3 REST API.

        Returns dict mapping instrument_key (| separator) → last_price float.
        """
        if not instrument_keys:
            return {}
        keys_param = ",".join(instrument_keys)
        try:
            resp = await self._request(
                "GET",
                "/v3/market-quote/ltp",
                params={"instrument_key": keys_param},
            )
        except Exception as e:
            log.warning("get_ltp_multi_request_failed", error=str(e))
            return {}
        result: dict[str, float] = {}
        try:
            data = resp.get("data") or {}
            for v in data.values():
                if not v:
                    continue
                ltp = v.get("last_price")
                if ltp is None:
                    continue
                # Prefer instrument_token (uses | separator, matches our stored keys)
                # Fall back to normalising the outer dict key
                key = v.get("instrument_token") or next(iter(data))
                result[key] = float(ltp)
        except Exception as e:
            log.warning("get_ltp_multi_parse_error", error=str(e))
        return result

    # ── Orders / trades ──────────────────────────────────────────────────────
    # Docs: https://upstox.com/developer/api-documentation/get-order-details
    #       https://upstox.com/developer/api-documentation/get-trades-by-order

    async def get_order_details(self, order_id: str) -> dict:
        # GET /v2/order/details?order_id=xxx
        return await self._request("GET", "/v2/order/details", params={"order_id": order_id})

    async def get_order_trades(self, order_id: str) -> dict:
        # GET /v2/order/trades?order_id=xxx
        return await self._request("GET", "/v2/order/trades", params={"order_id": order_id})

    # ── WebSocket auth ───────────────────────────────────────────────────────

    # ── Positions ────────────────────────────────────────────────────────────
    # Docs: https://upstox.com/developer/api-documentation/get-positions

    async def get_positions(self) -> list[dict]:
        resp = await self._request("GET", "/v2/portfolio/short-term-positions")
        return resp.get("data") or []

    # ── Place order ──────────────────────────────────────────────────────────
    # Docs: https://upstox.com/developer/api-documentation/place-order

    async def place_order(
        self,
        instrument_token: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        product: str = "I",
        price: float = 0,
        validity: str = "IOC",
    ) -> dict:
        payload = {
            "instrument_token": instrument_token,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "order_type": order_type,
            "product": product,
            "price": price,
            "validity": validity,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        # HFT endpoint for order placement
        return await self._request("POST", "/v2/order/place", json=payload)

    # ── WebSocket auth ───────────────────────────────────────────────────────

    async def get_portfolio_stream_url(self) -> str:
        resp = await self._request(
            "GET",
            "/v2/feed/portfolio-stream-feed/authorize",
            params={"update_types": "order,gtt_order,position,holding"},
        )
        data = resp.get("data", resp)
        return data.get("authorizedRedirectUri") or data.get("authorized_redirect_uri")

    async def get_market_stream_url(self) -> str:
        resp = await self._request("GET", "/v3/feed/market-data-feed/authorize")
        data = resp.get("data") if isinstance(resp, dict) else None
        if not data:
            errors = resp.get("errors") if isinstance(resp, dict) else []
            error_text = " ".join(str(e.get("message", e)) for e in (errors or [])).lower()
            if "token" in error_text or "unauthorized" in error_text or "expired" in error_text:
                raise ValueError(
                    "Upstox token expired — update UPSTOX_ACCESS_TOKEN in .env and run sync-env.sh"
                )
            raise ValueError(
                f"Market stream auth failed — {error_text or resp}"
            )
        url = data.get("authorizedRedirectUri") or data.get("authorized_redirect_uri")
        if not url:
            raise ValueError(f"Market stream URL missing in Upstox response: {data}")
        return url
