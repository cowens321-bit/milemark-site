import contextlib
from copy import deepcopy
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest

import update_rates as rates

TODAY = date(2026, 10, 15)
SAVED_HTML = Path("/private/tmp/claude-501/irs.html")
# Portable fixture for the standalone public repository's CI.
ROWS = [
    ("2026 (July&nbsp;1 – Dec.&nbsp;31)", "76", "IR-2026-29"),
    ("2026 (Jan.&nbsp;1 – June&nbsp;30)", "72.5", "IR-2025-128"),
    ("2025", "70", "IR-2024-312"), ("2024", "67", "IR-2023-239"),
    ("2023", "65.5", "IR-2022-234"),
    ("7/1/2022-12/31/2022", "62.5", "IR-2022-124"),
    ("1/1/2022-6/30/2022", "58.5", "IR-2021-251"),
]


def html(rows=ROWS):
    return "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in rates.HEADERS) + "</tr></thead><tbody>" + "".join(
        f"<tr><td>{period}</td><td>{rate}</td><td>14</td><td>21</td><td><a>{notice}</a></td></tr>"
        for period, rate, notice in rows) + "</tbody></table>"


class RatePublisherTests(unittest.TestCase):
    def setUp(self):
        self.previous = rates.build_update(html(), today=TODAY)

    def test_saved_html(self):
        saved = SAVED_HTML.read_text() if SAVED_HTML.exists() else html()
        self.assertEqual(rates.parse_entries(saved), self.previous["entries"])
        shared = Path(__file__).resolve().parents[2] / "shared/rates/irs-business-rates.json"
        if shared.exists():
            self.assertEqual(rates.parse_entries(saved), json.loads(shared.read_text())["entries"])

    def test_new_year(self):
        update = rates.build_update(html([("2027", "78", "IR-2026-300")] + ROWS), self.previous, TODAY)
        entry = update["entries"][-1]
        self.assertEqual((entry["id"], entry["start"], entry["end"], entry["rate"]),
                         ("irs-2027", "2027-01-01", "2027-12-31", "0.780"))

    def test_absurd_rate(self):
        with self.assertRaisesRegex(ValueError, "30–150"):
            rates.build_update(html().replace("<td>76</td>", "<td>7600</td>"), self.previous, TODAY)

    def test_removed_interval(self):
        with self.assertRaises(ValueError):
            rates.build_update(html(ROWS[1:]), self.previous, TODAY)

    def test_no_change_does_not_write(self):
        self.assertIsNone(rates.build_update(html(), self.previous, TODAY))
        with tempfile.TemporaryDirectory() as directory:
            previous, source, output = [Path(directory) / p for p in ("previous.json", "irs.html", "out.json")]
            previous.write_text(json.dumps(self.previous))
            source.write_text(html())
            output.write_text("untouched")
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(rates.main(["--previous", str(previous), "--html", str(source), "--out", str(output)]), 0)
            self.assertEqual(captured.getvalue().strip(), "no change")
            self.assertEqual(output.read_text(), "untouched")

    def test_split(self):
        previous = rates.build_update(html([("2026", "72.5", "IR-2025-128")] + ROWS[2:]), today=TODAY)
        self.assertIsNotNone(rates.build_update(html(), previous, TODAY))
        with self.assertRaisesRegex(ValueError, "retroactively"):
            rates.build_update(html().replace("<td>72.5</td>", "<td>73</td>"), previous, TODAY)

    def test_split_cannot_shrink_extend_or_leave_gap(self):
        previous = rates.build_update(html([("2026", "72.5", "IR-2025-128")] + ROWS[2:]), today=TODAY)
        for page in [html().replace("Dec.&nbsp;31", "Dec.&nbsp;30"),
                     html().replace("July&nbsp;1", "July&nbsp;2"),
                     html().replace("2026 (July&nbsp;1 – Dec.&nbsp;31)", "7/1/2026-1/1/2027")]:
            with self.subTest(page=page), self.assertRaises(ValueError):
                rates.build_update(page, previous, TODAY)

    def test_future_correction(self):
        previous = rates.build_update(html([("2027", "77", "IR-2026-300")] + ROWS), today=TODAY)
        self.assertIsNotNone(rates.build_update(html([("2027", "78", "IR-2026-300")] + ROWS), previous, TODAY))

    def test_past_correction_rejected(self):
        with self.assertRaisesRegex(ValueError, "retroactively"):
            rates.build_update(html().replace("<td>76</td>", "<td>77</td>"), self.previous, TODAY)

    def test_gap_overlap_headers_and_future(self):
        for page in [html().replace("July&nbsp;1", "July&nbsp;2"),
                     html().replace("June&nbsp;30", "July&nbsp;1"),
                     html().replace("Business use", "Wrong header"),
                     html([("2028", "78", "IR-2027-300")] + ROWS)]:
            with self.subTest(page=page), self.assertRaises(ValueError):
                rates.build_update(page, self.previous, TODAY)

    def test_version_sequence(self):
        previous = deepcopy(self.previous)
        previous["tableVersion"] = "2026-10-15.9"
        self.assertEqual(rates.next_version(previous, TODAY), "2026-10-15.10")
        self.assertEqual(rates.next_version(previous, date(2026, 10, 14)), "2026-10-15.10")
        self.assertEqual(rates.next_version(previous, date(2026, 10, 16)), "2026-10-16.1")

    def test_numeric_period(self):
        self.assertEqual(rates.period_dates("7/1/2022-12/31/2022"), (date(2022, 7, 1), date(2022, 12, 31)))

    def test_failure_leaves_output_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            previous, source = [Path(directory) / p for p in ("rates.json", "irs.html")]
            original = json.dumps(self.previous)
            previous.write_text(original)
            source.write_text(html(ROWS[1:]))
            self.assertEqual(rates.main(["--previous", str(previous), "--html", str(source), "--out", str(previous)]), 1)
            self.assertEqual(previous.read_text(), original)


if __name__ == "__main__":
    unittest.main()
