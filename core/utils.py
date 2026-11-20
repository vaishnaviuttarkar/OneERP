from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0")
Q2 = Decimal("0.01")
Q3 = Decimal("0.001")


def D(v):
    if v is None or v == "":
        return ZERO
    if isinstance(v, float):
        raise TypeError("floats are not allowed for money/quantity")
    return v if isinstance(v, Decimal) else Decimal(str(v))


def r2(v):
    return D(v).quantize(Q2, rounding=ROUND_HALF_UP)


def q3(v):
    return D(v).quantize(Q3, rounding=ROUND_HALF_UP)


PAID, PARTIAL, UNPAID = "paid", "partially_paid", "unpaid"
PAYMENT_STATUSES = [(PAID, "Paid"), (PARTIAL, "Partially paid"), (UNPAID, "Unpaid")]


def payment_status(total, paid):
    """Paid / partially paid / unpaid for any payable document (bill, wage statement...)."""
    total, paid = D(total), D(paid)
    if paid >= total:
        return PAID
    return PARTIAL if paid > 0 else UNPAID


def financial_year(d, start_month=4):
    y = d.year if d.month >= start_month else d.year - 1
    return f"{y}-{str(y + 1)[-2:]}"
