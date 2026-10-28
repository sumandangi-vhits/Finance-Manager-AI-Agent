from datetime import date, timedelta
from decimal import Decimal

AMOUNT_TOLERANCE = 100  
ITEM_TOLERANCE = 1      

FIELD_LABELS = {
    "merchant": "merchant name",
    "txn_date": "date",
    "txn_time": "time",
    "total_minor": "total amount",
    "currency": "currency",
    "tax_minor": "tax amount",
    "discount_minor": "discount",
    "other_charges_minor": "additional charges",
    "payment_method": "payment method",
    "invoice_number": "invoice number",
    "category": "category",
    "items": "item list",
}


def format_money(minor, currency="INR"):
    return f"{currency} {Decimal(minor) / 100:,.2f}"


def _issue(field, code, message):
    return {"field": field, "code": code, "message": message}


def validate_transaction(data, today=None):
    """Check a transaction dict (amounts in minor units).
    Returns a list of issues: {"field", "code", "message"}. Empty list = no problems."""
    today = today or date.today()
    currency = data.get("currency") or "INR"
    items = data.get("items") or []
    issues = []

    # --- required fields ---
    if not data.get("merchant"):
        issues.append(_issue(
            "merchant", "missing",
            "The merchant name is missing or unclear. Which shop is this bill from?",
        ))

    txn_date = data.get("txn_date")
    if not txn_date:
        issues.append(_issue(
            "txn_date", "missing",
            "I could not read a clear date. What date is this bill from?",
        ))
    else:
        try:
            parsed = date.fromisoformat(txn_date)
        except ValueError:
            parsed = None
        if parsed is not None and parsed > today + timedelta(days=1):
            issues.append(_issue(
                "txn_date", "future_date",
                f"The date {txn_date} is in the future. Is that correct?",
            ))

    total = data.get("total_minor")
    if total is None:
        issues.append(_issue(
            "total_minor", "missing",
            "I could not read the total amount. What was the total paid?",
        ))

    # --- item arithmetic: quantity x unit price = item total ---
    for item in items:
        qty = item.get("quantity")
        unit = item.get("unit_price_minor")
        line_total = item.get("total_minor")
        if qty is not None and unit is not None and line_total is not None:
            expected = Decimal(str(qty)) * unit
            if abs(expected - line_total) > ITEM_TOLERANCE:
                issues.append(_issue(
                    "items", "item_math",
                    f"For '{item.get('name', 'an item')}': {qty:g} x "
                    f"{format_money(unit, currency)} = {format_money(round(expected), currency)}, "
                    f"but the bill lists {format_money(line_total, currency)}. Which is correct?",
                ))

    # --- items add up to the bill total ---
    if total is not None and items and all(i.get("total_minor") is not None for i in items):
        items_sum = sum(i["total_minor"] for i in items)
        tax = data.get("tax_minor") or 0
        extra = data.get("other_charges_minor") or 0
        discount = data.get("discount_minor") or 0
        # Prices may already include tax, or tax may be added on top
        candidates = {
            items_sum,
            items_sum + extra - discount,
            items_sum + tax + extra - discount,
        }
        if min(abs(total - c) for c in candidates) > AMOUNT_TOLERANCE:
            issues.append(_issue(
                "total_minor", "total_mismatch",
                f"The item totals add up to {format_money(items_sum, currency)}, "
                f"but the bill total is {format_money(total, currency)}. "
                "Can you confirm the correct amount?",
            ))

    # --- fields the AI said it was unsure about ---
    already = {i["field"] for i in issues}
    for field in data.get("uncertain_fields", []):
        if field in already:
            continue
        if field == "currency":
            message = ("The currency was not shown on the bill, so I assumed INR. "
                       "Is that correct?")
        else:
            label = FIELD_LABELS.get(field, field)
            message = f"I am not confident about the {label}. Can you confirm it?"
        issues.append(_issue(field, "uncertain", message))
        already.add(field)

    return issues