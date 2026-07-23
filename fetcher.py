"""
Rentcast API integration for fetching for-sale listings and rent estimates.
Docs: https://developers.rentcast.io/reference/

Sign up for a free API key at https://app.rentcast.io/app/api-keys
Free tier: 50 API calls/month. Results are cached locally in .cache/ to minimize calls.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

RENTCAST_BASE = "https://api.rentcast.io/v1"
CACHE_DIR = Path(__file__).parent / ".cache"
LISTINGS_CACHE_TTL_HOURS = 12   # refresh listings twice a day
RENT_CACHE_TTL_DAYS = 7         # rent estimates are stable for a week
RENTCAST_PAGE_SIZE = 500        # API max per page

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def _read_cache(key: str, ttl: timedelta) -> Optional[object]:
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        saved_at = datetime.fromisoformat(data["saved_at"])
        if datetime.now() - saved_at < ttl:
            return data["value"]
    except Exception:
        pass
    return None


def _write_cache(key: str, value: object) -> None:
    _cache_path(key).write_text(
        json.dumps({"saved_at": datetime.now().isoformat(), "value": value})
    )


def _sanitize_key(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s).lower()


# ---------------------------------------------------------------------------
# Local API usage tracker (Rentcast has no usage endpoint — we count locally)
# ---------------------------------------------------------------------------

_USAGE_FILE = CACHE_DIR / "api_usage.json"


def _load_usage() -> dict:
    """Load this month's usage counter. Resets automatically each calendar month."""
    CACHE_DIR.mkdir(exist_ok=True)
    now = datetime.now()
    month_key = now.strftime("%Y-%m")
    try:
        data = json.loads(_USAGE_FILE.read_text())
        if data.get("month") == month_key:
            return data
    except Exception:
        pass
    # Start fresh for a new month
    return {"month": month_key, "calls": 0, "breakdown": {}}


def _save_usage(usage: dict) -> None:
    _USAGE_FILE.write_text(json.dumps(usage, indent=2))


def increment_usage(endpoint: str = "other") -> None:
    """Increment the live API call counter for the current month."""
    usage = _load_usage()
    usage["calls"] = usage.get("calls", 0) + 1
    breakdown = usage.setdefault("breakdown", {})
    breakdown[endpoint] = breakdown.get(endpoint, 0) + 1
    usage["last_call"] = datetime.now().isoformat()
    _save_usage(usage)


def get_usage() -> dict:
    """
    Return current month's API usage stats.

    Keys: month, calls, breakdown, last_call, plan_limit, remaining, pct_used
    """
    usage = _load_usage()
    plan_limit = 50  # Free tier default
    calls = usage.get("calls", 0)
    remaining = max(0, plan_limit - calls)
    over = max(0, calls - plan_limit)
    return {
        "month": usage.get("month", ""),
        "calls": calls,
        "breakdown": usage.get("breakdown", {}),
        "last_call": usage.get("last_call"),
        "plan_limit": plan_limit,
        "remaining": remaining,
        "over_limit": over,
        "pct_used": min(100, round(calls / plan_limit * 100)),
    }


def set_plan_limit(limit: int) -> None:
    """Persist a custom plan limit (e.g. 1000 for paid plans)."""
    CACHE_DIR.mkdir(exist_ok=True)
    cfg_file = CACHE_DIR / "plan_config.json"
    cfg_file.write_text(json.dumps({"plan_limit": limit}))


def get_plan_limit() -> int:
    cfg_file = CACHE_DIR / "plan_config.json"
    try:
        return json.loads(cfg_file.read_text()).get("plan_limit", 50)
    except Exception:
        return 50


# Patch get_usage to use persisted plan limit
_orig_get_usage = get_usage
def get_usage() -> dict:  # noqa: F811
    usage = _load_usage()
    plan_limit = get_plan_limit()
    calls = usage.get("calls", 0)
    remaining = max(0, plan_limit - calls)
    over = max(0, calls - plan_limit)
    return {
        "month": usage.get("month", ""),
        "calls": calls,
        "breakdown": usage.get("breakdown", {}),
        "last_call": usage.get("last_call"),
        "plan_limit": plan_limit,
        "remaining": remaining,
        "over_limit": over,
        "pct_used": min(100, round(calls / plan_limit * 100)),
    }


# ---------------------------------------------------------------------------
# Rentcast API calls
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict:
    return {"X-Api-Key": api_key, "accept": "application/json"}


def fetch_listings(
    api_key: str,
    city: str,
    state: str,
    zip_code: Optional[str] = None,
    property_type: Optional[str] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    min_beds: Optional[int] = None,
    max_listings: int = 500,
    use_cache: bool = True,
) -> list[dict]:
    """
    Fetch active for-sale listings from Rentcast with automatic pagination.

    Rentcast returns at most 500 results per call. This function pages through
    all results (up to max_listings) using the offset parameter.

    Returns a list of property dicts. Fields include:
      id, formattedAddress, addressLine1, city, state, zipCode,
      bedrooms, bathrooms, squareFootage, propertyType, yearBuilt,
      price, status, listedDate, daysOnMarket, latitude, longitude, hoa

    API call cost: 1 call per page of 500. A 1,200-listing city = 3 calls.
    """
    cache_key = _sanitize_key(
        f"listings_{zip_code or city}_{state}_{property_type}_{min_price}_{max_price}_{min_beds}_{max_listings}"
    )
    if use_cache:
        cached = _read_cache(cache_key, timedelta(hours=LISTINGS_CACHE_TTL_HOURS))
        if cached is not None:
            return cached

    base_params: dict = {"status": "Active", "limit": RENTCAST_PAGE_SIZE}

    if zip_code:
        base_params["zipCode"] = zip_code
    else:
        base_params["city"] = city
        base_params["state"] = state

    if min_price:
        base_params["minPrice"] = min_price
    if max_price:
        base_params["maxPrice"] = max_price
    if min_beds:
        base_params["bedrooms"] = min_beds
    if property_type and property_type.lower() not in ("all", "any", ""):
        base_params["propertyType"] = property_type

    all_listings: list[dict] = []
    offset = 0

    while len(all_listings) < max_listings:
        params = {**base_params, "offset": offset}
        resp = requests.get(
            f"{RENTCAST_BASE}/listings/sale",
            headers=_headers(api_key),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        increment_usage("listings")
        page = resp.json()

        # Rentcast returns a list or wraps in {"data": [...]}
        if isinstance(page, dict):
            page = page.get("data", page.get("listings", []))

        if not page:
            break  # No more results

        all_listings.extend(page)
        offset += len(page)

        if len(page) < RENTCAST_PAGE_SIZE:
            break  # Last page had fewer than max — we're done

    all_listings = all_listings[:max_listings]
    _write_cache(cache_key, all_listings)
    return all_listings


def fetch_rent_estimate(
    api_key: str,
    address: str,
    city: str,
    state: str,
    zip_code: str,
    bedrooms: int,
    bathrooms: float,
    property_type: str,
    sqft: Optional[int] = None,
    use_cache: bool = True,
) -> Optional[tuple[float, float, float]]:
    """
    Fetch long-term rent estimate from Rentcast AVM.

    Returns (rent_estimate, rent_low, rent_high) or None on failure.
    """
    cache_key = _sanitize_key(f"rent_{address}_{zip_code}_{bedrooms}_{bathrooms}")
    if use_cache:
        cached = _read_cache(cache_key, timedelta(days=RENT_CACHE_TTL_DAYS))
        if cached is not None:
            return tuple(cached)

    params: dict = {
        "address": address,
        "city": city,
        "state": state,
        "zipCode": zip_code,
        "bedrooms": max(int(bedrooms), 1),
        "bathrooms": float(bathrooms) if bathrooms else 1.0,
        "propertyType": _normalize_property_type(property_type),
    }
    if sqft:
        params["squareFootage"] = int(sqft)

    try:
        resp = requests.get(
            f"{RENTCAST_BASE}/avm/rent/long-term",
            headers=_headers(api_key),
            params=params,
            timeout=30,
        )
        if resp.status_code == 404:
            return None  # No data for this address
        resp.raise_for_status()
        increment_usage("rent_avm")
        data = resp.json()

        rent = data.get("rent") or data.get("rentZestimate") or data.get("price")
        low = data.get("rentRangeLow") or data.get("lowRent") or rent
        high = data.get("rentRangeHigh") or data.get("highRent") or rent

        if rent:
            result = (float(rent), float(low or rent), float(high or rent))
            _write_cache(cache_key, list(result))
            return result
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            raise RateLimitError("Rentcast API rate limit reached. Using fallback rent estimates.") from e
    except Exception:
        pass

    return None


def _normalize_property_type(prop_type: Optional[str]) -> str:
    """Map common property type strings to Rentcast-accepted values."""
    if not prop_type:
        return "Single Family"
    pt = prop_type.lower()
    if "single" in pt or "sfh" in pt or "house" in pt:
        return "Single Family"
    if "multi" in pt or "duplex" in pt or "triplex" in pt or "fourplex" in pt:
        return "Multi Family"
    if "condo" in pt or "townhouse" in pt or "townhome" in pt:
        return "Condo"
    if "apartment" in pt or "apt" in pt:
        return "Apartment"
    return "Single Family"


# ---------------------------------------------------------------------------
# Batch rent fetching with rate-limit protection
# ---------------------------------------------------------------------------

def batch_rent_estimates(
    api_key: str,
    listings: list[dict],
    delay_seconds: float = 0.5,
    max_calls: Optional[int] = None,
    progress_callback=None,
) -> dict[str, Optional[tuple[float, float, float]]]:
    """
    Fetch rent estimates for a list of listings, respecting rate limits.

    max_calls: cap how many live API calls to make (already-cached results
               are free and do not count toward this limit). Properties
               beyond the cap get None and will use the 0.8% fallback.

    Returns a dict mapping listing id -> (rent, low, high) or None.
    """
    results = {}
    live_calls = 0

    for i, listing in enumerate(listings):
        listing_id = listing.get("id", listing.get("formattedAddress", str(i)))

        # Check if already cached — free to retrieve
        cache_key = _sanitize_key(
            f"rent_{listing.get('addressLine1', '')}_{listing.get('zipCode', '')}"
            f"_{listing.get('bedrooms') or 3}_{listing.get('bathrooms') or 2}"
        )
        cached = _read_cache(cache_key, timedelta(days=RENT_CACHE_TTL_DAYS))
        if cached is not None:
            results[listing_id] = tuple(cached)
            if progress_callback:
                progress_callback(i + 1, len(listings))
            continue

        # Cap live API calls
        if max_calls is not None and live_calls >= max_calls:
            results[listing_id] = None
            if progress_callback:
                progress_callback(i + 1, len(listings))
            continue

        try:
            result = fetch_rent_estimate(
                api_key=api_key,
                address=listing.get("addressLine1", ""),
                city=listing.get("city", ""),
                state=listing.get("state", ""),
                zip_code=listing.get("zipCode", ""),
                bedrooms=listing.get("bedrooms") or 3,
                bathrooms=listing.get("bathrooms") or 2,
                property_type=listing.get("propertyType"),
                sqft=listing.get("squareFootage"),
                use_cache=False,  # already checked above
            )
            results[listing_id] = result
            live_calls += 1
        except RateLimitError:
            # Fill remainder with None so caller uses fallback
            for j in range(i, len(listings)):
                lid = listings[j].get("id", listings[j].get("formattedAddress", str(j)))
                results.setdefault(lid, None)
            break

        if progress_callback:
            progress_callback(i + 1, len(listings))

        # Polite delay between live API calls
        if i < len(listings) - 1:
            time.sleep(delay_seconds)

    return results


def prescreen_listings(
    listings: list[dict],
    assumptions,
    min_coc_pct: float = 0.0,
    rent_pct: float = 0.8,
) -> list[dict]:
    """
    Zero-API-call pre-screening using a conservative rent estimate (default 0.8% rule).

    Returns listings sorted by estimated CoC, highest first. Use this to
    identify the most promising candidates before spending API calls on
    accurate rent AVM estimates.

    Args:
        rent_pct: Monthly rent as % of purchase price (0.8 = conservative,
                  1.0 = classic 1% rule). Lower = safer screen.
    """
    from calculator import calculate_ltr, estimate_rent_1pct

    scored = []
    for listing in listings:
        price = listing.get("price") or listing.get("listPrice") or 0
        if not price or price <= 0:
            continue

        monthly_rent = price * rent_pct / 100

        hoa = 0.0
        hoa_data = listing.get("hoa")
        if isinstance(hoa_data, dict):
            fee = hoa_data.get("fee") or 0
            freq = (hoa_data.get("frequency") or "monthly").lower()
            hoa = fee / 12 if "annual" in freq or "year" in freq else fee

        address = (
            listing.get("formattedAddress")
            or f"{listing.get('addressLine1', '')}, {listing.get('city', '')}"
        )
        result = calculate_ltr(
            address=address,
            property_price=float(price),
            monthly_rent=float(monthly_rent),
            assumptions=assumptions,
            rent_source="prescreen",
            monthly_hoa=hoa,
        )
        scored.append((result.coc_return_pct, listing))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [listing for _, listing in scored]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RateLimitError(Exception):
    pass
