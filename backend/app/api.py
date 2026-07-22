import asyncio
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select, func
from .config import get_settings
from .db import session, Signal, Trade
from .engine import BotEngine

router = APIRouter()
settings = get_settings()
engine = BotEngine()


@router.get("/api/status")
async def status():
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
            "bot_status": engine.status, "mode": settings.bot_mode, "symbol": settings.market_symbol,
            "timeframe_seconds": 180, "auto_trade": settings.auto_trade,
            "current_signal": engine.current_signal, "last_error": engine.last_error,
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
        return [{"id": x.id, "created_at": x.created_at, "candle_epoch": x.candle_epoch,
                 "symbol": x.symbol, "direction": x.direction, "contract_type": x.contract_type,
                 "score": x.score, "status": x.status, "reason": x.reason} for x in rows]


@router.get("/api/trades")
async def trades(limit: int = 100):
    limit = max(1, min(limit, 500))
    async with session() as db:
        rows = (await db.execute(select(Trade).order_by(Trade.created_at.desc()).limit(limit))).scalars().all()
        return [{"id": x.id, "created_at": x.created_at, "contract_id": x.contract_id,
                 "symbol": x.symbol, "mode": x.mode, "direction": x.direction,
                 "stake": x.stake, "payout": x.payout, "profit": x.profit,
                 "status": x.status, "entry_spot": x.entry_spot} for x in rows]


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


@router.post("/api/bot/start")
async def start_bot():
    await engine.start()
    return {"status": engine.status}


@router.post("/api/bot/stop")
async def stop_bot():
    await engine.stop()
    return {"status": engine.status}


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({"type": "status", "data": {
                "status": engine.status, "mode": settings.bot_mode,
                "current_signal": engine.current_signal, "last_error": engine.last_error,
            }})
            await asyncio.sleep(2)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
