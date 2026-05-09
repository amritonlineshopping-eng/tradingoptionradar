"""
learner.py — Options Radar Weekly Learning Framework
=====================================================
Runs every Friday after market close (15:31 IST).
Analyzes all trades in trade_history.json and produces
scoring adjustments stored in learnings.json.

The scanner reads learnings.json on every scan and applies
multipliers to the raw score, so the system improves over time.

Learning logic:
  For each setup type (SMC Bullish Order Block, CPR Breakout, etc.):
    win_rate = profitable trades / total taken trades
    multiplier = clamp(1.0 + (win_rate - 0.5) * 0.6, 0.5, 1.8)
    > 50% win rate → score boosted (up to +80%)
    < 50% win rate → score penalized (down to -50%)

  For time-of-day:
    Track which hours produce wins vs losses.
    Hours with < 30% win rate → penalize trades in that hour.

  For vol_surge threshold:
    Find the vol_surge value where win rate maximizes.
    Suggest optimal threshold.

  For VIX levels:
    Track performance at different VIX ranges.
    Penalize entries when VIX is in historically bad range.

All adjustments are GRADUAL:
  New multiplier = 0.7 * old_multiplier + 0.3 * new_multiplier
  (Exponential moving average — prevents overfit to single bad week)
"""

import os, json, logging
from datetime import date, datetime, timedelta
from collections import defaultdict

log = logging.getLogger("learner")

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE   = os.path.join(BASE_DIR, "trade_history.json")
LEARNINGS_FILE = os.path.join(BASE_DIR, "learnings.json")
REPORT_FILE    = os.path.join(BASE_DIR, "weekly_report.json")

# Min trades required before adjusting a setup's multiplier
MIN_TRADES_FOR_ADJUSTMENT = 5

# How much weight to give new week vs historical learnings (EMA)
EMA_ALPHA = 0.35  # 35% new data, 65% history — prevents overfit


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _load_history():
    if not os.path.exists(HISTORY_FILE): return []
    try:
        with open(HISTORY_FILE) as f: return json.load(f)
    except: return []

def _load_learnings():
    if not os.path.exists(LEARNINGS_FILE):
        return {"setup_multipliers": {}, "hour_multipliers": {}, "vol_threshold": 1.3,
                "last_run": None, "weeks_analyzed": 0, "version": 1}
    try:
        with open(LEARNINGS_FILE) as f: return json.load(f)
    except:
        return {"setup_multipliers": {}, "hour_multipliers": {}, "vol_threshold": 1.3,
                "last_run": None, "weeks_analyzed": 0, "version": 1}

def _save_learnings(data):
    with open(LEARNINGS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

def _save_report(data):
    with open(REPORT_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

def _clamp(val, lo, hi):
    return max(lo, min(hi, val))

def _is_conclusive(result):
    """Trade had a clear outcome we can learn from."""
    return result in ("PROFIT", "LOSS", "SYSTEM_TARGET", "SYSTEM_SL")

def _is_win(result):
    return result in ("PROFIT", "SYSTEM_TARGET")

def _ema(old, new, alpha=EMA_ALPHA):
    """Exponential moving average: blends new signal into historical learnings."""
    if old is None: return new
    return round(old * (1 - alpha) + new * alpha, 4)


# ─── CORE ANALYSIS ────────────────────────────────────────────────────────────

def analyze(trades_subset=None, label="weekly"):
    """
    Analyze trade history and produce learning adjustments.

    trades_subset: list of trade dicts to analyze (default: last 4 weeks)
    label: "weekly" or "all_time"
    """
    all_trades = _load_history()
    learnings  = _load_learnings()

    # Default: analyze last 4 weeks (28 days) of conclusive trades
    cutoff = (date.today() - timedelta(days=28)).isoformat()
    if trades_subset is None:
        trades = [t for t in all_trades
                  if t.get("date","") >= cutoff and _is_conclusive(t.get("result",""))]
    else:
        trades = [t for t in trades_subset if _is_conclusive(t.get("result",""))]

    if not trades:
        log.info("Learner: no conclusive trades to analyze.")
        return learnings

    log.info(f"Learner: analyzing {len(trades)} conclusive trades ({label})")

    # ── 1. SETUP WIN RATES ────────────────────────────────────────────────────
    setup_stats = defaultdict(lambda: {"total": 0, "wins": 0, "losses": 0,
                                       "avg_vol_win": [], "avg_vol_loss": [],
                                       "avg_score": []})
    for t in trades:
        setup = t.get("setup", "Unknown")
        if not setup or setup == "—": continue
        win = _is_win(t.get("result",""))
        vs  = float(t.get("vol_surge", 1.0) or 1.0)
        sc  = float(t.get("score", 5.0) or 5.0)
        setup_stats[setup]["total"] += 1
        setup_stats[setup]["avg_score"].append(sc)
        if win:
            setup_stats[setup]["wins"] += 1
            setup_stats[setup]["avg_vol_win"].append(vs)
        else:
            setup_stats[setup]["losses"] += 1
            setup_stats[setup]["avg_vol_loss"].append(vs)

    new_setup_multipliers = {}
    setup_report = {}
    for setup, s in setup_stats.items():
        total = s["total"]
        if total < MIN_TRADES_FOR_ADJUSTMENT:
            # Not enough data — keep existing multiplier, note for report
            new_setup_multipliers[setup] = learnings["setup_multipliers"].get(setup, 1.0)
            setup_report[setup] = {
                "total": total, "wins": s["wins"], "losses": s["losses"],
                "win_rate": round(s["wins"]/total*100, 1) if total else 0,
                "multiplier": new_setup_multipliers[setup],
                "note": f"Only {total} trades — need {MIN_TRADES_FOR_ADJUSTMENT}+ to adjust",
                "avg_vol_win":  round(sum(s["avg_vol_win"])/len(s["avg_vol_win"]),2) if s["avg_vol_win"] else None,
                "avg_vol_loss": round(sum(s["avg_vol_loss"])/len(s["avg_vol_loss"]),2) if s["avg_vol_loss"] else None,
            }
            continue

        win_rate = s["wins"] / total
        # multiplier: linear scale from 0.5 (0% win) to 1.8 (100% win)
        raw_multiplier = _clamp(1.0 + (win_rate - 0.5) * 1.6, 0.5, 1.8)
        old_multiplier = learnings["setup_multipliers"].get(setup)
        blended        = _ema(old_multiplier, raw_multiplier)
        new_setup_multipliers[setup] = blended

        avg_vol_win  = round(sum(s["avg_vol_win"])/len(s["avg_vol_win"]),2)  if s["avg_vol_win"]  else None
        avg_vol_loss = round(sum(s["avg_vol_loss"])/len(s["avg_vol_loss"]),2) if s["avg_vol_loss"] else None
        avg_score    = round(sum(s["avg_score"])/len(s["avg_score"]),1)

        setup_report[setup] = {
            "total": total, "wins": s["wins"], "losses": s["losses"],
            "win_rate": round(win_rate*100, 1),
            "raw_multiplier": round(raw_multiplier, 3),
            "blended_multiplier": blended,
            "avg_score": avg_score,
            "avg_vol_win": avg_vol_win,
            "avg_vol_loss": avg_vol_loss,
            "verdict": (
                "BOOST" if blended > 1.1 else
                "PENALIZE" if blended < 0.9 else
                "NEUTRAL"
            ),
        }
        log.info(f"  {setup}: {s['wins']}/{total} wins ({win_rate*100:.0f}%) → x{blended:.2f}")

    # ── 2. HOUR-OF-DAY WIN RATES ─────────────────────────────────────────────
    hour_stats = defaultdict(lambda: {"total": 0, "wins": 0})
    for t in trades:
        given = t.get("given_at", "")
        if not given or ":" not in given: continue
        try:
            hour = int(given.split(":")[0])
            win  = _is_win(t.get("result",""))
            hour_stats[hour]["total"] += 1
            if win: hour_stats[hour]["wins"] += 1
        except: continue

    new_hour_multipliers = {}
    hour_report = {}
    for hour, s in hour_stats.items():
        total = s["total"]
        if total < 3:
            new_hour_multipliers[str(hour)] = learnings["hour_multipliers"].get(str(hour), 1.0)
            continue
        win_rate = s["wins"] / total
        raw_mult = _clamp(0.7 + win_rate * 0.6, 0.5, 1.3)  # smaller range for hour
        old_mult = learnings["hour_multipliers"].get(str(hour))
        blended  = _ema(old_mult, raw_mult)
        new_hour_multipliers[str(hour)] = blended
        hour_report[f"{hour:02d}:xx"] = {
            "total": total, "wins": s["wins"],
            "win_rate": round(win_rate*100, 1),
            "multiplier": blended,
        }

    # ── 3. OPTIMAL VOL_SURGE THRESHOLD ───────────────────────────────────────
    vol_bins = defaultdict(lambda: {"total": 0, "wins": 0})
    for t in trades:
        vs = float(t.get("vol_surge", 0) or 0)
        if vs <= 0: continue
        bucket = round(vs * 2) / 2  # bins: 1.0, 1.5, 2.0, 2.5, ...
        vol_bins[bucket]["total"] += 1
        if _is_win(t.get("result","")): vol_bins[bucket]["wins"] += 1

    best_vol_threshold = learnings.get("vol_threshold", 1.3)
    best_wr = 0
    vol_report = {}
    for bucket, s in sorted(vol_bins.items()):
        if s["total"] < 3: continue
        wr = s["wins"] / s["total"]
        vol_report[str(bucket)] = {"total": s["total"], "wins": s["wins"], "win_rate": round(wr*100,1)}
        if wr > best_wr and bucket >= 1.0:
            best_wr = wr
            best_vol_threshold = bucket

    # Blend with existing threshold
    old_threshold = learnings.get("vol_threshold", 1.3)
    new_threshold = round(_ema(old_threshold, max(1.0, best_vol_threshold)), 2)

    # ── 4. DIRECTION PERFORMANCE ──────────────────────────────────────────────
    bull_trades = [t for t in trades if t.get("direction") == "BULL"]
    bear_trades = [t for t in trades if t.get("direction") == "BEAR"]
    bull_wr = sum(1 for t in bull_trades if _is_win(t.get("result",""))) / len(bull_trades) if bull_trades else 0.5
    bear_wr = sum(1 for t in bear_trades if _is_win(t.get("result",""))) / len(bear_trades) if bear_trades else 0.5

    direction_multipliers = {
        "BULL": _ema(learnings.get("direction_multipliers",{}).get("BULL", 1.0),
                     _clamp(0.7 + bull_wr * 0.6, 0.5, 1.4)),
        "BEAR": _ema(learnings.get("direction_multipliers",{}).get("BEAR", 1.0),
                     _clamp(0.7 + bear_wr * 0.6, 0.5, 1.4)),
    }

    # ── 5. COMPILE FINAL LEARNINGS ────────────────────────────────────────────
    learnings["setup_multipliers"]     = new_setup_multipliers
    learnings["hour_multipliers"]      = new_hour_multipliers
    learnings["direction_multipliers"] = direction_multipliers
    learnings["vol_threshold"]         = new_threshold
    learnings["last_run"]              = datetime.now().isoformat()
    learnings["weeks_analyzed"]        = learnings.get("weeks_analyzed", 0) + 1
    learnings["total_trades_analyzed"] = len(all_trades)
    learnings["conclusive_this_run"]   = len(trades)

    _save_learnings(learnings)

    # ── 6. WEEKLY REPORT ──────────────────────────────────────────────────────
    all_wins   = sum(1 for t in trades if _is_win(t.get("result","")))
    overall_wr = round(all_wins / len(trades) * 100, 1) if trades else 0

    report = {
        "generated_at":   datetime.now().isoformat(),
        "period":         f"Last 28 days (as of {date.today()})",
        "total_conclusive_trades": len(trades),
        "overall_win_rate": overall_wr,
        "bull_win_rate":  round(bull_wr*100, 1),
        "bear_win_rate":  round(bear_wr*100, 1),
        "optimal_vol_surge_threshold": new_threshold,
        "setups": setup_report,
        "hours":  hour_report,
        "vol_surge_bins": vol_report,
        "direction_multipliers": direction_multipliers,
        "key_insights": _generate_insights(setup_report, hour_report, vol_report, overall_wr, bull_wr, bear_wr),
    }
    _save_report(report)
    log.info(f"Learner: report saved. Overall win rate: {overall_wr}%")
    return learnings


def _generate_insights(setup_report, hour_report, vol_report, overall_wr, bull_wr, bear_wr):
    """Generate human-readable insights from the analysis."""
    insights = []

    # Overall performance
    if overall_wr >= 55:
        insights.append(f"Strong week: {overall_wr}% win rate. Keep current criteria.")
    elif overall_wr >= 40:
        insights.append(f"Average week: {overall_wr}% win rate. Minor adjustments applied.")
    else:
        insights.append(f"Difficult week: {overall_wr}% win rate. Scoring tightened on underperforming setups.")

    # Best setups
    boosts = [(s, v["win_rate"]) for s, v in setup_report.items() if v.get("verdict") == "BOOST"]
    if boosts:
        boosts.sort(key=lambda x: -x[1])
        insights.append(f"Best setups this period: {', '.join(f'{s} ({w}%)' for s,w in boosts[:3])}")

    # Worst setups
    penalized = [(s, v["win_rate"]) for s, v in setup_report.items() if v.get("verdict") == "PENALIZE"]
    if penalized:
        penalized.sort(key=lambda x: x[1])
        insights.append(f"Underperforming setups (score penalized): {', '.join(f'{s} ({w}%)' for s,w in penalized[:3])}")

    # Direction bias
    if bull_wr > bear_wr + 0.15:
        insights.append(f"BULL trades outperforming ({bull_wr*100:.0f}% vs {bear_wr*100:.0f}%). Slight BULL bias applied.")
    elif bear_wr > bull_wr + 0.15:
        insights.append(f"BEAR trades outperforming ({bear_wr*100:.0f}% vs {bull_wr*100:.0f}%). Slight BEAR bias applied.")

    # Best hours
    good_hours = [(h, v["win_rate"]) for h, v in hour_report.items() if v["win_rate"] >= 55 and v["total"] >= 3]
    if good_hours:
        good_hours.sort(key=lambda x: -x[1])
        insights.append(f"Best trading hours: {', '.join(h for h,_ in good_hours[:3])}")

    return insights


# ─── APPLY LEARNINGS TO SCORE ─────────────────────────────────────────────────

def apply_learnings(raw_score, setup_name, direction, vol_surge, signal_hour):
    """
    Called by score_stock to apply learning multipliers to the raw score.
    Returns adjusted score. If no learnings exist, returns raw_score unchanged.
    """
    if not os.path.exists(LEARNINGS_FILE):
        return raw_score

    try:
        learnings = _load_learnings()
        mult = 1.0

        # Setup multiplier
        setup_mult = learnings.get("setup_multipliers", {}).get(setup_name)
        if setup_mult:
            mult *= setup_mult

        # Direction multiplier
        dir_mult = learnings.get("direction_multipliers", {}).get(direction)
        if dir_mult:
            mult *= dir_mult

        # Hour multiplier
        hour_mult = learnings.get("hour_multipliers", {}).get(str(signal_hour))
        if hour_mult:
            mult *= hour_mult

        adjusted = round(raw_score * mult, 1)
        # Never let learnings drop a score below 1.0 or boost above 10.0
        return _clamp(adjusted, 1.0, 10.0)

    except Exception as e:
        log.debug(f"apply_learnings error: {e}")
        return raw_score


def get_optimal_vol_threshold():
    """Returns the learned optimal vol_surge threshold (default 1.3)."""
    if not os.path.exists(LEARNINGS_FILE): return 1.3
    try:
        return _load_learnings().get("vol_threshold", 1.3)
    except: return 1.3


def get_latest_report():
    """Returns the latest weekly report dict, or empty dict."""
    if not os.path.exists(REPORT_FILE): return {}
    try:
        with open(REPORT_FILE) as f: return json.load(f)
    except: return {}


# ─── SCHEDULER — runs every Friday 15:31 IST ──────────────────────────────────

def run_weekly_learner_loop():
    """
    Background thread. Checks every minute if it's Friday after 15:31.
    If yes and not already run this Friday, triggers analyze().
    """
    import time
    last_run_date = None
    log.info("Weekly learner loop started (runs every Friday 15:31).")

    while True:
        try:
            now = datetime.now()
            today = date.today()
            is_friday = now.weekday() == 4  # 4 = Friday
            after_close = now.hour > 15 or (now.hour == 15 and now.minute >= 31)

            if is_friday and after_close and last_run_date != today:
                log.info("Friday post-close detected. Running weekly learning analysis...")
                try:
                    analyze(label="weekly")
                    last_run_date = today
                    log.info("Weekly learning complete.")
                except Exception as e:
                    log.error(f"Learning analysis error: {e}")
        except Exception as e:
            log.debug(f"Learner loop error: {e}")

        time.sleep(60)  # check every minute
