import asyncio
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select, func
from .config import get_settings, BUILD_VERSION
from .db import session, Signal, Trade, BotEvent, get_effective_bot_mode, set_bot_mode_override
from .engine import BotEngine

router = APIRouter()
settings = get_settings()
engine = BotEngine()


@router.get("/api/status")
async def status():
    mode = await get_effective_bot_mode(settings)
    async with session() as db:
        today = datetime.now(timezone.utc).date()
        start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        signal_count = (await db.execute(select(func.count(Signal.id)).where(Signal.created_at >= start, Signal.created_at < end))).scalar_one()
        wins = (await db.execute(select(func.count(Trade.id)).where(Trade.created_at >= start, Trade.created_at < end, Trade.status == "WON"))).scalar_one()
        losses = (await db.execute(select(func.count(Trade.id)).where(Trade.created_at >= start, Trade.created_at < end, Trade.status == "LOST"))).scalar_one()
        pending = (await db.execute(select(func.count(Trade.id)).where(Trade.created_at >= start, Trade.created_at < end, Trade.status.in_(["PENDING", "OPEN"])))).scalar_one()
        pnl = (await db.execute(select(func.coalesce(func.sum(Trade.profit), 0.0)).where(Trade.created_at >= start, Trade.created_at < end))).scalar_one() or 0.0
        trades = wins + losses
        return {
            "bot_status": engine.status, "mode": mode, "symbol": settings.market_symbol,
            "trade_duration_ticks": settings.trade_duration_ticks, "auto_trade": settings.auto_trade,
            "barrier_vol_fraction": settings.barrier_vol_fraction,
            "current_signal": engine.current_signal, "last_error": engine.last_error,
            "strategy": {
                "ready": engine.strategy.ready,
                "tick_count": engine.strategy.tick_count,
                "min_ticks_required": engine.strategy.MIN_TICKS,
                "ticks_since_decision": engine.ticks_since_decision,
            },
            "today": {"signals": int(signal_count or 0), "trades": int(trades), "pending": int(pending or 0),
                      "wins": int(wins or 0), "losses": int(losses or 0),
                      "win_rate": round((wins / trades * 100), 2) if trades else 0,
                      "pnl": round(float(pnl), 2)}
        }


@router.get("/api/signals")
async def signals(limit: int = 100):
    limit = max(1, min(limit, 500))
    async with session() as db:
        rows = (await db.execute(select(Signal).order_by(Signal.created_at.desc()).limit(limit))).scalars().all()
        return [{"id": x.id, "created_at": x.created_at, "decision_epoch": x.candle_epoch,
                 "symbol": x.symbol, "direction": x.direction, "contract_type": x.contract_type,
                 "score": x.score, "status": x.status, "reason": x.reason,
                 "barrier_offset": x.barrier_offset} for x in rows]


@router.get("/api/trades")
async def trades(limit: int = 100):
    limit = max(1, min(limit, 500))
    async with session() as db:
        rows = (await db.execute(select(Trade).order_by(Trade.created_at.desc()).limit(limit))).scalars().all()
        return [{"id": x.id, "created_at": x.created_at, "contract_id": x.contract_id,
                 "symbol": x.symbol, "mode": x.mode, "direction": x.direction,
                 "stake": x.stake, "payout": x.payout, "profit": x.profit,
                 "status": x.status, "entry_spot": x.entry_spot, "barrier": x.barrier} for x in rows]


@router.get("/api/pnl-history")
async def pnl_history(limit: int = 365):
    limit = max(1, min(limit, 3650))
    async with session() as db:
        rows = (await db.execute(select(Trade).order_by(Trade.created_at.desc()).limit(5000))).scalars().all()
    by_day = {}
    for row in rows:
        day = row.created_at.date().isoformat()
        item = by_day.setdefault(day, {"date": day, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        if row.status in {"WON", "LOST"}:
            item["trades"] += 1
        item["wins"] += int(row.status == "WON")
        item["losses"] += int(row.status == "LOST")
        item["pnl"] += float(row.profit or 0)
    return sorted(by_day.values(), key=lambda x: x["date"], reverse=True)[:limit]


@router.get("/api/diagnostics/contracts-for")
async def diagnostics_contracts_for():
    """Live query to Deriv asking what's actually valid for this account and
    symbol -- Deriv's own documented way to find real barrier/duration
    limits (see README). Requires the bot to be running (connected).

    Returns the *raw* response (or the raw error) rather than a parsed
    summary: this app's own research couldn't confidently confirm the exact
    current response shape in advance, so parsing it here risked hiding the
    real answer behind another wrong guess. Read the raw JSON directly, or
    copy it back for help reading it.
    """
    if engine.client.trade_ws is None:
        return {"error": "Bot is not currently connected -- start the bot first, then try again."}
    try:
        result = await engine.client.contracts_for(settings.market_symbol)
        return {"generated_at": datetime.now(timezone.utc), "symbol": settings.market_symbol, "result": result}
    except Exception as exc:
        return {"generated_at": datetime.now(timezone.utc), "symbol": settings.market_symbol, "error": str(exc)}


@router.get("/api/diagnostics")
async def diagnostics():
    """A single, copy-pasteable snapshot of recent bot activity and errors.

    Pulls from the BotEvent log (connection/execution/settlement failures),
    plus the last few signals and trades, so a problem can be diagnosed
    without exporting raw platform logs. Deliberately allowlists only safe
    fields below -- never include deriv_pat/deriv_app_id/database_url here,
    since this endpoint is designed to be copied and shared.
    """
    async with session() as db:
        events = (await db.execute(select(BotEvent).order_by(BotEvent.created_at.desc()).limit(25))).scalars().all()
        recent_signals = (await db.execute(select(Signal).order_by(Signal.created_at.desc()).limit(10))).scalars().all()
        recent_trades = (await db.execute(select(Trade).order_by(Trade.created_at.desc()).limit(10))).scalars().all()
    return {
        "generated_at": datetime.now(timezone.utc),
        "build_version": BUILD_VERSION,
        "bot_status": engine.status,
        "mode": await get_effective_bot_mode(settings),
        "symbol": settings.market_symbol,
        "auto_trade": settings.auto_trade,
        "barrier_vol_fraction": settings.barrier_vol_fraction,
        "last_error": engine.last_error,
        "current_signal": engine.current_signal,
        "recent_events": [
            {"created_at": e.created_at, "level": e.level, "event_type": e.event_type, "message": e.message}
            for e in events
        ],
        "recent_signals": [
            {"created_at": s.created_at, "direction": s.direction, "status": s.status, "score": s.score,
             "barrier_offset": s.barrier_offset, "reason": s.reason}
            for s in recent_signals
        ],
        "recent_trades": [
            {"created_at": t.created_at, "direction": t.direction, "status": t.status, "barrier": t.barrier,
             "stake": t.stake, "profit": t.profit, "contract_id": t.contract_id}
            for t in recent_trades
        ],
    }


@router.post("/api/bot/start")
async def start_bot():
    await engine.start()
    return {"status": engine.status}


@router.post("/api/bot/stop")
async def stop_bot():
    await engine.stop()
    return {"status": engine.status}


class ModeChangeRequest(BaseModel):
    mode: str


@router.post("/api/settings/mode")
async def set_mode(payload: ModeChangeRequest):
    """Change demo/live and persist it (RuntimeSetting -- see db.py),
    surviving restarts without needing a redeploy. If the bot is currently
    running, it's stopped and restarted so the new mode actually takes
    effect (account selection happens once, at connect time)."""
    try:
        new_mode = await set_bot_mode_override(payload.mode)
    except ValueError as exc:
        return {"error": str(exc)}
    restarted = False
    if engine.running:
        await engine.stop()
        await engine.start()
        restarted = True
    return {"mode": new_mode, "restarted": restarted, "bot_status": engine.status}


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            mode = await get_effective_bot_mode(settings)
            await websocket.send_json({"type": "status", "data": {
                "status": engine.status, "mode": mode,
                "current_signal": engine.current_signal, "last_error": engine.last_error,
            }})
            await asyncio.sleep(2)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
