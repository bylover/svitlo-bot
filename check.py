name: Svitlo check
on:
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch:
permissions:
  contents: write
concurrency:
  group: svitlo
  cancel-in-progress: false
jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 8
    steps:
      - uses: actions/checkout@v4
      - run: python3 check.py
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
          CHAT_ID: ${{ secrets.CHAT_ID }}
          GROUP: ${{ secrets.GROUP }}
          WIFE_ID: ${{ secrets.WIFE_ID }}
          WIFE_NAME: ${{ secrets.WIFE_NAME }}
          TMDB_KEY: ${{ secrets.TMDB_KEY }}
          OMDB_KEY: ${{ secrets.OMDB_KEY }}
          YT_KEY: ${{ secrets.YT_KEY }}
          SYNC_URL: ${{ secrets.SYNC_URL }}
          SYNC_KEY: ${{ secrets.SYNC_KEY }}
          SEND_NOW: ${{ github.event_name == 'workflow_dispatch' && '1' || '' }}
      - name: Save state
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898208+github-actions[bot]@users.noreply.github.com"
          git add state.json
          git diff --cached --quiet || (git commit -m "state" && git push)
