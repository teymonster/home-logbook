#!/usr/bin/env python3
"""Tests for tools/budget.py on synthetic data only: python3 tools/budget_test.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import budget as B  # noqa: E402

CHASE_TEXT = """
    Previous Balance                                                             $6,195.84
    Payment, Credits                                                            -$8,295.02
    Purchases                                                                     +$215.90
    Fees Charged                                                                      $0.00
    Opening/Closing Date                                             12/05/25 - 01/04/26

ACCOUNT ACTIVITY
    Date of
  Transaction                                 Merchant Name or Transaction Description                                     $ Amount
PAYMENTS AND OTHER CREDITS
  12/15                    Payment Thank You-Mobile                                                                         -8,295.02
  12/20                    AMAZON MKTPL*REFUND Amzn.com/bill WA                                                                 -4.49
                           Order Number 111-0000000-0000003
PURCHASE
  12/06                    Amazon.com*NV9SA2TZ2 Amzn.com/bill WA                                                                  .64
                           Order Number  111-0000000-0000001
  12/28                    DD *DOORDASH TARGET 855-431-0459 CA                                                                  150.00
  01/02                    AMAZON MKTPL*NV2LI9951 Amzn.com/bill WA                                                               15.11
                           Order Number 111-0000000-0000002
  01/03                    CANNAVERSE - NIAGARA FA NIAGARA FALLS ON                                                              50.15
                            01/03 CANADIAN DOLLAR
                            70.00 X 0.716428571 (EXCHG RATE)
INTEREST CHARGES
    Date of
  Transaction                           Merchant Name or Transaction Description                             $ Amount         Rewards
PURCHASES AND REDEMPTIONS
  12/28                    DD *DOORDASH TARGET 855-431-0459 CA                                                                  150.00
"""


class ChasePdfText(unittest.TestCase):
    def test_rows_years_orders_and_summary(self):
        rows, summary = B.parse_chase_text(CHASE_TEXT)
        self.assertEqual(summary["purchases"], 215.90)
        self.assertEqual(summary["credits"], -8295.02)
        by = {r["desc"].split(" ")[0] + r["date"]: r for r in rows}
        self.assertEqual(len(rows), 5, rows)                       # payment dropped, rewards section ignored
        self.assertEqual(by["Amazon.com*NV9SA2TZ22025-12-06"]["amount"], 0.64)
        self.assertEqual(by["Amazon.com*NV9SA2TZ22025-12-06"]["orderId"], "111-0000000-0000001")
        self.assertEqual(by["AMAZON2026-01-02"]["orderId"], "111-0000000-0000002")       # year rolls over at the statement boundary
        self.assertEqual(by["AMAZON2025-12-20"]["amount"], -4.49)
        self.assertEqual(by["AMAZON2025-12-20"]["type"], "Return")
        self.assertEqual(by["CANNAVERSE2026-01-03"]["amount"], 50.15)
        self.assertAlmostEqual(sum(r["amount"] for r in rows if r["type"] == "Sale"), 215.90, places=2)


class UsaaCsv(unittest.TestCase):
    def test_parse(self):
        p = os.path.join(B.CACHE, "_test_usaa.csv")
        os.makedirs(B.CACHE, exist_ok=True)
        with open(p, "w") as f:
            f.write("Date,Description,Original Description,Category,Amount,Status\n"
                    '2026-09-29,"Zelle: Pat Example","ZELLE: PAT EXAMPLE",Transfer,-150.00,Posted\n'
                    '2026-09-30,"Payroll","ACME PAYROLL FT",Paycheck,5000.00,Posted\n'
                    '2026-10-05,"WWE10522","WWE10522",Category Pending,-40.00,P\n')
        try:
            account, rows = B.parse_statement(p)
        finally:
            os.remove(p)
        self.assertEqual(account, "usaa")
        self.assertEqual([r["amount"] for r in rows], [150.0, -5000.0, 40.0])
        self.assertEqual(rows[2]["status"], "Pending")
        self.assertEqual(rows[0]["desc"], "ZELLE: PAT EXAMPLE")


def sheet(**kw):
    base = {"bills": {}, "payments": {}, "budget": {}, "transactions": {}, "receipts": {}}
    base.update(kw)
    return base


def tx(date, merchant, amount, **kw):
    t = {"date": date, "merchant": merchant, "amount": amount, "account": "chase", "category": "", "suggested": "", "tag": "", "note": "",
         "detail": "", "billId": "", "source": "alert", "csv": False, "gmailId": "g" + date.replace("-", ""), "u": "2026-10-01T00:00:00.000Z"}
    t.update(kw)
    return t


def csvrow(date, desc, amount, account="chase", orderId=""):
    return {"date": date, "postDate": date, "desc": desc, "csvCat": "", "amount": amount, "type": "Sale", "status": "Posted", "memo": "", "orderId": orderId}


class Matching(unittest.TestCase):
    def setUp(self):
        self.r = B.DEFAULT_RULES.copy()
        self.r["_compiled"] = [(B.re.compile(m["re"], B.re.I), m.get("category", ""), m.get("suggested", "")) for m in self.r["merchants"]]

    def run_match(self, sh, csvrows):
        rows = {B.csv_id(r.get("account", "chase"), r, 1): dict(r, account=r.get("account", "chase")) for r in csvrows}
        csvdata = {"rows": rows, "coverage": {"chase": ["2026-01-01", "2026-09-30"]}}
        return B.Matcher(sh, csvdata, self.r).run()

    def test_statement_line_enriches_the_alert_row_instead_of_duplicating(self):
        sh = sheet(transactions={"e-1": tx("2026-09-29", "AMAZON MKTPLACE PMTS", 44.64, gmailId="1")})
        out, _, _ = self.run_match(sh, [csvrow("2026-09-30", "AMAZON MKTPL*ABC Amzn.com/bill WA", 44.64, orderId="111-0000000-0000001")])
        self.assertEqual(list(out), ["e-1"])
        self.assertTrue(out["e-1"]["csv"])
        self.assertEqual(out["e-1"]["category"], "amazon")
        self.assertIn("order 111-0000000-0000001", out["e-1"]["detail"])   # order number kept even without an email yet

    def test_adjusted_alert_uses_the_posted_amount(self):
        sh = sheet(transactions={"e-1": tx("2026-10-04", "DD *DOORDASH TARGET", 226.02, gmailId="1")})
        out, _, notes = self.run_match(sh, [csvrow("2026-10-04", "DD *DOORDASH TARGET 855-431-0459 CA", 225.33)])
        self.assertEqual(list(out), ["e-1"])
        self.assertEqual(out["e-1"]["amount"], 225.33)
        self.assertIn("alert $226.02, posted $225.33", out["e-1"]["detail"])
        self.assertEqual(len(notes["adjusted"]), 1)

    def test_alert_missing_from_statement_is_suggested_skip(self):
        sh = sheet(transactions={"e-1": tx("2026-05-04", "SOME STORE", 20.00, gmailId="1")})
        out, _, notes = self.run_match(sh, [csvrow("2026-05-20", "OTHER", 5.0)])
        self.assertEqual(out["e-1"]["suggested"], "skip")
        self.assertEqual(len(notes["notOnStatement"]), 1)

    def test_amazon_order_number_links_exactly_and_subset_sum_covers_the_rest(self):
        sh = sheet(receipts={
            "a-111-0000000-0000001": {"kind": "amazon", "date": "2026-09-01", "merchant": "Amazon", "total": 30.00, "orderId": "111-0000000-0000001", "categories": "Plumbing, Hand Tools", "items": "", "gmailId": "m1", "txId": ""},
            "a-111-0000000-0000002": {"kind": "amazon", "date": "2026-09-10", "merchant": "Amazon", "total": 25.00, "orderId": "111-0000000-0000002", "categories": "Clothing", "items": "", "gmailId": "m2", "txId": ""},
        })
        rows = [csvrow("2026-09-02", "AMAZON MKTPL*A Amzn.com/bill WA", 30.00, orderId="111-0000000-0000001"),
                csvrow("2026-09-11", "AMAZON MKTPL*B Amzn.com/bill WA", 10.00),
                csvrow("2026-09-12", "AMAZON MKTPL*C Amzn.com/bill WA", 15.00),
                csvrow("2026-09-12", "AMAZON MKTPL*D Amzn.com/bill WA", 99.00)]
        out, links, notes = self.run_match(sh, rows)
        by_amt = {round(t["amount"], 2): t for t in out.values()}
        self.assertIn("order 111-0000000-0000001: Plumbing, Hand Tools (1 of 1 charges)", by_amt[30.0]["detail"])
        self.assertEqual(by_amt[30.0]["suggested"], "necessary")
        self.assertIn("(1 of 2 charges, matched by amount)", by_amt[10.0]["detail"])
        self.assertIn("(2 of 2 charges, matched by amount)", by_amt[15.0]["detail"])
        self.assertEqual(by_amt[15.0]["suggested"], "unnecessary")
        self.assertTrue(by_amt[99.0]["detail"].startswith("no order email matched"))
        self.assertEqual(len(links), 2)
        self.assertEqual(notes.get("amazonOrderUnmatched", []), [])

    def test_doordash_receipt_items_land_in_detail_with_fuzzy_amount(self):
        sh = sheet(receipts={"r-d1": {"kind": "doordash", "date": "2026-10-04", "merchant": "Target", "total": 225.33, "orderId": "", "categories": "",
                                      "items": "1x Nutmeg $4.69; 2x Chicken $12.99", "last4": "1234", "gmailId": "d1", "txId": ""}})
        out, links, _ = self.run_match(sh, [csvrow("2026-10-05", "DD *DOORDASH TARGET 855-431-0459 CA", 226.02),
                                            csvrow("2026-10-06", "DD *DOORDASH JEWEL-OSC 855-431-0459 CA", 80.00),
                                            csvrow("2026-10-06", "DD *DOORDASH MCDONALDS 855-431-0459 CA", 30.00)])
        by_amt = {round(t["amount"], 2): t for t in out.values()}
        self.assertEqual(by_amt[80.0]["suggested"], "necessary")      # grocery merchant read from the card descriptor, no receipt needed
        self.assertEqual(by_amt[30.0]["suggested"], "unnecessary")
        t = by_amt[226.02]
        self.assertEqual(t["category"], "delivery")
        self.assertEqual(t["suggested"], "necessary")           # Target via DoorDash counts as groceries per the default rules
        self.assertIn("Target: 1x Nutmeg $4.69", t["detail"])
        self.assertIn("amount differs from receipt $225.33", t["detail"])
        self.assertEqual(links["r-d1"], next(iter(out)))

    def test_bill_join_by_gmail_id_and_default_tags(self):
        sh = sheet(bills={"netflix": {"name": "Netflix", "category": "streaming", "cadence": "monthly", "active": True},
                          "comed": {"name": "ComEd electric", "category": "utility", "cadence": "monthly", "active": True}},
                   payments={"e-9": {"billId": "netflix", "date": "2026-09-05", "amount": 9.91, "gmailId": "9"},
                             "e-8": {"billId": "comed", "date": "2026-09-20", "amount": 143.0, "gmailId": "8"}},
                   transactions={"e-9": tx("2026-09-05", "NETFLIX.COM", 9.91, gmailId="9")})
        out, _, _ = self.run_match(sh, [csvrow("2026-09-21", "COMED PAYMENTS", 143.0, account="usaa"),
                                         csvrow("2026-09-06", "NETFLIX.COM 866-579-7172 CA", 9.91)])
        self.assertEqual(len(out), 2)
        self.assertEqual(out["e-9"]["billId"], "netflix")
        self.assertEqual(out["e-9"]["category"], "streaming")           # a joined bill's category wins over the merchant rule
        self.assertEqual(out["e-9"]["suggested"], "")
        comed = [t for k, t in out.items() if k != "e-9"][0]
        self.assertEqual((comed["billId"], comed["category"], comed["suggested"]), ("comed", "utility", "necessary"))

    def test_transfers_and_income(self):
        out, _, _ = self.run_match(sheet(), [csvrow("2026-09-01", "CHASE CREDIT CRD EPAY", 2000.0, account="usaa"),
                                              csvrow("2026-09-02", "ACME PAYROLL FT", -5000.0, account="usaa")])
        cats = sorted((t["category"], t["suggested"]) for t in out.values())
        self.assertEqual(cats, [("income", ""), ("transfer", "skip")])


class Diff(unittest.TestCase):
    def test_only_changed_non_owned_fields_count(self):
        cur = {"e-1": tx("2026-09-01", "M", 1.0, category="dining", tag="necessary", suggested="unnecessary")}
        same = {"e-1": dict(cur["e-1"], tag="", category="coffee")}          # Sheet owns tag + category: no push
        self.assertEqual(B.diff_rows(same, cur), {})
        changed = {"e-1": dict(cur["e-1"], suggested="frivolous")}
        self.assertEqual(list(B.diff_rows(changed, cur)), ["e-1"])
        empty_cat = {"e-1": dict(cur["e-1"], category="x")}
        cur2 = {"e-1": dict(cur["e-1"], category="")}
        self.assertEqual(list(B.diff_rows(empty_cat, cur2)), ["e-1"])         # an empty Sheet category gets filled


class Windows(unittest.TestCase):
    def test_month_windows_cover_the_span_without_gaps(self):
        w = B.month_windows(3)
        self.assertEqual(len(w), 4)
        for (a, b), (c, _) in zip(w, w[1:]):
            self.assertEqual(b, c)


if __name__ == "__main__":
    unittest.main(verbosity=1)
