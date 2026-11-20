from django.db import IntegrityError, transaction

from core.exceptions import DomainError
from core.services import record_audit
from .models import ProductTemplate, ProductVariant


@transaction.atomic
def create_product(*, business, user, name, kind, description="", default_workflow=None):
    name = " ".join((name or "").split())
    if not name:
        raise DomainError("Product name is required.")
    if kind not in dict(ProductTemplate.KINDS):
        raise DomainError("Invalid product kind.")
    if kind == ProductTemplate.FINISHED and default_workflow is None:
        raise DomainError("Finished goods require a default production workflow.")
    if ProductTemplate.objects.filter(business=business, name__iexact=name).exists():
        raise DomainError(f"Product '{name}' already exists.")
    p = ProductTemplate.objects.create(business=business, name=name, kind=kind, description=description)
    if kind == ProductTemplate.FINISHED:
        from production.models import ProductWorkflow
        if default_workflow.business_id != business.id:
            raise DomainError("Workflow belongs to another business.")
        ProductWorkflow.objects.create(business=business, product=p, workflow=default_workflow)
    record_audit(business=business, user=user, action="product.create", entity="ProductTemplate",
                 entity_id=p.pk, after={"name": p.name, "kind": kind})
    return p


@transaction.atomic
def add_variant(*, business, user, product, sku, size="", colour="", unit="pcs"):
    product = ProductTemplate.objects.select_for_update().get(pk=product.pk, business=business)
    sku = (sku or "").strip().upper()
    size, colour = (size or "").strip(), (colour or "").strip()
    if not sku:
        raise DomainError("SKU is required.")
    if product.kind != ProductTemplate.FINISHED:
        if size or colour:
            raise DomainError("Raw materials and accessories have no size or colour.")
        if product.variants.exists():
            raise DomainError("Raw materials and accessories have exactly one variant.")
    elif not size:
        raise DomainError("Finished-good variants need a size.")
    if ProductVariant.objects.filter(business=business, sku=sku).exists():
        raise DomainError(f"SKU {sku} already exists.")
    try:
        with transaction.atomic():
            v = ProductVariant.objects.create(business=business, product=product, sku=sku, size=size,
                                              colour=colour, unit=unit)
    except IntegrityError:
        raise DomainError("This size/colour combination already exists for the product.")
    record_audit(business=business, user=user, action="variant.create", entity="ProductVariant",
                 entity_id=v.pk, after={"sku": sku})
    return v


@transaction.atomic
def update_product(*, business, user, product_id, name, description="", default_workflow=None, is_active=True):
    from production.models import ProductWorkflow
    p = ProductTemplate.objects.select_for_update().get(pk=product_id, business=business)
    name = " ".join((name or "").split())
    if not name:
        raise DomainError("Product name is required.")
    if ProductTemplate.objects.filter(business=business, name__iexact=name).exclude(pk=p.pk).exists():
        raise DomainError(f"Product '{name}' already exists.")
    if p.kind == ProductTemplate.FINISHED:
        if default_workflow is None:
            raise DomainError("Finished goods require a default production workflow.")
        if default_workflow.business_id != business.id:
            raise DomainError("Workflow belongs to another business.")
    before = {"name": p.name, "active": p.is_active}
    p.name, p.description, p.is_active = name, description or "", bool(is_active)
    p.save(update_fields=["name", "description", "is_active"])
    if p.kind == ProductTemplate.FINISHED:
        ProductWorkflow.objects.update_or_create(product=p, defaults={"business": business, "workflow": default_workflow})
    record_audit(business=business, user=user, action="product.update", entity="ProductTemplate", entity_id=p.pk, before=before,
                 after={"name": p.name, "active": p.is_active})
    return p


@transaction.atomic
def delete_product(*, business, user, product_id):
    p = ProductTemplate.objects.select_for_update().get(pk=product_id, business=business)
    if p.batch_set.exists():
        raise DomainError(f"{p.name} is used by production batches and cannot be deleted. Deactivate it instead.")
    if any(_variant_in_use(v) for v in p.variants.all()):
        raise DomainError(f"{p.name} has stock, documents or work history and cannot be deleted. Deactivate it instead.")
    record_audit(business=business, user=user, action="product.delete", entity="ProductTemplate", entity_id=p.pk, before={"name": p.name})
    from production.models import ProductWorkflow
    ProductWorkflow.objects.filter(product=p).delete()
    p.variants.all().delete()
    p.delete()


def _variant_in_use(v):
    """A variant is in use once any stock, document, plan, work or rate record points at it."""
    from django.apps import apps
    for model in apps.get_models():
        for f in model._meta.get_fields():
            if f.is_relation and f.many_to_one and f.related_model is ProductVariant and model is not ProductVariant:
                if model.objects.filter(**{f.name: v}).exists():
                    return True
    return False


@transaction.atomic
def update_variant(*, business, user, variant_id, sku, size="", colour="", unit="pcs"):
    v = ProductVariant.objects.select_for_update().select_related("product").get(pk=variant_id, business=business)
    sku = (sku or "").strip().upper()
    size, colour = (size or "").strip(), (colour or "").strip()
    if not sku:
        raise DomainError("SKU is required.")
    if v.product.kind != ProductTemplate.FINISHED and (size or colour):
        raise DomainError("Raw materials and accessories have no size or colour.")
    if v.product.kind == ProductTemplate.FINISHED and not size:
        raise DomainError("Finished-good variants need a size.")
    if ProductVariant.objects.filter(business=business, sku=sku).exclude(pk=v.pk).exists():
        raise DomainError(f"SKU {sku} already exists.")
    if v.product.kind == ProductTemplate.FINISHED and v.unit != unit and v.stock_balances.exists():
        raise DomainError("The unit of a variant with stock cannot change.")
    before = {"sku": v.sku, "size": v.size, "colour": v.colour}
    v.sku, v.size, v.colour, v.unit = sku, size, colour, unit
    try:
        with transaction.atomic():
            v.save(update_fields=["sku", "size", "colour", "unit"])
    except IntegrityError:
        raise DomainError("This size/colour combination already exists for the product.")
    record_audit(business=business, user=user, action="variant.update", entity="ProductVariant", entity_id=v.pk, before=before,
                 after={"sku": sku, "size": size, "colour": colour})
    return v


@transaction.atomic
def delete_variant(*, business, user, variant_id):
    v = ProductVariant.objects.select_for_update().get(pk=variant_id, business=business)
    if _variant_in_use(v):
        raise DomainError(f"{v.sku} has stock, documents or work history and cannot be deleted. Deactivate it instead.")
    record_audit(business=business, user=user, action="variant.delete", entity="ProductVariant", entity_id=v.pk, before={"sku": v.sku})
    v.delete()


def require_variant(business, variant_id, *, kind=None, lock=False):
    qs = ProductVariant.objects.select_related("product").filter(business=business, pk=variant_id)
    v = qs.first()
    if v is None:
        raise DomainError("Variant not found.")
    if not v.is_active or not v.product.is_active:
        raise DomainError(f"{v.sku} is inactive.")
    if kind and v.product.kind != kind:
        raise DomainError(f"{v.sku} must be a {dict(ProductTemplate.KINDS)[kind].lower()}.")
    return v
