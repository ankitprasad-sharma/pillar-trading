try:
    import notifier as _notifier
except ImportError:
    _notifier = None

_limit_notified: set = set()


class RiskManager:
    def __init__(self,
                 max_daily_loss: float = 5000,
                 max_trades_per_day: int = 1,
                 max_position_size: float = 16250):

        self.max_daily_loss = max_daily_loss
        self.max_trades_per_day = max_trades_per_day
        self.max_position_size = max_position_size
        self.daily_loss = 0
        self.trades_today = 0

    def approve_trade(self, signal: dict,
                      premium: float,
                      quantity: int) -> dict:

        position_value = premium * quantity

        if self.daily_loss >= self.max_daily_loss:
            reason = f"Daily loss limit ₹{self.max_daily_loss} hit"
            if _notifier and reason not in _limit_notified:
                _limit_notified.add(reason)
                _notifier.notify_limit_hit(reason, -self.daily_loss)
            return {"approved": False, "reason": reason}

        if self.trades_today >= self.max_trades_per_day:
            strategy = signal.get("source", "")
            if strategy in ("orb", "hybrid") or self.max_trades_per_day == 1:
                reason = "ORB allows only 1 trade per day"
            else:
                reason = f"Max {self.max_trades_per_day} trades/day reached"
            if _notifier and reason not in _limit_notified:
                _limit_notified.add(reason)
                _notifier.notify_limit_hit(reason, -self.daily_loss)
            return {"approved": False, "reason": reason}

        if position_value > self.max_position_size:
            return {"approved": False,
                    "reason": f"Position ₹{position_value} exceeds limit"}

        if signal.get("confidence") == "LOW":
            return {"approved": False,
                    "reason": "Signal confidence too low"}

        return {"approved": True, "reason": "All checks passed"}

    def record_trade_result(self, pnl: float):
        self.daily_loss += min(0, pnl)
        self.trades_today += 1

    def reset_daily_counters(self):
        self.daily_loss = 0
        self.trades_today = 0
        _limit_notified.clear()
