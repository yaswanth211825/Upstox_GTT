#!/usr/bin/env python3
"""
Test GTT Strategy parsing and order placement
Run: python3 test_signal.py
"""

import json
import gtt_strategy

# Sample signal from Redis stream (as dict)
sample_signal = {
    'timestamp': '1765516397',
    'message': 'BUY SENSEX 85200PE Range 400 to 410 STOPLOSS 380 TARGETS 480/530/650 EXPIRY 18th DECEMBER',
    'hash': '513f606007d76bbf32d6a4faa28b4baa',
    'event_type': 'NEW',
    'priority': 'NORMAL',
    'action': 'BUY',
    'instrument': 'SENSEX',
    'strike': '85200',
    'option_type': 'PE',
    'entry_low': '400',
    'entry_high': '410',
    'stoploss': '380',
    'targets': '480/530/650',
    'expiry': '18th DECEMBER',
    'group_id': '1791881265',
    'message_id': '51752',
    'sender_id': '-1001791881265'
}

print("=" * 80)
print("🧪 Testing GTT Strategy Signal Parsing")
print("=" * 80)

print("\n📊 Input Signal (from Redis stream):")
print(json.dumps(sample_signal, indent=2))

parsed = gtt_strategy.parse_message_to_signal(sample_signal)

print("\n✅ Parsed Signal:")
print(json.dumps(parsed, indent=2, default=str))

print("\n📋 Validation Check:")
required = ["action", "underlying", "strike", "option_type", "entry_low", "stoploss", "targets", "expiry"]
missing = [f for f in required if not parsed.get(f)]
if missing:
    print(f"❌ Missing fields: {missing}")
else:
    print("✅ All required fields present")

print("\n" + "=" * 80)
print("✅ GTT Strategy parsing test complete")
print("=" * 80)
