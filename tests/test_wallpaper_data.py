import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wallpaper_data as data


class NumberParsingTests(unittest.TestCase):
    def test_cnbc_numbers(self):
        self.assertEqual(data._cnbc_number("7,568.92"), 7568.92)
        self.assertEqual(data._cnbc_number("+0.36%"), 0.36)
        self.assertIsNone(data._cnbc_number("UNCH"))
        self.assertIsNone(data._cnbc_number("N/A"))
        self.assertIsNone(data._cnbc_number("--"))

    def test_safe_float(self):
        self.assertEqual(data.safe_float("1.5"), 1.5)
        self.assertIsNone(data.safe_float(""))
        self.assertIsNone(data.safe_float("abc"))


class DownsampleTests(unittest.TestCase):
    def test_keeps_ends_and_limit(self):
        points = [[float(i), 100 + i] for i in range(400)]
        out = data._wallpaper_downsample(points, 80)
        self.assertLessEqual(len(out), 81)
        self.assertEqual(out[0], points[0])
        self.assertEqual(out[-1], points[-1])

    def test_short_series_is_untouched(self):
        points = [[1.0, 10.0], [2.0, 11.0]]
        self.assertEqual(data._wallpaper_downsample(points, 80), points)


class QuoteViewTests(unittest.TestCase):
    def test_view_shape_without_upstream(self):
        series = {"^GSPC": {"points": [[1, 10], [2, 11]], "source": "cnbc", "range": "5D", "tz": "America/New_York"}}
        sessions = [{"symbol": "^GSPC", "label": "NEW YORK", "status": "OPEN"}]
        row = {
            "last": "7,000.00",
            "change": "+10.00",
            "change_pct": "+0.14%",
            "previous_day_closing": "6,990.00",
            "open": "6,995.00",
            "high": "7,010.00",
            "low": "6,990.00",
            "yrhiprice": "7,500.00",
            "yrloprice": "5,000.00",
            "shortName": "S&P 500",
            "currencyCode": "USD",
            "curmktstatus": "REG_MKT",
            "last_time": "2026-11-03",
        }
        view = data.wallpaper_quote_view("^GSPC", "S&P 500", row, series, sessions)
        self.assertEqual(view["symbol"], "^GSPC")
        self.assertEqual(view["price"], 7000.0)
        self.assertEqual(view["seriesSource"], "cnbc")
        self.assertEqual(len(view["series"]), 2)
        self.assertEqual(view["label"], "S&P 500")

    def test_missing_series_gets_honest_two_point_flat(self):
        row = {"last": "100", "previous_day_closing": "99", "change": "+1", "change_pct": "+1%"}
        view = data.wallpaper_quote_view("XYZ", "XYZ", row, {}, [])
        self.assertEqual(view["seriesSource"], "flat")
        self.assertEqual(view["series"][0][1], 99.0)
        self.assertEqual(view["series"][-1][1], 100.0)


class SessionTests(unittest.TestCase):
    def test_sessions_have_four_zones(self):
        sessions = data.wallpaper_sessions(datetime.now(timezone.utc))
        labels = {item.get("label") for item in sessions}
        self.assertEqual(labels, {"NEW YORK", "LONDON", "TOKYO", "HONG KONG"})
        for item in sessions:
            self.assertIn(item.get("state"), ("open", "closed", "pre", "post"))
            self.assertIn(item.get("stateLabel"), ("OPEN", "CLOSED", "PRE", "AFTER"))
            self.assertRegex(item.get("localTime") or "", r"^\d{2}:\d{2}$")
            self.assertIn(item["next"]["kind"], ("open", "close"))


class PayloadShapeTests(unittest.TestCase):
    def test_payload_contract(self):
        rows = {"^GSPC": {"last": "100", "previous_day_closing": "99", "change": "+1", "change_pct": "+1%"}}
        original_quotes, original_series = data.wallpaper_cnbc_quotes, data.wallpaper_series
        data.wallpaper_cnbc_quotes = lambda symbols: rows
        data.wallpaper_series = lambda symbols: {}
        try:
            payload = data.wallpaper_payload(["^GSPC"], "^GSPC")
        finally:
            data.wallpaper_cnbc_quotes, data.wallpaper_series = original_quotes, original_series
        for key in ("generatedAt", "hero", "quotes", "sessions", "headlines", "sources", "errors"):
            self.assertIn(key, payload)
        self.assertEqual(payload["hero"]["symbol"], "^GSPC")
        self.assertEqual(payload["quotes"][0]["price"], 100.0)

    def test_live_wallpaper_dedupes_and_caps(self):
        captured = {}

        def fake_payload(symbols, hero):
            captured["symbols"] = symbols
            return {"hero": {"symbol": hero}, "quotes": []}

        original = data.wallpaper_payload
        data.wallpaper_payload = fake_payload
        data.CACHE.clear()
        try:
            data.live_wallpaper(",".join(["^GSPC"] * 3 + [f"SYM{i}" for i in range(20)]), "")
        finally:
            data.wallpaper_payload = original
        self.assertEqual(len(captured["symbols"]), 8)
        self.assertEqual(captured["symbols"][0], "^GSPC")


if __name__ == "__main__":
    unittest.main()
