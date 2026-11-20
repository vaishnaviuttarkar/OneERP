from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class ProductTemplate(models.Model):
    FINISHED, RAW, ACCESSORY = "finished", "raw", "accessory"
    KINDS = [(FINISHED, "Finished good"), (RAW, "Raw material"), (ACCESSORY, "Accessory")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=KINDS)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("name"), "business", name="uniq_product_name_ci")]

    def __str__(self):
        return self.name


class ProductVariant(models.Model):
    UNITS = [("pcs", "Pieces"), ("m", "Metres"), ("kg", "Kilograms"), ("g", "Grams"),
             ("roll", "Rolls"), ("set", "Sets"), ("box", "Boxes")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    product = models.ForeignKey(ProductTemplate, on_delete=models.PROTECT, related_name="variants")
    sku = models.CharField(max_length=50)
    size = models.CharField(max_length=20, blank=True)
    colour = models.CharField(max_length=30, blank=True)
    unit = models.CharField(max_length=10, choices=UNITS, default="pcs")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "sku"], name="uniq_variant_sku"),
            models.UniqueConstraint(fields=["product", "size", "colour"], name="uniq_variant_size_colour"),
        ]
        ordering = ["product__name", "sku"]

    @property
    def label(self):
        extra = " / ".join(x for x in (self.size, self.colour) if x)
        return f"{self.product.name} - {extra} [{self.sku}]" if extra else f"{self.product.name} [{self.sku}]"

    def __str__(self):
        return self.label
