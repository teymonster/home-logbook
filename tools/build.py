#!/usr/bin/env python3
"""Build the published page from the plaintext app source.

    src/index.html  --stamp, inject sync config-->  .build/index.html  --encrypt-->  docs/index.html

- src/index.html and .build/ are gitignored (plaintext). docs/index.html is the
  passphrase-encrypted page GitHub Pages serves.
- The passphrase is read from .bloom-passphrase (gitignored, one line).
- The salt lives in .staticrypt.json (committed). Never regenerate it: every
  remembered device would be logged out.
- Optional .sync.json {"url": ..., "token": ...} turns on Google Sheet sync by
  replacing the __SYNC_URL__ / __SYNC_TOKEN__ placeholders. Without it the app
  runs in "this device only" mode.

Usage: python3 tools/build.py        (run from the repo root)
"""
import datetime, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "index.html")
MID = os.path.join(ROOT, ".build", "index.html")
OUT = os.path.join(ROOT, "docs", "index.html")
PASSFILE = os.path.join(ROOT, ".bloom-passphrase")
SALTFILE = os.path.join(ROOT, ".staticrypt.json")
SYNCFILE = os.path.join(ROOT, ".sync.json")
STATICRYPT = "staticrypt@3.5.4"


def die(msg):
    sys.exit("build.py: " + msg)


def main():
    if not os.path.exists(SRC):
        die("src/index.html is missing (it is gitignored; recover it with --decrypt, see README)")
    if not os.path.exists(SALTFILE):
        die(".staticrypt.json (the salt) is missing; restore it from git, do not regenerate")
    if not os.path.exists(PASSFILE):
        die(".bloom-passphrase is missing; create it on the Mac, one line, 4-5 random words")
    pw = open(PASSFILE).read().strip()
    if len(pw) < 12:
        die("passphrase is too short; the encrypted file is public, use 4-5 random words")

    html = open(SRC).read()

    stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z") + " (GitHub Pages copy)"
    html, n = re.subn(r"App version: [^<]*", "App version: " + stamp, html)
    if n != 1:
        die("expected exactly one 'App version:' stamp in src, found %d" % n)

    if os.path.exists(SYNCFILE):
        cfg = json.load(open(SYNCFILE))
        url, token = cfg.get("url", ""), cfg.get("token", "")
        if not url.startswith("https://script.google.com/") or len(token) < 16:
            die(".sync.json needs an Apps Script /exec url and a token of 16+ chars")
        if token in open(SRC).read():
            die("the sync token is hard-coded in src/index.html; use the __SYNC_TOKEN__ placeholder")
        if "__SYNC_URL__" not in html or "__SYNC_TOKEN__" not in html:
            die("src/index.html has no __SYNC_URL__/__SYNC_TOKEN__ placeholders")
        html = html.replace("__SYNC_URL__", url).replace("__SYNC_TOKEN__", token)
        sync = "on"
    else:
        print("build.py: WARNING no .sync.json; the app will run in 'this device only' mode", file=sys.stderr)
        sync = "off (no .sync.json)"

    os.makedirs(os.path.dirname(MID), exist_ok=True)
    open(MID, "w").write(html)

    cmd = ["npx", "-y", STATICRYPT, os.path.relpath(MID, ROOT), "-d", "docs",
           "--remember", "0",
           "--template-title", "Home Logbook",
           "--template-instructions", "Enter the passphrase.",
           "--template-button", "Open",
           "--template-remember", "Remember this device",
           "--template-placeholder", "Passphrase",
           "--template-color-primary", "#2E6A56",
           "--template-color-secondary", "#F2F5F2"]
    env = dict(os.environ, STATICRYPT_PASSWORD=pw)
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        die("staticrypt failed:\n" + r.stdout + r.stderr)

    page = open(OUT).read()
    if "<meta charset=\"utf-8\" />" not in page and "<meta charset=\"utf-8\">" not in page:
        die("unexpected staticrypt template: no charset meta to anchor on")
    extra = ('<meta name="theme-color" content="#2E6A56">'
             '<meta name="apple-mobile-web-app-title" content="Logbook">')
    page = re.sub(r'(<meta charset="utf-8"\s*/?>)', r"\1" + extra, page, count=1)
    page = re.sub(r'<meta name="viewport"[^>]*>',
                  '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
                  page, count=1)
    open(OUT, "w").write(page)

    print("built  %s  %d bytes (plaintext, gitignored)" % (os.path.relpath(MID, ROOT), len(html)))
    print("built  %s  %d bytes (encrypted, commit this)" % (os.path.relpath(OUT, ROOT), len(page)))
    print("stamp  %s" % stamp)
    print("sync   %s" % sync)


if __name__ == "__main__":
    main()
