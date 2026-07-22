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
        trades = (await db.execute(select(Trade).order_by(Trade.created_at.desc()).limit(20))).scalars().all()
        signals = (await db.execute(select(Signal).order_by(Signal.created_at.desc()).limit(20))).scalars().all()
        today = datetime.now(timezone.utc).date()
        day_trades = [t for t in trades if t.created_at and t.created_at.date() == today]
        wins = sum(1 for t in day_trades if t.status == "WON")
        losses = sum(1 for t in day_trades if t.status == "LOST")
        pnl = sum((t.profit or 0) for t in day_trades)
        total = wins + losses
        return {
            "bot_status": engine.status,
            "mode": settings.bot_mode,
            "symbol": settings.market_symbol,
            "timeframe_seconds": settings.timeframe_seconds,
            "auto_trade": settings.auto_trade,
            "current_signal": engine.current_signal,
            "last_error": engine.last_error,
            "today": {
                "signals": len([s for s in signals if s.created_at and s.created_at.date() == today]),
                "trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / total * 100), 2) if total else 0,
                "pnl": round(pnl, 2),
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
