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
| `.sync.json` | `{"url": "<Apps Script /exec URL>", "token": "<secret>"}`; optional, enables Google Sheet sync |

`.staticrypt.json` (the salt) **is** committed. Regenerating it logs every remembered device out.

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
