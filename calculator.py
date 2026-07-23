"""
LTR (Long-Term Rental) Cash-on-Cash Return Calculator
Formula matches the Excel calculator in the parent folder.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LTRAssumptions:
    """Default investment assumptions — all can be overridden per property."""
    down_payment_pct: float = 20.0          # % of purchase price
    closing_costs_pct: float = 2.5          # % of purchase price (dynamic)
    rehab_costs: float = 0.0                # flat $ amount
    interest_rate: float = 6.5             # annual % for 30-yr fixed
    loan_years: int = 30
    vacancy_maintenance_pct: float = 15.0  # % of gross rent
    property_tax_rate_pct: float = 1.0     # % of purchase price annually
    insurance_pct: float = 0.5             # % of purchase price annually
    property_mgmt_pct: float = 0.0         # % of gross rent
    monthly_utilities: float = 0.0         # flat $ (landlord-paid utilities)


@dataclass
class LTRResult:
    address: str
    property_price: float
    monthly_rent: float
    rent_source: str                         # "api_estimate", "1pct_rule", "manual"
    bedrooms: Optional[float]
    bathrooms: Optional[float]
    sqft: Optional[float]
    year_built: Optional[int]
    days_on_market: Optional[int]
    property_type: Optional[str]
    url: Optional[str]

    # Financing
    loan_amount: float
    initial_investment: float
    down_payment: float
    closing_costs: float
    monthly_pi: float

    # Expenses
    vacancy_maintenance: float
    property_taxes_monthly: float
    insurance_monthly: float
    property_mgmt_monthly: float
    monthly_hoa: float
    monthly_utilities: float
    total_monthly_expenses: float

    # Returns
    monthly_cash_flow: float
    annual_cash_flow: float
    coc_return_pct: float
    gross_rent_multiplier: float
    rent_to_price_pct: float


def _monthly_mortgage(loan_amount: float, annual_rate: float, years: int) -> float:
    """Standard mortgage P&I formula."""
    if annual_rate == 0:
        return loan_amount / (years * 12)
    r = annual_rate / 100 / 12
    n = years * 12
    return loan_amount * (r * (1 + r) ** n) / ((1 + r) ** n - 1)


def calculate_ltr(
    address: str,
    property_price: float,
    monthly_rent: float,
    assumptions: LTRAssumptions,
    rent_source: str = "manual",
    monthly_hoa: float = 0.0,
    bedrooms: Optional[float] = None,
    bathrooms: Optional[float] = None,
    sqft: Optional[float] = None,
    year_built: Optional[int] = None,
    days_on_market: Optional[int] = None,
    property_type: Optional[str] = None,
    url: Optional[str] = None,
) -> LTRResult:
    """
    Calculate LTR Cash-on-Cash return for a property.

    Matches the logic in 'LTR COC calculator.xlsx':
      Initial Investment = Down Payment + Closing Costs + Rehab
      Monthly Expenses   = P&I + Vacancy/Maint + Tax + Insurance + Mgmt + HOA + Utilities
      Monthly Cash Flow  = Gross Rent - Monthly Expenses
      CoC Return %       = (Annual Cash Flow / Initial Investment) * 100
    """
    a = assumptions

    down_payment = property_price * a.down_payment_pct / 100
    loan_amount = property_price - down_payment
    closing_costs = property_price * a.closing_costs_pct / 100
    initial_investment = down_payment + closing_costs + a.rehab_costs

    monthly_pi = _monthly_mortgage(loan_amount, a.interest_rate, a.loan_years)

    vacancy_maint = monthly_rent * a.vacancy_maintenance_pct / 100
    property_taxes = property_price * a.property_tax_rate_pct / 100 / 12
    insurance = property_price * a.insurance_pct / 100 / 12
    property_mgmt = monthly_rent * a.property_mgmt_pct / 100

    total_expenses = (
        monthly_pi
        + vacancy_maint
        + property_taxes
        + insurance
        + property_mgmt
        + monthly_hoa
        + a.monthly_utilities
    )

    monthly_cf = monthly_rent - total_expenses
    annual_cf = monthly_cf * 12
    coc = (annual_cf / initial_investment * 100) if initial_investment > 0 else 0.0

    grm = property_price / (monthly_rent * 12) if monthly_rent > 0 else 0.0
    rtp = (monthly_rent / property_price * 100) if property_price > 0 else 0.0

    return LTRResult(
        address=address,
        property_price=property_price,
        monthly_rent=monthly_rent,
        rent_source=rent_source,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        sqft=sqft,
        year_built=year_built,
        days_on_market=days_on_market,
        property_type=property_type,
        url=url,
        loan_amount=loan_amount,
        initial_investment=initial_investment,
        down_payment=down_payment,
        closing_costs=closing_costs,
        monthly_pi=monthly_pi,
        vacancy_maintenance=vacancy_maint,
        property_taxes_monthly=property_taxes,
        insurance_monthly=insurance,
        property_mgmt_monthly=property_mgmt,
        monthly_hoa=monthly_hoa,
        monthly_utilities=a.monthly_utilities,
        total_monthly_expenses=total_expenses,
        monthly_cash_flow=monthly_cf,
        annual_cash_flow=annual_cf,
        coc_return_pct=coc,
        gross_rent_multiplier=grm,
        rent_to_price_pct=rtp,
    )


def estimate_rent_1pct(property_price: float) -> float:
    """
    Rough fallback: 0.8% rule (conservative vs classic 1% rule).
    Use only when no API estimate is available.
    """
    return property_price * 0.008
