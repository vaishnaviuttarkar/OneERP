from django.db.models import Sum

from core.models import FinancialAccount
from core.utils import ZERO
from .models import CashBookEntry


def account_balances(business):
    out = []
    for a in FinancialAccount.objects.filter(business=business).order_by("kind", "name"):
        agg = CashBookEntry.objects.filter(account=a)
        inn = agg.filter(direction="in").aggregate(t=Sum("amount"))["t"] or ZERO
        outv = agg.filter(direction="out").aggregate(t=Sum("amount"))["t"] or ZERO
        out.append({"account": a, "in": inn, "out": outv, "balance": a.opening_balance + inn - outv})
    return out


def cash_position(business):
    return sum((r["balance"] for r in account_balances(business)), ZERO)
