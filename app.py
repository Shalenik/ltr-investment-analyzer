"""
LTR Investment Analyzer — Streamlit Web App
Run with:  streamlit run app.py
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from calculator import LTRAssumptions, LTRResult, calculate_ltr, estimate_rent_1pct
from fetcher import (
    RateLimitError, batch_rent_estimates, fetch_listings, prescreen_listings,
    get_usage, set_plan_limit, get_plan_limit,
)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="LTR Investment Analyzer",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)

ENV_FILE = Path(__file__).parent / ".env"


def _load_env_key() -> str:
    """Load Rentcast API key from .env if it exists."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("RENTCAST_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("RENTCAST_API_KEY", "")


def _save_env_key(key: str) -> None:
    lines = []
    replaced = False
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("RENTCAST_API_KEY="):
                lines.append(f"RENTCAST_API_KEY={key}")
                replaced = True
            else:
                lines.append(line)
    if not replaced:
        lines.append(f"RENTCAST_API_KEY={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🏠 LTR Analyzer")

    # -- API Key --
    st.subheader("API Configuration")
    api_key = st.text_input(
        "Rentcast API Key",
        value=_load_env_key(),
        type="password",
        help="Get a free key at https://app.rentcast.io/app/api-keys (free tier: 50 calls/month)",
    )
    col_save, col_hint = st.columns([1, 2])
    with col_save:
        if st.button("Save Key"):
            _save_env_key(api_key)
            st.success("Saved!")
    with col_hint:
        st.caption("Stored in .env locally")

    # -- API Usage Meter --
    st.divider()
    usage = get_usage()
    calls = usage["calls"]
    plan_limit = usage["plan_limit"]
    remaining = usage["remaining"]
    over = usage["over_limit"]
    pct = usage["pct_used"]
    breakdown = usage["breakdown"]

    st.subheader("API Usage This Month")

    # Allow user to set their plan limit
    new_limit = st.number_input(
        "Plan limit (calls/month)",
        value=plan_limit,
        min_value=10,
        max_value=100000,
        step=50,
        help="Free tier = 50. Change this if you're on a paid Rentcast plan.",
        key="plan_limit_input",
    )
    if new_limit != plan_limit:
        set_plan_limit(int(new_limit))
        plan_limit = int(new_limit)
        remaining = max(0, plan_limit - calls)
        over = max(0, calls - plan_limit)
        pct = min(100, round(calls / plan_limit * 100))

    if over > 0:
        st.error(f"⚠️ {calls} / {plan_limit} calls used — **{over} over limit**")
    elif pct >= 80:
        st.warning(f"🔶 {calls} / {plan_limit} calls used ({remaining} remaining)")
    else:
        st.success(f"✅ {calls} / {plan_limit} calls used ({remaining} remaining)")

    st.progress(pct, text=f"{pct}% used")

    if breakdown:
        with st.expander("Breakdown"):
            for ep, count in sorted(breakdown.items(), key=lambda x: -x[1]):
                label = {"listings": "Listing pages", "rent_avm": "Rent AVM calls"}.get(ep, ep)
                st.markdown(f"- {label}: **{count}**")
        if usage.get("last_call"):
            from datetime import datetime as _dt
            last = _dt.fromisoformat(usage["last_call"]).strftime("%b %d %H:%M")
            st.caption(f"Last call: {last}")

    st.caption(
        "Tracked locally — resets on the 1st of each month. "
        "Cached results don't count."
    )

    st.divider()

    # -- Search Parameters --
    st.subheader("Search")
    search_mode = st.radio("Search by", ["City", "ZIP Code"], horizontal=True)

    if search_mode == "City":
        city = st.text_input("City", value="Raleigh")
        state = st.text_input("State (abbrev.)", value="NC", max_chars=2).upper()
        zip_code = None
    else:
        zip_code = st.text_input("ZIP Code", value="27601")
        city = ""
        state = "NC"
        st.text_input("State (abbrev.)", value=state, max_chars=2, key="state_zip").upper()

    prop_type_options = ["All", "Single Family", "Multi Family", "Condo", "Townhouse"]
    property_type = st.selectbox("Property Type", prop_type_options)

    col_min, col_max = st.columns(2)
    with col_min:
        min_price = st.number_input("Min Price ($)", value=100_000, step=10_000, format="%d")
    with col_max:
        max_price = st.number_input("Max Price ($)", value=500_000, step=10_000, format="%d")

    min_beds = st.selectbox("Min Bedrooms", [1, 2, 3, 4, 5], index=1)
    max_listings = st.number_input(
        "Max listings to fetch", value=500, step=100, min_value=50, max_value=5000,
        help="Rentcast paginates at 500/page (1 API call/page). 2,000 listings = up to 4 calls.",
    )

    st.divider()

    # -- Investment Assumptions --
    st.subheader("Investment Assumptions")

    with st.expander("Financing", expanded=True):
        down_pct = st.slider("Down Payment %", 5, 30, 20)
        interest_rate = st.slider("Interest Rate %", 4.0, 10.0, 6.5, step=0.125)
        closing_pct = st.slider(
            "Closing Costs % of Price",
            1.0, 5.0, 2.5, step=0.25,
            help="Dynamic — applied as % of each property's price",
        )
        rehab = st.number_input("Rehab Budget ($)", value=0, step=1000, format="%d")

    with st.expander("Operating Expenses", expanded=True):
        vacancy_maint_pct = st.slider(
            "Vacancy + Maintenance %", 5, 30, 15,
            help="% of gross rent reserved for vacancy and maintenance"
        )
        tax_rate_pct = st.slider(
            "Property Tax Rate % (annual)",
            0.5, 3.0, 1.0, step=0.05,
            help="% of purchase price annually. Wake County NC ≈ 1.12%",
        )
        insurance_pct = st.slider(
            "Insurance % (annual)", 0.2, 1.5, 0.5, step=0.05,
            help="% of purchase price annually (estimated if not known)"
        )
        mgmt_pct = st.slider("Property Mgmt %", 0, 15, 0,
                              help="% of gross rent if using a property manager")
        utilities = st.number_input("Monthly Utilities ($, landlord-paid)", value=0, step=50)

    st.divider()

    # -- Filters --
    st.subheader("Filters")
    min_coc = st.slider("Minimum CoC Return %", 0.0, 20.0, 5.0, step=0.5)
    min_cf = st.number_input("Minimum Monthly Cash Flow ($)", value=0, step=50)

    st.divider()
    st.subheader("Rent Estimate Source")
    rent_source_mode = st.radio(
        "How to estimate rent",
        ["Rentcast AVM (API)", "Manual Entry", "0.8% Rule (no API)"],
        help="Rentcast AVM is most accurate. Manual lets you set rent yourself. 0.8% Rule uses no API calls.",
    )

    manual_rent = None
    max_rent_calls = 0

    if rent_source_mode == "Manual Entry":
        manual_rent = st.number_input(
            "Monthly Rent to apply to all properties ($)",
            value=2000, step=50, min_value=0,
            help="This single rent figure will be used for every property in the results.",
        )
        st.caption("No API calls used. Great when you already know market rent for the area.")
    elif rent_source_mode == "Rentcast AVM (API)":
        max_rent_calls = st.number_input(
            "Max rent AVM calls",
            value=40,
            min_value=1,
            max_value=500,
            step=5,
            help=(
                "Each property needs 1 Rentcast API call for an accurate rent estimate. "
                "Free tier = 50 calls/month total (listings pages also count). "
                "Properties are pre-screened with the 0.8%% rule first — only the most "
                "promising ones spend real API calls."
            ),
        )
        st.caption(
            "All listings are ranked by 0.8% rule first (free). "
            "Real AVM estimates are fetched only for the top candidates."
        )
    else:
        st.caption("Conservative estimate: monthly rent = 0.8% of purchase price. No API calls used.")

    st.divider()
    search_btn = st.button("🔍 Search Properties", use_container_width=True, type="primary")

# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------

st.title("LTR Investment Analyzer")
st.caption("Automatically screens for-sale listings using Rentcast's AVM rent estimates and your assumptions.")

if not search_btn and "results_df" not in st.session_state:
    st.info(
        "**Get started:** Enter your Rentcast API key in the sidebar, set your search parameters, then click **Search Properties**.\n\n"
        "📌 No API key yet? [Get a free one here](https://app.rentcast.io/app/api-keys) (takes ~30 seconds)."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Run search
# ---------------------------------------------------------------------------

if search_btn:
    if not api_key:
        st.error("Please enter your Rentcast API key in the sidebar.")
        st.stop()

    assumptions = LTRAssumptions(
        down_payment_pct=down_pct,
        closing_costs_pct=closing_pct,
        rehab_costs=float(rehab),
        interest_rate=interest_rate,
        loan_years=30,
        vacancy_maintenance_pct=float(vacancy_maint_pct),
        property_tax_rate_pct=tax_rate_pct,
        insurance_pct=insurance_pct,
        property_mgmt_pct=float(mgmt_pct),
        monthly_utilities=float(utilities),
    )

    with st.spinner("Fetching listings from Rentcast..."):
        try:
            listings = fetch_listings(
                api_key=api_key,
                city=city,
                state=state,
                zip_code=zip_code or None,
                property_type=None if property_type == "All" else property_type,
                min_price=int(min_price),
                max_price=int(max_price),
                min_beds=int(min_beds),
                max_listings=int(max_listings),
            )
        except Exception as e:
            st.error(f"Failed to fetch listings: {e}")
            st.stop()

    if not listings:
        st.warning("No listings found for your search criteria. Try broadening the price range or location.")
        st.stop()

    # Pre-screen all listings with 0.8% rule (zero API calls) to sort best candidates first
    prescreened = prescreen_listings(listings, assumptions)
    n_all = len(prescreened)

    rent_map: dict = {}

    if rent_source_mode == "Manual Entry":
        st.info(f"Found **{n_all} listings**. Using manual rent of **${manual_rent:,}/mo** for all properties.")

    elif rent_source_mode == "0.8% Rule (no API)":
        st.info(f"Found **{n_all} listings**. Using 0.8% rule for all rent estimates (no API calls).")

    else:  # Rentcast AVM
        st.info(
            f"Found **{n_all} listings**. "
            f"Pre-screened with 0.8% rule; fetching Rentcast AVM for top **{int(max_rent_calls)}** candidates..."
        )
        progress_bar = st.progress(0, text="Fetching rent estimates...")

        def update_progress(done: int, total: int) -> None:
            progress_bar.progress(done / total, text=f"Rent estimates: {done}/{total}")

        try:
            rent_map = batch_rent_estimates(
                api_key=api_key,
                listings=prescreened,
                delay_seconds=0.3,
                max_calls=int(max_rent_calls),
                progress_callback=update_progress,
            )
        except RateLimitError:
            st.warning(
                "⚠️ Rentcast API rate limit reached. Remaining properties use the "
                "**0.8% rule** as a conservative fallback."
            )

        progress_bar.empty()
        api_hits = sum(1 for v in rent_map.values() if v is not None)
        fallback_count = n_all - api_hits
        if fallback_count > 0:
            st.caption(
                f"ℹ️ {api_hits} properties got AVM estimates; "
                f"{fallback_count} used 0.8% rule fallback."
            )

    # Calculate CoC for each listing (use prescreened order)
    results: list[LTRResult] = []
    for listing in prescreened:
        price = listing.get("price") or listing.get("listPrice") or 0
        if not price or price <= 0:
            continue

        listing_id = listing.get("id", listing.get("formattedAddress", ""))
        rent_data = rent_map.get(listing_id)

        if rent_source_mode == "Manual Entry":
            monthly_rent = float(manual_rent)
            rent_source = "manual"
        elif rent_data:
            monthly_rent, rent_low, rent_high = rent_data
            rent_source = "api_estimate"
        else:
            monthly_rent = estimate_rent_1pct(price)
            rent_source = "0.8pct_rule"
            rent_source = "0.8pct_rule"

        hoa = 0.0
        hoa_data = listing.get("hoa")
        if isinstance(hoa_data, dict):
            fee = hoa_data.get("fee") or 0
            freq = (hoa_data.get("frequency") or "monthly").lower()
            hoa = fee / 12 if "annual" in freq or "year" in freq else fee

        address = (
            listing.get("formattedAddress")
            or f"{listing.get('addressLine1', '')}, {listing.get('city', '')}, {listing.get('state', '')}"
        )

        result = calculate_ltr(
            address=address,
            property_price=float(price),
            monthly_rent=float(monthly_rent),
            assumptions=assumptions,
            rent_source=rent_source,
            monthly_hoa=hoa,
            bedrooms=listing.get("bedrooms"),
            bathrooms=listing.get("bathrooms"),
            sqft=listing.get("squareFootage"),
            year_built=listing.get("yearBuilt"),
            days_on_market=listing.get("daysOnMarket"),
            property_type=listing.get("propertyType"),
            url=listing.get("url") or listing.get("listingUrl"),
        )
        results.append(result)

    st.session_state["results"] = results
    st.session_state["assumptions"] = assumptions


# ---------------------------------------------------------------------------
# Display results
# ---------------------------------------------------------------------------

if "results" not in st.session_state:
    st.stop()

results: list[LTRResult] = st.session_state["results"]
assumptions: LTRAssumptions = st.session_state["assumptions"]

# Apply filters
filtered = [
    r for r in results
    if r.coc_return_pct >= min_coc and r.monthly_cash_flow >= min_cf
]
filtered.sort(key=lambda r: r.coc_return_pct, reverse=True)

# Summary metrics
col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Listings Analyzed", len(results))
col2.metric(f"Qualifying (CoC ≥ {min_coc}%)", len(filtered))
if filtered:
    col3.metric("Best CoC Return", f"{filtered[0].coc_return_pct:.1f}%")
    col4.metric("Best Monthly Cash Flow", f"${filtered[0].monthly_cash_flow:,.0f}/mo")
else:
    col3.metric("Best CoC Return", "—")
    col4.metric("Best Monthly Cash Flow", "—")

st.divider()

if not filtered:
    st.warning(
        f"No properties found meeting your criteria (CoC ≥ {min_coc}%, monthly CF ≥ ${min_cf:,}). "
        "Try lowering your thresholds, changing the price range, or adjusting your assumptions."
    )
else:
    st.subheader(f"Qualifying Properties ({len(filtered)} found)")
    st.caption("Click a row to see the full expense breakdown.")

    # Build display dataframe
    rows = []
    for r in filtered:
        rows.append({
            "Address": r.address,
            "Price": r.property_price,
            "Beds": r.bedrooms,
            "Baths": r.bathrooms,
            "SqFt": r.sqft,
            "Est. Rent": r.monthly_rent,
            "Rent Source": {"api_estimate": "✅ AVM", "manual": "✏️ Manual"}.get(r.rent_source, "📐 Est."),
            "Monthly CF": r.monthly_cash_flow,
            "CoC %": r.coc_return_pct,
            "GRM": r.gross_rent_multiplier,
            "Rent/Price %": r.rent_to_price_pct,
            "Days on Mkt": r.days_on_market,
            "Year Built": r.year_built,
            "HOA/mo": r.monthly_hoa,
        })

    df = pd.DataFrame(rows)

    # Format for display
    display_df = df.copy()
    display_df["Price"] = display_df["Price"].apply(lambda x: f"${x:,.0f}")
    display_df["Est. Rent"] = display_df["Est. Rent"].apply(lambda x: f"${x:,.0f}")
    display_df["Monthly CF"] = display_df["Monthly CF"].apply(lambda x: f"+${x:,.0f}" if x >= 0 else f"-${abs(x):,.0f}")
    display_df["CoC %"] = display_df["CoC %"].apply(lambda x: f"{x:.2f}%")
    display_df["GRM"] = display_df["GRM"].apply(lambda x: f"{x:.1f}" if x else "—")
    display_df["Rent/Price %"] = display_df["Rent/Price %"].apply(lambda x: f"{x:.3f}%")
    display_df["HOA/mo"] = display_df["HOA/mo"].apply(lambda x: f"${x:,.0f}" if x else "—")
    display_df["SqFt"] = display_df["SqFt"].apply(lambda x: f"{x:,.0f}" if x else "—")
    display_df["Beds"] = display_df["Beds"].apply(lambda x: str(int(x)) if x else "—")
    display_df["Baths"] = display_df["Baths"].apply(lambda x: str(x) if x else "—")
    display_df["Days on Mkt"] = display_df["Days on Mkt"].apply(lambda x: str(int(x)) if x else "—")
    display_df["Year Built"] = display_df["Year Built"].apply(lambda x: str(int(x)) if x else "—")

    selected = st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )

    # Export CSV
    csv_data = df.to_csv(index=False)
    st.download_button(
        "⬇️ Download Results CSV",
        data=csv_data,
        file_name=f"ltr_results_{city or zip_code}_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )

    # ---------------------------------------------------------------------------
    # Property detail panel
    # ---------------------------------------------------------------------------

    sel_rows = selected.selection.rows if selected.selection else []
    if sel_rows:
        idx = sel_rows[0]
        r = filtered[idx]

        st.divider()
        st.subheader(f"📋 Detail: {r.address}")
        if r.url:
            st.markdown(f"[View Listing →]({r.url})")

        dcol1, dcol2 = st.columns(2)

        with dcol1:
            st.markdown("**Purchase Summary**")
            detail_rows = {
                "Purchase Price": f"${r.property_price:,.0f}",
                "Down Payment": f"${r.down_payment:,.0f} ({assumptions.down_payment_pct:.0f}%)",
                "Closing Costs": f"${r.closing_costs:,.0f} ({assumptions.closing_costs_pct:.1f}%)",
                "Rehab Budget": f"${assumptions.rehab_costs:,.0f}",
                "**Total Cash In**": f"**${r.initial_investment:,.0f}**",
                "Loan Amount": f"${r.loan_amount:,.0f}",
            }
            for k, v in detail_rows.items():
                st.markdown(f"{k}: {v}")

            st.markdown("")
            st.markdown("**Monthly Income**")
            _rent_label = {"api_estimate": "Rentcast AVM", "manual": "manual entry"}.get(r.rent_source, "0.8% rule estimate")
            st.markdown(f"Gross Rent: **${r.monthly_rent:,.0f}** ({_rent_label})")

        with dcol2:
            st.markdown("**Monthly Expenses Breakdown**")
            expense_rows = {
                "Mortgage (P&I)": r.monthly_pi,
                "Vacancy + Maintenance": r.vacancy_maintenance,
                "Property Taxes": r.property_taxes_monthly,
                "Insurance": r.insurance_monthly,
                "Property Management": r.property_mgmt_monthly,
                "HOA": r.monthly_hoa,
                "Utilities": r.monthly_utilities,
            }
            for k, v in expense_rows.items():
                if v > 0:
                    st.markdown(f"{k}: ${v:,.2f}")
            st.markdown(f"**Total Expenses: ${r.total_monthly_expenses:,.2f}**")

        # Returns summary
        st.markdown("")
        ret_col1, ret_col2, ret_col3 = st.columns(3)
        cf_color = "green" if r.monthly_cash_flow >= 0 else "red"
        ret_col1.metric("Monthly Cash Flow", f"${r.monthly_cash_flow:,.2f}")
        ret_col2.metric("Annual Cash Flow", f"${r.annual_cash_flow:,.2f}")
        ret_col3.metric("Cash-on-Cash Return", f"{r.coc_return_pct:.2f}%")

        # Pie chart of expenses
        import plotly.express as px
        expense_data = {k: v for k, v in expense_rows.items() if v > 0}
        if expense_data:
            fig = px.pie(
                values=list(expense_data.values()),
                names=list(expense_data.keys()),
                title="Monthly Expense Breakdown",
                hole=0.3,
            )
            fig.update_layout(height=300, margin=dict(t=40, b=0, l=0, r=0))
            st.plotly_chart(fig, use_container_width=True)
