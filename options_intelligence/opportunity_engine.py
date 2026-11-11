"""Offline research watchlist merger; never produces executable directions.

Stage 1 must supply snapshot_utc (availability) and data_date (market date),
on each decision or at file level. generated_at_utc is NOT a market timestamp.
Defaults: stage snapshot <=36 hours old; daily bar <=5 calendar days old.
Daily bars must predate the as-of UTC day: conservative end-of-day policy,
without guessing exchange sessions/holidays. Future snapshots are rejected,
not silently truncated or used for historical reconstruction.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import date, datetime, timezone

from .technical_indicators import PROJECT_ROOT, build_report, validate_candles

SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
REQUIRED = ("ema_20", "ema_50", "rsi_14", "atr_14")
EXECUTION_BLOCKERS = ["OPTION_CONTRACT_MISSING", "CONTRACT_LIQUIDITY_MISSING",
                      "CONTRACT_PRICE_MISSING", "FEES_MISSING", "RISK_VALIDATION_MISSING"]


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        pass
    raise ValueError("expected ISO timestamp with timezone")


def market_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except (ValueError, TypeError):
        pass
    raise ValueError("expected YYYY-MM-DD market date")


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def evaluate_technicals(indicators, close, max_atr_fraction=0.08):
    """Pure heuristic, independently testable. Oversold alone never qualifies.

    Bullish: EMA20>EMA50 and RSI52..68; bearish: EMA20<EMA50 and RSI32..48.
    ATR/close must be <=8%. EMA200 is optional and does not affect ranking.
    Score rewards trend separation and momentum, penalizes relative volatility;
    it is an arbitrary ordering index, without a probability interpretation.
    """
    if not finite(max_atr_fraction) or max_atr_fraction <= 0:
        raise ValueError("max_atr_fraction must be positive and finite")
    missing = [key for key in REQUIRED if not finite(indicators.get(key))]
    if missing or not finite(close) or close <= 0:
        return {"status": "INSUFFICIENT_DATA", "reasons": ["MISSING_OR_INVALID:" + k for k in missing]
                + ([] if finite(close) and close > 0 else ["INVALID_CLOSE"]), "score": None}
    e20, e50, rsi, atr = (indicators[k] for k in REQUIRED)
    if e20 <= 0 or e50 <= 0 or not 0 <= rsi <= 100 or atr < 0:
        return {"status": "INSUFFICIENT_DATA", "reasons": ["INVALID_INDICATOR_RANGE"], "score": None}
    trend = "BULLISH" if e20 > e50 else "BEARISH" if e20 < e50 else "FLAT"
    aligned = (trend == "BULLISH" and 52 <= rsi <= 68) or (trend == "BEARISH" and 32 <= rsi <= 48)
    fraction = atr / close
    reasons = []
    if not aligned:
        reasons.append("TREND_MOMENTUM_NOT_ALIGNED")
    if rsi < 30:
        reasons.append("OVERSOLD_IS_NOT_BUY_CONFIRMATION")
    if fraction > max_atr_fraction:
        reasons.append("ATR_ABOVE_RESEARCH_LIMIT")
    score = None if reasons else round(50 + min(20, abs(e20 / e50 - 1) * 200)
                                       + min(20, abs(rsi - 50)) + max(0, 10 - fraction * 100), 6)
    return {"status": "REJECTED" if reasons else "WATCHLIST", "trend": trend,
            "rsi_14": rsi, "atr_fraction": fraction, "score": score,
            "reasons": reasons or ["TREND_MOMENTUM_AND_VOLATILITY_PASSED"]}


def check_eligibility(row):
    """Trust only an explicit Stage 1 outcome; preserve upstream reasons."""
    if not isinstance(row.get("reasons"), list) or any(not isinstance(r, str) for r in row["reasons"]):
        return {"status": "INSUFFICIENT_DATA", "reasons": ["INVALID_STAGE1_REASONS"]}
    if row.get("status") == "REJECTED":
        return {"status": "REJECTED", "reasons": ["STAGE1_REJECTED", *row.get("reasons", [])]}
    if row.get("status") != "WATCHLIST" or row.get("reasons") != []:
        return {"status": "INSUFFICIENT_DATA", "reasons": ["INVALID_STAGE1_OUTCOME"]}
    return {"status": "ELIGIBLE", "reasons": ["STAGE1_PASSED"]}


def evaluate(stage1, bundles, as_of, max_snapshot_hours=36, max_bar_age_days=5):
    """Merge ticker -> {report, daily, report_file}; no IO and no mutation."""
    now = timestamp(as_of.isoformat())
    if not finite(max_snapshot_hours) or max_snapshot_hours <= 0 or type(max_bar_age_days) is not int or max_bar_age_days < 1:
        raise ValueError("invalid freshness limits")
    if not isinstance(stage1, dict) or stage1.get("stage") != "FINVIZ_STAGE_1" or not stage1.get("source"):
        raise ValueError("expected sourced FINVIZ_STAGE_1 object")
    rows = stage1.get("decisions")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("expected Stage 1 decisions list")
    tickers = [r.get("ticker") for r in rows]
    if any(not isinstance(t, str) or not SYMBOL.fullmatch(t) for t in tickers) or len(set(tickers)) != len(tickers):
        raise ValueError("invalid or duplicate Stage 1 tickers")
    global_issues = []
    generated = None
    try:
        generated = timestamp(stage1.get("generated_at_utc"))
        if generated > now:
            global_issues.append("FUTURE_STAGE1_FILE")
    except ValueError:
        global_issues.append("INVALID_STAGE1_GENERATION_TIME")
    decisions = []
    for row in rows:
        ticker = row["ticker"]
        eligibility = check_eligibility(row)
        issues = list(global_issues)
        provenance = {"stage1_source": stage1["source"], "stage1_generated_at_utc": stage1.get("generated_at_utc"),
                      "stage1_snapshot_utc": row.get("snapshot_utc", stage1.get("snapshot_utc")),
                      "stage1_data_date": row.get("data_date", stage1.get("data_date"))}
        snapshot = None
        try:
            snapshot = timestamp(provenance["stage1_snapshot_utc"])
            if snapshot > now:
                issues.append("FUTURE_STAGE1_SNAPSHOT")
            elif (now - snapshot).total_seconds() > max_snapshot_hours * 3600:
                issues.append("STALE_STAGE1_SNAPSHOT")
            if generated is not None and snapshot > generated:
                issues.append("SNAPSHOT_AFTER_STAGE1_GENERATION")
        except ValueError:
            issues.append("MISSING_OR_INVALID_STAGE1_SNAPSHOT")
        try:
            stage_day = market_date(provenance["stage1_data_date"])
            if stage_day >= now.date():
                issues.append("STAGE1_DATE_NOT_COMPLETED")
            if (now.date() - stage_day).days > max_bar_age_days:
                issues.append("STALE_STAGE1_DATA_DATE")
            if snapshot is not None and stage_day >= snapshot.date():
                issues.append("STAGE1_SNAPSHOT_BEFORE_DATA_COMPLETION")
        except ValueError:
            issues.append("MISSING_OR_INVALID_STAGE1_DATA_DATE")
        technical, warnings = None, []
        bundle = bundles.get(ticker)
        if bundle is None:
            issues.append("TECHNICAL_DATA_MISSING")
        elif bundle.get("error"):
            issues.append("TECHNICAL_FILE_ERROR:" + bundle["error"])
        else:
            report, daily = bundle.get("report"), bundle.get("daily")
            try:
                if not isinstance(report, dict) or not isinstance(daily, dict):
                    raise ValueError("INVALID_TECHNICAL_OBJECT")
                provenance.update(technical_report_file=bundle.get("report_file"),
                                  technical_source=report.get("source"), daily_file=report.get("source_file"),
                                  technical_data_date=report.get("date"),
                                  source_fetched_at_utc=daily.get("fetched_at_utc"))
                if report.get("symbol") != ticker or daily.get("symbol") != ticker:
                    raise ValueError("SYMBOL_MISMATCH")
                if report.get("status") != "RESEARCH_ONLY" or report.get("trading_enabled") is not False:
                    raise ValueError("INVALID_TECHNICAL_RESEARCH_STATUS")
                recomputed = build_report(daily, report.get("source_file"))
                for key in ("date", "bar_count", "source", "source_fetched_at_utc", "price_adjustment"):
                    if report.get(key) != recomputed[key]:
                        raise ValueError("REPORT_DAILY_MISMATCH:" + key)
                day = market_date(report.get("date"))
                if day >= now.date():
                    issues.append("DAILY_DATE_NOT_COMPLETED")
                if (now.date() - day).days > max_bar_age_days:
                    issues.append("STALE_DAILY_DATA")
                if provenance["stage1_data_date"] and provenance["stage1_data_date"] != day.isoformat():
                    issues.append("STAGE1_TECHNICAL_DATE_MISMATCH")
                fetched = timestamp(daily.get("fetched_at_utc"))
                if fetched > now:
                    issues.append("FUTURE_DAILY_AVAILABILITY")
                if fetched.date() <= day:
                    issues.append("DAILY_FETCH_BEFORE_COMPLETION")
                indicators = report.get("indicators")
                unavailable = report.get("unavailable_indicators")
                if not isinstance(indicators, dict) or not isinstance(unavailable, dict):
                    raise ValueError("INVALID_INDICATOR_OBJECT")
                for key in (*REQUIRED, "ema_200"):
                    actual, expected = indicators.get(key), recomputed["indicators"].get(key)
                    if actual is not None and (not finite(actual) or expected is None or
                            not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-10)):
                        raise ValueError("INDICATOR_DAILY_MISMATCH:" + key)
                    if key in indicators and key in unavailable:
                        raise ValueError("CONTRADICTORY_INDICATOR:" + key)
                if indicators.get("ema_200") is None:
                    warnings.append("EMA200_UNAVAILABLE:" + str(unavailable.get("ema_200", "reason not supplied")))
                # Do not expose technical assessments calculated from future bars
                # or a file unavailable at the requested historical instant.
                if not any(reason in issues for reason in
                           ("DAILY_DATE_NOT_COMPLETED", "FUTURE_DAILY_AVAILABILITY", "DAILY_FETCH_BEFORE_COMPLETION")):
                    bars = validate_candles(daily["candles"])
                    technical = evaluate_technicals(indicators, bars[-1]["close"])
            except (ValueError, KeyError, TypeError) as exc:
                issues.append(str(exc))
        if eligibility["status"] == "REJECTED":
            status = "REJECTED"
        elif issues or eligibility["status"] != "ELIGIBLE" or technical is None:
            status = "INSUFFICIENT_DATA"
        else:
            status = technical["status"]
        decisions.append({"ticker": ticker, "status": status, "eligibility": eligibility,
                          "technical_assessment": technical, "data_issues": issues, "warnings": warnings,
                          "reasons": eligibility["reasons"] + issues + (technical["reasons"] if technical else []),
                          "provenance": provenance, "rank": None,
                          "score": technical["score"] if status == "WATCHLIST" else None,
                          "execution_enabled": False, "execution_blockers": list(EXECUTION_BLOCKERS)})
    decisions.sort(key=lambda d: (0 if d["status"] == "WATCHLIST" else 1,
                                 -(d["score"] or 0), d["ticker"]))
    for rank, item in enumerate((d for d in decisions if d["status"] == "WATCHLIST"), 1):
        item["rank"] = rank
    return {"schema_version": "1.0", "status": "RESEARCH_ONLY", "as_of_utc": now.isoformat(),
            "trading_enabled": False, "score_is_win_probability": False,
            "policy": {"max_snapshot_hours": max_snapshot_hours, "max_bar_age_calendar_days": max_bar_age_days,
                       "bullish_rsi": [52, 68], "bearish_rsi": [32, 48], "max_atr_fraction": 0.08,
                       "ema_200_required": False, "same_day_daily_bars_allowed": False,
                       "score_formula": "50+min(20,abs(EMA20/EMA50-1)*200)+min(20,abs(RSI-50))+max(0,10-ATR/close*100)"},
            "counts": {s: sum(d["status"] == s for d in decisions)
                       for s in ("WATCHLIST", "REJECTED", "INSUFFICIENT_DATA")},
            "limitations": ["Heuristic research ranking, not a validated profit probability.",
                            "Stage 1 eligibility is an upstream assertion, not independently verified.",
                            "No exchange holiday calendar; conservative UTC completion and calendar-age checks.",
                            "EMA200 optional; corporate actions not adjusted; no executable CALL/PUT."],
            "decisions": decisions}


def local_json(raw, folder):
    path = (PROJECT_ROOT / raw).resolve()
    allowed = PROJECT_ROOT / folder
    if not path.is_relative_to(allowed) or path.suffix.lower() != ".json":
        raise ValueError(f"only project {folder} JSON files are allowed")
    if any(part.lower().startswith('.env') or any(word in part.lower() for word in
           ('secret', 'credential', 'private_key')) for part in path.relative_to(PROJECT_ROOT).parts):
        raise ValueError("secret paths are forbidden")
    return path


def run(stage_file, report_files, output_file, as_of):
    stage_path, target = local_json(stage_file, "outputs"), local_json(output_file, "outputs")
    stage = json.loads(stage_path.read_text(encoding="utf-8-sig"))
    bundles = {}
    for raw in report_files:
        path = local_json(raw, "outputs")
        report = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(report, dict) or not isinstance(report.get("symbol"), str):
            raise ValueError("technical report requires a symbol")
        symbol = report["symbol"]
        if symbol in bundles:
            raise ValueError("duplicate technical report symbol: " + symbol)
        bundle = {"report": report, "report_file": path.relative_to(PROJECT_ROOT).as_posix()}
        try:
            source = local_json(report.get("source_file", ""), "data/output/alpha_vantage")
            bundle["daily"] = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError) as exc:
            bundle["error"] = str(exc)
        bundles[symbol] = bundle
    result = evaluate(stage, bundles, as_of)
    result["input_files"] = {"stage1": stage_path.relative_to(PROJECT_ROOT).as_posix(),
                             "technical_reports": list(report_files)}
    result["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
    with target.open("x", encoding="utf-8") as handle:
        handle.write(encoded + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description="Offline research opportunity engine")
    parser.add_argument("--stage1", default="outputs/finviz_stage1.json")
    parser.add_argument("--technical", action="append", help="Repeat for each local technical report")
    parser.add_argument("--output", default="outputs/opportunities_research.json")
    parser.add_argument("--as-of-utc", help="Timezone-aware evaluation time; defaults to current UTC")
    args = parser.parse_args()
    try:
        now = timestamp(args.as_of_utc) if args.as_of_utc else datetime.now(timezone.utc)
        result = run(args.stage1, args.technical or ["outputs/NVDA_technical_indicators.json"], args.output, now)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print("RESEARCH_ONLY:", result["counts"])
    print("Saved:", args.output)


if __name__ == "__main__":
    main()
