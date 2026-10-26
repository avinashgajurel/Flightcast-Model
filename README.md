# FlightCast model trainer

Trains the runway-flow model that the FlightCast airport screen uses to predict aircraft between updates and when
the live feeds go quiet: how arrivals and departures actually fly at Melbourne Airport, for each runway, aircraft size
and departure direction, wind-corrected. It is learned from months of past flights in adsb.lol's public archive.

## One-time setup (about 10 minutes)

1. **Create a repository** on github.com (top-right **+** → **New repository**), for example `flightcast-model`.
   A public repository gets unlimited free Actions minutes; a private one has 2,000 minutes a month, which is
   enough for this.
2. **Upload these files**: open the repository → **Add file** → **Upload files** → drag in everything from this
   folder, *including the hidden `.github` folder*. If the upload skips it, use **Add file** → **Create new file**,
   name it `.github/workflows/train.yml` and paste that file's contents.
3. **Allow the job to save its results**: **Settings** → **Actions** → **General** → *Workflow permissions* →
   **Read and write permissions** → **Save**.
4. **Run it**: **Actions** tab → **Train FlightCast model** → **Run workflow**.
   - *days*: how many past days to learn from (60 is a good start).
   - *until*: leave blank for "up to 2 days ago", or enter a date such as `2025-12-31` to train on an older period.
   The first run downloads and filters every day (~1–2 hours); later runs only fetch new days.
   It then runs by itself on the 2nd of every month.
5. When it finishes, `model/ymml-model.json` (the model) and `model/report.txt` (its accuracy on held-back days)
   appear in the repository.

## Connect it to the screens

In Cloudflare → the **flightcast2** Worker → **Settings** → **Variables and Secrets** → **Add**:

- Public repository: type **Text**, name **`MODEL_URL`**, value
  `https://raw.githubusercontent.com/<your-github-name>/flightcast-model/main/model/ymml-model.json`
- Private repository: `MODEL_URL` =
  `https://api.github.com/repos/<your-github-name>/flightcast-model/contents/model/ymml-model.json`, plus a
  **Secret** named **`MODEL_TOKEN`** holding a GitHub fine-grained token with read-only *Contents* access to that one
  repository.

Deploy. The Worker checks for a newer model every 6 hours; screens load it on start and keep a copy for offline use.
When the screen is predicting, its bottom line says how many past flights the model learned from.

## Run it on your own computer instead

Python 3 and Node 18+:

```
python fetch_filter.py 2025-12-01        # repeat for each day; ~2 GB download each, keeps a few MB
python train.py                          # → model/ymml-model.json
node eval.mjs app/flightcast-airport.html model/ymml-model.json model/holdout.json
```

## Files

| File | What it does |
|---|---|
| `fetch_filter.py` | Streams one archive day and keeps only aircraft that came within 40 km of the airport |
| `train.py` | Finds every landing and take-off, learns the flow tables, holds back the last 20% of days |
| `eval.mjs` | Scores the model on the held-back flights with the screen's own prediction code |
| `pick_days.py` | Chooses which days still need fetching |
| `app/flightcast-airport.html` | Copy of the airport screen, used by `eval.mjs` (replace it when the screen is updated) |

## Data licences

Flight data: adsb.lol (ODbL 1.0). The model is a database derived from it, so if you share or sell it, it may need to
be offered under the same licence: check before using it commercially. Weather: Open-Meteo (CC BY 4.0). Runways:
OpenStreetMap (ODbL).
