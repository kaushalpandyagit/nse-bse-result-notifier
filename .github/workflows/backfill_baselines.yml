name: Backfill Result Baselines (manual)

on:
  workflow_dispatch:
    inputs:
      days:
        description: 'How many days back to scan for already-declared results'
        required: false
        default: '75'

permissions:
  contents: write

jobs:
  backfill:
    runs-on: ubuntu-latest
    steps:
      - name: Check out repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements_breakout.txt

      - name: Run backfill
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python3 backfill_baselines.py --days ${{ github.event.inputs.days }}

      - name: Commit updated state
        run: |
          git config user.name "breakout-notifier-bot"
          git config user.email "actions@github.com"
          FILES_TO_ADD=""
          for f in breakout_state.json nifty500_symbols.json; do
            if [ -f "$f" ]; then FILES_TO_ADD="$FILES_TO_ADD $f"; fi
          done
          if [ -n "$FILES_TO_ADD" ]; then
            git add $FILES_TO_ADD
            git diff --quiet --cached || git commit -m "Backfill result baselines [skip ci]"
            git push
          else
            echo "No state files to commit."
          fi
