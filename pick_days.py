#!/usr/bin/env python3
"""Which archive days to fetch: the last N days (the archive runs ~2 days behind) that aren't already saved.
Prints a GitHub Actions output line: list=["2025-11-01", ...]"""
import datetime as dt, json, os, subprocess, sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
until = os.environ.get('UNTIL', '').strip()
end = dt.date.fromisoformat(until) if until else dt.date.today() - dt.timedelta(days=2)
want = [(end - dt.timedelta(days=i)).isoformat() for i in range(n)]
try:
    have = json.loads(subprocess.run(['gh', 'release', 'view', 'data-cache', '--json', 'assets'], capture_output=True, text=True, check=True).stdout)['assets']
    have = {a['name'][:10] for a in have}
except Exception:
    have = set()
todo = [d for d in want if d not in have][:200]
print('list=' + json.dumps(todo))
print(f'{len(want)} days wanted, {len(have & set(want))} already saved, {len(todo)} to fetch', file=sys.stderr)
