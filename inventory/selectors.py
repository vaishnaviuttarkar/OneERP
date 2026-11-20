from django.db.models import F, Sum

from catalogue.models import ProductVariant
from .models import StockBalance, StockMovement


def stock_rows(business, kind=None, search=""):
    qs = StockBalance.objects.filter(business=business).select_related("variant__product")
    if kind:
        qs = qs.filter(variant__product__kind=kind)
    if search:
        qs = qs.filter(variant__product__name__icontains=search) | qs.filter(variant__sku__icontains=search)
    return qs.order_by("variant__product__name", "variant__sku")


def total_stock_value(business):
    return StockBalance.objects.filter(business=business).aggregate(t=Sum("value"))["t"] or 0


def finished_goods_quantity(business):
    return StockBalance.objects.filter(business=business, variant__product__kind="finished").aggregate(
        t=Sum("quantity"))["t"] or 0


def low_stock(business, threshold):
    return StockBalance.objects.filter(business=business, quantity__lte=threshold,
                                       variant__product__kind__in=["raw", "accessory"]
                                       ).select_related("variant__product")
