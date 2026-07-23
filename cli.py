#!/usr/bin/env python3
"""
LTR Investment Analyzer — CLI
Usage examples:
  python cli.py --city Raleigh --state NC
  python cli.py --city Raleigh --state NC --max-price 350000 --beds 3 --coc 8
  python cli.py --zip 27601 --coc 5 --save
  python cli.py --city Raleigh --state NC --down 25 --rate 7.0 --no-api-rent
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from calculator import LTRAssumptions, LTRResult, calculate_ltr, estimate_rent_1pct
from fetcher import RateLimitError, batch_rent_estimates, fetch_listings, prescreen_listings

ENV_FILE = Path(__file__).parent / ".env"


def _load_api_key() -> str:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("RENTCAST_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("RENTCAST_API_KEY", "")


def _fmt_money(v: float) -> str:
    return f"${v:>10,.0f}"


def _fmt_pct(v: float) -> str:
    return f"{v:>7.2f}%"


def _fmt_cf(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):>8,.0f}/mo"


def print_results_table(results: list[LTRResult], top_n: int = 50) -> None:
    if not results:
        print("  No qualifying properties found.")
        return

    col_w = [5, 42, 11, 6, 6, 9, 12, 8, 10]
    header = (
        f"{'Rank':>5}  "
        f"{'Address':<42}  "
        f"{'Price':>11}  "
        f"{'Beds':>4}  "
        f"{'Baths':>5}  "
        f"{'Est. Rent':>9}  "
        f"{'Monthly CF':>12}  "
        f"{'CoC %':>7}  "
        f"{'DOM':>5}"
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)

    for i, r in enumerate(results[:top_n], 1):
        beds = f"{int(r.bedrooms)}" if r.bedrooms else "—"
        baths = f"{r.bathrooms}" if r.bathrooms else "—"
        dom = f"{int(r.days_on_market)}" if r.days_on_market else "—"
        addr = (r.address[:40] + "..") if len(r.address) > 42 else r.address
        src = "~" if r.rent_source != "api_estimate" else " "
        print(
            f"{i:>5}  "
            f"{addr:<42}  "
            f"${r.property_price:>10,.0f}  "
            f"{beds:>4}  "
            f"{baths:>5}  "
            f"${r.monthly_rent:>8,.0f}  "
            f"{_fmt_cf(r.monthly_cash_flow):>12}  "
            f"{r.coc_return_pct:>6.2f}%  "
            f"{dom:>5}"
            f"{src}"
        )

    print(sep)
    if any(r.rent_source != "api_estimate" for r in results[:top_n]):
        print("~ = rent estimate from 0.8% rule (no API data available)")


def print_detail(r: LTRResult, assumptions: LTRAssumptions) -> None:
    print(f"\n{'='*60}")
    print(f"DETAIL: {r.address}")
    print(f"{'='*60}")
    print(f"  Purchase Price      : ${r.property_price:>12,.0f}")
    print(f"  Down Payment        : ${r.down_payment:>12,.0f}  ({assumptions.down_payment_pct:.0f}%)")
    print(f"  Closing Costs       : ${r.closing_costs:>12,.0f}  ({assumptions.closing_costs_pct:.1f}% of price)")
    print(f"  Rehab Budget        : ${assumptions.rehab_costs:>12,.0f}")
    print(f"  Total Cash In       : ${r.initial_investment:>12,.0f}")
    print(f"  Loan Amount         : ${r.loan_amount:>12,.0f}")
    print()
    print(f"  Gross Monthly Rent  : ${r.monthly_rent:>12,.0f}  ({'API estimate' if r.rent_source == 'api_estimate' else '0.8% rule est.'})")
    print()
    print("  Monthly Expenses:")
    print(f"    Mortgage (P&I)    : ${r.monthly_pi:>10,.2f}")
    print(f"    Vacancy + Maint.  : ${r.vacancy_maintenance:>10,.2f}")
    print(f"    Property Taxes    : ${r.property_taxes_monthly:>10,.2f}")
    print(f"    Insurance         : ${r.insurance_monthly:>10,.2f}")
    print(f"    Property Mgmt     : ${r.property_mgmt_monthly:>10,.2f}")
    print(f"    HOA               : ${r.monthly_hoa:>10,.2f}")
    print(f"    Utilities         : ${r.monthly_utilities:>10,.2f}")
    print(f"    ─────────────────────────────")
    print(f"    TOTAL EXPENSES    : ${r.total_monthly_expenses:>10,.2f}")
    print()
    print(f"  Monthly Cash Flow   : {_fmt_cf(r.monthly_cash_flow)}")
    print(f"  Annual Cash Flow    : ${r.annual_cash_flow:>12,.0f}")
    print(f"  Cash-on-Cash Return : {r.coc_return_pct:>11.2f}%")
    print(f"  Gross Rent Multi.   : {r.gross_rent_multiplier:>11.1f}x")
    print(f"  Rent / Price        : {r.rent_to_price_pct:>11.3f}%")
    if r.url:
        print(f"\n  Listing URL: {r.url}")


def save_csv(results: list[LTRResult], filename: str) -> None:
    fields = [
        "address", "property_price", "bedrooms", "bathrooms", "sqft",
        "year_built", "days_on_market", "property_type",
        "monthly_rent", "rent_source",
        "initial_investment", "down_payment", "closing_costs",
        "monthly_pi", "vacancy_maintenance", "property_taxes_monthly",
        "insurance_monthly", "property_mgmt_monthly", "monthly_hoa",
        "monthly_utilities", "total_monthly_expenses",
        "monthly_cash_flow", "annual_cash_flow", "coc_return_pct",
        "gross_rent_multiplier", "rent_to_price_pct",
    ]
    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow({field: getattr(r, field, None) for field in fields})
    print(f"\nSaved {len(results)} results to: {filename}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LTR Investment Analyzer — screens for-sale properties for cash flow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Search params
    loc = parser.add_mutually_exclusive_group()
    loc.add_argument("--city", default="Raleigh", help="City name (default: Raleigh)")
    loc.add_argument("--zip", dest="zip_code", help="ZIP code (overrides --city/--state)")
    parser.add_argument("--state", default="NC", help="State abbreviation (default: NC)")
    parser.add_argument("--type", dest="property_type", default="All",
                        choices=["All", "Single Family", "Multi Family", "Condo", "Townhouse"])
    parser.add_argument("--min-price", type=int, default=100_000)
    parser.add_argument("--max-price", type=int, default=500_000)
    parser.add_argument("--beds", type=int, default=2, help="Minimum bedrooms")
    parser.add_argument("--max-listings", type=int, default=500,
                        help="Max listings to fetch. Rentcast returns up to 500/page = 1 API call (default: 500)")

    # Investment assumptions
    parser.add_argument("--down", type=float, default=20.0, help="Down payment %% (default: 20)")
    parser.add_argument("--rate", type=float, default=6.5, help="Interest rate %% (default: 6.5)")
    parser.add_argument("--closing", type=float, default=2.5, help="Closing costs %% of price (default: 2.5)")
    parser.add_argument("--rehab", type=float, default=0, help="Rehab budget $ (default: 0)")
    parser.add_argument("--vacancy", type=float, default=15.0, help="Vacancy+maint %% of rent (default: 15)")
    parser.add_argument("--tax", type=float, default=1.0, help="Property tax rate %% annual (default: 1.0)")
    parser.add_argument("--insurance", type=float, default=0.5, help="Insurance %% annual (default: 0.5)")
    parser.add_argument("--mgmt", type=float, default=0.0, help="Property mgmt %% of rent (default: 0)")
    parser.add_argument("--utilities", type=float, default=0, help="Monthly landlord utilities $ (default: 0)")

    # Filters
    parser.add_argument("--coc", type=float, default=5.0, help="Min Cash-on-Cash return %% (default: 5)")
    parser.add_argument("--min-cf", type=float, default=0.0, help="Min monthly cash flow $ (default: 0)")

    # Output
    parser.add_argument("--save", action="store_true", help="Save results to CSV")
    parser.add_argument("--top", type=int, default=20, help="Show top N results (default: 20)")
    parser.add_argument("--detail", type=int, metavar="RANK", help="Print full detail for property at given rank")
    parser.add_argument("--no-api-rent", action="store_true",
                        help="Skip Rentcast rent AVM and use 0.8%% rule for all rent estimates")
    parser.add_argument("--rent", type=float, default=None, metavar="AMOUNT",
                        help="Manual rent override $ applied to every property (skips all AVM calls). "
                             "e.g. --rent 2200")
    parser.add_argument("--max-rent-calls", type=int, default=40,
                        help=(
                            "Max live rent AVM API calls to make (default: 40). "
                            "All listings are pre-screened with the 0.8%% rule first; "
                            "only the top candidates spend real calls. "
                            "Cached results are always free and don't count toward this limit."
                        ))
    parser.add_argument("--no-cache", action="store_true", help="Bypass local cache and force fresh API calls")
    parser.add_argument("--api-key", help="Rentcast API key (overrides .env / env var)")

    args = parser.parse_args()

    api_key = args.api_key or _load_api_key()
    if not api_key:
        print("ERROR: No Rentcast API key found.")
        print("  Set it in .env as RENTCAST_API_KEY=your_key_here")
        print("  Or pass it with --api-key YOUR_KEY")
        print("  Get a free key at: https://app.rentcast.io/app/api-keys")
        sys.exit(1)

    assumptions = LTRAssumptions(
        down_payment_pct=args.down,
        closing_costs_pct=args.closing,
        rehab_costs=args.rehab,
        interest_rate=args.rate,
        loan_years=30,
        vacancy_maintenance_pct=args.vacancy,
        property_tax_rate_pct=args.tax,
        insurance_pct=args.insurance,
        property_mgmt_pct=args.mgmt,
        monthly_utilities=args.utilities,
    )

    location = args.zip_code if args.zip_code else f"{args.city}, {args.state}"
    print(f"\nSearching {location} for properties...")
    print(f"Price: ${args.min_price:,} – ${args.max_price:,} | Min {args.beds} beds | Type: {args.property_type}")
    print(
        f"Assumptions: {args.down:.0f}% down | {args.rate:.2f}% rate | "
        f"{args.closing:.1f}% closing | {args.vacancy:.0f}% vacancy+maint | "
        f"{args.tax:.2f}% tax | {args.insurance:.2f}% insurance | "
        f"{args.mgmt:.0f}% mgmt"
    )

    try:
        listings = fetch_listings(
            api_key=api_key,
            city=args.city or "",
            state=args.state,
            zip_code=args.zip_code,
            property_type=None if args.property_type == "All" else args.property_type,
            min_price=args.min_price,
            max_price=args.max_price,
            min_beds=args.beds,
            max_listings=args.max_listings,
            use_cache=not args.no_cache,
        )
    except Exception as e:
        print(f"ERROR fetching listings: {e}")
        sys.exit(1)

    if not listings:
        print("No listings found. Try broadening your search criteria.")
        sys.exit(0)

    print(f"Found {len(listings)} listings.", end="")

    # Phase 1: Pre-screen all listings with 0.8% rule — zero API calls.
    # Sorts by estimated CoC so real API calls go to the best candidates first.
    prescreened = prescreen_listings(listings, assumptions)

    # Phase 2 (optional): Accurate rent AVM for top candidates
    if args.rent is not None:
        print(f" Using manual rent override: ${args.rent:,.0f}/mo for all properties.")
        rent_map: dict = {}
    elif args.no_api_rent:
        print(" Using 0.8% rule for all rent estimates (--no-api-rent).")
        rent_map: dict = {}
    else:
        max_calls = args.max_rent_calls
        print(
            f" Pre-screening with 0.8% rule, then fetching AVM for top {max_calls} candidates...",
            flush=True
        )

        total = [0]

        def progress(done: int, total_count: int) -> None:
            total[0] = total_count
            print(f"\r  Rent estimates: {done}/{total_count}", end="", flush=True)

        try:
            rent_map = batch_rent_estimates(
                api_key=api_key,
                listings=prescreened,
                delay_seconds=0.3,
                max_calls=max_calls,
                progress_callback=progress,
            )
            api_hits = sum(1 for v in rent_map.values() if v is not None)
            print(f"  ({api_hits} AVM estimates, {len(listings) - api_hits} used 0.8% rule fallback)")
        except RateLimitError as e:
            print(f"\nWARNING: {e}")
            rent_map = {}

    # Calculate CoC for all listings (pre-screened order)
    all_results: list[LTRResult] = []
    for listing in prescreened:
        price = listing.get("price") or listing.get("listPrice") or 0
        if not price or price <= 0:
            continue

        listing_id = listing.get("id", listing.get("formattedAddress", ""))
        rent_data = rent_map.get(listing_id)

        if args.rent is not None:
            monthly_rent = float(args.rent)
            rent_source = "manual"
        elif rent_data:
            monthly_rent, _, _ = rent_data
            rent_source = "api_estimate"
        else:
            monthly_rent = estimate_rent_1pct(price)
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
        all_results.append(result)

    # Filter and sort
    filtered = [
        r for r in all_results
        if r.coc_return_pct >= args.coc and r.monthly_cash_flow >= args.min_cf
    ]
    filtered.sort(key=lambda r: r.coc_return_pct, reverse=True)

    print(f"\nResults: {len(filtered)}/{len(all_results)} properties meet CoC ≥ {args.coc}% and CF ≥ ${args.min_cf:,.0f}/mo\n")

    if args.detail and 1 <= args.detail <= len(filtered):
        print_detail(filtered[args.detail - 1], assumptions)
    else:
        print_results_table(filtered, top_n=args.top)

    if args.save and filtered:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        loc_slug = (args.zip_code or f"{args.city}_{args.state}").replace(" ", "_").lower()
        filename = str(Path(__file__).parent / f"results_{loc_slug}_{timestamp}.csv")
        save_csv(filtered, filename)


if __name__ == "__main__":
    main()
