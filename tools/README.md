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
| `.passphrase` | one line; the page's passphrase |
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
curl -sL "$url?admin=$admin&op=file&id=<gmailId>&dry=1"                                    # list a message's PDF attachments
curl -sL "$url?admin=$admin&op=file&id=<gmailId>&folder=House/2025"                  # save them into that Drive folder (skips names already there; &overwrite=1 replaces; &all=1 for non-PDFs)
curl -sL "$url?admin=$admin&op=drivels&folder=<folder>/2026"                                # files + subfolders in a Drive folder, with ids and links (nothing is created)
curl -sL "$url?admin=$admin&op=drivefind&name=Quick%20Kill"                               # files anywhere in Drive whose name contains the text
curl -sL "$url?admin=$admin&op=sheetinfo&id=<spreadsheetId>"                              # another spreadsheet's tabs with their gids
curl -sL "$url?admin=$admin&op=sheetget&id=<spreadsheetId>&gid=<gid>"                     # one tab's cells as displayed (&tab=<name> also works; &max=500 rows)
python3 tools/sheetops.py sheetput <spreadsheetId> --gid <gid> rows.json                  # append rows (POST {admin, op:"sheetput", id, gid, values}); --range B5 anchors instead
```

`sheetput` cells are plain values, or `{"d":"2026-02-03"}` for a real date cell, or
`{"rt":[{"t":"invoice.pdf","u":"https://…"},{"t":" · "},…]}` for several links in one cell. A string
starting with `=` is written as a formula. `tools/sheetops.py` wraps all five ops and reads `.sync.json`.
The `sheet*` ops need the full `https://www.googleapis.com/auth/spreadsheets` scope in `appsscript.json`
(`spreadsheets.currentonly` only reaches the bound Sheet); changing a scope means re-consenting
(run any function from the editor, or revoke the script at myaccount.google.com/connections first if
the prompt does not appear) and deploying a new version.

`op=file` needs the `https://www.googleapis.com/auth/drive` scope in `appsscript.json` (the folder
lookup by name has to see existing folders, which the narrower `drive.file` scope cannot).

A bill's `sender` is normally a domain for `from:(…)`. For chatty senders (card alerts) use a
raw query instead: `q:from:chase.com subject:"transaction with" subject:(NETFLIX)`. Scans cap
at 60 messages per bill per run (`max=` up to 300 for admin backfills).

Bill rows can also be written with the app token: `POST {token, bills:{id:{...,u}}}`.

A bill's `account` column (the provider's account number) is what the app's Utilities tab shows; it
lives only in the Sheet, never in the page source. Edit it in the Sheet or under Bills › Edit in the app.

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
python3 tools/budget.py accept-suggestions     # tag = suggested on every untagged row, suggestion cleared
python3 tools/budget.py rescan-receipts        # re-read mis-parsed Amazon receipt emails via the peek op
node tools/script_test.js && python3 tools/budget_test.py
```

`budget/` is gitignored: `statements/` takes Chase monthly statement PDFs or CSV exports and USAA
CSV exports, `rules.json` maps merchants to categories and default tags, `cache/` holds the pulled
Sheet and the proposal, `report.md` is the breakdown. The daily `scanBudgetDaily` trigger keeps
harvesting new emails; `installTrigger` installs both triggers.

## Binder (v15)

The app's Binder tab reads three things beyond the progress tabs:

- **History** is one tab of the owner's house-costs ledger (another spreadsheet). The script
  properties `HISTORY_ID` and `HISTORY_GID` name it; the page token can read and edit only that
  tab (`GET ?token=&binder=1`, `POST {token, history:{id:{...,u}}}`, newest `u` wins, stale edits
  are reported, never applied). The ledger's purchase tab and store-receipts tab are refused by gid.
- **docs** and **inventory** are tabs in the progress Sheet and sync like bills.
- **Files**: `GET ?token=&op=files[&folder=2026]` lists one level below `BINDER_ROOT` (a Drive
  path, also a script property); `POST {token, op:"upload", folder:"2026"|"Warranties", name,
  mimeType, data:<base64>}` saves a JPEG/PNG/HEIC/PDF up to 20 MB there, never overwriting.

- **Reading photos** (Items → Take or choose photos): `POST {token, op:"readlabel", images:[{mimeType, data}]}` sends up
  to 4 photos/PDFs to Claude (`claude-opus-5-5`, low effort, JSON-schema output, server-side refusal fallback) and returns
  the item fields it can read. Needs the script property `ANTHROPIC_API_KEY` (an Anthropic API key, set only in Project
  Settings) and the `script.external_request` scope; capped at 40 reads a day (`LABEL_USE`).

`binderprep` sets all three properties while converting the ledger tab, once:

```
python3 tools/sheetops.py binderprep <id> --gid <gid>                  # report: cells + Notes links
python3 tools/sheetops.py binderprep <id> --gid <gid> --plan plan.json # preview (writes nothing)
python3 tools/sheetops.py binderprep <id> --gid <gid> --plan plan.json --apply
```

The plan names every data row by row number and item text, with its id, category, capital flag,
warranty date, files and notes; a mismatch rejects the whole plan. `--apply` copies the tab to
`<name> (backup YYYY-MM-DD)` first, then writes totals in row 1, the header in row 2, data from
row 3, and hides the id/updated columns K:L.

## Devices

- A device remembers the passphrase until you open the site with `?staticrypt_logout` on
  the URL, or until the passphrase changes.
- Changing the passphrase: edit `.passphrase`, rebuild, push. Every device asks again.
- Rotating the sync token: new `TOKEN` in the Apps Script project's Script properties, new
  `.sync.json`, rebuild, push.

## Apps Script changes

After editing `tools/apps-script.gs`, paste it into the Sheet's script editor and use
Deploy → Manage deployments → pencil → Version: **New**. Without a new version the `/exec`
URL keeps serving the old code.
