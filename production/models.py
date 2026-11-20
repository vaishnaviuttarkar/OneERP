from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower

from core.fields import money, qty
from core.models import ImmutableModel

SRC_WORK, SRC_INSPECTION, SRC_NONE = "work_entry", "inspection", "none"
OUTCOME_SOURCES = [(SRC_WORK, "Counting operation"), (SRC_INSPECTION, "Direct inspection"), (SRC_NONE, "No outcome (e.g. finished-goods stage)")]


class StageDefinition(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    name = models.CharField(max_length=60)
    code = models.CharField(max_length=30)
    default_outcome_source = models.CharField(max_length=12, choices=OUTCOME_SOURCES, default=SRC_WORK)
    sequence = models.PositiveSmallIntegerField(default=0, help_text="Position of the stage in a batch's workflow (lower runs first).")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sequence", "id"]
        constraints = [models.UniqueConstraint(fields=["business", "code"], name="uniq_stage_code"),
                       models.UniqueConstraint(Lower("name"), "business", name="uniq_stage_name_ci")]

    def __str__(self):
        return self.name


class Workflow(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("name"), "business", name="uniq_workflow_name_ci")]

    def __str__(self):
        return self.name


class WorkflowStep(models.Model):
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="steps")
    stage = models.ForeignKey(StageDefinition, on_delete=models.PROTECT)
    sequence = models.PositiveSmallIntegerField()
    outcome_source = models.CharField(max_length=12, choices=OUTCOME_SOURCES, default=SRC_WORK)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["workflow", "stage"], name="uniq_workflow_stage"),   # stage at most once
            models.UniqueConstraint(fields=["workflow", "sequence"], name="uniq_workflow_sequence"),
        ]


class ProductWorkflow(models.Model):
    """Default production workflow of a finished-good product."""
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    product = models.OneToOneField("catalogue.ProductTemplate", on_delete=models.PROTECT, related_name="default_workflow")
    workflow = models.ForeignKey(Workflow, on_delete=models.PROTECT)


class Worker(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20)
    phone = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "code"], name="uniq_worker_code")]
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Operation(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    stage = models.ForeignKey(StageDefinition, null=True, blank=True, on_delete=models.PROTECT)
    is_rework = models.BooleanField(default=False)
    is_payroll_only = models.BooleanField(default=False,
                                          help_text="Pays for processed quantity; never creates production outcomes.")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("name"), "business", name="uniq_operation_name_ci")]
        ordering = ["name"]

    def __str__(self):
        return self.name


class RateCard(ImmutableModel):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    operation = models.ForeignKey(Operation, on_delete=models.PROTECT)
    worker = models.ForeignKey(Worker, null=True, blank=True, on_delete=models.PROTECT)
    product = models.ForeignKey("catalogue.ProductTemplate", null=True, blank=True, on_delete=models.PROTECT)
    variant = models.ForeignKey("catalogue.ProductVariant", null=True, blank=True, on_delete=models.PROTECT)
    rate = money()
    effective_from = models.DateField()
    reason = models.CharField(max_length=200, blank=True)
    is_backdated = models.BooleanField(default=False)
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(rate__gte=0), name="rate_non_negative"),
            models.CheckConstraint(condition=Q(variant__isnull=True) | Q(product__isnull=False),
                                   name="rate_variant_needs_product"),
            models.CheckConstraint(condition=Q(is_backdated=False) | ~Q(reason=""), name="rate_backdated_needs_reason"),
        ]
        indexes = [models.Index(fields=["business", "operation", "effective_from"])]


class Batch(models.Model):
    PLANNED, IN_PROGRESS, COMPLETED, CANCELLED = "planned", "in_progress", "completed", "cancelled"
    STATUSES = [(PLANNED, "Planned"), (IN_PROGRESS, "In progress"), (COMPLETED, "Completed"), (CANCELLED, "Cancelled")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    number = models.CharField(max_length=30)
    product = models.ForeignKey("catalogue.ProductTemplate", on_delete=models.PROTECT)
    client = models.ForeignKey("parties.Party", null=True, blank=True, on_delete=models.PROTECT, related_name="batches",
                               help_text="Customer (clientele) the batch is produced for.")
    workflow = models.ForeignKey(Workflow, on_delete=models.PROTECT)   # source only; stages are snapshotted
    workflow_fingerprint = models.CharField(max_length=64)
    planned_qty = qty()
    start_date = models.DateField()
    expected_completion = models.DateField(null=True, blank=True)
    actual_completion = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default=PLANNED)
    notes = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["business", "number"], name="uniq_batch_number"),
            models.CheckConstraint(condition=Q(planned_qty__gt=0), name="batch_planned_positive"),
        ]


class BatchStage(models.Model):
    """Immutable snapshot of a workflow step taken when the batch was created."""
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="stages")
    sequence = models.PositiveSmallIntegerField()
    stage = models.ForeignKey(StageDefinition, on_delete=models.PROTECT)
    stage_name = models.CharField(max_length=60)
    outcome_source = models.CharField(max_length=12, choices=OUTCOME_SOURCES)

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["batch", "sequence"], name="uniq_batchstage_seq"),
                       models.UniqueConstraint(fields=["batch", "stage"], name="uniq_batchstage_stage")]


class BatchVariantPlan(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="plans")
    variant = models.ForeignKey("catalogue.ProductVariant", on_delete=models.PROTECT)
    planned_qty = qty()
    fabric_type = models.CharField(max_length=60, blank=True)
    gsm = models.PositiveIntegerField(null=True, blank=True, help_text="Fabric weight in grams per square metre.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["batch", "variant"], name="uniq_batch_variant_plan"),
                       models.CheckConstraint(condition=Q(planned_qty__gt=0), name="bvp_planned_positive")]


class WorkEntry(models.Model):
    REASONS = [("stitching_defect", "Stitching defect"), ("finishing_defect", "Finishing/ironing defect"),
               ("fabric_defect", "Fabric defect"), ("cutting_defect", "Cutting defect"),
               ("size_error", "Size/measurement error"), ("stain_damage", "Stain/damage"), ("other", "Other")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    worker = models.ForeignKey(Worker, on_delete=models.PROTECT)
    operation = models.ForeignKey(Operation, on_delete=models.PROTECT)
    work_date = models.DateField()
    is_manual = models.BooleanField(default=False)
    batch_stage = models.ForeignKey(BatchStage, null=True, blank=True, on_delete=models.PROTECT, related_name="work_entries")
    product = models.ForeignKey("catalogue.ProductTemplate", null=True, blank=True, on_delete=models.PROTECT)
    variant = models.ForeignKey("catalogue.ProductVariant", null=True, blank=True, on_delete=models.PROTECT)
    processed_qty = qty()
    good_qty = qty(default=0)
    rejected_worker_qty = qty(default=0)
    rejected_other_qty = qty(default=0)
    rework_worker_qty = qty(default=0)
    rework_other_qty = qty(default=0)
    rejection_reason = models.CharField(max_length=20, choices=REASONS, blank=True)
    rejection_note = models.CharField(max_length=200, blank=True)
    paid_qty = qty()
    # snapshots
    rate = money()
    rate_tier = models.CharField(max_length=40)
    rate_card = models.ForeignKey(RateCard, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    earnings = money()
    op_name = models.CharField(max_length=100)
    op_is_rework = models.BooleanField(default=False)
    op_is_payroll_only = models.BooleanField(default=False)
    stage_name = models.CharField(max_length=60, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-work_date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(processed_qty__gt=0), name="we_processed_positive"),
            models.CheckConstraint(
                condition=Q(op_is_payroll_only=True) | Q(is_manual=True) | Q(processed_qty=F("good_qty") + F("rejected_worker_qty")
                           + F("rejected_other_qty") + F("rework_worker_qty") + F("rework_other_qty")),
                name="we_processed_equals_parts"),
            models.CheckConstraint(
                condition=Q(good_qty__gte=0, rejected_worker_qty__gte=0, rejected_other_qty__gte=0,
                            rework_worker_qty__gte=0, rework_other_qty__gte=0, paid_qty__gte=0),
                name="we_quantities_non_negative"),
            models.CheckConstraint(condition=Q(is_manual=False) | Q(batch_stage__isnull=True), name="we_manual_has_no_batch"),
            models.CheckConstraint(condition=Q(is_manual=True) | Q(batch_stage__isnull=False), name="we_batch_entry_has_stage"),
        ]
        indexes = [models.Index(fields=["business", "worker", "work_date"])]


class StageOutcome(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    batch_stage = models.ForeignKey(BatchStage, on_delete=models.PROTECT, related_name="outcomes")
    variant = models.ForeignKey("catalogue.ProductVariant", on_delete=models.PROTECT)
    source = models.CharField(max_length=12, choices=[(SRC_WORK, "Work entry"), (SRC_INSPECTION, "Inspection")])
    work_entry = models.OneToOneField(WorkEntry, null=True, blank=True, on_delete=models.CASCADE, related_name="outcome")
    is_rework_completion = models.BooleanField(default=False)
    good_qty = qty(default=0)
    rejected_worker_qty = qty(default=0)
    rejected_other_qty = qty(default=0)
    rework_worker_qty = qty(default=0)
    rework_other_qty = qty(default=0)
    processed_qty = qty()
    rejection_reason = models.CharField(max_length=20, blank=True)
    rejection_note = models.CharField(max_length=200, blank=True)
    outcome_date = models.DateField()
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(processed_qty=F("good_qty") + F("rejected_worker_qty") + F("rejected_other_qty")
                            + F("rework_worker_qty") + F("rework_other_qty")),
                name="so_processed_equals_parts"),
            models.CheckConstraint(condition=Q(processed_qty__gt=0), name="so_processed_positive"),
            models.CheckConstraint(
                condition=Q(source="work_entry", work_entry__isnull=False) | Q(source="inspection", work_entry__isnull=True),
                name="so_source_matches_entry"),
            models.CheckConstraint(
                condition=Q(source="work_entry") | Q(rework_worker_qty=0, rework_other_qty=0),
                name="so_inspection_no_rework"),
        ]


class ReworkLink(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    source_entry = models.ForeignKey(WorkEntry, on_delete=models.PROTECT, related_name="rework_links_out")
    rework_entry = models.ForeignKey(WorkEntry, on_delete=models.CASCADE, related_name="rework_links_in")
    quantity = qty()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="rework_link_positive"),
                       models.UniqueConstraint(fields=["source_entry", "rework_entry"], name="uniq_rework_link")]


class MaterialIssue(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="material_issues")
    variant = models.ForeignKey("catalogue.ProductVariant", on_delete=models.PROTECT)
    quantity = qty()
    unit_cost = models.DecimalField(max_digits=16, decimal_places=6)
    value = money()
    movement = models.OneToOneField("inventory.StockMovement", on_delete=models.PROTECT, related_name="+")
    over_cut_variance = models.ForeignKey("OverCutVariance", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="material_issues")
    issue_date = models.DateField()
    issued_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="mi_qty_positive")]


class OverCutVariance(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="overcuts")
    batch_stage = models.ForeignKey(BatchStage, on_delete=models.PROTECT)
    variant = models.ForeignKey("catalogue.ProductVariant", on_delete=models.PROTECT)
    extra_qty = qty()
    reason = models.CharField(max_length=200)
    work_entry = models.ForeignKey(WorkEntry, null=True, on_delete=models.CASCADE, related_name="overcuts")
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(extra_qty__gt=0) & ~Q(reason=""), name="overcut_valid")]


class FinishedGoodsReceipt(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, related_name="fg_receipts")
    variant = models.ForeignKey("catalogue.ProductVariant", on_delete=models.PROTECT)
    quantity = qty()
    movement = models.OneToOneField("inventory.StockMovement", on_delete=models.PROTECT, related_name="+")
    receipt_date = models.DateField()
    received_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="fgr_qty_positive")]
