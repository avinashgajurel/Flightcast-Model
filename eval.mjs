// Score the model on held-back flights using the airport screen's own prediction code.
//   node eval.mjs <flightcast-airport.html> <model.json> <holdout.json>
// For every update a screen would get (every 10 s), it predicts 10/20/30 s ahead three ways and compares with
// where the aircraft really was: the old straight line, the screen's built-in defaults, and the trained model.
import fs from 'fs';
const [htmlPath, modelPath, holdPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, 'utf8'), model = JSON.parse(fs.readFileSync(modelPath, 'utf8')), hold = JSON.parse(fs.readFileSync(holdPath, 'utf8'));
const cut = (a, b) => { const i = html.indexOf(a), j = html.indexOf(b, i); if (i < 0 || j < 0) throw new Error('page changed: ' + a); return html.slice(i, j); };
const code = [cut('function distKm(', '\nconst PTS'), cut('function rwyEnds(r){', 'let ACTIVE'), cut('\n/* ================= LEARNED RUNWAY FLOW', 'function flowStats')].join('\n');
let NOW = 0; Date.now = () => NOW;
function makeScreen(withModel){
  const store = {}; globalThis.localStorage = { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } }; globalThis.setInterval = () => 0;
  const A = model.airport;
  return new Function('MODEL_IN', `const DEG=Math.PI/180, rad=d=>d*DEG, deg=r=>r/DEG, LAT=${A.lat}, LON=${A.lon}, FIELD_FT=${A.elev_ft}, q=new URLSearchParams(''), SERVED=false, WX={wind:0,windDir:0};
    const RWY=new Map(), PREV=new Map(), TOUCH=new Map(), routes=new Map(), legs={}, AIRPORTS=[]; let DATA_AT=0;
    const aglFtOf = f => f.alt_baro === 'ground' ? 0 : ((typeof f.alt_baro === 'number' ? f.alt_baro : f.alt_geom ?? 99999) - FIELD_FT);
    ${code}
    MODEL = MODEL_IN;
    if (!MODEL) for (const e of MODEL_IN0.ends) RWY.set(e.num, null);
    return { learnFlow, planFor, lpState, routes, PREV, TOUCH, WX, FL_ASSIGN, bearing };`.replace('if (!MODEL) for (const e of MODEL_IN0.ends) RWY.set(e.num, null);', ''))(withModel ? model : { ...model, flow: {} });
}
// the "defaults" screen gets the same runway geometry but no learned tables (it still learns as it goes, like a new screen)
const interp = (pts, t) => { let i = pts.findIndex(p => p[0] >= t); if (i < 0) return null; if (i === 0) return pts[0];
  const a = pts[i - 1], b = pts[i], f = (t - a[0]) / (b[0] - a[0] || 1); return [t, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, (a[3] === 'g' || b[3] === 'g') ? (f < .5 ? a[3] : b[3]) : a[3] + (b[3] - a[3]) * f]; };
const R = { straight: {}, defaults: {}, model: {} };
const add = (m, kind, h, pos, alt) => { const k = kind + h, r = R[m][k] || (R[m][k] = { pos: [], alt: [] }); r.pos.push(pos); r.alt.push(alt); };
for (const mode of ['defaults', 'model']){
  const S = makeScreen(mode === 'model');
  for (const fl of hold){
    const pts = fl.pts, cs = 'TST' + fl.hex.slice(-3);
    if (fl.sec) S.routes.set(cs, { destination: { latitude: model.airport.lat + 6 * Math.cos(fl.sec * Math.PI / 8), longitude: model.airport.lon + 6 * Math.sin(fl.sec * Math.PI / 8) / Math.cos(model.airport.lat * Math.PI / 180) } });
    let prev = null;
    for (let t = pts[0][0]; t <= pts[pts.length - 1][0]; t += 10){
      const p = interp(pts, t); if (!p) break; const src = pts.find(q => q[0] >= t) || pts[pts.length - 1];
      NOW = t * 1000;
      const ground = p[3] === 'g', a = { hex: fl.hex, flight: cs, t: fl.type, lat: p[1], lon: p[2], alt_baro: ground ? 'ground' : p[3], gs: src[4], track: src[5], baro_rate: src[6], _t: NOW };
      const alt = ground ? 99999 : p[3], vr = src[6] || 0;
      let ph = ground ? (a.gs >= 40 ? 'takeoff' : 'lineup') : alt < 5000 && vr < -250 ? 'landing' : alt < 5000 && vr > 250 ? 'takeoff' : 'air';
      if (ground && (prev === 'landing' || prev === 'landed')) ph = 'landed';
      S.PREV.set(fl.hex, ph); prev = ph;
      S.learnFlow([a]); const lp = S.planFor(a);
      for (const h of [10, 20, 30]){
        const T = interp(pts, t + h); if (!T) continue;
        const err = (lat, lon) => Math.hypot((lat - T[1]) * 110574, (lon - T[2]) * 111320 * Math.cos(T[1] * Math.PI / 180));
        const trueAgl = T[3] === 'g' ? 0 : T[3] - model.airport.elev_ft;
        const v = a.gs * .514444, tr = a.track * Math.PI / 180;
        const sl = [a.lat + v * Math.cos(tr) * h / 111320, a.lon + v * Math.sin(tr) * h / (111320 * Math.cos(a.lat * Math.PI / 180))];
        const slAlt = ground ? 0 : Math.max(0, alt - model.airport.elev_ft + vr * h / 60);
        if (mode === 'defaults') add('straight', fl.kind, h, err(...sl), Math.abs(slAlt - trueAgl));
        if (lp){ const s = S.lpState(lp, NOW + h * 1000); add(mode, fl.kind, h, err(s.lat, s.lon), Math.abs(s.agl - trueAgl)); }
        else add(mode, fl.kind, h, err(...sl), Math.abs(slAlt - trueAgl));
      }
    }
    S.FL_ASSIGN.delete(fl.hex);
  }
}
const avg = a => a.length ? Math.round(a.reduce((s, x) => s + x, 0) / a.length) : '-';
const p90 = a => { const b = [...a].sort((x, y) => x - y); return b.length ? Math.round(b[Math.floor(b.length * .9)]) : '-'; };
const lines = [`Held-back flights: ${hold.filter(f => f.kind === 'arr').length} arrivals, ${hold.filter(f => f.kind === 'dep').length} departures (model trained on ${model.trained.arrivals} + ${model.trained.departures}, ${model.trained.from} → ${model.trained.to})`,
  'how far off the prediction was (average · 9 in 10 within):'];
for (const kind of ['arr', 'dep']) for (const h of [10, 20, 30]){ const k = kind + h, s = R.straight[k], d = R.defaults[k], m = R.model[k]; if (!s) continue;
  lines.push(`${kind === 'arr' ? 'arrivals  ' : 'departures'} +${h}s   straight line ${avg(s.pos)} m · ${p90(s.pos)} m   defaults ${avg(d.pos)} m · ${p90(d.pos)} m   model ${avg(m.pos)} m · ${p90(m.pos)} m   | height: ${avg(s.alt)} / ${avg(d.alt)} / ${avg(m.alt)} ft`); }
console.log(lines.join('\n'));
if (process.env.REPORT) fs.writeFileSync(process.env.REPORT, lines.join('\n') + '\n');
