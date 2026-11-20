from django.db import models


def money(**kw):
    """NUMERIC(14,2)"""
    return models.DecimalField(max_digits=14, decimal_places=2, **kw)


def qty(**kw):
    """NUMERIC(14,3)"""
    return models.DecimalField(max_digits=14, decimal_places=3, **kw)
