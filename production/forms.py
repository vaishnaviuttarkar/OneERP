from django import forms
from django.forms import formset_factory
from django.utils import timezone

from catalogue.models import ProductTemplate, ProductVariant
from core.forms import BootstrapMixin, DateInput
from parties.models import Party
from .models import (OUTCOME_SOURCES, Batch, BatchStage, Operation, StageDefinition, Worker, WorkEntry)

CENTS = dict(max_digits=14, decimal_places=3, required=False, min_value=0)


class BatchForm(BootstrapMixin, forms.Form):
    """Header of a production batch. `stages` is the workflow checklist (rendered by production/batch_form.html)."""
    custom_render = ("stages",)
    product = forms.ModelChoiceField(queryset=ProductTemplate.objects.none())
    client = forms.ModelChoiceField(queryset=Party.objects.none(), required=False, label="Client",
                                    empty_label="Select client (optional)", help_text="The school / customer this batch is produced for.")
    start_date = forms.DateField(widget=DateInput(), initial=timezone.localdate)
    expected_completion = forms.DateField(widget=DateInput(), required=False)
    notes = forms.CharField(required=False, max_length=300)
    stages = forms.ModelMultipleChoiceField(queryset=StageDefinition.objects.none(), required=False,
                                            widget=forms.CheckboxSelectMultiple, label="Workflow stages")

    def __init__(self, *a, business=None, batch=None, **k):
        super().__init__(*a, **k)
        self.fields["product"].queryset = ProductTemplate.objects.filter(business=business, kind="finished", is_active=True)
        clients = Party.objects.filter(business=business, is_customer=True, is_active=True)
        if batch is not None and batch.client_id:
            clients = Party.objects.filter(pk=batch.client_id) | clients      # keep a since-deactivated client selectable
        self.fields["client"].queryset = clients.order_by("name")
        stages = StageDefinition.objects.filter(business=business, is_active=True)
        if batch is not None:
            stages = StageDefinition.objects.filter(pk__in=batch.stages.values("stage_id")) | stages
        self.fields["stages"].queryset = stages.distinct().order_by("sequence", "id")
        if batch is not None:                       # editing: the product (and so the sizes) is fixed
            del self.fields["product"]

    def clean_stages(self):
        stages = self.cleaned_data.get("stages")
        if not stages:
            raise forms.ValidationError("Tick at least one workflow stage this batch goes through.")
        return stages


class PlanLineForm(BootstrapMixin, forms.Form):
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.none(), required=False, label="Size / variant")
    quantity = forms.DecimalField(max_digits=14, decimal_places=3, required=False, min_value=0, label="Planned qty")
    fabric_type = forms.CharField(required=False, max_length=60, label="Fabric type",
                                  widget=forms.TextInput(attrs={"placeholder": "e.g. Poly-cotton", "list": "fabric-types"}))
    gsm = forms.IntegerField(required=False, min_value=1, max_value=2000, label="GSM",
                             widget=forms.NumberInput(attrs={"placeholder": "e.g. 180"}))

    def __init__(self, *a, business=None, product=None, **k):
        super().__init__(*a, **k)
        qs = ProductVariant.objects.filter(business=business, is_active=True, product__kind="finished").select_related("product")
        if product is not None:
            qs = qs.filter(product=product)
        self.fields["variant"].queryset = qs

    def clean(self):
        d = super().clean()
        if d.get("variant") and not d.get("quantity"):
            self.add_error("quantity", "Enter the planned quantity for this size.")
        if (d.get("quantity") or d.get("fabric_type") or d.get("gsm")) and not d.get("variant"):
            self.add_error("variant", "Select the size for this line.")
        return d


PlanFormSet = formset_factory(PlanLineForm, extra=8)
PlanEditFormSet = formset_factory(PlanLineForm, extra=0)


class WorkEntryForm(BootstrapMixin, forms.Form):
    batch_stage = forms.ModelChoiceField(queryset=BatchStage.objects.none(), label="Batch / stage")
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.none(), label="Size / variant")
    worker = forms.ModelChoiceField(queryset=Worker.objects.none())
    operation = forms.ModelChoiceField(queryset=Operation.objects.none())
    work_date = forms.DateField(widget=DateInput(), initial=timezone.localdate)
    quantity = forms.DecimalField(label="Quantity (payroll-only operations)", **CENTS)
    good = forms.DecimalField(label="Good", **CENTS)
    rejected_worker = forms.DecimalField(label="Rejected - worker attributable", **CENTS)
    rejected_other = forms.DecimalField(label="Rejected - not worker attributable", **CENTS)
    rework_worker = forms.DecimalField(label="Sent to rework - worker attributable", **CENTS)
    rework_other = forms.DecimalField(label="Sent to rework - not worker attributable", **CENTS)
    reason = forms.ChoiceField(choices=[("", "-")] + WorkEntry.REASONS, required=False, label="Rejection / rework reason")
    note = forms.CharField(required=False, max_length=200, label="Note (required for 'Other')")
    rework_source = forms.ModelChoiceField(queryset=WorkEntry.objects.none(), required=False,
                                           label="Rework: original entry being reworked")
    over_cut_reason = forms.CharField(required=False, max_length=200, label="Over-cut reason (if exceeding plan)")
    extra_material_1 = forms.ModelChoiceField(queryset=ProductVariant.objects.none(), required=False, label="Extra material 1")
    extra_qty_1 = forms.DecimalField(label="Extra qty 1", **CENTS)
    extra_material_2 = forms.ModelChoiceField(queryset=ProductVariant.objects.none(), required=False, label="Extra material 2")
    extra_qty_2 = forms.DecimalField(label="Extra qty 2", **CENTS)

    def __init__(self, *a, business=None, batch=None, **k):
        super().__init__(*a, **k)
        stages = BatchStage.objects.filter(business=business, batch__status__in=["planned", "in_progress"]).select_related("batch")
        if batch:
            stages = stages.filter(batch=batch)
        self.fields["batch_stage"].queryset = stages.order_by("-batch_id", "sequence")
        self.fields["batch_stage"].label_from_instance = lambda s: f"{s.batch.number} - {s.stage_name}"
        self.fields["variant"].queryset = ProductVariant.objects.filter(business=business, product__kind="finished", is_active=True).select_related("product")
        self.fields["worker"].queryset = Worker.objects.filter(business=business, is_active=True)
        self.fields["operation"].queryset = Operation.objects.filter(business=business, is_active=True)
        self.fields["rework_source"].queryset = WorkEntry.objects.filter(business=business, op_is_rework=False, is_manual=False).exclude(
            rework_worker_qty=0, rework_other_qty=0).select_related("worker", "batch_stage")
        self.fields["rework_source"].label_from_instance = lambda e: f"#{e.pk} {e.batch_stage.batch.number} {e.worker.name} (rework {e.rework_worker_qty + e.rework_other_qty})"
        mats = ProductVariant.objects.filter(business=business, is_active=True, product__kind__in=["raw", "accessory"]).select_related("product")
        self.fields["extra_material_1"].queryset = mats
        self.fields["extra_material_2"].queryset = mats


class ManualEntryForm(BootstrapMixin, forms.Form):
    worker = forms.ModelChoiceField(queryset=Worker.objects.none())
    operation = forms.ModelChoiceField(queryset=Operation.objects.none())
    work_date = forms.DateField(widget=DateInput(), initial=timezone.localdate)
    quantity = forms.DecimalField(max_digits=14, decimal_places=3, min_value=0)
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.none(), required=False,
                                     help_text="Optional - only used to pick a product/size specific rate.")
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["worker"].queryset = Worker.objects.filter(business=business, is_active=True)
        self.fields["operation"].queryset = Operation.objects.filter(business=business, is_active=True)
        self.fields["variant"].queryset = ProductVariant.objects.filter(business=business, product__kind="finished", is_active=True).select_related("product")


class InspectionForm(BootstrapMixin, forms.Form):
    batch_stage = forms.ModelChoiceField(queryset=BatchStage.objects.none(), label="Batch / inspection stage")
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.none())
    inspected_date = forms.DateField(widget=DateInput(), initial=timezone.localdate)
    good = forms.DecimalField(**CENTS)
    rejected_worker = forms.DecimalField(label="Rejected - worker attributable (unidentified)", **CENTS)
    rejected_other = forms.DecimalField(label="Rejected - not worker attributable", **CENTS)
    reason = forms.ChoiceField(choices=[("", "-")] + WorkEntry.REASONS, required=False)
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["batch_stage"].queryset = BatchStage.objects.filter(
            business=business, outcome_source="inspection", batch__status__in=["planned", "in_progress"]).select_related("batch")
        self.fields["batch_stage"].label_from_instance = lambda s: f"{s.batch.number} - {s.stage_name}"
        self.fields["variant"].queryset = ProductVariant.objects.filter(business=business, product__kind="finished").select_related("product")


class MaterialIssueForm(BootstrapMixin, forms.Form):
    batch = forms.ModelChoiceField(queryset=Batch.objects.none())
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.none(), label="Material")
    quantity = forms.DecimalField(max_digits=14, decimal_places=3, min_value=0)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["batch"].queryset = Batch.objects.filter(business=business, status__in=["planned", "in_progress"])
        self.fields["variant"].queryset = ProductVariant.objects.filter(business=business, is_active=True, product__kind__in=["raw", "accessory"]).select_related("product")


class FGReceiptForm(BootstrapMixin, forms.Form):
    batch = forms.ModelChoiceField(queryset=Batch.objects.none())
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.none())
    quantity = forms.DecimalField(max_digits=14, decimal_places=3, min_value=0)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["batch"].queryset = Batch.objects.filter(business=business, status__in=["planned", "in_progress"])
        self.fields["variant"].queryset = ProductVariant.objects.filter(business=business, product__kind="finished").select_related("product")


class WorkerForm(BootstrapMixin, forms.Form):
    name = forms.CharField(max_length=100)
    code = forms.CharField(max_length=20)
    phone = forms.CharField(required=False)


class OperationForm(BootstrapMixin, forms.Form):
    name = forms.CharField(max_length=100)
    stage = forms.ModelChoiceField(queryset=StageDefinition.objects.none(), required=False)
    is_rework = forms.BooleanField(required=False, label="Rework operation")
    is_payroll_only = forms.BooleanField(required=False, label="Payroll-only (no production outcome)")

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["stage"].queryset = StageDefinition.objects.filter(business=business)


class StageForm(BootstrapMixin, forms.Form):
    name = forms.CharField(max_length=60)
    code = forms.CharField(max_length=30)
    default_outcome_source = forms.ChoiceField(choices=OUTCOME_SOURCES)
    sequence = forms.IntegerField(min_value=0, max_value=999, required=False, initial=0, label="Order in workflow",
                                  help_text="Lower numbers run first when stages are ticked on a batch (e.g. Cutting 10, Stitching 20).")


class WorkflowForm(BootstrapMixin, forms.Form):
    name = forms.CharField(max_length=100)


class StepForm(BootstrapMixin, forms.Form):
    stage = forms.ModelChoiceField(queryset=StageDefinition.objects.none(), required=False)
    outcome_source = forms.ChoiceField(choices=OUTCOME_SOURCES, required=False)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["stage"].queryset = StageDefinition.objects.filter(business=business, is_active=True)


StepFormSet = formset_factory(StepForm, extra=7)


class RateCardForm(BootstrapMixin, forms.Form):
    operation = forms.ModelChoiceField(queryset=Operation.objects.none())
    worker = forms.ModelChoiceField(queryset=Worker.objects.none(), required=False)
    product = forms.ModelChoiceField(queryset=ProductTemplate.objects.none(), required=False)
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.none(), required=False)
    rate = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, label="Rate per piece (₹)")
    effective_from = forms.DateField(widget=DateInput(), initial=timezone.localdate)
    reason = forms.CharField(required=False, max_length=200, help_text="Mandatory for backdated cards (Owner only).")

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["operation"].queryset = Operation.objects.filter(business=business, is_active=True)
        self.fields["worker"].queryset = Worker.objects.filter(business=business)
        self.fields["product"].queryset = ProductTemplate.objects.filter(business=business, kind="finished")
        self.fields["variant"].queryset = ProductVariant.objects.filter(business=business, product__kind="finished").select_related("product")
