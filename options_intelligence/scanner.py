"""Finviz CSV screening: no brokerage calls, no scraping, no execution.

CSV files must be obtained from an authorized source. This module intentionally
fails closed on missing market data, stale snapshots, and unknown earnings dates.
Score is a heuristic ranking index, NOT a calibrated win probability.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

UTC = timezone.utc
WATCHLIST = {"NVDA", "AMD", "TSLA", "SPY", "QQQ"}
REQUIRED = {"ticker", "market_cap", "average_volume", "relative_volume", "rsi", "sma20", "sma50", "sma200", "optionable", "snapshot_utc", "earnings_date"}

@dataclass
class Decision:
    ticker: str
    decision: str
    direction: str
    score: Optional[int]
    reasons: list[str]
    source: str
    snapshot_utc: str
    strategy: str = "US_EQUITIES_WATCHLIST_V1"
    instrument_type: str = "US_EQUITY_UNDERLYING"
    execution_enabled: bool = False
    option_contract_validated: bool = False
    option_max_loss: Optional[float] = None


def number(raw: str) -> Optional[float]:
    s = str(raw or "").strip().replace(",", "").replace("$", "")
    if not s or s.lower() in {"-", "n/a", "na"}: return None
    factor = 1
    if s[-1:].upper() in {"K", "M", "B", "T"}:
        factor = {"K":1e3, "M":1e6, "B":1e9, "T":1e12}[s[-1].upper()]; s = s[:-1]
    try:
        v = float(s) * factor
        return v if math.isfinite(v) else None
    except ValueError: return None


def dt_utc(raw: str) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        return d.astimezone(UTC) if d.tzinfo else None
    except (ValueError, TypeError): return None


def earnings_date(raw: str):
    try: return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError): return None


def analyze(row: dict, now: datetime, max_age_hours: float, source: str, days_around_earnings: int = 3) -> Decision:
    ticker = str(row.get("ticker", "")).strip().upper()
    reasons=[]
    snapshot=dt_utc(row.get("snapshot_utc", ""))
    stamp = snapshot.isoformat() if snapshot else "UNKNOWN"
    if not snapshot: reasons.append("MISSING_OR_INVALID_SNAPSHOT_UTC")
    elif snapshot>now+timedelta(minutes=5): reasons.append("FUTURE_SNAPSHOT")
    elif (now-snapshot).total_seconds()>max_age_hours*3600: reasons.append("STALE_SNAPSHOT")
    if str(row.get("optionable", "")).strip().lower() not in {"yes", "true", "1"}: reasons.append("NOT_CONFIRMED_OPTIONABLE")
    cap = number(row.get("market_cap"))
    vol = number(row.get("average_volume"))
    rvol = number(row.get("relative_volume"))
    rsi = number(row.get("rsi"))
    s20, s50, s200 = (number(row.get(k)) for k in ("sma20", "sma50", "sma200"))
    if cap is None: reasons.append("MISSING_MARKET_CAP")
    elif cap<10e9: reasons.append("SMALL_MARKET_CAP")
    if vol is None: reasons.append("MISSING_AVERAGE_VOLUME")
    elif vol<1e6: reasons.append("LOW_AVERAGE_VOLUME")
    if rvol is None: reasons.append("MISSING_RELATIVE_VOLUME")
    elif rvol<=1.5: reasons.append("LOW_RELATIVE_VOLUME")
    if rsi is None or not 0<=rsi<=100: reasons.append("MISSING_OR_INVALID_RSI")
    if None in (s20,s50,s200): reasons.append("MISSING_SMA_DATA")
    ed=earnings_date(row.get("earnings_date"))
    if not ed: reasons.append("EARNINGS_UNVERIFIED")
    elif abs((ed-now.date()).days)<=days_around_earnings: reasons.append("EARNINGS_WINDOW")
    if ticker not in WATCHLIST: reasons.append("NOT_IN_INITIAL_WATCHLIST")
    if reasons:
        return Decision(ticker,"REJECTED","NO_TRADE",None,reasons,source,stamp)
    if s20>s50>s200 and 52<=rsi<=68:
        direction="CALL_CANDIDATE"
    elif s20<s50<s200 and 32<=rsi<=48:
        direction="PUT_CANDIDATE"
    else:
        return Decision(ticker,"REJECTED","NO_TRADE",None,["TREND_OR_MOMENTUM_NOT_CONFIRMED"],source,stamp)
    # Relative ranking only: intentionally not a win-probability estimate.
    score=min(100, int(round(50+min(20,(rvol-1.5)*10)+min(15,vol/1e7*10)+min(15,cap/1e12*10))))
    return Decision(ticker,"WATCHLIST_CANDIDATE",direction,score,["SCREEN_PASSED", "NOT_AN_OPTION_CONTRACT_QUOTE"],source,stamp)


def scan(input_path: Path, output_path: Path, max_age_hours: float, as_of: Optional[datetime]=None) -> dict:
    now=as_of or datetime.now(UTC)
    with input_path.open("r",encoding="utf-8-sig",newline="") as fp:
        reader=csv.DictReader(fp)
        if not reader.fieldnames: raise ValueError("CSV has no header")
        missing=REQUIRED-set(reader.fieldnames)
        if missing: raise ValueError("Missing required CSV columns: "+", ".join(sorted(missing)))
        rows=list(reader)
    items=[analyze(r,now,max_age_hours,"finviz_user_export_csv",3) for r in rows]
    eligible=sorted((r for r in items if r.decision=="WATCHLIST_CANDIDATE"),key=lambda x:(-x.score,x.ticker))[:5]
    chosen={r.ticker for r in eligible}
    for r in items:
        if r.decision=="WATCHLIST_CANDIDATE" and r.ticker not in chosen:
            r.decision="REJECTED";r.direction="NO_TRADE";r.reasons=["OUTSIDE_TOP_5"]
    payload={"schema_version":"1.0", "generated_at_utc":now.isoformat(),
             "source":"finviz_user_export_csv", "market":"US_EQUITIES",
             "purpose":"RESEARCH_ONLY", "trading_enabled":False,
             "signal_score_is_win_probability":False,
             "candidate_count":len(eligible),"inputs":len(rows),
             "notes":["No option chain, strike, expiration, bid/ask, or implied volatility is supplied.",
                      "No options strategy entry/exit can be validated from this equity screen.",
                      "No orders may be sent from this module."],
             "decisions":[asdict(x) for x in items]}
    output_path.parent.mkdir(parents=True,exist_ok=True)
    output_path.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding="utf-8")
    return payload


def main():
    p=argparse.ArgumentParser(description="Read-only Finviz CSV Options Scanner for Trading Brain")
    p.add_argument("--input",required=True,type=Path,help="CSV export from authorized Finviz access, normalized columns")
    p.add_argument("--output",default=Path("outputs/options_signals.json"),type=Path)
    p.add_argument("--max-age-hours",type=float,default=36)
    p.add_argument("--as-of-utc",help="ISO8601 for reproducible testing (must include UTC timezone)")
    args=p.parse_args()
    if args.max_age_hours<=0: p.error("max-age-hours must be positive")
    asof=dt_utc(args.as_of_utc) if args.as_of_utc else None
    if args.as_of_utc and not asof: p.error("as-of-utc must have timezone, e.g. 2026-11-10T15:00:00Z")
    result=scan(args.input,args.output,args.max_age_hours,asof)
    print(f"CSV rows: {result['inputs']}; eligible watchlist candidates: {result['candidate_count']}")
    for r in result["decisions"]:
        print(f"{r['ticker']:<6} {r['decision']:<20} {r['direction']:<16} score={str(r['score']):<4} {';'.join(r['reasons'])}")
    print("Saved:",args.output)
    print("RESEARCH ONLY: zero order execution; data may be delayed.")
if __name__=="__main__": main()
