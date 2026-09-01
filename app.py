import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from ppi.analytics import (
    ALL_METRIC_LABELS, DERIVED_METRICS, METRIC_GROUPS, METRIC_LABELS, PERIOD_NOUN, PERIODS,
    SHORT_LABELS, TXN_TYPES, _FORMERLY_DISPLAY_RE, entity_instrument_split, entity_timeseries, entry_exit,
    filter_txn_type, generate_compare_insights, generate_entity_insights, get_metric_wide, hhi,
    hhi_reading, industry_total, instrument_mix, is_additive, is_partial_period, generate_insights,
    load_facts, movers, peer_benchmark, period_coverage, period_label, rank_series,
    rankings, scorecard, selectable_metrics, share_of_growth, span_label, trailing_growth,
)
from ppi.config import DEFAULT_METRIC, MATERIALITY_THRESHOLD, TOP_N
from ppi.load import get_conn
from ppi.visitor_log import log_visitor

st.set_page_config(page_title="RBI Entity-wise PPI Statistics", layout="wide")

if not st.session_state.get("visitor_verified"):
    st.title("RBI Entity-wise PPI Statistics")
    st.caption("Quick intro before you dive in — takes 5 seconds.")
    with st.form("visitor_gate"):
        name = st.text_input("Your name")
        linkedin_url = st.text_input("Your LinkedIn profile URL")
        submitted = st.form_submit_button("Continue to dashboard")
    if submitted:
        if not name.strip() or not linkedin_url.strip():
            st.warning("Both fields are required.")
        else:
            log_visitor(name.strip(), linkedin_url.strip())
            st.session_state["visitor_verified"] = True
            st.rerun()
    st.stop()

INSTRUMENT_ICON = {"card": "💳", "wallet": "👛", "both": "💳👛", "neither": "—"}
INSTRUMENT_LABEL = {"card": "Cards only", "wallet": "Wallets only", "both": "Cards & Wallets", "neither": "No activity"}


def display_name(name: str) -> str:
    """Strip '(formerly ...)' clauses for display. The underlying `entity` string used
    for data joins/selection is never touched — this is a rendering-time transform only."""
    return _FORMERLY_DISPLAY_RE.sub("", name).strip()


@st.cache_data(ttl=3600)
def _load() -> pd.DataFrame:
    conn = get_conn()
    df = load_facts(conn)
    conn.close()
    return df


def fmt_value(v: float, unit_hint: str) -> str:
    if pd.isna(v):
        return "—"
    if unit_hint.endswith("_pct"):
        return f"{v:.1f}%"
    if unit_hint.startswith("avg_txn_value"):
        return f"₹{v:,.0f}"
    if unit_hint == "txns_per_ppi":
        return f"{v:.2f}"
    if "value" in unit_hint:
        cr = v / 1e5  # value stored in ₹'000 -> Cr = /100000
        return f"₹{cr:,.1f} Cr" if abs(cr) >= 1 else f"₹{v:,.0f} K"
    return f"{v:,.0f}"


def fmt_pct(v: float) -> str:
    if pd.isna(v):
        return "—"
    if v in (float("inf"), float("-inf")):
        return "▲ new" if v > 0 else "▼ new"
    arrow = "▲" if v >= 0 else "▼"
    return f"{arrow} {abs(v):.1f}%"


PERIOD_DTICK = {"M": "M1", "Q": "M3", "QF": "M3", "H": "M6", "A": "M12", "AF": "M12"}


def style_time_axis(fig: go.Figure, period: str = "M") -> go.Figure:
    """Plotly's date axis auto-picks tick spacing from the plot's pixel width, which can
    (a) generate sub-second ticks on a narrow/near-empty range (e.g. a single-instrument
    split chart), and (b) — worse — interpolate extra monthly ticks between two Annual/
    Semi-Annual data points that don't actually exist at that granularity. Pinning both
    the tick text and the tick spacing to the selected period fixes both."""
    fig.update_xaxes(tickformat="%b %Y", dtick=PERIOD_DTICK[period])
    return fig


def instrument_badge(entity: str, mix: pd.Series) -> str:
    kind = mix.get(entity, "neither")
    return f"{INSTRUMENT_ICON[kind]} {INSTRUMENT_LABEL[kind]}"


def render_insights(items: list[dict], title: str, caption: str = "", expanded: bool = False) -> None:
    """Insights as a collapsed expander of plain text — no charts. They're read as prose,
    and a column of sparklines competed with the sentence rather than adding to it."""
    if not items:
        return
    with st.expander(f"{title}  ({len(items)})", expanded=expanded):
        if caption:
            st.caption(caption)
        for ins in items:
            text = ins["text"]
            if ins.get("entity"):
                text = text.replace(ins["entity"], display_name(ins["entity"]))
            color = "green" if ins["direction"] == "up" else "red"
            st.markdown(f":{color}[{'▲' if ins['direction'] == 'up' else '▼'}] {text}")


df = _load()
if df.empty:
    st.error("No data loaded. Run `python -m ppi.refresh` first.")
    st.stop()

mix = instrument_mix(df)  # computed before any filtering: what an issuer *offers* doesn't
                          # change because you're looking at one transaction type

if st.sidebar.button("🔄 Reload data", help="Data is cached for 1 hour. Click after running `python -m ppi.refresh` to see new data immediately."):
    _load.clear()
    st.rerun()

st.sidebar.header("Filters")
txn_type = st.sidebar.selectbox(
    "Transaction type", list(TXN_TYPES), format_func=lambda t: TXN_TYPES[t][0],
    help="P2M = paying a merchant (RBI's 'purchase of goods and services', in-store or online). "
         "P2P = RBI's 'fund transfer' bucket. Cash withdrawals are counted separately as neither.")
# One filter at the source: every view below reads the filtered frame, so rankings, movers,
# concentration, insights and exports are all consistently scoped without extra plumbing.
df = filter_txn_type(df, txn_type)
ALL_METRICS = selectable_metrics(txn_type)

metric_group = st.sidebar.selectbox("Metric", ALL_METRICS, format_func=lambda m: ALL_METRIC_LABELS[m],
                                     index=ALL_METRICS.index(DEFAULT_METRIC))
instrument = st.sidebar.selectbox("Instrument", ["all", "card", "wallet"],
                                  format_func=lambda x: {"all": "All", "card": "Cards", "wallet": "Wallets"}[x])
period_base = st.sidebar.selectbox(
    "Period", ["M", "Q", "H", "A"], index=0,
    format_func=lambda p: {"M": "Monthly", "Q": "Quarterly", "H": "Semi-Annual", "A": "Annual"}[p],
    help="Aggregates transactions over the chosen window. Outstanding/active PPI counts use the period's latest reading, not a sum.")

# Indian FY runs Apr-Mar. Quarter boundaries are identical either way (only the naming
# differs), annual boundaries genuinely differ, and semi-annual only exists as FY halves.
if period_base in ("Q", "A"):
    basis = st.sidebar.radio("Year basis", ["F", ""], horizontal=True, key=f"basis_{period_base}",
                             format_func=lambda b: "Financial year (Apr–Mar)" if b == "F" else "Calendar year")
    period = period_base + basis
else:
    period = period_base
    if period_base == "H":
        st.sidebar.caption("Halves follow the financial year: H1 = Apr–Sep, H2 = Oct–Mar.")
delta_label = PERIODS[period][3]

period_cols = sorted(get_metric_wide(df, DEFAULT_METRIC, period=period).columns)
# Default to the newest *complete* period: with data ending mid-year, the last FY bucket is a
# part-year stub whose totals and deltas would read as a collapse (see period_coverage()).
_complete = [c for c in period_cols if period_coverage(df, period).loc[c, "is_complete"]]
month = st.sidebar.select_slider("Period ending", options=period_cols,
                                  value=(_complete or period_cols)[-1],
                                  format_func=lambda ts: period_label(ts, period), key=f"period_slider_{period}")
materiality = st.sidebar.number_input(f"Materiality threshold (prior-{PERIOD_NOUN[period]} base)",
                                       value=float(MATERIALITY_THRESHOLD), min_value=0.0)
top_n = st.sidebar.slider("Top N", 5, 20, TOP_N)

page = st.sidebar.radio("Page", ["Overview", "Company Explorer", "Compare"])

st.sidebar.divider()
st.sidebar.download_button(
    "⬇ Download full dataset (CSV)",
    df.to_csv(index=False).encode("utf-8"),
    file_name="rbi_ppi_full_dataset.csv",
    mime="text/csv",
    help=f"Every entity, month and metric currently loaded ({len(df):,} rows) — the tidy long-format table "
         "behind every page, for plugging into your own model.",
)

unit_hint = metric_group  # "value" or "volume" or "count" is embedded in the name

st.title("RBI Entity-wise PPI Statistics")

# Spell out exactly which window is being measured and what it's measured against, so a
# "+2.6% QoQ" is never left ambiguous about which two windows produced it.
def labelled_span(ts: pd.Timestamp) -> str:
    """'Q1 FY27 (Apr–Jun 2026)' — but just 'Jun 2026' for Monthly, where the period label
    already *is* the span and repeating it reads as a mistake."""
    label, span = period_label(ts, period), span_label(ts, period, df)
    return f"**{label}**" if label == span else f"**{label}** ({span})"


_prior_cols = [c for c in period_cols if c < month]
if _prior_cols:
    comparison_caption = (f"{delta_label} compares {labelled_span(month)} against "
                          f"{labelled_span(max(_prior_cols))}.")
else:
    comparison_caption = (f"Showing {labelled_span(month)}. No earlier period is loaded, "
                          f"so there is nothing to compare against.")

partial = is_partial_period(df, month, period)
if partial:
    _cov = period_coverage(df, period).loc[month]
    st.warning(f"**{period_label(month, period)} is incomplete** — {int(_cov['months_present'])} of "
               f"{int(_cov['months_expected'])} months loaded ({span_label(month, period, df)}). Totals are "
               f"part-period and {delta_label} comparisons against a full period are not like-for-like, "
               f"so they're suppressed.")

st.caption(comparison_caption)

if txn_type != "all":
    _detail = {
        "p2m": "RBI's *purchase of goods and services* columns — payments to a merchant, "
               "at a PoS terminal or online.",
        "p2p": "RBI's *fund transfer* columns. Worth knowing: that bucket also contains "
               "PPI-to-own-bank-account transfers, so treat this as an **upper bound** on "
               "true person-to-person volume rather than a clean P2P figure.",
        "cash": "cash withdrawals at ATMs and at PoS — money leaving the PPI as cash, "
                "which is neither a merchant payment nor a transfer.",
    }[txn_type]
    st.info(f"**Filtered to {TXN_TYPES[txn_type][0]}.** Transaction figures below count only "
            f"{_detail} PPI outstanding/active counts are unaffected — an instrument isn't "
            f"specific to one transaction purpose.")

if page == "Overview":
    st.caption(f"Metric: **{ALL_METRIC_LABELS[metric_group]}**"
               f"{' — latest period, may still be revised' if month == period_cols[-1] and not partial else ''}")

    wide = get_metric_wide(df, metric_group, instrument, period)
    if month in wide.columns:
        cur_total = industry_total(df, metric_group, month, instrument, period)
        prev_cols = wide.columns[wide.columns < month]
        delta_pct = float("nan")
        if len(prev_cols):
            prev_total = industry_total(df, metric_group, prev_cols.max(), instrument, period)
            delta_pct = (cur_total - prev_total) / prev_total * 100 if prev_total else float("nan")
        c1, c2 = st.columns(2)
        c1.metric(f"Industry {ALL_METRIC_LABELS[metric_group]}", fmt_value(cur_total, unit_hint), f"{delta_label} {fmt_pct(delta_pct)}")
        c2.metric("Entities reporting", f"{wide[month].notna().sum()}")

    tab_rank, tab_growth, tab_structure = st.tabs(["Rankings & Key Changes", "Movers & Growth", "Market Structure"])

    with tab_rank:
      ranking_exports = []
      rank_seg = st.selectbox("Segment", ["bank", "non_bank"], format_func=lambda s: "Top Banks" if s == "bank" else "Top Non-Banks (Fintechs)")
      for etype, title in [("bank", "Top Banks"), ("non_bank", "Top Non-Banks (Fintechs)")]:
            r = rankings(df, metric_group, month, entity_type=etype, instrument=instrument, top_n=top_n, period=period)
            # Export carries raw numbers plus the filter context, so the file still makes
            # sense once it's detached from the screen that produced it, for both segments
            # regardless of which one is on screen.
            if not r.empty:
                raw = r.copy()
                raw.insert(0, "segment", "Bank" if etype == "bank" else "Non-Bank")
                raw.insert(1, "period", period_label(month, period))
                raw.insert(2, "period_months", span_label(month, period, df))
                raw.insert(3, "metric", ALL_METRIC_LABELS[metric_group])
                raw.insert(4, "instrument", instrument)
                raw["instruments_issued"] = raw["entity"].map(lambda e: INSTRUMENT_LABEL[mix.get(e, "neither")])
                ranking_exports.append(raw.rename(columns={"mom_pct": f"{delta_label}_pct"}))

            if etype != rank_seg:
                continue
            st.subheader(title)
            if r.empty:
                st.info("No data for this period/filter.")
                continue
            show = r.copy()
            show["Entity"] = show["entity"].apply(lambda e: f"{INSTRUMENT_ICON[mix.get(e, 'neither')]} {display_name(e)}")
            show["Value"] = show["value"].apply(lambda v: fmt_value(v, unit_hint))
            show[delta_label] = show["mom_pct"].apply(fmt_pct)
            show["Share"] = show.apply(lambda r: f"{r['share_pct']:.1f}% ({r['share_delta_pp']:+.2f}pp)" if pd.notna(r["share_delta_pp"]) else f"{r['share_pct']:.1f}%", axis=1)
            st.dataframe(show[["Entity", "Value", delta_label, "Share"]], hide_index=True, use_container_width=True)
            st.caption("💳 Cards only · 👛 Wallets only · 💳👛 Both · **Share** shows (change vs previous period) in brackets.")

      if ranking_exports:
          st.download_button(
              "⬇ Download these rankings (CSV)",
              pd.concat(ranking_exports, ignore_index=True).to_csv(index=False).encode("utf-8"),
              file_name=f"rankings_{metric_group}_{instrument}_{period_label(month, period).replace(' ', '')}.csv",
              mime="text/csv",
              help="Both segments, with the metric / instrument / period filters currently applied.",
          )

      st.divider()
      insights = generate_insights(df, month, materiality=materiality, period=period)
      if not insights:
          st.info("Not enough history yet to generate insights for this period.")
      render_insights(insights, "🔑 Key changes this period", comparison_caption)

    with tab_growth:
        st.subheader("Biggest movers")
        st.caption(f"Change vs the immediately preceding {PERIOD_NOUN[period]}.")
        movers_seg = st.radio("Segment", ["all", "bank", "non_bank"], horizontal=True,
                              format_func=lambda s: {"all": "All", "bank": "Banks", "non_bank": "Non-Banks"}[s],
                              key="movers_segment")
        mv = movers(df, metric_group, month, instrument=instrument, materiality=materiality, top_n=top_n,
                    period=period, entity_type=movers_seg)
        mcol1, mcol2 = st.columns(2)
        for mcol, key, heading in [(mcol1, "gainers_pct", "Gainers"), (mcol2, "losers_pct", "Losers")]:
            with mcol:
                st.markdown(f"**{heading} (% {delta_label})**")
                mdf = mv[key].copy()
                if mdf.empty:
                    st.info(f"No material {heading.lower()}.")
                    continue
                mdf["Entity"] = mdf["entity"].apply(display_name)
                mdf[delta_label] = mdf["delta_pct"].apply(fmt_pct)
                mdf["Value"] = mdf["value"].apply(lambda v: fmt_value(v, unit_hint))
                st.dataframe(mdf[["Entity", "Value", delta_label]], hide_index=True, use_container_width=True)

        st.divider()
        st.subheader("Fastest growing over a trailing window")
        st.caption("Compounded growth across several periods, not a single-period snapshot — "
                   "this is the screening view for who has sustained momentum rather than had one good month.")
        max_back = max(1, len(period_cols) - 1)
        if max_back < 2:
            st.info(f"Need at least 2 {PERIOD_NOUN[period]}s of history to calculate trailing growth. Only {len(period_cols)} loaded.")
        else:
            default_back = min(6 if period == "M" else 4 if period == "Q" else 2, max_back)
            month_idx = period_cols.index(month)
            back_options = [b for b in range(1, max_back + 1) if month_idx - b >= 0]
            periods_back = st.select_slider(
                "Trailing window", options=back_options, value=default_back,
                format_func=lambda b: f"Since {period_label(period_cols[month_idx - b], period)}",
            )
            gcol1, gcol2 = st.columns(2)
            for gcol, etype, title in [(gcol1, "bank", "Banks"), (gcol2, "non_bank", "Non-Banks (Fintechs)")]:
                with gcol:
                    st.markdown(f"**{title}**")
                    tg = trailing_growth(df, metric_group, month, periods_back, entity_type=etype,
                                          instrument=instrument, top_n=top_n, materiality=materiality, period=period)
                    if tg.empty:
                        st.info("Not enough history for this window.")
                        continue
                    tg["Entity"] = tg["entity"].apply(display_name)
                    tg[f"Growth ({periods_back}{period.lower()})"] = tg["growth_pct"].apply(fmt_pct)
                    tg["Now"] = tg["value"].apply(lambda v: fmt_value(v, unit_hint))
                    tg["Then"] = tg["base_value"].apply(lambda v: fmt_value(v, unit_hint))
                    st.dataframe(tg[["Entity", "Then", "Now", f"Growth ({periods_back}{period.lower()})"]],
                                 hide_index=True, use_container_width=True)

    with tab_structure:
        structure_metric = metric_group if is_additive(metric_group) else DEFAULT_METRIC
        if structure_metric != metric_group:
            st.info(f"Market share isn't defined for a ratio metric ({ALL_METRIC_LABELS[metric_group]}), "
                    f"so this tab uses **{ALL_METRIC_LABELS[structure_metric]}**. Pick an additive metric in the sidebar to change it.")
        s_unit = structure_metric

        st.subheader("Market concentration (HHI)")
        st.caption("Herfindahl-Hirschman Index — the sum of every issuer's squared market share. "
                   "Rising means the market is consolidating toward a few winners; falling means it's fragmenting. "
                   "Standard reading: below 1500 unconcentrated, 1500–2500 moderate, above 2500 concentrated.")
        hhi_seg = st.radio("Segment", ["all", "bank", "non_bank"], horizontal=True,
                           format_func=lambda s: {"all": "All issuers", "bank": "Banks", "non_bank": "Non-Banks"}[s],
                           key="hhi_segment")
        h = hhi(df, structure_metric, instrument, period)
        h_cols = [hhi_seg] if hhi_seg != "all" else list(h.columns)
        h_plot = h[h_cols].rename(columns={"all": "All issuers", "bank": "Banks", "non_bank": "Non-Banks"})
        fig = px.line(h_plot, labels={"value": "HHI", "month": PERIODS[period][0], "variable": "Segment"})
        style_time_axis(fig, period)
        fig.add_hline(y=1500, line_dash="dot", line_color="gray", annotation_text="1500 — moderate")
        fig.add_hline(y=2500, line_dash="dot", line_color="gray", annotation_text="2500 — concentrated")
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)
        if month in h.index:
            seg_list = [("all", "All issuers"), ("bank", "Banks"), ("non_bank", "Non-Banks")] if hhi_seg == "all" else \
                       [(hhi_seg, {"bank": "Banks", "non_bank": "Non-Banks"}[hhi_seg])]
            hcols = st.columns(len(seg_list))
            for hcol, (seg, lbl) in zip(hcols, seg_list):
                prior = h.loc[h.index < month, seg]
                delta = h.loc[month, seg] - prior.iloc[-1] if len(prior) else float("nan")
                hcol.metric(lbl, f"{h.loc[month, seg]:,.0f}",
                            f"{delta:+,.0f} vs prior {PERIOD_NOUN[period]}" if pd.notna(delta) else "—")

            # The index alone means nothing to most readers — say what the number implies.
            bank_v, nb_v = h.loc[month, "bank"], h.loc[month, "non_bank"]
            trend = h.loc[h.index <= month, hhi_seg if hhi_seg != "all" else "all"]
            direction = ("broadly flat" if len(trend) < 2 or abs(trend.iloc[-1] - trend.iloc[0]) < 50
                         else "consolidating" if trend.iloc[-1] > trend.iloc[0] else "fragmenting")
            if hhi_seg == "all":
                st.info(
                    f"**What this means.** Banks sit at {bank_v:,.0f} — {hhi_reading(bank_v)}. "
                    f"Non-banks sit at {nb_v:,.0f} — {hhi_reading(nb_v)}. "
                    f"{'Bank' if bank_v > nb_v else 'Non-bank'} PPI volume is the more concentrated of the two. "
                    f"Across the loaded window the all-issuer index is **{direction}**. "
                    f"The all-issuer figure is lower than either segment because pooling banks and non-banks "
                    f"spreads the same volume across roughly twice as many issuers — compare segments to each "
                    f"other, not to the combined line."
                )
            else:
                seg_v = bank_v if hhi_seg == "bank" else nb_v
                seg_name = "Banks" if hhi_seg == "bank" else "Non-banks"
                st.info(
                    f"**What this means.** {seg_name} sit at {seg_v:,.0f} — {hhi_reading(seg_v)}. "
                    f"Across the loaded window this segment's index is **{direction}**."
                )

        st.divider()
        st.subheader("Who drove the change")
        st.caption("Each issuer's share of the segment's **net** change vs the previous period. "
                   "Contributions sum to 100%; a negative one means that issuer moved against the segment's direction "
                   "(grew while the segment shrank, or vice versa).")
        seg_choice = st.radio("Segment", ["non_bank", "bank", "all"], horizontal=True,
                              format_func=lambda s: {"non_bank": "Non-Banks (Fintechs)", "bank": "Banks", "all": "All"}[s])
        sog = share_of_growth(df, structure_metric, month, entity_type=seg_choice, instrument=instrument,
                               top_n=top_n, period=period)
        if sog.empty:
            st.info("No prior period to compare against.")
        else:
            sog["Entity"] = sog["entity"].apply(display_name)
            sog["Change"] = sog["delta_abs"].apply(lambda v: fmt_value(v, s_unit))
            sog["Share of net change"] = sog["contribution_pct"].apply(lambda v: f"{v:+.1f}%" if pd.notna(v) else "—")
            st.dataframe(sog[["Entity", "Change", "Share of net change"]], hide_index=True, use_container_width=True)

        st.divider()
        st.subheader("New entrants & exits")
        st.caption("Based on when an issuer's reported transaction value starts or stops within the loaded window. "
                   "Issuers active in the very first or last loaded period are excluded — we can't see past the window edge. "
                   "An 'exit' means activity fell to zero, which can be a licence action or wind-down rather than a shutdown.")
        ee = entry_exit(df, instrument=instrument, period=period)
        ecol1, ecol2 = st.columns(2)
        with ecol1:
            st.markdown("**New entrants**")
            ent = ee["entrants"]
            if ent.empty:
                st.info("None in this window.")
            else:
                st.dataframe(pd.DataFrame({"Entity": ent["entity"].apply(display_name),
                                            "First active": ent["first_active"].apply(lambda t: period_label(t, period))}),
                             hide_index=True, use_container_width=True)
        with ecol2:
            st.markdown("**Stopped reporting**")
            ex = ee["exits"]
            if ex.empty:
                st.info("None in this window.")
            else:
                st.dataframe(pd.DataFrame({"Entity": ex["entity"].apply(display_name),
                                            "Last active": ex["last_active"].apply(lambda t: period_label(t, period))}),
                             hide_index=True, use_container_width=True)

elif page == "Company Explorer":
    entities = sorted(df["entity"].unique())
    # Default to the largest issuer rather than the alphabetically-first one, which is a
    # zero-activity data artifact ("*Piramal...") and makes the page look broken on arrival.
    _latest = get_metric_wide(df, DEFAULT_METRIC, period=period)[month]
    _default = _latest.idxmax() if _latest.notna().any() else entities[0]
    entity = st.selectbox("Entity", entities, index=entities.index(_default), format_func=display_name)
    entity_type = df.loc[df["entity"] == entity, "entity_type"].iloc[0]
    ic1, ic2 = st.columns([1, 1])
    ic1.caption(f"Segment: **{'Bank' if entity_type == 'bank' else 'Non-Bank (Fintech)'}**")
    ic2.caption(f"Instruments issued: **{instrument_badge(entity, mix)}**")

    render_insights(generate_entity_insights(df, entity, month, period=period),
                    f"🔑 What changed for {display_name(entity)}", comparison_caption)

    sc = scorecard(df, entity, period=period, month=month)
    if not sc.empty:
        st.subheader("Scorecard: improving vs deteriorating")
        st.caption(f"vs previous {PERIOD_NOUN[period]}" + (" and vs the same period a year ago" if "yoy_pct" in sc.columns else ""))

        verdict_icon = {"improving": "🟢", "deteriorating": "🔴", "flat": "🟡", "n/a": "⚪"}
        counts = sc["delta_verdict"].value_counts()
        vc1, vc2, vc3 = st.columns(3)
        vc1.metric("🟢 Improving", int(counts.get("improving", 0)))
        vc2.metric("🔴 Deteriorating", int(counts.get("deteriorating", 0)))
        vc3.metric("🟡 Flat", int(counts.get("flat", 0)))

        table = pd.DataFrame({
            "Metric": sc.apply(lambda r: SHORT_LABELS.get(r["metric"], r["label"]), axis=1),
            "Latest": sc.apply(lambda r: fmt_value(r["latest"], r["metric"]), axis=1),
            delta_label: sc["delta_pct"].apply(fmt_pct),
            f"{delta_label} Verdict": sc["delta_verdict"].map(verdict_icon),
        })
        if "yoy_pct" in sc.columns:
            table["YoY"] = sc["yoy_pct"].apply(fmt_pct)
            table["YoY Verdict"] = sc["yoy_verdict"].map(verdict_icon)

        st.dataframe(table, hide_index=True, use_container_width=True, height=min(35 * (len(table) + 1), 700))

    pb = peer_benchmark(df, entity, metric_group, month, instrument=instrument, period=period)
    if pb:
        st.divider()
        st.subheader("How it compares to peers")
        seg_name = "banks" if entity_type == "bank" else "non-banks"
        st.caption(f"{ALL_METRIC_LABELS[metric_group]} for {period_label(month, period)}, against the other "
                   f"{pb['n_peers'] - 1} {seg_name}. The growth comparison is the one that matters: it separates "
                   f"riding the market from actually taking share.")
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("This entity", fmt_value(pb["value"], unit_hint))
        p2.metric(f"Median {seg_name[:-1]}", fmt_value(pb["median"], unit_hint))
        p3.metric("Percentile in segment", f"{pb['percentile']:.0f}th",
                  help="Percentage of peers this entity is larger than.")
        if pd.notna(pb["growth_pct"]) and pd.notna(pb["peer_median_growth_pct"]):
            gap = pb["growth_pct"] - pb["peer_median_growth_pct"]
            p4.metric(f"Growth vs median peer", f"{gap:+.1f}pp",
                      delta="outpacing peers" if gap > 0 else "lagging peers",
                      delta_color="normal" if gap > 0 else "inverse",
                      help=f"This entity {fmt_pct(pb['growth_pct'])} vs median peer {fmt_pct(pb['peer_median_growth_pct'])}.")
        else:
            p4.metric("Growth vs median peer", "—")

    st.divider()
    st.subheader("Time series across metrics")
    ts = entity_timeseries(df, entity, period=period)
    metric_pick = st.multiselect("Metrics to plot", ALL_METRICS, default=["total_txn_value", "outstanding_count"],
                                  format_func=lambda m: ALL_METRIC_LABELS[m])
    for m in metric_pick:
        if m not in ts.columns:
            continue
        fig = px.line(ts, y=m, labels={"value": ALL_METRIC_LABELS[m], "month": PERIODS[period][0]}, title=ALL_METRIC_LABELS[m])
        style_time_axis(fig, period)
        fig.update_layout(height=250, margin=dict(l=0, r=0, t=30, b=0))
        st.plotly_chart(fig, use_container_width=True)

    st.download_button(
        "Download this entity's full metric history (CSV)",
        ts.to_csv().encode("utf-8"),
        file_name=f"{display_name(entity)}_metrics.csv",
        mime="text/csv",
    )

    st.divider()
    st.subheader("Wallets vs Cards split")
    split_metric = st.selectbox("Metric for split", ["total_txn_value", "total_txn_volume"], format_func=lambda m: METRIC_LABELS[m])
    split = entity_instrument_split(df, entity, split_metric, period=period)
    if not split.empty:
        zero_cols = [c for c in ["card", "wallet"] if c in split.columns and split[c].sum() == 0]
        if zero_cols:
            only = "Wallets" if "card" in zero_cols else "Cards"
            missing = "Cards" if "card" in zero_cols else "Wallets"
            st.caption(f"ℹ️ {display_name(entity)} only issues {only} in this period — no {missing} activity, so the split shows a single band.")
        fig = px.area(split, labels={"value": METRIC_LABELS[split_metric], "month": PERIODS[period][0]})
        style_time_axis(fig, period)
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("Rank trend within segment")
    st.caption(f"{display_name(entity)}'s rank among all {'banks' if entity_type == 'bank' else 'non-banks'} by "
               f"{ALL_METRIC_LABELS[metric_group]} each period — rank 1 is the segment leader, so a line moving up the chart means gaining ground.")
    ranks = rank_series(df, metric_group, entity_type, instrument, period=period)
    if entity in ranks.index:
        rank_row = ranks.loc[entity].sort_index()
        fig = px.line(x=rank_row.index, y=rank_row.values, labels={"x": PERIODS[period][0], "y": "Rank (1 = highest)"})
        style_time_axis(fig, period)
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(height=250, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

elif page == "Compare":
    entities = sorted(df["entity"].unique())
    # Seed with the three largest issuers, not the alphabetically-first three (which are
    # near-zero artifacts and make the chart look empty on arrival).
    _latest = get_metric_wide(df, DEFAULT_METRIC, period=period)[month]
    _seed = _latest.nlargest(3).index.tolist() if _latest.notna().any() else entities[:3]
    selected = st.multiselect("Entities to compare", entities, default=_seed, format_func=display_name)
    cmp_metrics = st.multiselect("Metrics to compare", ALL_METRICS, default=[DEFAULT_METRIC],
                                  format_func=lambda m: ALL_METRIC_LABELS[m])
    if selected and cmp_metrics:
        render_insights(
            generate_compare_insights(df, selected, cmp_metrics[0], month, instrument=instrument, period=period),
            f"🔑 How these {len(selected)} compare",
            f"{comparison_caption} Measured on {ALL_METRIC_LABELS[cmp_metrics[0]]}"
            + (" (the first metric selected)." if len(cmp_metrics) > 1 else "."),
        )
        all_long = []
        for cmp_metric in cmp_metrics:
            wide = get_metric_wide(df, cmp_metric, instrument, period)
            present = [e for e in selected if e in wide.index]
            g = wide.loc[present].reset_index().melt(id_vars="entity", var_name="period_end", value_name="value")
            g["metric"] = cmp_metric
            g["Entity"] = g["entity"].apply(display_name)
            all_long.append(g)

            fig = px.line(g, x="period_end", y="value", color="Entity",
                          labels={"value": ALL_METRIC_LABELS[cmp_metric], "period_end": PERIODS[period][0]},
                          title=ALL_METRIC_LABELS[cmp_metric])
            style_time_axis(fig, period)
            fig.update_layout(height=350, margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig, use_container_width=True)

            st.caption(f"**{ALL_METRIC_LABELS[cmp_metric]} — by entity and period**")
            tbl = g[["Entity", "period_end", "value"]].copy()
            tbl["Period"] = tbl["period_end"].apply(lambda ts: period_label(ts, period))
            tbl["Value"] = tbl["value"].apply(lambda v: fmt_value(v, cmp_metric))
            tbl_pivot = tbl.pivot_table(index="Entity", columns="Period", values="Value", aggfunc="first")
            tbl_pivot = tbl_pivot[[c for c in tbl_pivot.columns if c in sorted(tbl["Period"].unique())]]
            st.dataframe(tbl_pivot, use_container_width=True)

        combined = pd.concat(all_long, ignore_index=True)
        st.download_button(
            "Download this comparison (CSV)",
            combined[["entity", "metric", "period_end", "value"]].to_csv(index=False).encode("utf-8"),
            file_name="compare.csv",
            mime="text/csv",
        )
    else:
        st.info("Select at least one entity and one metric to compare.")
