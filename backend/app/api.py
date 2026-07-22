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
    async with await session() as db:
        today = datetime.now(timezone.utc).date()
        start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        signal_count = (await db.execute(
            select(func.count(Signal.id)).where(Signal.created_at >= start, Signal.created_at < end)
        )).scalar_one()
        wins = (await db.execute(
            select(func.count(Trade.id)).where(Trade.created_at >= start, Trade.created_at < end, Trade.status == "WON")
        )).scalar_one()
        losses = (await db.execute(
            select(func.count(Trade.id)).where(Trade.created_at >= start, Trade.created_at < end, Trade.status == "LOST")
        )).scalar_one()
        pnl = (await db.execute(
            select(func.coalesce(func.sum(Trade.profit), 0.0)).where(Trade.created_at >= start, Trade.created_at < end)
        )).scalar_one() or 0.0
        trades = wins + losses
        return {
            "bot_status": engine.status,
            "mode": settings.bot_mode,
            "symbol": settings.market_symbol,
            "timeframe_seconds": settings.timeframe_seconds,
            "auto_trade": settings.auto_trade,
            "current_signal": engine.current_signal,
            "last_error": engine.last_error,
            "today": {
                "signals": int(signal_count or 0),
                "trades": int(trades),
                "wins": int(wins or 0),
                "losses": int(losses or 0),
                "win_rate": round((wins / trades * 100), 2) if trades else 0,
                "pnl": round(float(pnl), 2),
            },
        }


@router.get("/api/signals")
async def signals(limit: int = 100):
    async with await session() as db:
        rows = (await db.execute(select(Signal).order_by(Signal.created_at.desc()).limit(limit))).scalars().all()
        return [{
            "id": x.id, "created_at": x.created_at, "candle_epoch": x.candle_epoch,
            "symbol": x.symbol, "direction": x.direction, "contract_type": x.contract_type,
            "barrier": x.barrier, "score": x.score, "status": x.status, "reason": x.reason
        } for x in rows]


@router.get("/api/trades")
async def trades(limit: int = 100):
    async with await session() as db:
        rows = (await db.execute(select(Trade).order_by(Trade.created_at.desc()).limit(limit))).scalars().all()
        return [{
            "id": x.id, "created_at": x.created_at, "contract_id": x.contract_id,
            "symbol": x.symbol, "mode": x.mode, "direction": x.direction,
            "stake": x.stake, "payout": x.payout, "profit": x.profit,
            "status": x.status, "entry_spot": x.entry_spot, "barrier": x.barrier
        } for x in rows]


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
            await websocket.send_json({
                "type": "status",
                "data": {
                    "status": engine.status,
                    "mode": settings.bot_mode,
                    "current_signal": engine.current_signal,
                },
            })
            await __import__("asyncio").sleep(2)
    except WebSocketDisconnect:
        return
