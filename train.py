# Trains the FlightCast runway-flow model from adsb.lol's public flight archive.
# Runs on the 2nd of every month, or any time from the Actions tab ("Run workflow").
name: Train FlightCast model
on:
  schedule: [{ cron: '0 3 2 * *' }]
  workflow_dispatch:
    inputs:
      days: { description: 'How many past days to train on', default: '60' }
      until: { description: 'Last day to use, e.g. 2025-12-31 (blank = 2 days ago)', default: '' }
permissions: { contents: write }
env:
  DAYS: '${{ inputs.days || 60 }}'
  UNTIL: '${{ inputs.until }}'
  SCREEN_URL: 'https://flightcast2.avinashgajurel.workers.dev/airport'
jobs:
  pick:
    runs-on: ubuntu-latest
    outputs: { list: '${{ steps.p.outputs.list }}' }
    steps:
      - uses: actions/checkout@v5
      - run: |
          gh release view data-cache >/dev/null 2>&1 || gh release create data-cache --title "Filtered archive days (Melbourne)" --notes "Inputs for training. Data from adsb.lol, ODbL 1.0."
        env: { GH_TOKEN: '${{ github.token }}' }
      - id: p
        run: python3 pick_days.py "$DAYS" >> "$GITHUB_OUTPUT"
        env: { GH_TOKEN: '${{ github.token }}' }
  fetch:
    needs: pick
    if: needs.pick.outputs.list != '[]'
    runs-on: ubuntu-latest
    strategy: { matrix: { day: '${{ fromJson(needs.pick.outputs.list) }}' }, max-parallel: 8, fail-fast: false }
    steps:
      - uses: actions/checkout@v5
      - run: python3 fetch_filter.py ${{ matrix.day }}
        env: { GITHUB_TOKEN: '${{ github.token }}' }
      - run: gh release upload data-cache data/${{ matrix.day }}.jsonl.gz --clobber
        env: { GH_TOKEN: '${{ github.token }}' }
  train:
    needs: [pick, fetch]
    if: always() && needs.pick.result == 'success'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-node@v5
        with: { node-version: 22 }
      - run: mkdir -p data && gh release download data-cache -p '*.jsonl.gz' -D data --clobber
        env: { GH_TOKEN: '${{ github.token }}' }
      - run: python3 train.py --last "$DAYS"
      - name: Score the model on held-back days
        continue-on-error: true
        env: { REPORT: model/report.txt }
        run: |
          mkdir -p app
          if curl -fsSL "$SCREEN_URL" -o app/live.html; then P=app/live.html; else P=app/flightcast-airport.html; fi
          node eval.mjs "$P" model/ymml-model.json model/holdout.json
      - name: Save the model
        run: |
          git config user.name "flightcast-trainer"; git config user.email "actions@users.noreply.github.com"
          git add model/
          git commit -m "Model trained $(date -u +%Y-%m-%d)" || echo "no change"
          git push
