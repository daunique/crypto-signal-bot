"""
Telegram notification module
Sends signal alerts and win/loss results
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


def _bot_token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")

def _chat_id():
    return os.environ.get("TELEGRAM_CHAT_ID", "")


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    token = _bot_token()
    chat_id = _chat_id()
    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping message")
        return False
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False


def send_signal_alert(signal: dict, mode: str, position_size: float,
                      contracts: float, contract_price: float) -> bool:
    """Send new signal notification."""
    direction = signal['direction']
    symbol = signal['symbol']
    confidence = signal['confidence'] * 100
    tier = signal['tier']
    candle_close = signal['candle_close_time'].strftime("%H:%M UTC")
    candle_open = signal['candle_open_time'].strftime("%H:%M UTC")

    direction_emoji = "🟢 UP ▲" if direction == "UP" else "🔴 DOWN ▼"
    tier_emoji = "⭐⭐" if tier == "T1" else "⭐"
    mode_badge = "🔴 LIVE" if mode == "live" else "👻 SHADOW"

    msg = (
        f"{'─'*32}\n"
        f"📡 <b>NEW SIGNAL</b> {mode_badge}\n"
        f"{'─'*32}\n"
        f"🪙 <b>Pair:</b> {symbol}\n"
        f"📊 <b>Direction:</b> {direction_emoji}\n"
        f"🎯 <b>Confidence:</b> {confidence:.1f}%\n"
        f"{tier_emoji} <b>Tier:</b> {tier} {'| 🔥 Volume Spike' if signal.get('vol_spike') else ''}\n"
        f"\n"
        f"⏰ <b>Candle:</b> {candle_open} → {candle_close}\n"
        f"💰 <b>Est. Entry:</b> ~${signal['open_price']:,.4f} <i>(next candle open)</i>\n"
        f"\n"
        f"📈 <b>Indicators</b>\n"
        f"   RSI(14): {signal['rsi_14']:.1f}\n"
        f"   ADX: {signal['adx']:.1f}\n"
        f"   Vol Ratio: {signal['vol_ratio']:.2f}x\n"
        f"\n"
        f"💼 <b>Position</b>\n"
        f"   Size: ${position_size:.2f}\n"
        f"   Contracts: {contracts:.2f} @ ${contract_price:.2f}\n"
        f"{'─'*32}\n"
        f"🔗 <a href='https://limitless.exchange/markets/crypto/15-min'>Open Limitless</a>"
    )
    return send_message(msg)


def send_result_alert(signal_dict: dict, outcome: str,
                      open_price: float, close_price: float) -> bool:
    """Send win/loss result after candle closes."""
    direction = signal_dict['signal_direction']
    symbol = signal_dict['symbol']
    mode = signal_dict.get('mode', 'shadow')

    if outcome == "WIN":
        result_emoji = "✅ WIN"
        change = close_price - open_price if direction == "UP" else open_price - close_price
    else:
        result_emoji = "❌ LOSS"
        change = close_price - open_price if direction == "UP" else open_price - close_price

    pct_change = (close_price - open_price) / open_price * 100

    msg = (
        f"{'─'*32}\n"
        f"{result_emoji} <b>RESULT</b>\n"
        f"{'─'*32}\n"
        f"🪙 {symbol} | {direction}\n"
        f"📉 Open: ${open_price:,.4f}\n"
        f"📈 Close: ${close_price:,.4f}\n"
        f"📊 Change: {pct_change:+.2f}%\n"
        f"{'─'*32}"
    )
    return send_message(msg)


def send_daily_summary(date_str: str, wins: int, losses: int,
                       total: int, mode: str) -> bool:
    """Send end-of-day performance summary."""
    win_rate = (wins / total * 100) if total > 0 else 0
    streak_emoji = "🔥" if win_rate >= 60 else ("⚠️" if win_rate < 50 else "✅")
    mode_badge = "🔴 LIVE" if mode == "live" else "👻 SHADOW"

    msg = (
        f"📊 <b>DAILY SUMMARY</b> — {date_str} {mode_badge}\n"
        f"{'─'*32}\n"
        f"✅ Wins: {wins}\n"
        f"❌ Losses: {losses}\n"
        f"📈 Total Signals: {total}\n"
        f"{streak_emoji} Win Rate: {win_rate:.1f}%\n"
        f"{'─'*32}"
    )
    return send_message(msg)
