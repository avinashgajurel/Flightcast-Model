#!/usr/bin/env python3
"""
Download one day of adsb.lol's public flight archive and keep only the aircraft that came near the airport.

  python fetch_filter.py 2025-11-03                 # writes data/2025-11-03.jsonl.gz (a few MB)
  python fetch_filter.py 2025-11-03 --local DIR     # use already-downloaded release files in DIR (testing)

The archive (github.com/adsblol/globe_history_<year>, ODbL licence) is ~1.5–3 GB per day for the whole world. It is
streamed: nothing but the Melbourne-area tracks is ever written to disk. Inside the area every position is kept;
outside it one position per 2 minutes, so the trainer can still tell where a departure went.
"""
import argparse, gzip, io, json, math, os, re, sys, tarfile, urllib.request

AP = {'lat': -37.6690, 'lon': 144.8410}          # Melbourne Airport (YMML)
NEAR_KM = 40                                     # "in the area"
BOX = (AP['lat'] - .40, AP['lat'] + .40, AP['lon'] - .50, AP['lon'] + .50)


def km(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


class Concat(io.RawIOBase):
    """Reads several streams (the split .tar.aa, .tar.ab … parts) one after another as a single stream."""
    def __init__(self, openers):
        self.openers, self.cur = list(openers), None
    def readable(self): return True
    def readinto(self, b):
        while True:
            if self.cur is None:
                if not self.openers: return 0
                self.cur = self.openers.pop(0)()
            n = self.cur.readinto(b) if hasattr(self.cur, 'readinto') else None
            if n is None:
                data = self.cur.read(len(b)); n = len(data); b[:n] = data
            if n: return n
            self.cur.close(); self.cur = None


def release_parts(day, local):
    """The day's archive parts, in order, as stream openers."""
    tag = f'v{day.replace("-", ".")}-planes-readsb-prod-0'
    if local:
        names = sorted(f for f in os.listdir(local) if f.startswith(tag) and '.tar' in f)
        if not names: sys.exit(f'no files starting with {tag} in {local}')
        return [lambda p=os.path.join(local, n): open(p, 'rb') for n in names]
    hdr = {'User-Agent': 'FlightCast-trainer', 'Accept': 'application/vnd.github+json'}
    if os.environ.get('GITHUB_TOKEN'): hdr['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    url = f'https://api.github.com/repos/adsblol/globe_history_{day[:4]}/releases/tags/{tag}'
    try:
        rel = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60))
    except Exception as e:
        sys.exit(f'{day}: no archive release found ({e})')
    assets = sorted((a for a in rel['assets'] if '.tar' in a['name'] and not a['name'].endswith(('.sha256', '.md5', '.txt'))), key=lambda a: a['name'])
    print(f'{day}: {len(assets)} part(s), {sum(a["size"] for a in assets) / 1e9:.2f} GB', flush=True)
    dl = {'User-Agent': 'FlightCast-trainer'}
    return [lambda u=a['browser_download_url']: urllib.request.urlopen(urllib.request.Request(u, headers=dl), timeout=300) for a in assets]


def keep_track(doc):
    """Positions near the airport in full, and every 2 minutes elsewhere; None if it never came near."""
    base, near, pts, last_far = doc.get('timestamp', 0), False, [], -1e9
    for p in doc.get('trace') or []:
        if not isinstance(p, list) or len(p) < 6 or p[1] is None: continue
        t, lat, lon = base + p[0], p[1], p[2]
        inside = BOX[0] < lat < BOX[1] and BOX[2] < lon < BOX[3] and km(lat, lon, AP['lat'], AP['lon']) <= NEAR_KM
        if not inside and t - last_far < 120: continue
        if not inside: last_far = t
        near |= inside
        det = p[8] if len(p) > 8 and isinstance(p[8], dict) else {}
        alt = 'g' if p[3] == 'ground' else p[3]
        row = [round(t, 1), round(lat, 6), round(lon, 6), alt, p[4], p[5], p[7] if len(p) > 7 else None]
        if det.get('flight') or det.get('category'): row.append({k: det[k] for k in ('flight', 'category') if det.get(k)})
        pts.append(row)
    if not near: return None
    return {'hex': doc.get('icao'), 't': doc.get('t'), 'r': doc.get('r'), 'pts': pts}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('day'); ap.add_argument('--local'); ap.add_argument('--out', default='data')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    out_path = os.path.join(a.out, a.day + '.jsonl.gz')
    seen = kept = 0
    hint = re.compile(rb'-3[78]\.\d')                  # cheap pre-check: latitude near Melbourne appears somewhere in the file
    with tarfile.open(fileobj=io.BufferedReader(Concat(release_parts(a.day, a.local)), 1 << 20), mode='r|*') as tar, \
         gzip.open(out_path + '.part', 'wt') as out:
        for m in tar:
            if not m.isfile() or '/traces/' not in '/' + m.name or not m.name.endswith('.json'): continue
            raw = tar.extractfile(m).read(); seen += 1
            if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
            if not hint.search(raw): continue
            try: tr = keep_track(json.loads(raw))
            except ValueError: continue
            if tr: out.write(json.dumps(tr, separators=(',', ':')) + '\n'); kept += 1
            if seen % 20000 == 0: print(f'  {seen} aircraft scanned, {kept} came near', flush=True)
    os.replace(out_path + '.part', out_path)
    print(f'{a.day}: {seen} aircraft in the archive, {kept} came within {NEAR_KM} km → {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB)')


if __name__ == '__main__':
    main()
