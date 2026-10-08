#!/usr/bin/env python3
"""Admin client for the Apps Script's Drive and cross-spreadsheet ops (needs .sync.json's admin token).

  python3 tools/sheetops.py drivels "299 Bloom/2026"
  python3 tools/sheetops.py drivefind "Quick Kill"
  python3 tools/sheetops.py sheetinfo <spreadsheetId>
  python3 tools/sheetops.py sheetget <spreadsheetId> --gid 869386941 [--max 500]
  python3 tools/sheetops.py sheetput <spreadsheetId> --gid 869386941 rows.json [--range B5] [--dry]

rows.json is a JSON array of rows; a cell is a value, {"d":"YYYY-MM-DD"} (date cell) or
{"rt":[{"t":"text","u":"url"}, ...]} (rich text with links). Output is the script's JSON.
"""
import argparse, json, os, subprocess, sys, tempfile, urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cfg():
    with open(os.path.join(ROOT, ".sync.json")) as f:
        c = json.load(f)
    if not c.get("admin"):
        sys.exit(".sync.json has no admin token")
    return c


def get(c, **params):
    q = urllib.parse.urlencode({"admin": c["admin"], **{k: v for k, v in params.items() if v is not None}})
    r = subprocess.run(["curl", "-sL", "--max-time", "120", f"{c['url']}?{q}"], capture_output=True, text=True)
    return parse(r.stdout)


def post(c, body):
    body = {"admin": c["admin"], **body}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body, f)
        path = f.name
    try:
        r = subprocess.run(["curl", "-sL", "--max-time", "120", "-H", "Content-Type: application/json", "--data", "@" + path, c["url"]],
                           capture_output=True, text=True)
    finally:
        os.unlink(path)
    return parse(r.stdout)


def parse(text):
    try:
        return json.loads(text)
    except ValueError:
        sys.exit("non-JSON reply (Apps Script redirect or outage?):\n" + text[:300])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("drivels").add_argument("folder")
    sub.add_parser("drivefind").add_argument("name")
    sub.add_parser("sheetinfo").add_argument("id")
    g = sub.add_parser("sheetget"); g.add_argument("id"); g.add_argument("--gid"); g.add_argument("--tab"); g.add_argument("--max", type=int)
    w = sub.add_parser("sheetput"); w.add_argument("id"); w.add_argument("rows"); w.add_argument("--gid"); w.add_argument("--tab")
    w.add_argument("--range"); w.add_argument("--dry", action="store_true", help="print the request instead of sending it")
    a = ap.parse_args()
    c = cfg()
    if a.op == "drivels":
        out = get(c, op="drivels", folder=a.folder)
    elif a.op == "drivefind":
        out = get(c, op="drivefind", name=a.name)
    elif a.op == "sheetinfo":
        out = get(c, op="sheetinfo", id=a.id)
    elif a.op == "sheetget":
        out = get(c, op="sheetget", id=a.id, gid=a.gid, tab=a.tab, max=a.max)
    else:
        with open(a.rows) as f:
            values = json.load(f)
        if not isinstance(values, list) or not all(isinstance(r, list) for r in values):
            sys.exit("rows.json must be a JSON array of rows")
        body = {"op": "sheetput", "id": a.id, "values": values}
        if a.gid: body["gid"] = a.gid
        if a.tab: body["tab"] = a.tab
        if a.range: body["range"] = a.range
        if a.dry:
            print(json.dumps(body, indent=1, ensure_ascii=False)); return
        out = post(c, body)
    print(json.dumps(out, indent=1, ensure_ascii=False))
    if not out.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
