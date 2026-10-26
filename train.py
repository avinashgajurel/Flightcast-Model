#!/usr/bin/env python3
"""
Train the FlightCast runway-flow model from filtered archive days (see fetch_filter.py).

  python train.py                         # all days in data/, last 20% held back for testing
  python train.py --runways rw.json       # runway geometry from a file instead of OpenStreetMap
  python train.py --no-weather            # skip the wind correction (no internet)

What it learns, for every runway end at the airport, in 250 m steps along the flight:
  arrivals   path, height and airspeed over the last 16 km of final, the touchdown, and the roll-out
  departures the take-off roll, lift-off, climb and the turn out — separately for each departure direction
each also split by aircraft size (L light/regional · M narrow-body · H wide-body), plus which runway is used
in which wind. Speeds are airspeed (ground speed + headwind from hourly historic weather), so wind doesn't skew them.
The learning rules are the same ones the screen uses on the device; the screen reads this file directly.

Output: model/ymml-model.json (the model) and model/holdout.json (unseen flights, for eval.mjs).
"""
import argparse, datetime as dt, glob, gzip, json, math, os, sys, urllib.parse, urllib.request
from collections import defaultdict

AP = {'icao': 'YMML', 'lat': -37.6690, 'lon': 144.8410, 'elev_ft': 434}
BIN = 250

# ---------- shared rules (mirror flightcast-airport.html) ----------
HEAVY = ('A33', 'A34', 'A35', 'A38', 'A30', 'A31', 'B74', 'B76', 'B77', 'B78', 'MD1', 'DC1', 'IL7', 'C17', 'C5', 'A40', 'KC1', 'K35')
LIGHT = ('DH8', 'AT4', 'AT7', 'SF3', 'B19', 'JS3', 'E12', 'C2', 'C1', 'PA', 'BE', 'SR2', 'PC1', 'TBM', 'C5', 'C6', 'CL3', 'CL6', 'GLF', 'GL', 'LJ', 'FA', 'E5', 'H25', 'C25', 'C68', 'C70', 'C75', 'SW4', 'F2T', 'AS', 'EC', 'R4', 'B06', 'S76')


def size_class(t, cat):
    """L light/regional · M narrow-body · H wide-body. Type designator first, emitter category as fallback."""
    t = (t or '').upper()
    if t:
        if t.startswith(HEAVY) and not t.startswith(('C5', 'C25', 'C56', 'C68')): return 'H'
        if t.startswith(LIGHT): return 'L'
        return 'M'
    return {'A5': 'H', 'A4': 'M', 'A3': 'M', 'A2': 'L', 'A1': 'L', 'A7': 'L'}.get(cat or '', 'M')


def km(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def bearing(a, b, c, d):
    r = math.radians
    y = math.sin(r(d - b)) * math.cos(r(c)); x = math.cos(r(a)) * math.sin(r(c)) - math.sin(r(a)) * math.cos(r(c)) * math.cos(r(d - b))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def ang(x, y): return abs(((x - y) % 360 + 540) % 360 - 180)


def seg_km(lat, lon, A, B):
    k = 111.32 * math.cos(math.radians(lat)); ax, ay = (A[0] - lon) * k, (A[1] - lat) * 110.574; bx, by = (B[0] - lon) * k, (B[1] - lat) * 110.574
    dx, dy = bx - ax, by - ay; t = max(0, min(1, -(ax * dx + ay * dy) / ((dx * dx + dy * dy) or 1)))
    return math.hypot(ax + t * dx, ay + t * dy)


def runway_ends(rw):
    """Same as rwyEnds() in the page: which number sits at which end; the threshold is where landings start."""
    parts = [p.strip() for p in rw['ref'].split('/')]
    if len(parts) != 2: return []
    hab = bearing(rw['a'][1], rw['a'][0], rw['b'][1], rw['b'][0]); hba = (hab + 180) % 360
    num = lambda s: int(''.join(ch for ch in s if ch.isdigit()) or 0) * 10
    a_first = ang(num(parts[0]), hab) + ang(num(parts[1]), hba) <= ang(num(parts[1]), hab) + ang(num(parts[0]), hba)
    at_a, at_b = parts if a_first else (parts[1], parts[0])
    ln = km(rw['a'][1], rw['a'][0], rw['b'][1], rw['b'][0]) * 1000
    return [{'num': at_a, 'thr': rw['a'], 'hdg': hab, 'len': ln}, {'num': at_b, 'thr': rw['b'], 'hdg': hba, 'len': ln}]


def to_frame(E, lat, lon):
    dx = (lon - E['thr'][0]) * 111320 * math.cos(math.radians(E['thr'][1])); dy = (lat - E['thr'][1]) * 110574; h = math.radians(E['hdg'])
    return dx * math.sin(h) + dy * math.cos(h), dx * math.cos(h) - dy * math.sin(h)


# ---------- inputs ----------
OVERPASS = ['https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter',
            'https://overpass.private.coffee/api/interpreter', 'https://maps.mail.ru/osm/tools/overpass/api/interpreter']


def osm_runways():
    """Runway centrelines from OpenStreetMap (the same source as the screen's map). Tries several mirrors, twice."""
    import time
    q = f'[out:json];way[aeroway=runway]({AP["lat"]-.06},{AP["lon"]-.07},{AP["lat"]+.06},{AP["lon"]+.07});out geom;'
    last = None
    for attempt in range(2):
        for url in OVERPASS:
            try:
                req = urllib.request.Request(url, data=urllib.parse.urlencode({'data': q}).encode(), headers={'User-Agent': 'FlightCast-trainer'})
                j = json.load(urllib.request.urlopen(req, timeout=60))
                out = []
                for w in j.get('elements', []):
                    g, ref = w.get('geometry') or [], (w.get('tags') or {}).get('ref', '')
                    if len(g) >= 2 and '/' in ref: out.append({'ref': ref, 'a': [g[0]['lon'], g[0]['lat']], 'b': [g[-1]['lon'], g[-1]['lat']]})
                if out: print(f'runways from OpenStreetMap ({url.split("/")[2]})'); return out
            except Exception as e:
                last = e; print(f'  {url.split("/")[2]}: {e}')
        time.sleep(20)
    raise RuntimeError(f'all OpenStreetMap mirrors failed ({last})')


def runways_from_data(files):
    """Fallback: find the runways from the flights themselves — every take-off roll and landing roll-out is a fast
    straight line on the ground. Group them by direction, fit a centreline to each group, and take its extent."""
    groups = defaultdict(list)
    for f in files:
        with gzip.open(f, 'rt') as fh:
            for line in fh:
                for p in json.loads(line)['pts']:
                    if p[3] == 'g' and p[4] and 70 <= p[4] <= 170 and p[5] is not None and km(p[1], p[2], AP['lat'], AP['lon']) < 6:
                        groups[round((p[5] % 180) / 5) * 5 % 180].append((p[1], p[2]))
    # merge neighbouring 5° buckets into runway directions
    dirs = []
    for h in sorted(groups, key=lambda k: -len(groups[k])):
        if len(groups[h]) < 40: break
        d = next((x for x in dirs if ang(x['h'] * 2, h * 2) / 2 <= 7), None)
        if d: d['pts'] += groups[h]
        else: dirs.append({'h': h, 'pts': list(groups[h])})
    out = []
    for d in dirs:
        pts = d['pts']; lat0 = sum(p[0] for p in pts) / len(pts); lon0 = sum(p[1] for p in pts) / len(pts)
        kx = 111320 * math.cos(math.radians(lat0))
        xy = [((p[1] - lon0) * kx, (p[0] - lat0) * 110574) for p in pts]
        sxx = sum(x * x for x, _ in xy); syy = sum(y * y for _, y in xy); sxy = sum(x * y for x, y in xy)
        th = 0.5 * math.atan2(2 * sxy, sxx - syy); ux, uy = math.cos(th), math.sin(th)          # main axis of the points
        perp = sorted(-x * uy + y * ux for x, y in xy); mid = perp[len(perp) // 2]
        keep = [(x, y) for x, y in xy if abs(-x * uy + y * ux - mid) < 40]                     # on that line (not a parallel taxiway)
        if len(keep) < 30: continue
        s = sorted(x * ux + y * uy for x, y in keep); s0, s1 = s[int(len(s) * .01)] - 150, s[int(len(s) * .99)] + 150
        cx, cy = -mid * uy, mid * ux
        A = [lon0 + (cx + s0 * ux) / kx, lat0 + (cy + s0 * uy) / 110574]; B = [lon0 + (cx + s1 * ux) / kx, lat0 + (cy + s1 * uy) / 110574]
        hab = bearing(A[1], A[0], B[1], B[0]); n1 = round(hab / 10) % 36 or 36; n2 = round(((hab + 180) % 360) / 10) % 36 or 36
        out.append({'ref': f'{min(n1, n2):02d}/{max(n1, n2):02d}', 'a': A, 'b': B})
        print(f'  runway {out[-1]["ref"]} found from {len(keep)} take-off/landing roll positions, {km(A[1], A[0], B[1], B[0]):.1f} km long')
    return out


def hourly_wind(day_from, day_to):
    u = ('https://archive-api.open-meteo.com/v1/archive?' + urllib.parse.urlencode({'latitude': AP['lat'], 'longitude': AP['lon'], 'start_date': day_from,
         'end_date': day_to, 'hourly': 'wind_speed_10m,wind_direction_10m', 'timezone': 'UTC'}))
    j = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'FlightCast-trainer'}), timeout=90))
    h = j['hourly']; w = {}
    for t, s, d in zip(h['time'], h['wind_speed_10m'], h['wind_direction_10m']):
        if s is not None and d is not None: w[int(dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp() // 3600)] = (s, d)
    return w


# ---------- learning ----------
class Acc:
    """Running sums per bin: mean x, y, height, airspeed."""
    def __init__(self): self.b = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])
    def add(self, i, v):
        b = self.b[i]; b[0] += 1
        for k in range(4): b[k + 1] += v[k]
    def export(self, min_n):
        return {str(i): [b[0], round(b[1] / b[0], 1), round(b[2] / b[0], 1), round(b[3] / b[0], 1), round(b[4] / b[0], 1)] for i, b in sorted(self.b.items()) if b[0] >= min_n}


def phase_of(alt, gs, vr, ground):
    if ground: return 'takeoff' if gs >= 40 else 'lineup'
    if alt < 5000 and vr < -250: return 'landing'
    if alt < 5000 and vr > 250: return 'takeoff'
    return 'air'


def flights_of(track, ends, wind):
    """Walk one aircraft's day: yield (kind, end, sector, size, [points in runway frame]) for each landing / departure seen."""
    pts = track['pts']; cls_cat = None
    for p in pts:
        if len(p) > 7 and isinstance(p[7], dict) and p[7].get('category'): cls_cat = p[7]['category']
    cls = size_class(track.get('t'), cls_cat)
    prev_ph, A, seq, touched = None, None, [], False
    out = []
    def close():
        nonlocal A, seq
        if A and len(seq) >= 3: out.append((A, seq))
        A, seq = None, []
    for idx, p in enumerate(pts):
        t, lat, lon, alt, gs, trk, vr = p[:7]
        if gs is None or trk is None or lat is None: continue
        ground = alt == 'g'; altv = 0 if ground else (alt if isinstance(alt, (int, float)) else None)
        if altv is None: continue
        if km(lat, lon, AP['lat'], AP['lon']) > 40: 
            if A and A['kind'] == 'dep': close()
            continue
        vr = vr or 0; agl = 0 if ground else max(0, altv - AP['elev_ft'])
        ph = phase_of(altv, gs, vr, ground)
        if ground and prev_ph in ('landing', 'landed'): ph = 'landed'
        if ph == 'landed': touched = True
        if not ground: touched = touched and agl < 1500
        prev_ph = ph
        wkey = int(t // 3600); ws, wd = wind.get(wkey, (0, 0))
        if A:
            E = A['E']; x, y = to_frame(E, lat, lon)
            if A['kind'] == 'arr':
                if (ground and gs < 25) or x > E['len'] + 300 or abs(y) > 2000: close(); prev_ph = ph; continue
                pp = x
            else:
                L = seq[-1]
                pp = max(x, L['p']) if ground else L['p'] + max(0, math.hypot(x - L['x'], y - L['y']))
                if pp > 22000 or (not ground and agl > 9000): close(); continue
            hw = ws * .54 * math.cos(math.radians(wd - E['hdg']))
            seq.append({'t': t, 'p': pp, 'x': x, 'y': y, 'agl': agl, 'as': gs + hw, 'lat': lat, 'lon': lon, 'alt': 'g' if ground else altv, 'gs': gs, 'trk': trk, 'vr': vr, 'i': idx})
            continue
        landed = ground and (ph == 'landed' or touched)
        for E in ends:
            if ang(trk, E['hdg']) > 22: continue
            x, y = to_frame(E, lat, lon)
            arr = (not ground and agl < 5000 and vr < 300 and -16000 < x < E['len'] and abs(y) < max(120, -x * .09) and (x < 0 or agl < 400)) or \
                  (landed and gs >= 25 and -100 < x < E['len'] + 200 and abs(y) < 60)
            dep = ground and not landed and -150 < x < E['len'] * .7 and abs(y) < 45
            if arr or dep:
                A = {'kind': 'arr' if arr else 'dep', 'E': E}
                hw = ws * .54 * math.cos(math.radians(wd - E['hdg']))
                seq = [{'t': t, 'p': x, 'x': x, 'y': y, 'agl': agl, 'as': gs + hw, 'lat': lat, 'lon': lon, 'alt': 'g' if ground else altv, 'gs': gs, 'trk': trk, 'vr': vr, 'i': idx}]
                break
    close()
    res = []
    for A, seq in out:
        if A['kind'] == 'dep':
            if max(s['agl'] for s in seq) < 300: continue                    # never got airborne: a rejected take-off or a taxi along the runway
            # departure direction: where it went — the last position later in the day (its destination, or as far as we saw it)
            last = pts[-1]; i0 = seq[-1]['i']
            for q in pts[i0:]:
                if q[3] == 'g' and km(q[1], q[2], AP['lat'], AP['lon']) > 60: last = q; break
            far = km(last[1], last[2], AP['lat'], AP['lon']) > 40
            sec = str(round(bearing(AP['lat'], AP['lon'], last[1], last[2]) / 22.5) % 16) if far else '*'   # 16 directions, as the screen uses
        else:
            if not any(s['agl'] == 0 for s in seq): continue                  # went around / not seen landing
            sec = ''
        res.append((A['kind'], A['E'], sec, cls, seq))
    return res


def fill(acc, seq):
    """Every 250 m bin between consecutive fixes gets the interpolated values (one flight teaches its whole path)."""
    for L, C in zip(seq, seq[1:]):
        if not (C['p'] > L['p'] and C['p'] - L['p'] < 4000 and C['t'] > L['t']): continue
        for i in range(math.ceil(L['p'] / BIN), int(math.floor(C['p'] / BIN)) + 1):
            f = (i * BIN - L['p']) / (C['p'] - L['p'])
            acc.add(i, [L[k] + (C[k] - L[k]) * f for k in ('x', 'y', 'agl', 'as')])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='data'); ap.add_argument('--out', default='model')
    ap.add_argument('--runways'); ap.add_argument('--no-weather', action='store_true'); ap.add_argument('--wind-file')
    ap.add_argument('--holdout', type=float, default=.2); ap.add_argument('--min-n', type=int, default=3)
    ap.add_argument('--last', type=int, default=0, help='use only the most recent N days')
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(a.data, '*.jsonl.gz')))
    if a.last: files = files[-a.last:]
    if not files: sys.exit('no data: run fetch_filter.py first')
    days = [os.path.basename(f)[:10] for f in files]
    n_hold = max(1, round(len(files) * a.holdout)) if len(files) > 1 else 0
    train_f, hold_f = files[:len(files) - n_hold], files[len(files) - n_hold:]
    if a.runways: rws = json.load(open(a.runways))
    else:
        cache = os.path.join(a.out, 'runways.json'); os.makedirs(a.out, exist_ok=True)
        try:
            rws = osm_runways(); json.dump(rws, open(cache, 'w'))
        except Exception as e:
            if os.path.exists(cache): print('OpenStreetMap unavailable, using the saved runway layout'); rws = json.load(open(cache))
            else:
                print('OpenStreetMap unavailable, finding the runways from the flights instead')
                rws = runways_from_data(files)
                if not rws: sys.exit('could not determine the runways')
    ends = [e for r in rws for e in runway_ends(r)]
    print(f'{len(ends)} runway ends: {", ".join(e["num"] for e in ends)} · {len(train_f)} training day(s), {len(hold_f)} held back')
    if a.wind_file: wind = {int(k): tuple(v) for k, v in json.load(open(a.wind_file)).items()}
    elif a.no_weather: wind = {}
    else:
        try: wind = hourly_wind(days[0], days[-1]); print(f'weather: {len(wind)} hourly winds')
        except Exception as e: print('weather unavailable, training without wind correction:', e); wind = {}

    accs = defaultdict(Acc); use = defaultdict(lambda: defaultdict(int)); counts = defaultdict(int)
    def learn_day(path, hold):
        flights = []
        with gzip.open(path, 'rt') as fh:
            for line in fh:
                tr = json.loads(line)
                for kind, E, sec, cls, seq in flights_of(tr, ends, wind):
                    if hold: flights.append({'hex': tr['hex'], 'kind': kind, 'num': E['num'], 'sec': sec, 'cls': cls, 'type': tr.get('t'),
                                             'pts': [[s['t'], s['lat'], s['lon'], s['alt'], s['gs'], s['trk'], s['vr']] for s in seq]}); continue
                    counts[kind] += 1
                    keys = ['arr', 'arr|' + cls] if kind == 'arr' else ['dep*', 'dep*|' + cls] + (['dep' + sec, 'dep' + sec + '|' + cls] if sec != '*' else [])
                    for k in keys: fill(accs[(E['num'], k)], seq)
                    ws, wd = wind.get(int(seq[0]['t'] // 3600), (None, None))
                    if ws is not None: use[f'{kind}|{int(wd // 30) % 12}|{0 if ws < 15 else 1 if ws < 30 else 2}'][E['num']] += 1
        return flights
    for f in train_f: learn_day(f, False); print(f'  learned {os.path.basename(f)[:10]}: {counts["arr"]} arrivals, {counts["dep"]} departures so far', flush=True)
    holdout = [fl for f in hold_f for fl in learn_day(f, True)]

    flow = defaultdict(dict)
    for (num, k), acc in accs.items():
        b = acc.export(a.min_n)
        if b: flow[num][k] = b
    model = {'version': 1, 'airport': AP, 'bin': BIN, 'trained': {'from': days[0], 'to': days[len(train_f) - 1] if train_f else None, 'days': len(train_f),
             'arrivals': counts['arr'], 'departures': counts['dep'], 'built': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ'),
             'source': 'adsb.lol globe_history (ODbL 1.0), Open-Meteo, OpenStreetMap'},
             'ends': [{'num': e['num'], 'thr': [round(e['thr'][0], 7), round(e['thr'][1], 7)], 'hdg': round(e['hdg'], 3), 'len': round(e['len'])} for e in ends],
             'flow': flow, 'use': {k: dict(v) for k, v in use.items()}}
    os.makedirs(a.out, exist_ok=True)
    mp = os.path.join(a.out, AP['icao'].lower() + '-model.json')
    with open(mp, 'w') as fh: json.dump(model, fh, separators=(',', ':'))
    with open(os.path.join(a.out, 'holdout.json'), 'w') as fh: json.dump(holdout[:600], fh, separators=(',', ':'))
    print(f'model: {counts["arr"]} arrivals + {counts["dep"]} departures learned → {mp} ({os.path.getsize(mp) / 1e3:.0f} KB); {len(holdout)} held-back flights for testing')


if __name__ == '__main__':
    main()
