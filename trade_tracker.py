"""
trade_tracker.py
================
Two separate stores:
  1. trades_state.json  — active/today trades (cleared each night)
  2. trade_history.json — PERMANENT record of ALL trades ever given (never deleted)

Trade statuses:
  ACTIVE   → live trade, monitoring
  TARGET   → system detected option hit target price
  SL_HIT   → system detected option hit SL price
  EXPIRED  → end-of-day auto-expire (trade never confirmed as taken/not-taken)

History record (in trade_history.json) additionally has:
  taken        → True/False (did user actually take this trade?)
  user_entry   → premium at which user entered (if taken)
  user_exit    → premium at which user exited (if taken)
  user_pnl     → user_exit - user_entry (positive = profit)
  result       → "PROFIT" / "LOSS" / "BREAKEVEN" / "NOT_TAKEN" / "SYSTEM_TARGET" / "SYSTEM_SL"
  moved_at     → timestamp when trade was moved to history
  duration_hrs → hours between given_at and hit_at or moved_at
"""

import os, json, logging
from datetime import datetime, date, timedelta

log = logging.getLogger("tracker")

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE   = os.path.join(BASE_DIR, "trades_state.json")
HISTORY_FILE  = os.path.join(BASE_DIR, "trade_history.json")


# ─── INTERNAL HELPERS ────────────────────────────────────────────────────────

def _load_active():
    if not os.path.exists(TRADES_FILE): return {}
    try:
        with open(TRADES_FILE) as f: return json.load(f)
    except: return {}

def _save_active(t):
    with open(TRADES_FILE, "w") as f: json.dump(t, f, indent=2, default=str)

def _load_history():
    if not os.path.exists(HISTORY_FILE): return []
    try:
        with open(HISTORY_FILE) as f: return json.load(f)
    except: return []

def _save_history(h):
    with open(HISTORY_FILE, "w") as f: json.dump(h, f, indent=2, default=str)

def _trade_key(symbol, strike):
    return f"{symbol}_{str(strike).replace(' ','_')}_{date.today().isoformat()}"

def _duration_hrs(given_at_str, end_dt=None):
    """Calculate hours between given_at (HH:MM) on today and end_dt."""
    try:
        today = date.today()
        parts = given_at_str.split(":")
        given_dt = datetime(today.year, today.month, today.day, int(parts[0]), int(parts[1]))
        end = end_dt or datetime.now()
        diff = (end - given_dt).total_seconds() / 3600
        return round(diff, 1)
    except:
        return 0


# ─── ACTIVE TRADE OPERATIONS ─────────────────────────────────────────────────

def register_trade(symbol, direction, strike, entry, sl_price, tgt_price, setup,
                   given_at=None, expiry=None, expiry_date=None, sector=None, extra=None):
    """
    Register a new trade. No-op if this stock already has a trade today.
    Stores all extra fields needed for the lock mechanism.
    """
    trades = _load_active()
    key    = _trade_key(symbol, strike)
    # Also check if this SYMBOL already has ANY trade today (different strike = still locked)
    today  = date.today().isoformat()
    for k, v in trades.items():
        if v.get("symbol") == symbol and v.get("date") == today and v.get("status") != "EXPIRED":
            log.debug(f"  {symbol}: already has trade today ({v.get('strike')}). Skipping new register.")
            return  # one trade per stock per day
    if key not in trades:
        record = {
            "symbol":      symbol,
            "direction":   direction,
            "strike":      strike,
            "setup":       setup,
            "entry":       entry,
            "sl_price":    sl_price,
            "tgt_price":   tgt_price,
            "given_at":    given_at or datetime.now().strftime("%H:%M"),
            "date":        today,
            "expiry":      expiry or "",
            "expiry_date": expiry_date or "",
            "sector":      sector or "",
            "status":      "ACTIVE",
            "hit_at":      None,
        }
        if extra:
            record.update(extra)  # store all extra fields for lock retrieval
        trades[key] = record
        _save_active(trades)
        log.info(f"Trade registered: {symbol} {strike} entry:{entry} sl:{sl_price} tgt:{tgt_price}")


def update_status(symbol, strike, current_ltp):
    """
    Check if option LTP has hit target or SL. Returns new status.
    Also persists current_ltp into the trade record so dashboard shows fresh LTP.
    Stops fetching LTP once status becomes TARGET or SL_HIT (we don't need updates after).
    """
    trades = _load_active()
    key    = _trade_key(symbol, strike)
    if key not in trades: return "UNKNOWN"
    t = trades[key]
    if t["status"] != "ACTIVE":
        # Trade closed — return existing status; do NOT update LTP further
        return t["status"]
    now = datetime.now().strftime("%H:%M")
    # Always persist current LTP for ACTIVE trades
    trades[key]["option_ltp"] = round(current_ltp, 2)
    trades[key]["last_ltp_update"] = datetime.now().isoformat()
    if current_ltp >= t["tgt_price"]:
        trades[key].update({"status": "TARGET", "hit_at": now, "exit_ltp": current_ltp})
        _save_active(trades)
        log.info(f"TARGET HIT: {symbol} {strike} @ ₹{current_ltp}")
        return "TARGET"
    if current_ltp <= t["sl_price"]:
        trades[key].update({"status": "SL_HIT", "hit_at": now, "exit_ltp": current_ltp})
        _save_active(trades)
        log.info(f"SL HIT: {symbol} {strike} @ ₹{current_ltp}")
        return "SL_HIT"
    _save_active(trades)
    return "ACTIVE"


def get_today_trades():
    """Returns today's active (non-expired) trades."""
    trades = _load_active()
    today  = date.today().isoformat()
    return {k: v for k, v in trades.items()
            if v.get("date") == today and v.get("status") != "EXPIRED"}


def get_status_label(symbol, strike):
    trades = _load_active()
    key    = _trade_key(symbol, strike)
    if key not in trades: return None
    t  = trades[key]
    s  = t.get("status")
    h  = t.get("hit_at", "")
    if s == "TARGET": return f"✓ TARGET HIT at {h}"
    if s == "SL_HIT": return f"✗ SL HIT at {h}"
    if s == "ACTIVE":  return f"Given at {t.get('given_at','')}"
    return None


def get_locked_trade(symbol):
    """
    Returns the existing trade record for a stock today if one exists (any status except EXPIRED).
    This is used by score_stock to lock trades — one per stock per day.
    Returns the full record dict or None if no trade exists today.
    """
    trades = _load_active()
    today  = date.today().isoformat()
    for k, v in trades.items():
        if v.get("symbol") == symbol and v.get("date") == today and v.get("status") != "EXPIRED":
            return v
    return None


def cleanup_trades(force_execute=False):
    """
    Called at 11 PM nightly.
    Moves all today's trades to history (as EXPIRED if not already moved).
    The active store is then cleared for next day.
    """
    trades  = _load_active()
    history = _load_history()
    today   = date.today().isoformat()
    moved   = 0
    for key, t in list(trades.items()):
        if t.get("date") != today: continue
        # Only auto-move if not already manually moved to history
        existing_keys = {h.get("key") for h in history}
        if key not in existing_keys:
            status  = t.get("status", "ACTIVE")
            hit_at  = t.get("hit_at")
            dur     = _duration_hrs(t.get("given_at","00:00"),
                                     datetime.now() if not hit_at else None)
            record = {
                "key":        key,
                "symbol":     t["symbol"],
                "direction":  t["direction"],
                "strike":     t["strike"],
                "setup":      t["setup"],
                "expiry":     t.get("expiry",""),
                "sector":     t.get("sector",""),
                "entry":      t["entry"],
                "sl_price":   t["sl_price"],
                "tgt_price":  t["tgt_price"],
                "given_at":   t["given_at"],
                "date":       t["date"],
                "hit_at":     hit_at,
                "status":     "NON_EXECUTED" if status == "ACTIVE" else status,
                # User trade fields (filled by manual move)
                "taken":      None,
                "user_entry": None,
                "user_exit":  None,
                "user_pnl":   None,
                "result":     "NON_EXECUTED" if status == "ACTIVE" else ("SYSTEM_TARGET" if status == "TARGET" else "SYSTEM_SL"),
                "moved_at":   datetime.now().isoformat(),
                "duration_hrs": dur,
                "notes":      "",
            }
            history.append(record)
            moved += 1
        trades[key]["status"] = "EXPIRED"
    _save_active(trades)
    _save_history(history)
    log.info(f"Cleanup: moved {moved} trades to history.")


# ─── HISTORY / EXPIRED OPERATIONS ────────────────────────────────────────────

def get_all_history():
    """Returns full permanent history, newest first."""
    h = _load_history()
    return sorted(h, key=lambda x: x.get("date","") + " " + x.get("given_at",""), reverse=True)


def manually_expire_trade(symbol, strike, trade_date,
                           taken, user_entry=None, user_exit=None, notes=""):
    """
    Called when user clicks 'Move to Expired' on an active trade.
    Moves trade from active store to permanent history with user P&L info.

    taken:      True / False
    user_entry: float — premium user actually paid (may differ from system entry)
    user_exit:  float — premium at which user exited
    notes:      free text
    """
    trades  = _load_active()
    history = _load_history()

    key = f"{symbol}_{str(strike).replace(' ','_')}_{trade_date}"
    if key not in trades:
        return {"ok": False, "msg": f"Trade not found: {key}"}

    t = trades[key]

    # Calculate P&L
    user_pnl = None
    if taken:
        result = "EXECUTED"
        if user_entry is not None and user_exit is not None:
            user_pnl = round(float(user_exit) - float(user_entry), 2)
            if   user_pnl > 0:  result = "PROFIT"
            elif user_pnl < 0:  result = "LOSS"
            else:                result = "BREAKEVEN"
    else:
        result = "NON_EXECUTED"  # user chose not to take the trade

    now = datetime.now()
    dur = _duration_hrs(t.get("given_at", "00:00"), now)

    record = {
        "key":          key,
        "symbol":       t["symbol"],
        "direction":    t["direction"],
        "strike":       t["strike"],
        "setup":        t["setup"],
        "expiry":       t.get("expiry", ""),
        "sector":       t.get("sector", ""),
        "entry":        t["entry"],      # system entry (Ask at signal time)
        "sl_price":     t["sl_price"],
        "tgt_price":    t["tgt_price"],
        "given_at":     t["given_at"],
        "date":         t["date"],
        "hit_at":       t.get("hit_at"),
        "status":       t.get("status", "ACTIVE"),
        # User trade data
        "taken":        taken,
        "user_entry":   float(user_entry)  if user_entry  is not None else None,
        "user_exit":    float(user_exit)   if user_exit   is not None else None,
        "user_pnl":     user_pnl,
        "result":       result,
        "moved_at":     now.isoformat(),
        "duration_hrs": dur,
        "notes":        notes,
    }

    # Check for duplicate key in history
    existing = [h for h in history if h.get("key") == key]
    if existing:
        # Update existing record
        for i, h in enumerate(history):
            if h.get("key") == key:
                history[i] = record
                break
    else:
        history.append(record)

    # Mark as expired in active store
    trades[key]["status"] = "EXPIRED"
    _save_active(trades)
    _save_history(history)

    log.info(f"Manually expired: {symbol} {strike} | taken={taken} | result={result} | pnl={user_pnl}")
    return {"ok": True, "record": record}


def update_history_record(key, taken, user_entry=None, user_exit=None, notes=""):
    """Update an already-expired record in history (e.g. to add exit price later)."""
    history = _load_history()
    for i, h in enumerate(history):
        if h.get("key") == key:
            user_pnl = None
            result   = "NOT_TAKEN"
            if taken and user_entry is not None and user_exit is not None:
                user_pnl = round(float(user_exit) - float(user_entry), 2)
                result = "PROFIT" if user_pnl > 0 else ("LOSS" if user_pnl < 0 else "BREAKEVEN")
            elif taken:
                result = "TAKEN_NO_EXIT"
            history[i].update({
                "taken": taken, "user_entry": user_entry, "user_exit": user_exit,
                "user_pnl": user_pnl, "result": result, "notes": notes,
                "updated_at": datetime.now().isoformat(),
            })
            _save_history(history)
            return {"ok": True}
    return {"ok": False, "msg": "Record not found"}


def get_history_stats():
    """Summary stats for the history (for a potential dashboard summary)."""
    h = _load_history()
    taken    = [r for r in h if r.get("taken")]
    not_taken= [r for r in h if r.get("taken") is False]
    profit   = [r for r in taken if r.get("result") == "PROFIT"]
    loss     = [r for r in taken if r.get("result") == "LOSS"]
    sys_tgt  = [r for r in h if r.get("result") == "SYSTEM_TARGET"]
    sys_sl   = [r for r in h if r.get("result") == "SYSTEM_SL"]
    total_pnl= sum(r.get("user_pnl") or 0 for r in taken)
    return {
        "total_trades":   len(h),
        "taken":          len(taken),
        "not_taken":      len(not_taken),
        "profit":         len(profit),
        "loss":           len(loss),
        "system_target":  len(sys_tgt),
        "system_sl":      len(sys_sl),
        "total_user_pnl": round(total_pnl, 2),
        "win_rate":       round(len(profit)/len(taken)*100, 1) if taken else 0,
    }


def get_expired_symbols_today():
    """
    Returns a set of symbol names that were manually expired today.
    Used by /api/data to filter them out of the active stocks list immediately.
    """
    trades = _load_active()
    today  = date.today().isoformat()
    return {v.get("symbol") for k, v in trades.items()
            if v.get("date") == today and v.get("status") == "EXPIRED"}


def get_executed_trades():
    """Trades user explicitly executed (taken=True)."""
    h = _load_history()
    return sorted([r for r in h if r.get("taken") is True],
                  key=lambda x: x.get("date","")+" "+x.get("given_at",""), reverse=True)


def get_non_executed_trades():
    """Trades user did not take, or that expired at close without action."""
    h = _load_history()
    return sorted([r for r in h if r.get("taken") is False or r.get("result") in ("NON_EXECUTED","EXPIRED")],
                  key=lambda x: x.get("date","")+" "+x.get("given_at",""), reverse=True)
