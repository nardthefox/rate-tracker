"""Rules for the public rate snapshot. These tests do not call publishers."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import fetch_rates as fr

TODAY = date(2026, 11, 9)
ROOT = Path(__file__).resolve().parent


def snap(series_id: str, value, as_of: str, frequency: str = "daily") -> dict:
    return {
        "series_id": series_id,
        "value": value,
        "as_of": as_of,
        "status": "ok",
        "frequency": frequency,
        "history": [{"date": as_of, "value": value}],
        "source_name": "Example",
        "source_url": "https://example.com",
    }


class ParserTests(unittest.TestCase):
    def test_pmms_does_not_carry_a_blank_forward(self) -> None:
        text = (
            "date,pmms30,pmms30p,pmms15,pmms15p\n"
            "10/29/2026,7.24,,6.6,\n"
            "11/5/2026,7.18,,,\n"
        )
        rows30, rows15 = fr.parse_pmms_csv(text)
        thirty = fr.build_rate_snapshot(series_id="pmms30", rows=rows30, today=TODAY)
        fifteen = fr.build_rate_snapshot(series_id="pmms15", rows=rows15, today=TODAY)
        self.assertEqual(thirty["as_of"], "2026-11-05")
        self.assertEqual(f"{thirty['value']:.2f}", "7.18")
        self.assertEqual(f"{thirty['change']:.2f}", "-0.06")
        self.assertEqual(fifteen["as_of"], "2026-10-29")
        self.assertEqual(f"{fifteen['value']:.2f}", "6.60")
        self.assertNotIn("2026-11-05", [point["date"] for point in fifteen["history"]])

    def test_pmms_latest_published_pair(self) -> None:
        text = (
            "date,pmms30,pmms30p,pmms15,pmms15p\n"
            "10/29/2026,7.24,,6.6,\n"
            "11/5/2026,7.18,,6.57,\n"
        )
        rows30, rows15 = fr.parse_pmms_csv(text)
        thirty = fr.build_rate_snapshot(series_id="pmms30", rows=rows30, today=TODAY)
        fifteen = fr.build_rate_snapshot(series_id="pmms15", rows=rows15, today=TODAY)
        self.assertEqual(f"{fifteen['value']:.2f}", "6.57")
        self.assertEqual(fifteen["as_of"], "2026-11-05")
        self.assertEqual(f"{fifteen['change']:.2f}", "-0.03")
        self.assertEqual(thirty["prior_as_of"], "2026-10-29")

    def test_effr_skips_nd_and_requires_series_id(self) -> None:
        text = (
            '"Series Description","Federal funds effective rate"\n'
            '"Unique Identifier: ","H15/H15/RIFSPFF_N.B"\n'
            '"Time Period","RIFSPFF_N.B"\n'
            "2026-11-05,3.88\n"
            "2026-11-06,ND\n"
        )
        rows = fr.parse_effr_csv(text)
        snap_effr = fr.build_rate_snapshot(series_id="effr", rows=rows, today=TODAY)
        self.assertEqual(snap_effr["as_of"], "2026-11-05")
        self.assertEqual(f"{snap_effr['value']:.2f}", "3.88")
        self.assertEqual([point["date"] for point in snap_effr["history"]], ["2026-11-05"])
        # The data column is the second header field. A different series id is refused.
        wrong = (
            '"Time Period","RIFSPFF_N.D"\n'
            "2026-11-06,3.88\n"
        )
        with self.assertRaises(fr.SourceError):
            fr.parse_effr_csv(wrong)

    def test_treasury_skips_gaps_and_uses_10yr_column(self) -> None:
        text = (
            'Date,"1 Mo","10 Yr","30 Yr"\n'
            "11/09/2026,4.04,4.88,5.26\n"
            "11/08/2026,N/A,N/A,N/A\n"
            "11/06/2026,4.02,4.86,5.26\n"
        )
        rows = fr.parse_treasury_csv(text)
        snap_t = fr.build_rate_snapshot(series_id="treasury10y", rows=rows, today=TODAY)
        self.assertEqual([point["date"] for point in snap_t["history"]], ["2026-11-06", "2026-11-09"])
        self.assertEqual(snap_t["as_of"], "2026-11-09")
        self.assertEqual(f"{snap_t['value']:.2f}", "4.88")
        self.assertEqual(f"{snap_t['change']:.2f}", "0.02")
        self.assertNotEqual(f"{snap_t['value']:.2f}", "5.26")

    def test_extra_precision_is_not_rounded_into_a_print(self) -> None:
        self.assertIsNone(fr.accept_number("7.185"))
        self.assertEqual(fr.accept_number("7.4"), Decimal("7.40"))
        self.assertIsNone(fr.accept_number("ND"))
        self.assertIsNone(fr.accept_number("N/A"))

    def test_fomc_range_ignores_the_dissent(self) -> None:
        text = (
            "The Committee decided to maintain the target range for the federal funds rate "
            "at 3-3/4 to 4 percent, in support of its goals. "
            "Voting against were A and B, who preferred to raise the target range for the "
            "federal funds rate by 1/4 percentage point at this meeting."
        )
        low, high, _quote = fr.parse_target_range(fr.html_to_text(f"<p>{text}</p>"))
        self.assertEqual(fr.format_range(low, high), "3.75\u20134.00")
        raised = "decided to raise the target range for the federal funds rate to 4-1/2 to 4-3/4 percent"
        low, high, _quote = fr.parse_target_range(raised)
        self.assertEqual(fr.format_range(low, high), "4.50\u20134.75")
        with self.assertRaises(fr.SourceError):
            fr.parse_target_range("preferred to raise the target range for the federal funds rate by 1/4 percentage point")


class FreshnessTests(unittest.TestCase):
    def test_last_good_keeps_its_date(self) -> None:
        prev = snap("effr", 3.88, "2026-11-06")
        out = fr.mark_delayed(prev, "effr", "down")
        self.assertEqual(out["value"], 3.88)
        self.assertEqual(out["as_of"], "2026-11-06")
        self.assertEqual(out["status"], "delayed")
        self.assertEqual(out["history"], prev["history"])
        self.assertNotEqual(out["as_of"], TODAY.isoformat())

    def test_value_without_as_of_is_unavailable(self) -> None:
        out = fr.mark_delayed({"value": 3.88, "as_of": None}, "effr", "down")
        self.assertIsNone(out["value"])
        self.assertIsNone(out["as_of"])
        self.assertEqual(out["status"], "unavailable")
        self.assertEqual(out["history"], [])

    def test_stale_thresholds(self) -> None:
        daily = fr.annotate_freshness(snap("effr", 3.88, "2026-11-06", "daily"), date(2026, 11, 13))
        self.assertFalse(daily["stale"])
        later = fr.annotate_freshness(snap("effr", 3.88, "2026-11-06", "daily"), date(2026, 11, 16))
        self.assertTrue(later["stale"])
        weekly = fr.annotate_freshness(snap("pmms30", 7.18, "2026-11-01", "weekly"), date(2026, 11, 11))
        self.assertFalse(weekly["stale"])
        weekly_old = fr.annotate_freshness(snap("pmms30", 7.18, "2026-11-01", "weekly"), date(2026, 11, 12))
        self.assertTrue(weekly_old["stale"])
        self.assertEqual(fr.business_days_between(date(2026, 11, 6), date(2026, 11, 9)), 1)

    def test_one_failure_does_not_drop_other_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            fr.write_json(data / "effr.json", snap("effr", 3.88, "2026-11-06"))

            def pmms():
                return (snap("pmms30", 7.18, "2026-11-05", "weekly"), snap("pmms15", 6.57, "2026-11-05", "weekly"))

            def boom():
                raise fr.SourceError("effr down")

            results = fr.collect(
                data,
                today=TODAY,
                pmms=pmms,
                effr=boom,
                treasury=lambda: snap("treasury10y", 4.88, "2026-11-09"),
                fomc=lambda: snap("fomc_target", "3.75–4.00", "2026-10-28", "meeting"),
            )
        self.assertEqual(results["pmms30"]["value"], 7.18)
        self.assertEqual(results["pmms15"]["as_of"], "2026-11-05")
        self.assertEqual(results["treasury10y"]["value"], 4.88)
        self.assertEqual(results["fomc_target"]["value"], "3.75–4.00")
        self.assertEqual(results["effr"]["status"], "delayed")
        self.assertEqual(results["effr"]["value"], 3.88)
        self.assertEqual(results["effr"]["as_of"], "2026-11-06")

    def test_failed_series_with_no_history_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = fr.collect(
                Path(tmp),
                today=TODAY,
                pmms=lambda: (_ for _ in ()).throw(fr.SourceError("pmms down")),
                effr=lambda: snap("effr", 3.88, "2026-11-06"),
                treasury=lambda: (_ for _ in ()).throw(fr.SourceError("treasury down")),
                fomc=lambda: snap("fomc_target", "3.75–4.00", "2026-10-28", "meeting"),
            )
        self.assertEqual(results["pmms30"]["status"], "unavailable")
        self.assertIsNone(results["pmms30"]["value"])
        self.assertIsNone(results["pmms30"]["as_of"])
        self.assertEqual(results["treasury10y"]["status"], "unavailable")
        self.assertEqual(results["effr"]["status"], "ok")

    def test_choose_and_seed_previous(self) -> None:
        local = snap("effr", 3.87, "2026-11-05")
        remote = snap("effr", 3.88, "2026-11-06")
        self.assertIs(fr.choose_previous(local, remote), remote)
        self.assertIs(fr.choose_previous(remote, local), remote)
        self.assertIs(fr.choose_previous(local, {"value": 9, "as_of": None}), local)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            bundle = {"series": {"effr": remote, "pmms30": {"value": 1}}}
            fr.seed_previous(data, "https://example.com/all.json", lambda url, attempts=1: json.dumps(bundle).encode())
            saved = json.loads((data / "effr.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["as_of"], "2026-11-06")
            self.assertFalse((data / "pmms30.json").exists())

            def explode(url, attempts=1):
                raise fr.SourceError("pages 404")

            fr.seed_previous(data, "https://example.com/all.json", explode)
            fr.seed_previous(data, "http://insecure.example/all.json", explode)


class PageAndWorkflowTests(unittest.TestCase):
    def test_page_copy_and_embed_rules(self) -> None:
        html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        self.assertIn(fr.DISCLAIMER, html)
        self.assertIn("Harbor Realty &amp; Management Group", html)
        self.assertIn("No tracking. Figures shown exactly as published (display rounding only).", html)
        self.assertIn("National market averages from public sources. Updated when publishers release new figures.", html)
        self.assertIn("30-year fixed average", html)
        self.assertIn("15-year fixed average", html)
        self.assertIn('src="data/all.js"', html)
        self.assertIn("@media (max-width: 420px)", html)
        self.assertNotIn("window.top", html)
        for banned in (
            "fred.stlouisfed.org",
            "optimalblue",
            "obmmi",
            "fanniemae",
            "mba.org",
            "googletagmanager",
            "google-analytics",
            "api.stlouisfed.org",
        ):
            self.assertNotIn(banned, html.lower())
        script = html.split("<script>", 1)[1]
        self.assertIsNone(re.search(r"\bAPRs?\b", html))
        self.assertIsNone(re.search(r"\bpoints\b", html, re.I))
        for banned in ("per month", "apply now", "get this rate", "$"):
            self.assertNotIn(banned, script.lower())

    def test_fetch_script_stays_on_the_allow_list(self) -> None:
        text = (ROOT / "fetch_rates.py").read_text(encoding="utf-8").lower()
        for banned in ("fred.stlouisfed.org", "optimalblue", "obmmi", "fanniemae.com", "mba.org"):
            self.assertNotIn(banned, text)
        self.assertIn("rifspff_n.b", text)
        self.assertIn("pmms_history.csv", text)
        self.assertIn("daily_treasury_yield_curve", text)

    def test_workflow_deploys_with_official_pages_actions(self) -> None:
        text = (ROOT / ".github" / "workflows" / "update-rates.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch", text)
        self.assertIn("actions/upload-pages-artifact@v5", text)
        self.assertIn("actions/deploy-pages@v5", text)
        self.assertIn("enablement: false", text)
        self.assertNotIn("enablement: true", text)
        self.assertNotIn("secrets.", text)
        self.assertIn('cron: "15 14 * * 1-5"', text)
        self.assertIn('cron: "45 17 * * 1-5"', text)
        self.assertIn('cron: "20 22 * * 1-5"', text)
        self.assertIn("push:", text)
        self.assertIn("- main", text)

    def test_why_it_matters_lines_render(self) -> None:
        html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        fed = (
            "Why it matters: The Fed's rate moves short-term borrowing costs, "
            "like HELOCs and adjustable-rate loans, and signals where policy is headed. "
            "It doesn't set mortgage rates directly."
        )
        treasury = "Why it matters: 30-year mortgage rates tend to move with the 10-year Treasury yield."
        self.assertEqual(html.count(fed), 1)
        self.assertEqual(html.count(treasury), 1)
        self.assertIn("whyLine(FED_WHY)", html)
        self.assertIn('card(s.effr, "Effective federal funds rate", 2, FED_WHY)', html)
        self.assertIn('card(s.treasury10y, "10-year Treasury yield", 2, TREASURY_WHY)', html)


if __name__ == "__main__":
    unittest.main()
