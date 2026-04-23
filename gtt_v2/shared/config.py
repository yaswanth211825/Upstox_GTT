import uuid
import structlog
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    postgres_dsn: str = "postgres://postgres:postgres@localhost:5432/gtt_trading"

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_stream_name: str = "raw_trade_signals"

    # Upstox
    upstox_access_token: str = ""
    upstox_base_url: str = "https://api.upstox.com"

    # Lot sizes
    lot_size_nifty: int = 75
    lot_size_banknifty: int = 15
    lot_size_sensex: int = 20
    default_quantity: int = 1

    # Trading Floor rules
    daily_pnl_limit_pct: float = 0.10
    account_capital: float = 100_000.0   # fallback only — live value fetched from Upstox at 9:15 IST
    trader_profile: str = "SAFE"         # SAFE or RISK (profile filter disabled — see trading_engine)
    near_sl_buffer_pts: float = 5.0      # BUY_ABOVE SAFE: second lot placed at stoploss_adj + this value
    gtt_expiry_hours: int = 6
    use_entry_touch_logic: bool = True

    # Telegram
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_phone: str = ""
    group_entity_name: str = "MS-OPTIONS-PREMIUM"
    group_entity_name_2: str = ""   # optional second channel

    # AI
    ai_provider: str = "GEMINI"
    gemini_api_key: str = ""
    ai_model: str = "gemini-2.0-flash"

    # Admin API
    admin_api_token: str = ""   # Bearer token for admin API; if empty, auth is disabled

    # Ops
    dry_run: bool = False
    log_level: str = "INFO"


settings = Settings()


def configure_logging(service: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(settings.log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]
