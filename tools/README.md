# Publishing the logbook

```
# 1. edit the plaintext app
$EDITOR src/index.html

# 2. stamp + encrypt  ->  docs/index.html
python3 tools/build.py

# 3. publish (plain push; no signed-commit rule on this repo)
git add docs/index.html
git commit -m "..."
git push
```

GitHub Pages rebuilds from `docs/` on `main` within about a minute.

## Files the build needs (all gitignored, on the owner's machine)

| File | What |
|---|---|
| `src/index.html` | the app, plaintext |
| `.bloom-passphrase` | one line; the page's passphrase |
| `.sync.json` | `{"url": "<Apps Script /exec URL>", "token": "<app token>", "admin": "<admin token>"}`; `url`+`token` enable Google Sheet sync and are injected into the page; `admin` is used only from this machine and is never injected |

`.staticrypt.json` (the salt) **is** committed. Regenerating it logs every remembered device out.

## Google Sheet backend

`tools/apps-script.gs` is the web app bound to the Sheet; `tools/appsscript.json` is its manifest
(Gmail read-only scope + the Gmail advanced service). Script properties: `TOKEN` (app) and
`ADMIN_TOKEN` (owner only, must differ). Tabs `log`, `bills`, `payments` are created on first use.

A daily trigger (`installTrigger`, run once from the editor) scans the last 40 days of Gmail for
each bill with a `sender` and writes payment rows. The app's Refresh button runs the same scan on
demand (at most once per 5 minutes).

### Admin ops (never from the page)

```sh
url=$(python3 -c 'import json;print(json.load(open(".sync.json"))["url"])')
admin=$(python3 -c 'import json;print(json.load(open(".sync.json"))["admin"])')
curl -sL "$url?admin=$admin&op=discover&months=12" | python3 -m json.tool        # who bills me, how often
curl -sL "$url?admin=$admin&op=peek&id=<gmailId>" | python3 -m json.tool          # one email's text
curl -sL "$url?admin=$admin&op=scan&months=12&bill=<id>&dry=1" | python3 -m json.tool   # backfill preview
curl -sL "$url?admin=$admin&op=search&max=100&q=$(python3 -c 'import urllib.parse;print(urllib.parse.quote("from:chase.com subject:NETFLIX newer_than:12m"))')"   # any Gmail search, headers only
curl -sL "$url?admin=$admin&op=raw&id=<gmailId>"                                           # MIME skeleton + decode diagnostics
```

A bill's `sender` is normally a domain for `from:(…)`. For chatty senders (card alerts) use a
raw query instead: `q:from:chase.com subject:"transaction with" subject:(NETFLIX)`. Scans cap
at 60 messages per bill per run (`max=` up to 300 for admin backfills).

Bill rows can also be written with the app token: `POST {token, bills:{id:{...,u}}}`.

## Budget (tools/budget.py)

Three more tabs, `transactions`, `receipts` and `budget`, hold every charge, the parsed receipt
emails and the monthly targets. They are outside the app's sync set: `GET ?token&budget=1` returns
them and a POST writes them only when it sends them. Columns `tag`, `note` and `category` on
`transactions` (and `target`, `note` on `budget`) belong to the Sheet: a filled cell survives every
push, an empty one is filled on the next push.

```sh
python3 tools/budget.py backfill --months 12   # admin: harvest Chase alerts, Zelle sends, Amazon/DoorDash/Uber receipts, month by month
python3 tools/budget.py run                    # pull → ingest budget/statements/ → match → push → report (budget/report.md)
python3 tools/budget.py push --dry             # preview what would be written
node tools/script_test.js && python3 tools/budget_test.py
```

`budget/` is gitignored: `statements/` takes Chase monthly statement PDFs or CSV exports and USAA
CSV exports, `rules.json` maps merchants to categories and default tags, `cache/` holds the pulled
Sheet and the proposal, `report.md` is the breakdown. The daily `scanBudgetDaily` trigger keeps
harvesting new emails; `installTrigger` installs both triggers.

## Devices

- A device remembers the passphrase until you open the site with `?staticrypt_logout` on
  the URL, or until the passphrase changes.
- Changing the passphrase: edit `.bloom-passphrase`, rebuild, push. Every device asks again.
- Rotating the sync token: new `TOKEN` in the Apps Script project's Script properties, new
  `.sync.json`, rebuild, push.

## Apps Script changes

After editing `tools/apps-script.gs`, paste it into the Sheet's script editor and use
Deploy → Manage deployments → pencil → Version: **New**. Without a new version the `/exec`
URL keeps serving the old code.
