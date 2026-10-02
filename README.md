# Home Logbook

A phone-friendly maintenance logbook for a 1927 Highland Park bungalow: what's due,
what's been done, how to do it.

- **App:** https://teymonster.github.io/home-logbook/ (GitHub Pages). The page asks for a
  passphrase the first time on each device; tick *Remember this device*. Add it to your
  phone's home screen.
- **Progress** syncs to a private Google Sheet. *House → Move your progress* still works
  as a manual copy between devices.

## How it's built

- `src/index.html` is the plaintext app. It is **not in this repo** (gitignored); it lives
  on the owner's machine.
- `docs/index.html` is that page encrypted with [StatiCrypt](https://github.com/robinmoisson/staticrypt);
  it is what Pages serves. `.staticrypt.json` holds the salt and must never be regenerated.
- `tools/build.py` stamps the version, injects the sync endpoint, and encrypts.
- `tools/apps-script.gs` is the Google Apps Script that stores progress in the Sheet.

Recovering the plaintext from the published page, given the passphrase:

```
STATICRYPT_PASSWORD="$(cat .bloom-passphrase)" npx -y staticrypt@3.5.4 docs/index.html --decrypt -d out
```

Notes, paperwork, photos and secrets are kept out of the repo on purpose.
