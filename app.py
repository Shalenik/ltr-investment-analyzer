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
# Constants & helpers
# ---------------------------------------------------------------------------

TEMPLATE_COLUMNS = [
    "address", "price", "monthly_rent", "bedrooms", "bathrooms",
    "sqft", "year_built", "property_type", "monthly_hoa", "url",
]

RESULT_COLUMNS = [
    "address", "property_price", "bedrooms", "bathrooms", "sqft", "year_built",
    "property_type", "days_on_market", "monthly_rent", "rent_source",
    "initial_investment", "down_payment", "closing_costs",
    "monthly_pi", "vacancy_maintenance", "property_taxes_monthly",
    "insurance_monthly", "property_mgmt_monthly", "monthly_hoa", "monthly_utilities",
    "total_monthly_expenses", "monthly_cash_flow", "annual_cash_flow",
    "coc_return_pct", "gross_rent_multiplier", "rent_to_price_pct", "url",
]


def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> Optional[int]:
    f = _safe_float(v)
    return int(f) if f is not None else None


def _template_csv() -> str:
    rows = [
        {"address": "123 Main St, Raleigh, NC 27601", "price": 325000,
         "monthly_rent": 2200, "bedrooms": 3, "bathrooms": 2, "sqft": 1450,
         "year_built": 2005, "property_type": "Single Family", "monthly_hoa": 0,
         "url": "https://zillow.com/homedetails/example"},
        {"address": "456 Oak Ave, Durham, NC 27701", "price": 280000,
         "monthly_rent": "", "bedrooms": 3, "bathrooms": 2, "sqft": 1200,
         "year_built": 1998, "property_type": "Single Family", "monthly_hoa": 50,
         "url": ""},
    ]
    return pd.DataFrame(rows, columns=TEMPLATE_COLUMNS).to_csv(index=False)


def _template_excel() -> bytes:
    import io
    buf = io.BytesIO()
    df = pd.read_csv(io.StringIO(_template_csv()))
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Properties")
        # Add a notes sheet
        notes = pd.DataFrame({
            "Column": TEMPLATE_COLUMNS,
            "Required": ["Yes", "Yes", "No", "No", "No", "No", "No", "No", "No", "No"],
            "Notes": [
                "Full address string",
                "Purchase price in $",
                "Monthly rent in $ — leave blank to use sidebar rent source",
                "Number of bedrooms",
                "Number of bathrooms",
                "Square footage",
                "Year built",
                "Single Family / Multi Family / Condo / Townhouse",
                "Monthly HOA fee in $ (0 if none)",
                "Listing URL (optional, for reference)",
            ],
        })
        notes.to_excel(writer, index=False, sheet_name="Instructions")
    return buf.getvalue()


def _parse_upload(uploaded_file) -> list[dict]:
    """Parse uploaded CSV or Excel into listing dicts compatible with calculate_ltr."""
    name = uploaded_file.name.lower()
    if name.endswith(".csv"):
        df = pd.read_csv(uploaded_file)
    elif name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(uploaded_file, sheet_name=0)
    else:
        raise ValueError("Unsupported file type. Please upload a .csv or .xlsx file.")

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    required = {"address", "price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"File is missing required columns: {', '.join(sorted(missing))}")

    listings = []
    for i, row in df.iterrows():
        price = _safe_float(row.get("price"))
        if not price or price <= 0:
            continue
        listings.append({
            "formattedAddress": str(row.get("address", f"Row {i+2}")).strip(),
            "addressLine1": str(row.get("address", "")).split(",")[0].strip(),
            "city": "",
            "state": "",
            "zipCode": "",
            "price": price,
            "_monthly_rent_override": _safe_float(row.get("monthly_rent")),
            "bedrooms": _safe_float(row.get("bedrooms")),
            "bathrooms": _safe_float(row.get("bathrooms")),
            "squareFootage": _safe_float(row.get("sqft")),
            "yearBuilt": _safe_int(row.get("year_built")),
            "propertyType": str(row.get("property_type", "")).strip() or None,
            "hoa": {"fee": _safe_float(row.get("monthly_hoa")) or 0.0, "frequency": "monthly"},
            "url": str(row.get("url", "")).strip() or None,
            "daysOnMarket": None,
        })
    return listings


def _results_to_df(results: list) -> pd.DataFrame:
    return pd.DataFrame(
        [{col: getattr(r, col, None) for col in RESULT_COLUMNS} for r in results]
    )


def _results_to_excel(results: list) -> bytes:
    import io
    buf = io.BytesIO()
    df = _results_to_df(results)
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Results")
    return buf.getvalue()


def _display_results(
    results: list,
    all_count: int,
    assumptions,
    location_label: str,
    min_coc: float,
    min_cf: float,
) -> None:
    """Shared results renderer used by both the Search and Upload tabs."""
    filtered = [
        r for r in results
        if r.coc_return_pct >= min_coc and r.monthly_cash_flow >= min_cf
    ]
    filtered.sort(key=lambda r: r.coc_return_pct, reverse=True)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Analyzed", all_count)
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
            f"No properties met CoC ≥ {min_coc}% and CF ≥ ${min_cf:,}/mo. "
            "Try lowering the thresholds or adjusting assumptions."
        )
        return

    st.subheader(f"Qualifying Properties ({len(filtered)} found)")
    st.caption("Click a row to see the full expense breakdown.")

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
    display_df = df.copy()
    display_df["Price"] = display_df["Price"].apply(lambda x: f"${x:,.0f}")
    display_df["Est. Rent"] = display_df["Est. Rent"].apply(lambda x: f"${x:,.0f}")
    display_df["Monthly CF"] = display_df["Monthly CF"].apply(
        lambda x: f"+${x:,.0f}" if x >= 0 else f"-${abs(x):,.0f}"
    )
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

    # Downloads
    dl_col1, dl_col2 = st.columns(2)
    with dl_col1:
        st.download_button(
            "⬇️ Download CSV",
            data=_results_to_df(filtered).to_csv(index=False),
            file_name=f"ltr_{location_label}_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )
    with dl_col2:
        st.download_button(
            "⬇️ Download Excel",
            data=_results_to_excel(filtered),
            file_name=f"ltr_{location_label}_{pd.Timestamp.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # Detail panel
    sel_rows = selected.selection.rows if selected.selection else []
    if sel_rows:
        r = filtered[sel_rows[0]]
        st.divider()
        st.subheader(f"📋 Detail: {r.address}")
        if r.url:
            st.markdown(f"[View Listing →]({r.url})")

        dcol1, dcol2 = st.columns(2)
        with dcol1:
            st.markdown("**Purchase Summary**")
            for k, v in {
                "Purchase Price": f"${r.property_price:,.0f}",
                "Down Payment": f"${r.down_payment:,.0f} ({assumptions.down_payment_pct:.0f}%)",
                "Closing Costs": f"${r.closing_costs:,.0f} ({assumptions.closing_costs_pct:.1f}%)",
                "Rehab Budget": f"${assumptions.rehab_costs:,.0f}",
                "**Total Cash In**": f"**${r.initial_investment:,.0f}**",
                "Loan Amount": f"${r.loan_amount:,.0f}",
            }.items():
                st.markdown(f"{k}: {v}")
            st.markdown("")
            st.markdown("**Monthly Income**")
            _rl = {"api_estimate": "Rentcast AVM", "manual": "manual entry"}.get(r.rent_source, "0.8% rule estimate")
            st.markdown(f"Gross Rent: **${r.monthly_rent:,.0f}** ({_rl})")

        with dcol2:
            st.markdown("**Monthly Expenses**")
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
            st.markdown(f"**Total: ${r.total_monthly_expenses:,.2f}**")

        st.markdown("")
        m1, m2, m3 = st.columns(3)
        m1.metric("Monthly Cash Flow", f"${r.monthly_cash_flow:,.2f}")
        m2.metric("Annual Cash Flow", f"${r.annual_cash_flow:,.2f}")
        m3.metric("Cash-on-Cash Return", f"{r.coc_return_pct:.2f}%")

        import plotly.express as px
        exp_data = {k: v for k, v in expense_rows.items() if v > 0}
        if exp_data:
            fig = px.pie(
                values=list(exp_data.values()), names=list(exp_data.keys()),
                title="Monthly Expense Breakdown", hole=0.3,
            )
            fig.update_layout(height=300, margin=dict(t=40, b=0, l=0, r=0))
            st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------------------

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
# Rent source — stored in session state so sidebar can read it before main renders
# ---------------------------------------------------------------------------

if "rent_source_mode" not in st.session_state:
    st.session_state["rent_source_mode"] = "Rentcast AVM (API)"


# ---------------------------------------------------------------------------
# Sidebar — API config, (conditional) usage meter, assumptions, filters
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

    # -- API Usage Meter — only shown when using Rentcast AVM --
    if st.session_state["rent_source_mode"] == "Rentcast AVM (API)":
        st.divider()
        usage = get_usage()
        calls = usage["calls"]
        plan_limit = usage["plan_limit"]
        remaining = usage["remaining"]
        over = usage["over_limit"]
        pct = usage["pct_used"]
        breakdown = usage["breakdown"]

        st.subheader("API Usage This Month")
        new_limit = st.number_input(
            "Plan limit (calls/month)", value=plan_limit, min_value=10,
            max_value=100000, step=50,
            help="Free tier = 50. Change if you're on a paid Rentcast plan.",
            key="plan_limit_input",
        )
        if new_limit != plan_limit:
            set_plan_limit(int(new_limit))
            plan_limit = int(new_limit)
            remaining = max(0, plan_limit - calls)
            over = max(0, calls - plan_limit)
            pct = min(100, round(calls / plan_limit * 100))

        if over > 0:
            st.error(f"⚠️ {calls} / {plan_limit} calls — **{over} over limit**")
        elif pct >= 80:
            st.warning(f"🔶 {calls} / {plan_limit} calls ({remaining} remaining)")
        else:
            st.success(f"✅ {calls} / {plan_limit} calls ({remaining} remaining)")
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
        st.caption("Tracked locally — resets 1st of month. Cached results are free.")

    st.divider()

    # -- Investment Assumptions --
    st.subheader("Investment Assumptions")
    with st.expander("Financing", expanded=True):
        down_pct = st.slider("Down Payment %", 5, 30, 20)
        interest_rate = st.slider("Interest Rate %", 4.0, 10.0, 6.5, step=0.125)
        closing_pct = st.slider(
            "Closing Costs % of Price", 1.0, 5.0, 2.5, step=0.25,
            help="Dynamic — applied as % of each property's price",
        )
        rehab = st.number_input("Rehab Budget ($)", value=0, step=1000, format="%d")

    with st.expander("Operating Expenses", expanded=True):
        vacancy_maint_pct = st.slider(
            "Vacancy + Maintenance %", 5, 30, 15,
            help="% of gross rent reserved for vacancy and maintenance",
        )
        tax_rate_pct = st.slider(
            "Property Tax Rate % (annual)", 0.5, 3.0, 1.0, step=0.05,
            help="% of purchase price annually. Wake County NC ≈ 1.12%",
        )
        insurance_pct = st.slider(
            "Insurance % (annual)", 0.2, 1.5, 0.5, step=0.05,
            help="% of purchase price annually",
        )
        mgmt_pct = st.slider("Property Mgmt %", 0, 15, 0,
                              help="% of gross rent if using a property manager")
        utilities = st.number_input("Monthly Utilities ($, landlord-paid)", value=0, step=50)

    st.divider()

    # -- Filters --
    st.subheader("Filters")
    min_coc = st.slider("Minimum CoC Return %", 0.0, 20.0, 5.0, step=0.5)
    min_cf = st.number_input("Minimum Monthly Cash Flow ($)", value=0, step=50)


# Build shared assumptions object
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

# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------

st.title("LTR Investment Analyzer")

# -- Rent Source Selector — at the very top of the page --
rent_source_mode = st.radio(
    "Rent Estimate Source",
    ["Rentcast AVM (API)", "Manual Entry", "0.8% Rule (no API)"],
    horizontal=True,
    help=(
        "**Rentcast AVM** — accurate per-property estimate via API. "
        "**Manual Entry** — you set the rent; no API calls. "
        "**0.8% Rule** — quick rule-of-thumb, no API calls."
    ),
    key="rent_source_mode",   # writes back to session_state automatically
)

st.divider()

# -- Rent source sub-options --
manual_rent = None
max_rent_calls = 0

if rent_source_mode == "Manual Entry":
    manual_rent = st.number_input(
        "Monthly Rent ($) to apply to all properties",
        value=2000, step=50, min_value=0,
        help="Used for every property when no per-row rent is provided.",
    )
elif rent_source_mode == "Rentcast AVM (API)":
    max_rent_calls = st.number_input(
        "Max AVM calls per search",
        value=40, min_value=1, max_value=500, step=5,
        help=(
            "Properties are pre-screened with the 0.8% rule first (free). "
            "Real API calls go only to the top candidates."
        ),
    )
    st.caption(
        f"💡 Free tier = 50 calls/month total. Each listing page = 1 call. "
        f"Budget **{int(max_rent_calls)}** for rent AVM + ~1 for listing fetch."
    )
else:
    st.caption("Conservative estimate: 0.8% of purchase price per month. No API calls.")

st.divider()

# ===========================================================================
# Tabs
# ===========================================================================

tab_search, tab_upload = st.tabs(["🔍 Search Listings", "📂 Upload File"])

# ===========================================================================
# TAB 1 — Search Listings
# ===========================================================================

with tab_search:
    sc1, sc2 = st.columns([3, 1])
    with sc1:
        search_mode = st.radio("Search by", ["City", "ZIP Code"], horizontal=True)
        if search_mode == "City":
            cc1, cc2 = st.columns(2)
            city = cc1.text_input("City", value="Raleigh")
            state = cc2.text_input("State", value="NC", max_chars=2).upper()
            zip_code = None
        else:
            cc1, cc2 = st.columns(2)
            zip_code = cc1.text_input("ZIP Code", value="27601")
            state = cc2.text_input("State", value="NC", max_chars=2).upper()
            city = ""

        pc1, pc2, pc3, pc4 = st.columns(4)
        property_type = pc1.selectbox("Type", ["All", "Single Family", "Multi Family", "Condo", "Townhouse"])
        min_price = pc2.number_input("Min Price ($)", value=100_000, step=10_000, format="%d")
        max_price = pc3.number_input("Max Price ($)", value=500_000, step=10_000, format="%d")
        min_beds = pc4.selectbox("Min Beds", [1, 2, 3, 4, 5], index=1)

    with sc2:
        max_listings = st.number_input(
            "Max listings to fetch", value=500, step=100, min_value=50, max_value=5000,
            help="Rentcast returns up to 500/page = 1 API call.",
        )

    search_btn = st.button("🔍 Search Properties", type="primary")

    if search_btn:
        if not api_key:
            st.error("Please enter your Rentcast API key in the sidebar.")
            st.stop()

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
            st.warning("No listings found. Try broadening the price range or location.")
            st.stop()

        prescreened = prescreen_listings(listings, assumptions)
        n_all = len(prescreened)
        rent_map: dict = {}

        if rent_source_mode == "Manual Entry":
            st.info(f"Found **{n_all} listings**. Using manual rent **${manual_rent:,}/mo**.")
        elif rent_source_mode == "0.8% Rule (no API)":
            st.info(f"Found **{n_all} listings**. Using 0.8% rule (no API calls).")
        else:
            st.info(
                f"Found **{n_all} listings**. "
                f"Fetching Rentcast AVM for top **{int(max_rent_calls)}** candidates..."
            )
            pb = st.progress(0, text="Fetching rent estimates...")

            def _upd(done: int, total: int) -> None:
                pb.progress(done / total, text=f"Rent estimates: {done}/{total}")

            try:
                rent_map = batch_rent_estimates(
                    api_key=api_key, listings=prescreened,
                    delay_seconds=0.3, max_calls=int(max_rent_calls),
                    progress_callback=_upd,
                )
            except RateLimitError:
                st.warning("⚠️ Rate limit reached. Remaining properties use 0.8% fallback.")
            pb.empty()
            api_hits = sum(1 for v in rent_map.values() if v is not None)
            if n_all - api_hits > 0:
                st.caption(f"ℹ️ {api_hits} AVM estimates, {n_all - api_hits} used 0.8% fallback.")

        results: list[LTRResult] = []
        for listing in prescreened:
            price = listing.get("price") or listing.get("listPrice") or 0
            if not price or price <= 0:
                continue
            listing_id = listing.get("id", listing.get("formattedAddress", ""))
            rent_data = rent_map.get(listing_id)

            if rent_source_mode == "Manual Entry":
                monthly_rent_val = float(manual_rent)
                rs = "manual"
            elif rent_data:
                monthly_rent_val, _, _ = rent_data
                rs = "api_estimate"
            else:
                monthly_rent_val = estimate_rent_1pct(price)
                rs = "0.8pct_rule"

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
            results.append(calculate_ltr(
                address=address, property_price=float(price),
                monthly_rent=float(monthly_rent_val), assumptions=assumptions,
                rent_source=rs, monthly_hoa=hoa,
                bedrooms=listing.get("bedrooms"), bathrooms=listing.get("bathrooms"),
                sqft=listing.get("squareFootage"), year_built=listing.get("yearBuilt"),
                days_on_market=listing.get("daysOnMarket"),
                property_type=listing.get("propertyType"),
                url=listing.get("url") or listing.get("listingUrl"),
            ))

        st.session_state["search_results"] = results
        st.session_state["search_assumptions"] = assumptions
        st.session_state["search_label"] = (zip_code or f"{city}_{state}").replace(" ", "_").lower()

    if "search_results" in st.session_state:
        _display_results(
            results=st.session_state["search_results"],
            all_count=len(st.session_state["search_results"]),
            assumptions=st.session_state["search_assumptions"],
            location_label=st.session_state.get("search_label", "search"),
            min_coc=min_coc,
            min_cf=min_cf,
        )
    elif not search_btn:
        st.info(
            "Set your search parameters above and click **Search Properties**.\n\n"
            "📌 No API key yet? [Get a free one here](https://app.rentcast.io/app/api-keys)."
        )

# ===========================================================================
# TAB 2 — Upload File
# ===========================================================================

with tab_upload:
    st.subheader("Analyze Your Own Properties")
    st.caption(
        "Upload a CSV or Excel file with properties you've found. "
        "The calculator runs the same CoC analysis using your sidebar assumptions."
    )

    dl1, dl2, dl3 = st.columns([1, 1, 2])
    with dl1:
        st.download_button(
            "⬇️ Download CSV Template",
            data=_template_csv(),
            file_name="ltr_template.csv",
            mime="text/csv",
        )
    with dl2:
        st.download_button(
            "⬇️ Download Excel Template",
            data=_template_excel(),
            file_name="ltr_template.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with dl3:
        st.caption(
            "**Required columns:** `address`, `price`  \n"
            "**Optional:** `monthly_rent` (per-row rent overrides the global source above), "
            "`bedrooms`, `bathrooms`, `sqft`, `year_built`, `property_type`, `monthly_hoa`, `url`"
        )

    st.divider()

    uploaded_file = st.file_uploader(
        "Upload CSV or Excel file",
        type=["csv", "xlsx", "xls"],
        help="Must include 'address' and 'price' columns.",
    )

    analyze_btn = st.button("📊 Analyze Uploaded Properties", type="primary", disabled=uploaded_file is None)

    if analyze_btn and uploaded_file:
        try:
            upload_listings = _parse_upload(uploaded_file)
        except ValueError as e:
            st.error(str(e))
            st.stop()

        if not upload_listings:
            st.warning("No valid rows found. Make sure 'price' column has numeric values.")
            st.stop()

        st.info(f"Loaded **{len(upload_listings)} properties**.")

        needs_rent = [l for l in upload_listings if l.get("_monthly_rent_override") is None]
        up_rent_map: dict = {}

        if needs_rent and rent_source_mode == "Rentcast AVM (API)":
            if not api_key:
                st.error("Enter your Rentcast API key in the sidebar for AVM estimates.")
                st.stop()
            pb2 = st.progress(0, text="Fetching rent estimates...")

            def _upd2(done: int, total: int) -> None:
                pb2.progress(done / total, text=f"Rent AVM: {done}/{total}")

            try:
                up_rent_map = batch_rent_estimates(
                    api_key=api_key, listings=needs_rent,
                    delay_seconds=0.3, max_calls=int(max_rent_calls),
                    progress_callback=_upd2,
                )
            except RateLimitError:
                st.warning("⚠️ Rate limit reached. Remaining rows will use 0.8% fallback.")
            pb2.empty()

        up_results: list[LTRResult] = []
        for listing in upload_listings:
            price = listing.get("price") or 0
            if not price or price <= 0:
                continue

            override = listing.get("_monthly_rent_override")
            listing_id = listing.get("formattedAddress", "")

            if override is not None:
                monthly_rent_val = float(override)
                rs = "manual"
            elif rent_source_mode == "Rentcast AVM (API)":
                rent_data = up_rent_map.get(listing_id)
                if rent_data:
                    monthly_rent_val, _, _ = rent_data
                    rs = "api_estimate"
                else:
                    monthly_rent_val = estimate_rent_1pct(price)
                    rs = "0.8pct_rule"
            elif rent_source_mode == "Manual Entry":
                monthly_rent_val = float(manual_rent)
                rs = "manual"
            else:
                monthly_rent_val = estimate_rent_1pct(price)
                rs = "0.8pct_rule"

            hoa = 0.0
            hoa_data = listing.get("hoa")
            if isinstance(hoa_data, dict):
                fee = hoa_data.get("fee") or 0
                freq = (hoa_data.get("frequency") or "monthly").lower()
                hoa = fee / 12 if "annual" in freq or "year" in freq else fee

            up_results.append(calculate_ltr(
                address=listing.get("formattedAddress", ""),
                property_price=float(price),
                monthly_rent=float(monthly_rent_val),
                assumptions=assumptions,
                rent_source=rs,
                monthly_hoa=hoa,
                bedrooms=listing.get("bedrooms"),
                bathrooms=listing.get("bathrooms"),
                sqft=listing.get("squareFootage"),
                year_built=listing.get("yearBuilt"),
                days_on_market=None,
                property_type=listing.get("propertyType"),
                url=listing.get("url"),
            ))

        st.session_state["upload_results"] = up_results
        st.session_state["upload_assumptions"] = assumptions

    if "upload_results" in st.session_state:
        _display_results(
            results=st.session_state["upload_results"],
            all_count=len(st.session_state["upload_results"]),
            assumptions=st.session_state["upload_assumptions"],
            location_label="upload",
            min_coc=min_coc,
            min_cf=min_cf,
        )
    elif not analyze_btn:
        st.info("Download the template above, fill it in, then upload it here.")
