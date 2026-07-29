"""Tests for the trickiest analytics.py logic: period resampling and the active_count
NaN-vs-0 distinction (a real bug hit twice while building this — see get_metric_wide's
docstring). Uses a small synthetic frame, not the real DB, so it's fast and self-contained.
Run: python -m unittest tests.test_analytics -v
"""
from __future__ import annotations

import unittest

import pandas as pd

from ppi.analytics import (
    detect_renames, entry_exit, filter_txn_type, generate_compare_insights, generate_entity_insights,
    get_metric_wide, hhi, hhi_reading, industry_total, is_additive, is_partial_period, movers,
    peer_benchmark, period_coverage, period_label, scorecard, selectable_metrics, share_of_growth,
    span_label, trailing_growth,
)


def _facts(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["month"] = pd.to_datetime(df["month"])
    return df


class TestActiveCountMasking(unittest.TestCase):
    """active_count doesn't exist for some months (RBI schema drift) — those months must
    stay NaN, never silently become 0, or MoM deltas manufacture a fake spike."""

    def setUp(self):
        # Entity A: outstanding_count reported every month; active_count only from month 3.
        rows = []
        for i, m in enumerate(["2025-01-01", "2025-02-01", "2025-03-01", "2025-04-01"]):
            rows.append({"month": m, "entity": "A", "entity_type": "bank", "instrument": "card",
                         "metric": "outstanding_count", "value": 100 + i, "unit": "count"})
            if i >= 2:  # active_count only present from March onward
                rows.append({"month": m, "entity": "A", "entity_type": "bank", "instrument": "card",
                             "metric": "active_count", "value": 50 + i, "unit": "count"})
        self.df = _facts(rows)

    def test_missing_months_are_nan_not_zero(self):
        wide = get_metric_wide(self.df, "active_count")
        jan, feb = pd.Timestamp("2025-01-01"), pd.Timestamp("2025-02-01")
        mar = pd.Timestamp("2025-03-01")
        self.assertTrue(pd.isna(wide.loc["A", jan]))
        self.assertTrue(pd.isna(wide.loc["A", feb]))
        self.assertEqual(wide.loc["A", mar], 52)

    def test_derived_ratio_stays_nan_not_corrupted(self):
        ratio = get_metric_wide(self.df, "active_ratio_pct")
        jan = pd.Timestamp("2025-01-01")
        self.assertTrue(pd.isna(ratio.loc["A", jan]))  # not 0%, not a spike later

    def test_no_fake_mom_spike_at_boundary(self):
        wide = get_metric_wide(self.df, "active_count")
        mom = wide.pct_change(axis=1) * 100
        mar = pd.Timestamp("2025-03-01")
        # Feb (NaN) -> Mar (52) must not compute to some huge finite percentage.
        self.assertTrue(pd.isna(mom.loc["A", mar]))


class TestPeriodResampling(unittest.TestCase):
    def setUp(self):
        rows = []
        for i, m in enumerate(pd.date_range("2025-01-01", periods=6, freq="MS")):
            rows.append({"month": m, "entity": "A", "entity_type": "bank", "instrument": "card",
                         "metric": "purchase_txn_value", "value": 10.0, "unit": "inr_thousand"})
            rows.append({"month": m, "entity": "A", "entity_type": "bank", "instrument": "card",
                         "metric": "outstanding_count", "value": 100 + i, "unit": "count"})
        self.df = _facts(rows)

    def test_quarterly_sums_flow_metric(self):
        wide = get_metric_wide(self.df, "purchase_txn_value", period="Q")
        q1 = pd.Timestamp("2025-03-31")
        self.assertEqual(wide.loc["A", q1], 30.0)  # 3 months x 10

    def test_quarterly_takes_last_for_stock_metric(self):
        wide = get_metric_wide(self.df, "outstanding_count", period="Q")
        q1 = pd.Timestamp("2025-03-31")
        self.assertEqual(wide.loc["A", q1], 102)  # last reading in the quarter, not a sum

    def test_industry_total_matches_get_metric_wide(self):
        month = pd.Timestamp("2025-01-01")
        self.assertEqual(industry_total(self.df, "purchase_txn_value", month), 10.0)


def _txn(month, entity, value, entity_type="non_bank"):
    return {"month": month, "entity": entity, "entity_type": entity_type, "instrument": "wallet",
            "metric": "purchase_txn_value", "value": float(value), "unit": "inr_thousand"}


class TestHHI(unittest.TestCase):
    def test_monopoly_and_even_split(self):
        # Jan: one issuer has everything (HHI 10000). Feb: four equal issuers (4 * 25^2 = 2500).
        rows = [_txn("2025-01-01", "A", 100)]
        rows += [_txn("2025-01-01", e, 0) for e in "BCD"]
        rows += [_txn("2025-02-01", e, 25) for e in "ABCD"]
        h = hhi(_facts(rows), "purchase_txn_value")
        self.assertAlmostEqual(h.loc[pd.Timestamp("2025-01-01"), "all"], 10000.0)
        self.assertAlmostEqual(h.loc[pd.Timestamp("2025-02-01"), "all"], 2500.0)

    def test_rejects_ratio_metric(self):
        self.assertFalse(is_additive("active_ratio_pct"))
        with self.assertRaises(ValueError):
            hhi(_facts([_txn("2025-01-01", "A", 1)]), "active_ratio_pct")


class TestTrailingGrowth(unittest.TestCase):
    def test_growth_over_window_not_last_step(self):
        # A doubles over 3 months but is flat in the final step — only a trailing window sees it.
        rows = [_txn("2025-01-01", "A", 100), _txn("2025-02-01", "A", 200),
                _txn("2025-03-01", "A", 200)]
        tg = trailing_growth(_facts(rows), "purchase_txn_value", pd.Timestamp("2025-03-01"), 2)
        self.assertAlmostEqual(tg.loc[0, "growth_pct"], 100.0)

    def test_materiality_filters_tiny_base(self):
        rows = [_txn("2025-01-01", "A", 1), _txn("2025-02-01", "A", 50)]
        f = _facts(rows)
        self.assertTrue(trailing_growth(f, "purchase_txn_value", pd.Timestamp("2025-02-01"), 1,
                                         materiality=10).empty)
        self.assertFalse(trailing_growth(f, "purchase_txn_value", pd.Timestamp("2025-02-01"), 1,
                                          materiality=0).empty)


class TestMoversSegmentFilter(unittest.TestCase):
    """Gainers/losers must respect the Bank/Non-Bank segment filter, not just instrument/period —
    otherwise a bank-only view can leak a fintech's move into the list."""

    def test_segment_filter_excludes_other_segment(self):
        rows = [_txn("2025-01-01", "Bank1", 100, entity_type="bank"),
                _txn("2025-02-01", "Bank1", 110, entity_type="bank"),
                _txn("2025-01-01", "Fintech1", 100, entity_type="non_bank"),
                _txn("2025-02-01", "Fintech1", 900, entity_type="non_bank")]
        f = _facts(rows)
        all_mv = movers(f, "purchase_txn_value", pd.Timestamp("2025-02-01"), materiality=0)
        bank_mv = movers(f, "purchase_txn_value", pd.Timestamp("2025-02-01"), materiality=0, entity_type="bank")
        self.assertIn("Fintech1", all_mv["gainers_pct"]["entity"].values)
        self.assertNotIn("Fintech1", bank_mv["gainers_pct"]["entity"].values)
        self.assertIn("Bank1", bank_mv["gainers_pct"]["entity"].values)


class TestShareOfGrowth(unittest.TestCase):
    def test_contributions_sum_to_100(self):
        rows = [_txn("2025-01-01", "A", 100), _txn("2025-01-01", "B", 100), _txn("2025-01-01", "C", 100),
                _txn("2025-02-01", "A", 160), _txn("2025-02-01", "B", 90), _txn("2025-02-01", "C", 100)]
        sog = share_of_growth(_facts(rows), "purchase_txn_value", pd.Timestamp("2025-02-01"), top_n=99)
        self.assertAlmostEqual(sog["contribution_pct"].sum(), 100.0)

    def test_counter_trend_entity_is_negative(self):
        """Segment shrinks overall; the one grower must read as a negative contribution."""
        rows = [_txn("2025-01-01", "A", 100), _txn("2025-01-01", "B", 100),
                _txn("2025-02-01", "A", 120), _txn("2025-02-01", "B", 50)]
        sog = share_of_growth(_facts(rows), "purchase_txn_value", pd.Timestamp("2025-02-01"), top_n=99)
        by_entity = sog.set_index("entity")["contribution_pct"]
        self.assertLess(by_entity["A"], 0)
        self.assertGreater(by_entity["B"], 0)


class TestEntryExit(unittest.TestCase):
    """The rename case is the one that matters: RBI relabels an issuer mid-window and a
    naive reading reports a fake exit plus a fake new entrant for the same company."""

    def setUp(self):
        months = pd.date_range("2025-01-01", periods=4, freq="MS")
        rows = []
        for m in months:
            rows.append(_txn(m, "Steady Bank Limited", 100, "bank"))
        # Old name trades months 1-2, new name (annotated "formerly") takes over months 3-4.
        rows += [_txn(m, "Oldco Private Limited", 50) for m in months[:2]]
        rows += [_txn(m, "Newco Limited (formerly Oldco Private Limited)", 50) for m in months[2:]]
        # A genuine new entrant, no formerly-clause.
        rows += [_txn(m, "Genuine Newcomer Limited", 10) for m in months[2:]]
        self.df = _facts(rows)

    def test_rename_pair_detected_and_excluded(self):
        ee = entry_exit(self.df, metric_key="purchase_txn_value")
        renames = ee["renames"]
        self.assertEqual(len(renames), 1)
        self.assertEqual(renames.loc[0, "former_name"], "Oldco Private Limited")
        self.assertNotIn("Newco Limited (formerly Oldco Private Limited)", set(ee["entrants"]["entity"]))
        self.assertNotIn("Oldco Private Limited", set(ee["exits"]["entity"]))

    def test_genuine_entrant_still_reported(self):
        ee = entry_exit(self.df, metric_key="purchase_txn_value")
        self.assertIn("Genuine Newcomer Limited", set(ee["entrants"]["entity"]))

    def test_entity_present_throughout_is_neither(self):
        ee = entry_exit(self.df, metric_key="purchase_txn_value")
        self.assertNotIn("Steady Bank Limited", set(ee["entrants"]["entity"]))
        self.assertNotIn("Steady Bank Limited", set(ee["exits"]["entity"]))


class TestPeerBenchmark(unittest.TestCase):
    def test_percentile_and_growth_gap(self):
        rows = []
        for m in ("2025-01-01", "2025-02-01"):
            rows += [_txn(m, "Small", 10), _txn(m, "Mid", 50)]
        rows += [_txn("2025-01-01", "Big", 100), _txn("2025-02-01", "Big", 200)]  # Big doubles
        pb = peer_benchmark(_facts(rows), "Big", "purchase_txn_value", pd.Timestamp("2025-02-01"))
        self.assertEqual(pb["n_peers"], 3)
        self.assertAlmostEqual(pb["percentile"], 200 / 3)  # larger than 2 of 3 (itself included)
        self.assertAlmostEqual(pb["growth_pct"], 100.0)
        self.assertAlmostEqual(pb["peer_median_growth_pct"], 0.0)  # Small/Mid flat

    def test_unknown_entity_returns_empty(self):
        rows = [_txn("2025-01-01", "A", 10)]
        self.assertEqual(peer_benchmark(_facts(rows), "Nope", "purchase_txn_value", pd.Timestamp("2025-01-01")), {})


class TestFiscalPeriods(unittest.TestCase):
    """Indian FY runs Apr-Mar. FY26 = Apr 2025 - Mar 2026, and its Q1 is Apr-Jun 2025."""

    def setUp(self):
        rows = [_txn(m, "A", 10) for m in pd.date_range("2025-04-01", periods=15, freq="MS")]
        self.df = _facts(rows)

    def test_fy_quarter_labels(self):
        self.assertEqual(period_label(pd.Timestamp("2025-06-30"), "QF"), "Q1 FY26")
        self.assertEqual(period_label(pd.Timestamp("2025-09-30"), "QF"), "Q2 FY26")
        self.assertEqual(period_label(pd.Timestamp("2025-12-31"), "QF"), "Q3 FY26")
        self.assertEqual(period_label(pd.Timestamp("2026-03-31"), "QF"), "Q4 FY26")
        self.assertEqual(period_label(pd.Timestamp("2026-06-30"), "QF"), "Q1 FY27")

    def test_calendar_quarter_labels_differ_from_fy(self):
        ts = pd.Timestamp("2026-06-30")
        self.assertEqual(period_label(ts, "Q"), "Q2 2026")
        self.assertEqual(period_label(ts, "QF"), "Q1 FY27")

    def test_fy_and_calendar_annual_have_different_windows(self):
        fy = get_metric_wide(self.df, "purchase_txn_value", period="AF")
        cal = get_metric_wide(self.df, "purchase_txn_value", period="A")
        # Apr2025-Mar2026 is a full 12 months of the 15 loaded -> 120. Calendar 2025 only
        # holds Apr-Dec (9 months) -> 90. Same data, different buckets.
        self.assertAlmostEqual(fy.loc["A", pd.Timestamp("2026-03-31")], 120.0)
        self.assertAlmostEqual(cal.loc["A", pd.Timestamp("2025-12-31")], 90.0)

    def test_fy_halves(self):
        self.assertEqual(period_label(pd.Timestamp("2025-09-30"), "H"), "H1 FY26")
        self.assertEqual(period_label(pd.Timestamp("2026-03-31"), "H"), "H2 FY26")


class TestPartialPeriods(unittest.TestCase):
    """Data ending mid-year leaves a part-covered bucket. Comparing that stub against a full
    period reads as a collapse that is purely an artefact of the window."""

    def setUp(self):
        rows = [_txn(m, "A", 10) for m in pd.date_range("2025-04-01", periods=15, freq="MS")]
        self.df = _facts(rows)

    def test_complete_vs_partial_fy(self):
        cov = period_coverage(self.df, "AF")
        self.assertTrue(cov.loc[pd.Timestamp("2026-03-31"), "is_complete"])       # Apr25-Mar26, 12 months
        self.assertFalse(cov.loc[pd.Timestamp("2027-03-31"), "is_complete"])      # Apr-Jun26 only, 3 months
        self.assertEqual(cov.loc[pd.Timestamp("2027-03-31"), "months_present"], 3)

    def test_is_partial_period(self):
        self.assertFalse(is_partial_period(self.df, pd.Timestamp("2026-03-31"), "AF"))
        self.assertTrue(is_partial_period(self.df, pd.Timestamp("2027-03-31"), "AF"))

    def test_partial_period_suppresses_misleading_delta(self):
        """The stub FY must not emit a '-71% YoY' style insight built on unequal windows."""
        got = generate_entity_insights(self.df, "A", pd.Timestamp("2027-03-31"), period="AF")
        self.assertEqual(len(got), 1)
        self.assertIn("only 3 of 12 months", got[0]["text"])
        self.assertNotIn("%", got[0]["text"].split("implication")[0].replace("100%", ""))


class TestEntityInsightsArePeriodSensitive(unittest.TestCase):
    def setUp(self):
        # Rising monthly series so monthly and quarterly deltas genuinely differ.
        rows = [_txn(m, "A", 10 + i) for i, m in enumerate(pd.date_range("2025-04-01", periods=15, freq="MS"))]
        rows += [_txn(m, "B", 100) for m in pd.date_range("2025-04-01", periods=15, freq="MS")]
        self.df = _facts(rows)

    def test_monthly_and_quarterly_insights_differ(self):
        monthly = generate_entity_insights(self.df, "A", pd.Timestamp("2026-06-01"), period="M")
        quarterly = generate_entity_insights(self.df, "A", pd.Timestamp("2026-06-30"), period="QF")
        self.assertTrue(monthly and quarterly)
        self.assertNotEqual(monthly[0]["text"], quarterly[0]["text"])
        self.assertIn("MoM", monthly[0]["text"])
        self.assertIn("QoQ", quarterly[0]["text"])

    def test_single_period_history_explains_itself(self):
        got = generate_entity_insights(self.df, "A", pd.Timestamp("2026-03-31"), period="AF")
        self.assertEqual(len(got), 1)
        self.assertIn("nothing to compare", got[0]["text"])

    def test_scorecard_respects_selected_period(self):
        """Pinned to the first FY there is no prior period to compare, so the scorecard must
        be empty rather than quietly reporting the newest (part-period) bucket instead."""
        self.assertTrue(scorecard(self.df, "A", period="AF", month=pd.Timestamp("2026-03-31")).empty)
        self.assertFalse(scorecard(self.df, "A", period="AF", month=pd.Timestamp("2027-03-31")).empty)


class TestDetectRenames(unittest.TestCase):
    """Renames must fold into one continuous series — a company whose history splits at the
    rename month breaks every trend, rank and YoY built on top of it."""

    def test_maps_former_to_current(self):
        got = detect_renames([
            "PhonePe Limited (formerly PhonePe Private Limited)",
            "Phonepe Private Limited (formerly FX Mart Pvt. Ltd.)",
            "Unrelated Bank Limited",
        ])
        self.assertEqual(got, {"Phonepe Private Limited (formerly FX Mart Pvt. Ltd.)":
                               "PhonePe Limited (formerly PhonePe Private Limited)"})

    def test_ignores_formerly_clause_with_no_matching_entity(self):
        self.assertEqual(detect_renames(["Newco Limited (formerly Ghostco Limited)", "Other Limited"]), {})

    def test_collapses_rename_chain(self):
        got = detect_renames(["A Limited", "B Limited (formerly A Limited)", "C Limited (formerly B Limited)"])
        self.assertEqual(got["A Limited"], "C Limited (formerly B Limited)")
        self.assertEqual(got["B Limited (formerly A Limited)"], "C Limited (formerly B Limited)")

    def test_merging_produces_continuous_history(self):
        months = pd.date_range("2025-01-01", periods=4, freq="MS")
        rows = [_txn(m, "Oldco Limited", 10) for m in months[:2]]
        rows += [_txn(m, "Newco Limited (formerly Oldco Limited)", 10) for m in months[2:]]
        df = _facts(rows)
        before = get_metric_wide(df, "purchase_txn_value")
        # Two separate rows, each only trading for half the window (gaps are filled with 0).
        self.assertEqual(len(before), 2)
        self.assertEqual((before.loc["Oldco Limited"] > 0).sum(), 2)

        df["entity"] = df["entity"].replace(detect_renames(df["entity"].unique()))
        after = get_metric_wide(df, "purchase_txn_value")
        self.assertEqual(len(after), 1)
        self.assertEqual((after.iloc[0] > 0).sum(), 4)  # one unbroken 4-month series


class TestSpanLabels(unittest.TestCase):
    def test_spans_name_the_months_covered(self):
        self.assertEqual(span_label(pd.Timestamp("2026-06-30"), "QF"), "Apr–Jun 2026")
        self.assertEqual(span_label(pd.Timestamp("2026-03-31"), "AF"), "Apr 2025–Mar 2026")
        self.assertEqual(span_label(pd.Timestamp("2026-06-01"), "M"), "Jun 2026")

    def test_span_clipped_to_loaded_data(self):
        df = _facts([_txn(m, "A", 1) for m in pd.date_range("2025-04-01", periods=3, freq="MS")])
        # The calendar-2025 bucket spans Jan-Dec, but only Apr onward was ever loaded.
        self.assertEqual(span_label(pd.Timestamp("2025-12-31"), "A", df), "Apr–Dec 2025")


class TestCompareInsights(unittest.TestCase):
    def setUp(self):
        rows = []
        for m in pd.date_range("2025-01-01", periods=3, freq="MS"):
            rows += [_txn(m, "Big", 1000), _txn(m, "Small", 10), _txn(m, "Other", 500)]
        rows.append(_txn("2025-03-01", "Big", 500))  # Big grows in the final month
        self.df = _facts(rows)

    def test_needs_at_least_two_entities(self):
        self.assertEqual(generate_compare_insights(self.df, ["Big"], "purchase_txn_value",
                                                    pd.Timestamp("2025-03-01")), [])

    def test_identifies_leader_and_combined_share(self):
        got = generate_compare_insights(self.df, ["Big", "Small"], "purchase_txn_value",
                                         pd.Timestamp("2025-03-01"))
        self.assertTrue(got)
        self.assertIn("Big", got[0]["text"])
        self.assertTrue(any("% of all-issuer" in i["text"] for i in got))


class TestTxnTypeFilter(unittest.TestCase):
    """P2M / P2P / cash come straight from RBI's own column definitions, so the three slices
    must partition the total exactly — any drift means a metric was mapped to the wrong bucket."""

    def setUp(self):
        def row(metric, value):
            return {"month": "2025-01-01", "entity": "A", "entity_type": "non_bank",
                    "instrument": "wallet", "metric": metric, "value": float(value), "unit": "x"}
        self.df = _facts([
            row("purchase_txn_value", 700),
            row("fund_transfer_txn_value", 250),
            row("cash_withdrawal_atm_txn_value", 30),
            row("cash_withdrawal_pos_txn_value", 20),
            row("outstanding_count", 5000),
        ])
        self.month = pd.Timestamp("2025-01-01")

    def test_slices_partition_the_total(self):
        total = industry_total(self.df, "total_txn_value", self.month)
        parts = sum(industry_total(filter_txn_type(self.df, t), "total_txn_value", self.month)
                    for t in ("p2m", "p2p", "cash"))
        self.assertAlmostEqual(total, 1000.0)
        self.assertAlmostEqual(parts, total)

    def test_each_slice_picks_the_right_rows(self):
        for txn_type, expected in [("p2m", 700.0), ("p2p", 250.0), ("cash", 50.0)]:
            got = industry_total(filter_txn_type(self.df, txn_type), "total_txn_value", self.month)
            self.assertAlmostEqual(got, expected, msg=txn_type)

    def test_instrument_counts_survive_the_filter(self):
        """A PPI isn't specific to a transaction purpose, so counts must not be filtered away."""
        for txn_type in ("all", "p2m", "p2p", "cash"):
            got = industry_total(filter_txn_type(self.df, txn_type), "outstanding_count", self.month)
            self.assertAlmostEqual(got, 5000.0, msg=txn_type)

    def test_p2m_share_matches_the_filter(self):
        share = get_metric_wide(self.df, "p2m_share_pct").loc["A", self.month]
        self.assertAlmostEqual(share, 70.0)

    def test_p2p_share_matches_the_filter(self):
        share = get_metric_wide(self.df, "p2p_share_pct").loc["A", self.month]
        self.assertAlmostEqual(share, 25.0)

    def test_p2m_and_p2p_share_sum_with_cash_reliance_to_100(self):
        # The three MIX_ONLY_METRICS shares partition the same total the txn-type filter does,
        # so they must sum to 100 exactly — same invariant as test_slices_partition_the_total.
        p2m = get_metric_wide(self.df, "p2m_share_pct").loc["A", self.month]
        p2p = get_metric_wide(self.df, "p2p_share_pct").loc["A", self.month]
        cash = get_metric_wide(self.df, "cash_reliance_pct").loc["A", self.month]
        self.assertAlmostEqual(p2m + p2p + cash, 100.0)

    def test_metric_list_drops_degenerate_options(self):
        under_p2m = selectable_metrics("p2m")
        # Would be a duplicate of the total, empty, or a guaranteed 0%/100% under this filter.
        for key in ("purchase_txn_value", "fund_transfer_txn_value", "cash_reliance_pct", "p2m_share_pct", "p2p_share_pct"):
            self.assertNotIn(key, under_p2m)
        self.assertIn("total_txn_value", under_p2m)
        self.assertIn("outstanding_count", under_p2m)
        self.assertIn("cash_reliance_pct", selectable_metrics("all"))
        self.assertIn("p2p_share_pct", selectable_metrics("all"))


class TestHHIReading(unittest.TestCase):
    def test_bands(self):
        self.assertIn("unconcentrated", hhi_reading(729))
        self.assertIn("moderately", hhi_reading(1913))
        self.assertIn("highly", hhi_reading(3000))


if __name__ == "__main__":
    unittest.main()
