"""
AI-Powered Signal Parser using LLMs (Gemini or OpenAI)
Replaces regex-based parsing with LLM for robust signal extraction
"""
import os
import re
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
import json
import logging

@dataclass
class TradingSignal:
    action: str  # BUY or SELL
    instrument: str  # NIFTY, BANKNIFTY, SENSEX (full names, no abbreviations)
    strike: str
    option_type: str  # CE or PE
    entry_price: Optional[Tuple[float, float]] = None
    stoploss: Optional[float] = None
    targets: List[float] = field(default_factory=list)
    expiry: Optional[str] = None
    event_type: str = "NEW"  # NEW | REENTRY
    signal_hash: str = ""
    # Latency metrics (milliseconds)
    ai_latency_ms: Optional[float] = None

    def to_one_line(self) -> str:
        entry = f"Range {int(self.entry_price[0])} to {int(self.entry_price[1])}" if self.entry_price else "Market"
        sl = f"STOPLOSS {int(self.stoploss)}" if self.stoploss is not None else ""
        targets = "/".join(str(int(t)) for t in self.targets) if self.targets else ""
        prefix = "RE-ENTRY " if self.event_type == "REENTRY" else ""
        parts = [f"{prefix}{self.action}", self.instrument, f"{self.strike}{self.option_type}", entry]
        if sl:
            parts.append(sl)
        if targets:
            parts.append(f"TARGETS {targets}")
        if self.expiry:
            parts.append(f"EXPIRY {self.expiry}")
        return " ".join(p for p in parts if p).strip()


class AISignalParser:
    """
    AI-powered signal parser using either Google Gemini or OpenAI models
    - Pre-filters messages with regex to avoid unnecessary API calls
    - Uses prompt engineering to extract structured trading signals
    - Validates required fields (stoploss, targets)
    - Calculates expiry dates automatically
    """
    
    # Noise patterns — result/update messages, not new entry signals
    _NOISE_RE = re.compile(
        r'ORDER\s+ACTIVATED|PREMIUM\s+SERVICE|TARGET(?:\s*\d+)?\s*DONE'
        r'|SL\s+HIT|STOP\s*LOSS\s+HIT|STOP\s*LOSS\s+TRIGGERED'
        r'|PROFIT\s+BOOK|BOOK\s+PROFIT',
        re.IGNORECASE,
    )
    # A valid entry signal MUST have an instrument AND option type or strike number
    _INSTRUMENT_RE = re.compile(r'\b(SENSEX|BANKNIFTY|BNF|NIFTY|NF)\b', re.IGNORECASE)
    _OPTION_TYPE_RE = re.compile(r'(CE|PE|CALL|PUT)\b', re.IGNORECASE)  # no leading \b (glued to strike)
    _STRIKE_NUM_RE = re.compile(r'(?<!\d)\d{4,6}(?!\d)')
    
    # Re-entry detection pattern
    REENTRY_PATTERN = re.compile(
        r'\b(RE\s*[- ]?ENTRY|RE\s*[- ]?ENTER|REBUY|RE\s*BUY|ADD\s+MORE)\b',
        re.IGNORECASE
    )
    
    def __init__(self, api_key: str, provider: str = "GEMINI", model: Optional[str] = None):
        """Initialize the chosen LLM provider"""
        self.provider = provider.upper()
        self.model_name = model  # store raw model name for openai branch
        self.client = None
        self.model = None
        
        if self.provider == "GEMINI":
            try:
                import google.generativeai as genai
            except ImportError as exc:
                raise ImportError("google-generativeai is required for GEMINI provider") from exc
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel(self.model_name or 'gemini-2.0-flash')
            logging.info("✅ AI Signal Parser initialized with Gemini API")
        elif self.provider == "OPENAI":
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ImportError("openai package is required for OPENAI provider") from exc
            self.client = OpenAI(api_key=api_key)
            self.model_name = self.model_name or 'gpt-4o-mini'
            logging.info("✅ AI Signal Parser initialized with OpenAI API")
        else:
            raise ValueError(f"Unsupported AI provider: {self.provider}")
    
    def should_process(self, message: str) -> bool:
        """
        Pre-filter to avoid unnecessary LLM API calls.
        Blocks noise (SL hit, target done, etc.) and requires an instrument
        keyword plus at least an option type or 4-6 digit strike number.
        """
        if self._NOISE_RE.search(message):
            return False
        if not self._INSTRUMENT_RE.search(message):
            return False
        return bool(self._OPTION_TYPE_RE.search(message)) or bool(self._STRIKE_NUM_RE.search(message))
    
    def _make_hash(self, signal: TradingSignal) -> str:
        """Generate unique hash for deduplication
        
        Includes expiry because same strike/price but different expiry 
        represents a different contract and should not be deduped.
        """
        parts = [
            signal.instrument,
            signal.strike,
            signal.option_type,
            str(signal.entry_price) if signal.entry_price else "None",
            str(signal.stoploss) if signal.stoploss is not None else "None",
            str(sorted(signal.targets)) if signal.targets else "[]",
            signal.expiry or "",  # Include expiry - different expiry = different contract
        ]
        if signal.event_type == "REENTRY":
            parts.append("REENTRY")
        key = '|'.join(parts)
        return hashlib.md5(key.encode()).hexdigest()
    
    def _calculate_expiry(self, instrument: str) -> str:
        """
        Calculate THIS WEEK's expiry date for the instrument.
        
        Expiry days by instrument:
        - NIFTY, BANKNIFTY: Tuesday
        - SENSEX: Thursday
        
        If today is the expiry day, returns today's date.
        Otherwise, returns the nearest upcoming expiry.
        """
        import datetime
        today = datetime.date.today()
        weekday = today.weekday()  # Monday=0, Tuesday=1, ..., Sunday=6
        
        # Determine expiry day based on instrument
        # Schedule (as of Sep 2025):
        # Monday: Midcpnifty  Tuesday: Nifty/Finnifty  Wednesday: BankNifty
        # Thursday: Sensex/Bankex  Friday: Nifty Next 50
        inst = instrument.upper()
        if inst == "BANKNIFTY":
            target_weekday = 2   # Wednesday
        elif inst == "SENSEX":
            target_weekday = 3   # Thursday
        else:  # NIFTY and others
            target_weekday = 1   # Tuesday
        
        # Calculate days until target weekday
        if weekday <= target_weekday:
            # Target day is this week (including today if today is expiry)
            days_until_expiry = target_weekday - weekday
        else:
            # Target day is next week
            days_until_expiry = (7 - weekday) + target_weekday
        
        expiry_date = today + datetime.timedelta(days=days_until_expiry)
        
        # Format as '7th NOVEMBER'
        day = expiry_date.day
        month = expiry_date.strftime('%B').upper()
        
        # Add ordinal suffix
        if 11 <= (day % 100) <= 13:
            suffix = 'th'
        else:
            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
        
        return f"{day}{suffix} {month}"
    
    def parse(self, raw_message: str) -> Optional[TradingSignal]:
        """
        Parse trading signal using AI with robust prompt engineering.
        Returns None if:
        - Message doesn't look like a trading signal
        - AI couldn't extract required fields
        - Required fields (stoploss, targets) are missing
        """
        # Quick filter: skip if doesn't look like a trading signal
        if not self.should_process(raw_message):
            logging.debug("Message filtered out by pre-check (not a trading signal)")
            return None
        
        # Detect re-entry
        event_type = "REENTRY" if self.REENTRY_PATTERN.search(raw_message) else "NEW"
        
        # Prepare the prompt for the selected LLM
        prompt = f"""You are a trading signal parser. Extract structured information from the trading message below.

STRICT RULES:
1. Instrument: Must be one of NIFTY, BANKNIFTY, or SENSEX (full names only, convert NF→NIFTY, BNF→BANKNIFTY)
2. Strike: A 4-6 digit number (e.g., 25600, 83000, 46000)
3. Option Type: Either CE (call) or PE (put)
4. Action: BUY or SELL (default to BUY if not specified)
5. Entry Price: Can be a range (e.g., 350-360) or single value (e.g., 350). Look for "PRICE @", "@", "ENTRY", "BUY AROUND" keywords.
6. Stoploss: REQUIRED - the stop loss price (look for STOPLOSS, SL, STOP LOSS keywords)
7. Targets: REQUIRED - list of target prices (look for TARGETS, TGT, TARGET, T1, T2, emoji 🎯)
8. Expiry: OPTIONAL. Extract the date if mentioned (e.g. "23rd APRIL", "29 APR", "23 APRIL EXPIRY"). Strip the word "EXPIRY" and return just the date like "23rd APRIL". If not mentioned, return null. NEVER return an error for missing expiry.

OUTPUT FORMAT (JSON only, no explanation):
{{
  "instrument": "NIFTY|BANKNIFTY|SENSEX",
  "strike": "25600",
  "option_type": "CE|PE",
  "action": "BUY|SELL",
  "entry_low": 350,
  "entry_high": 360,
  "stoploss": 345,
  "targets": [390, 450, 574],
  "expiry": "23rd APRIL"
}}

Only return an error if stoploss or targets are completely missing:
{{"error": "missing_field", "missing": "stoploss_or_targets"}}

Trading Message:
{raw_message}

JSON Response:"""
        
        try:
            # Call selected LLM provider and measure latency
            api_t0 = time.perf_counter()
            if self.provider == "GEMINI":
                response = self.model.generate_content(prompt)
                response_text = response.text.strip()
            else:  # OPENAI
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0
                )
                response_text = response.choices[0].message.content.strip()
            api_ms = (time.perf_counter() - api_t0) * 1000.0
            
            # Extract JSON from response (may have markdown code blocks)
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0].strip()
            
            # Parse JSON response
            data = json.loads(response_text)
            
            # Check for error response
            if "error" in data:
                missing = data.get("missing", "unknown")
                # Expiry is auto-calculated — never drop a signal for it
                if "expiry" in str(missing).lower():
                    logging.warning(f"⚠️ AI couldn't parse expiry — will auto-calculate")
                    data = {k: v for k, v in data.items() if k != "error" and k != "missing"}
                    data.setdefault("expiry", None)
                else:
                    logging.warning(f"❌ AI parsing failed - missing: {missing}")
                    print(f"❌ PARSING FAILED ({missing} missing)")
                    return None

            # Fallbacks if the model omits key fields (common with terse messages)
            if not data.get("instrument"):
                m = re.search(r"\b(NIFTY|BANKNIFTY|SENSEX|NF|BNF)\b", raw_message, re.IGNORECASE)
                if m:
                    inst = m.group(1).upper()
                    if inst == "NF":
                        inst = "NIFTY"
                    elif inst == "BNF":
                        inst = "BANKNIFTY"
                    data["instrument"] = inst

            if (not data.get("strike")) or (not data.get("option_type")):
                m = re.search(r"(\d{4,6})(CE|PE)\b", raw_message, re.IGNORECASE)
                if m:
                    data.setdefault("strike", m.group(1))
                    data.setdefault("option_type", m.group(2).upper())

            # Validate required fields
            required = ["instrument", "strike", "option_type", "stoploss", "targets"]
            missing_fields = [f for f in required if not data.get(f)]
            if missing_fields:
                logging.warning(f"❌ AI response missing required fields: {missing_fields}")
                print(f"❌ PARSING FAILED ({'/'.join(missing_fields)} missing)")
                return None
            
            # Normalize instrument name
            instrument = data["instrument"].upper()
            if instrument not in ["NIFTY", "BANKNIFTY", "SENSEX"]:
                logging.warning(f"Invalid instrument: {instrument}")
                return None
            
            # Build entry price tuple
            entry_low = data.get("entry_low")
            entry_high = data.get("entry_high", entry_low)
            entry_price = (float(entry_low), float(entry_high)) if entry_low else None
            
            # Parse targets
            targets = [float(t) for t in data["targets"]] if data.get("targets") else []
            
            # Calculate expiry if not provided
            expiry = data.get("expiry")
            if not expiry or expiry.lower() in ["null", "none", ""]:
                expiry = self._calculate_expiry(instrument)
            
            # Create signal object
            signal = TradingSignal(
                action=data.get("action", "BUY").upper(),
                instrument=instrument,
                strike=str(data["strike"]),
                option_type=data["option_type"].upper(),
                entry_price=entry_price,
                stoploss=float(data["stoploss"]),
                targets=targets,
                expiry=expiry,
                event_type=event_type,
                ai_latency_ms=api_ms
            )
            
            # Generate hash for deduplication
            signal.signal_hash = self._make_hash(signal)
            
            logging.info(f"✅ AI parsed signal: {signal.to_one_line()}")
            try:
                # Print a concise latency message to console
                print(f"⏱️ AI latency: {api_ms:.0f} ms")
            except Exception:
                pass
            return signal
            
        except json.JSONDecodeError as e:
            logging.error(f"AI returned invalid JSON: {e}")
            logging.error(f"Response: {response_text}")
            return None
        except Exception as e:
            logging.error(f"Error during AI parsing: {e}")
            return None