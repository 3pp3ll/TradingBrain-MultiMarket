"""Stage 1: read-only US stock universe filter; no broker access or network requests.

Input: normalized CSV exported from an authorized data source. Optionable and
market-cap/average-volume filters apply; relative volume is deliberately NOT a
Stage 1 gate. Results are watchlist candidates, NEVER trade signals.
"""
from __future__ import annotations
import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path


def parse_number(raw):
    s = str(raw or '').strip().replace(',', '').replace('$', '')
    if not s or s.lower() in {'na', 'n/a', '-'}:
        return None
    multipliers = {'K': 1e3, 'M': 1e6, 'B': 1e9, 'T': 1e12}
    factor = multipliers.get(s[-1].upper(), 1)
    if factor != 1:
        s = s[:-1]
    try:
        return float(s) * factor
    except ValueError:
        return None


def run(source: Path, target: Path):
    with source.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        required = {'ticker', 'market_cap', 'average_volume', 'optionable'}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError('CSV must include ticker, market_cap, average_volume, optionable')
        decisions = []
        for row in reader:
            ticker = (row.get('ticker') or '').strip().upper()
            reasons = []
            cap = parse_number(row.get('market_cap'))
            volume = parse_number(row.get('average_volume'))
            if not ticker:
                reasons.append('MISSING_TICKER')
            if cap is None:
                reasons.append('MISSING_MARKET_CAP')
            elif cap < 10_000_000_000:
                reasons.append('MARKET_CAP_BELOW_10B')
            if volume is None:
                reasons.append('MISSING_AVERAGE_VOLUME')
            elif volume <= 1_000_000:
                reasons.append('AVERAGE_VOLUME_NOT_OVER_1M')
            if (row.get('optionable') or '').strip().lower() not in {'yes', 'true', '1'}:
                reasons.append('OPTIONABLE_NOT_CONFIRMED')
            decisions.append({'ticker': ticker, 'status': 'WATCHLIST' if not reasons else 'REJECTED', 'reasons': reasons})
    result = {
        'schema_version': '1.0',
        'stage': 'FINVIZ_STAGE_1',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'source': 'USER_SUPPLIED_CSV',
        'data_is_live': False,
        'trading_enabled': False,
        'criteria': {'market_cap_min_usd': 10_000_000_000, 'average_volume_over': 1_000_000, 'optionable': True, 'relative_volume': 'NOT_APPLIED'},
        'input_count': len(decisions),
        'watchlist_count': sum(x['status'] == 'WATCHLIST' for x in decisions),
        'decisions': decisions,
        'notes': ['Input is not live market data. No equity or option prices are validated.', 'NO TRADE EXECUTION. Stage 2 must validate momentum, timestamp, options contract, and risk.'],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description='Options Intelligence: Finviz Stage 1 (read only)')
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, default=Path('outputs/finviz_stage1.json'))
    a = p.parse_args()
    result = run(a.input, a.output)
    print(f"Stage 1: {result['watchlist_count']}/{result['input_count']} WATCHLIST; Relative Volume NOT applied")
    print('Saved:', a.output)
    print('RESEARCH ONLY. NO ORDER EXECUTION.')


if __name__ == '__main__':
    main()
