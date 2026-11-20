import hashlib
import json

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from catalogue.models import ProductTemplate, ProductVariant
from catalogue.services import require_variant
from core.exceptions import DomainError
from core.services import next_number, record_audit
from core.utils import D, ZERO, r2
from inventory.models import StockMovement
from inventory.services import post_movement
from .models import (
    SRC_INSPECTION, SRC_NONE, SRC_WORK, Batch, BatchStage, BatchVariantPlan, FinishedGoodsReceipt,
    MaterialIssue, Operation, OverCutVariance, ProductWorkflow, RateCard, ReworkLink, StageDefinition,
    StageOutcome, Worker, Workflow, WorkflowStep, WorkEntry,
)

REASON_KEYS = {k for k, _ in WorkEntry.REASONS}


# ---------------------------------------------------------------- masters
@transaction.atomic
def create_worker(*, business, user, name, code, phone=""):
    code = (code or "").strip().upper()
    if not name.strip() or not code:
        raise DomainError("Worker name and code are required.")
    if Worker.objects.filter(business=business, code=code).exists():
        raise DomainError(f"Worker code {code} already exists.")
    w = Worker.objects.create(business=business, name=name.strip(), code=code, phone=phone)
    record_audit(business=business, user=user, action="worker.create", entity="Worker", entity_id=w.pk, after={"code": code})
    return w


@transaction.atomic
def create_operation(*, business, user, name, stage=None, is_rework=False, is_payroll_only=False):
    name = " ".join(name.split())
    if not name:
        raise DomainError("Operation name is required.")
    if Operation.objects.filter(business=business, name__iexact=name).exists():
        raise DomainError(f"Operation '{name}' already exists.")
    if is_payroll_only and is_rework:
        raise DomainError("A payroll-only operation cannot be a rework operation.")
    if not is_payroll_only and stage is None:
        raise DomainError("Counting operations must be linked to a stage.")
    op = Operation.objects.create(business=business, name=name, stage=stage, is_rework=is_rework,
                                  is_payroll_only=is_payroll_only)
    record_audit(business=business, user=user, action="operation.create", entity="Operation", entity_id=op.pk, after={"name": name})
    return op


@transaction.atomic
def set_operation_active(*, business, user, operation_id, active):
    op = Operation.objects.select_for_update().get(pk=operation_id, business=business)
    op.is_active = active
    op.save(update_fields=["is_active"])
    record_audit(business=business, user=user, action="operation.active", entity="Operation", entity_id=op.pk, after={"active": active})
    return op


def _delete_or_explain(obj, label):
    """Delete a master record; if other records still point at it, explain instead of crashing."""
    from django.db.models import ProtectedError
    try:
        with transaction.atomic():
            obj.delete()
    except ProtectedError:
        raise DomainError(f"{label} is already used in other records and cannot be deleted. Deactivate it instead.")


@transaction.atomic
def update_worker(*, business, user, worker_id, name, code, phone=""):
    w = Worker.objects.select_for_update().get(pk=worker_id, business=business)
    code = (code or "").strip().upper()
    if not (name or "").strip() or not code:
        raise DomainError("Worker name and code are required.")
    if Worker.objects.filter(business=business, code=code).exclude(pk=w.pk).exists():
        raise DomainError(f"Worker code {code} already exists.")
    before = {"name": w.name, "code": w.code, "phone": w.phone}
    w.name, w.code, w.phone = name.strip(), code, (phone or "").strip()
    w.save(update_fields=["name", "code", "phone"])
    record_audit(business=business, user=user, action="worker.update", entity="Worker", entity_id=w.pk, before=before,
                 after={"name": w.name, "code": w.code, "phone": w.phone})
    return w


@transaction.atomic
def set_worker_active(*, business, user, worker_id, active):
    w = Worker.objects.select_for_update().get(pk=worker_id, business=business)
    w.is_active = active
    w.save(update_fields=["is_active"])
    record_audit(business=business, user=user, action="worker.active", entity="Worker", entity_id=w.pk, after={"active": active})
    return w


@transaction.atomic
def delete_worker(*, business, user, worker_id):
    w = Worker.objects.select_for_update().get(pk=worker_id, business=business)
    record_audit(business=business, user=user, action="worker.delete", entity="Worker", entity_id=w.pk, before={"code": w.code, "name": w.name})
    _delete_or_explain(w, f"Worker {w.name}")


@transaction.atomic
def update_operation(*, business, user, operation_id, name, stage=None, is_rework=False, is_payroll_only=False):
    op = Operation.objects.select_for_update().get(pk=operation_id, business=business)
    name = " ".join((name or "").split())
    if not name:
        raise DomainError("Operation name is required.")
    if Operation.objects.filter(business=business, name__iexact=name).exclude(pk=op.pk).exists():
        raise DomainError(f"Operation '{name}' already exists.")
    if is_payroll_only and is_rework:
        raise DomainError("A payroll-only operation cannot be a rework operation.")
    if not is_payroll_only and stage is None:
        raise DomainError("Counting operations must be linked to a stage.")
    if (op.is_rework, op.is_payroll_only) != (is_rework, is_payroll_only) and WorkEntry.objects.filter(operation=op).exists():
        raise DomainError("Work has been recorded with this operation; its type (rework / payroll-only) can no longer change.")
    before = {"name": op.name, "stage": op.stage_id, "is_rework": op.is_rework, "is_payroll_only": op.is_payroll_only}
    op.name, op.stage, op.is_rework, op.is_payroll_only = name, stage, is_rework, is_payroll_only
    op.save(update_fields=["name", "stage", "is_rework", "is_payroll_only"])
    record_audit(business=business, user=user, action="operation.update", entity="Operation", entity_id=op.pk, before=before,
                 after={"name": name, "stage": stage.pk if stage else None, "is_rework": is_rework, "is_payroll_only": is_payroll_only})
    return op


@transaction.atomic
def delete_operation(*, business, user, operation_id):
    op = Operation.objects.select_for_update().get(pk=operation_id, business=business)
    record_audit(business=business, user=user, action="operation.delete", entity="Operation", entity_id=op.pk, before={"name": op.name})
    _delete_or_explain(op, f"Operation {op.name}")


@transaction.atomic
def create_workflow(*, business, user, name, steps):
    """steps: [(StageDefinition, outcome_source)] in order. A stage may appear at most once."""
    if not steps:
        raise DomainError("A workflow needs at least one stage.")
    ids = [s.pk for s, _ in steps]
    if len(ids) != len(set(ids)):
        raise DomainError("A stage can appear at most once in a workflow.")
    if Workflow.objects.filter(business=business, name__iexact=name.strip()).exists():
        raise DomainError("A workflow with this name already exists.")
    wf = Workflow.objects.create(business=business, name=name.strip())
    for i, (stage, src) in enumerate(steps, start=1):
        WorkflowStep.objects.create(workflow=wf, stage=stage, sequence=i, outcome_source=src)
    record_audit(business=business, user=user, action="workflow.create", entity="Workflow", entity_id=wf.pk,
                 after={"stages": [s.name for s, _ in steps]})
    return wf


@transaction.atomic
def save_stage(*, business, user, name, code, default_outcome_source, sequence=0, stage_id=None):
    """Create (stage_id=None) or update a stage definition."""
    from .models import OUTCOME_SOURCES
    name, code = " ".join((name or "").split()), (code or "").strip().upper()
    if not name or not code:
        raise DomainError("Stage name and code are required.")
    if default_outcome_source not in dict(OUTCOME_SOURCES):
        raise DomainError("Invalid outcome source.")
    others = StageDefinition.objects.filter(business=business).exclude(pk=stage_id)
    if others.filter(code=code).exists():
        raise DomainError("Stage code already exists.")
    if others.filter(name__iexact=name).exists():
        raise DomainError("A stage with this name already exists.")
    if stage_id:
        st = StageDefinition.objects.select_for_update().get(pk=stage_id, business=business)
        before = {"name": st.name, "code": st.code, "source": st.default_outcome_source, "sequence": st.sequence}
        st.name, st.code, st.default_outcome_source, st.sequence = name, code, default_outcome_source, sequence or 0
        st.save(update_fields=["name", "code", "default_outcome_source", "sequence"])
        action = "stage.update"
    else:
        before = None
        st = StageDefinition.objects.create(business=business, name=name, code=code, default_outcome_source=default_outcome_source,
                                            sequence=sequence or 0)
        action = "stage.create"
    record_audit(business=business, user=user, action=action, entity="StageDefinition", entity_id=st.pk, before=before,
                 after={"name": name, "code": code, "source": default_outcome_source, "sequence": st.sequence})
    return st


@transaction.atomic
def delete_stage(*, business, user, stage_id):
    st = StageDefinition.objects.select_for_update().get(pk=stage_id, business=business)
    record_audit(business=business, user=user, action="stage.delete", entity="StageDefinition", entity_id=st.pk, before={"code": st.code})
    _delete_or_explain(st, f"Stage {st.name}")


@transaction.atomic
def update_workflow(*, business, user, workflow_id, name, steps):
    """Replace a workflow's name and ordered steps. Existing batches keep their own stage snapshot."""
    wf = Workflow.objects.select_for_update().get(pk=workflow_id, business=business)
    if not steps:
        raise DomainError("A workflow needs at least one stage.")
    ids = [s.pk for s, _ in steps]
    if len(ids) != len(set(ids)):
        raise DomainError("A stage can appear at most once in a workflow.")
    name = (name or "").strip()
    if not name:
        raise DomainError("Workflow name is required.")
    if Workflow.objects.filter(business=business, name__iexact=name).exclude(pk=wf.pk).exists():
        raise DomainError("A workflow with this name already exists.")
    before = {"name": wf.name, "stages": [s.stage.name for s in wf.steps.select_related("stage")]}
    wf.name = name
    wf.save(update_fields=["name"])
    wf.steps.all().delete()
    for i, (stage, src) in enumerate(steps, start=1):
        WorkflowStep.objects.create(workflow=wf, stage=stage, sequence=i, outcome_source=src)
    record_audit(business=business, user=user, action="workflow.update", entity="Workflow", entity_id=wf.pk, before=before,
                 after={"name": name, "stages": [s.name for s, _ in steps]})
    return wf


@transaction.atomic
def delete_workflow(*, business, user, workflow_id):
    wf = Workflow.objects.select_for_update().get(pk=workflow_id, business=business)
    if ProductWorkflow.objects.filter(workflow=wf).exists() or Batch.objects.filter(workflow=wf).exists():
        raise DomainError(f"Workflow {wf.name} is the default of a product or the source of a batch and cannot be deleted.")
    record_audit(business=business, user=user, action="workflow.delete", entity="Workflow", entity_id=wf.pk, before={"name": wf.name})
    wf.steps.all().delete()
    wf.delete()


def stage_fingerprint(rows):
    """rows: iterable of (stage_id, sequence, outcome_source). Display names are excluded."""
    canon = json.dumps([[sid, seq, src] for sid, seq, src in sorted(rows, key=lambda r: r[1])],
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


# ---------------------------------------------------------------- rates
@transaction.atomic
def create_rate_card(*, business, user, operation, rate, effective_from, worker=None, product=None,
                     variant=None, reason=""):
    from accounts.permissions import OWNER
    rate = D(rate)
    if rate < 0:
        raise DomainError("Rate cannot be negative.")
    if variant is not None:
        if product is None:
            product = variant.product
        elif variant.product_id != product.pk:
            raise DomainError("Variant does not belong to the product.")
    backdated = effective_from < timezone.localdate()
    if backdated:
        if user.effective_role != OWNER:
            raise DomainError("Backdated rate cards can only be created by the Owner.")
        if not (reason or "").strip():
            raise DomainError("A reason is mandatory for a backdated rate card.")
    card = RateCard.objects.create(business=business, operation=operation, worker=worker, product=product,
                                   variant=variant, rate=rate, effective_from=effective_from,
                                   reason=(reason or "").strip(), is_backdated=backdated, created_by=user)
    record_audit(business=business, user=user, action="rate_card.create", entity="RateCard", entity_id=card.pk,
                 after={"rate": rate, "effective_from": effective_from, "backdated": backdated, "reason": reason})
    return card


def select_rate(*, business, operation, worker, product, variant, work_date):
    """Precedence: W+P+V, W+P, W, P+V, P, default. Latest effective date within a tier."""
    base = RateCard.objects.filter(business=business, operation=operation, effective_from__lte=work_date)
    tiers = []
    if product and variant:
        tiers.append(("worker+product+variant", dict(worker=worker, product=product, variant=variant)))
    if product:
        tiers.append(("worker+product", dict(worker=worker, product=product, variant__isnull=True)))
    tiers.append(("worker", dict(worker=worker, product__isnull=True, variant__isnull=True)))
    if product and variant:
        tiers.append(("product+variant", dict(worker__isnull=True, product=product, variant=variant)))
    if product:
        tiers.append(("product", dict(worker__isnull=True, product=product, variant__isnull=True)))
    tiers.append(("default", dict(worker__isnull=True, product__isnull=True, variant__isnull=True)))
    for name, flt in tiers:
        card = base.filter(**flt).order_by("-effective_from", "-id").first()
        if card:
            return name, card
    raise DomainError(f"No rate card applies to {operation.name} on {work_date}. Create a rate card first.")


# ---------------------------------------------------------------- batches
def default_workflow(product):
    pw = ProductWorkflow.objects.select_related("workflow").filter(product=product).first()
    return pw.workflow if pw else None


def resolve_batch_stages(business, workflow, stages=None):
    """Ordered [(StageDefinition, outcome_source)] a batch will run through.
    stages=None -> the workflow exactly as defined. Otherwise only the ticked stages (each client may need a
    different route): ordered by the stage's configured sequence, then its position in the workflow; the outcome
    source comes from the workflow step when the stage is part of it, else from the stage's default."""
    steps = list(workflow.steps.select_related("stage").order_by("sequence")) if workflow else []
    if stages is None:
        ordered = [(s.stage, s.outcome_source) for s in steps]
    else:
        pos = {s.stage_id: s.sequence for s in steps}
        src = {s.stage_id: s.outcome_source for s in steps}
        uniq = {}
        for st in stages:
            if st.business_id != business.id:
                raise DomainError("Stage belongs to another business.")
            uniq[st.pk] = st
        for st in uniq.values():
            if not st.is_active and st.pk not in pos:
                raise DomainError(f"Stage {st.name} is inactive.")
        chosen = sorted(uniq.values(), key=lambda st: (st.sequence, pos.get(st.pk, 10_000), st.pk))
        ordered = [(st, src.get(st.pk, st.default_outcome_source)) for st in chosen]
    if not ordered:
        raise DomainError("Select at least one workflow stage for the batch.")
    measured = [src for _, src in ordered if src != SRC_NONE]
    if not measured:
        raise DomainError("Select at least one stage that records output (cutting, stitching, inspection...).")
    if measured[0] == SRC_INSPECTION:
        raise DomainError("The first output stage cannot be a direct inspection; tick a counting stage (e.g. cutting) before it.")
    return ordered


def _snapshot_stages(business, batch, ordered):
    rows = []
    for i, (stage, src) in enumerate(ordered, start=1):
        BatchStage.objects.create(business=business, batch=batch, sequence=i, stage=stage, stage_name=stage.name, outcome_source=src)
        rows.append((stage.pk, i, src))
    return stage_fingerprint(rows)


def _normalise_plans(business, product, plans):
    """plans: [(variant, qty)] or [(variant, qty, fabric_type, gsm)] -> [dict]. Lines without quantity are ignored."""
    out, seen = [], set()
    for p in plans:
        v, q = p[0], D(p[1])
        fabric = (p[2] if len(p) > 2 else "") or ""
        gsm = p[3] if len(p) > 3 else None
        if q <= 0:
            continue
        if v.product_id != product.pk or v.business_id != business.id or not v.is_active:
            raise DomainError(f"{v.sku} is not an active variant of {product.name}.")
        if v.pk in seen:
            raise DomainError("Duplicate size in plan.")
        if gsm is not None and (int(gsm) <= 0 or int(gsm) > 2000):
            raise DomainError(f"{v.sku}: GSM must be between 1 and 2000.")
        seen.add(v.pk)
        out.append({"variant": v, "qty": q, "fabric_type": " ".join(str(fabric).split())[:60], "gsm": int(gsm) if gsm else None})
    if not out:
        raise DomainError("Enter a planned quantity for at least one size.")
    return out


def _check_client(business, client):
    if client is None:
        return None
    if client.business_id != business.id or not client.is_customer:
        raise DomainError(f"{client.name} is not a customer.")
    if not client.is_active:
        raise DomainError(f"{client.name} is deactivated.")
    return client


@transaction.atomic
def create_batch(*, business, user, product, plans, start_date, expected_completion=None, notes="", client=None, stages=None):
    """plans: [(variant, qty[, fabric_type, gsm])] -> size-wise plan.
    stages: StageDefinitions ticked on the workflow checklist (None = the product's default workflow).
    The resulting route is snapshotted into the batch; later workflow edits never affect it."""
    if product.kind != ProductTemplate.FINISHED or product.business_id != business.id:
        raise DomainError("Batches are created for finished-good products.")
    wf = default_workflow(product)
    if wf is None:
        raise DomainError("This product has no default production workflow.")
    if stages is None and not wf.steps.exists():
        raise DomainError("The product's workflow has no stages.")
    ordered = resolve_batch_stages(business, wf, stages)
    lines = _normalise_plans(business, product, plans)
    client = _check_client(business, client)
    if expected_completion and expected_completion < start_date:
        raise DomainError("Expected completion is before the start date.")
    number = next_number(business, "batch")
    batch = Batch.objects.create(business=business, number=number, product=product, client=client, workflow=wf,
                                 workflow_fingerprint="pending", planned_qty=sum(l["qty"] for l in lines),
                                 start_date=start_date, expected_completion=expected_completion,
                                 notes=notes, created_by=user)
    fp = _snapshot_stages(business, batch, ordered)
    Batch.objects.filter(pk=batch.pk).update(workflow_fingerprint=fp)
    batch.workflow_fingerprint = fp
    for l in lines:
        BatchVariantPlan.objects.create(business=business, batch=batch, variant=l["variant"], planned_qty=l["qty"],
                                        fabric_type=l["fabric_type"], gsm=l["gsm"])
    record_audit(business=business, user=user, action="batch.create", entity="Batch", entity_id=batch.pk,
                 after={"number": number, "planned": batch.planned_qty, "fingerprint": fp, "client": client.name if client else None,
                        "stages": [s.name for s, _ in ordered]})
    return batch


def _lock_batch(business, batch_id):
    b = Batch.objects.select_for_update().get(pk=batch_id, business=business)
    if b.status in (Batch.COMPLETED, Batch.CANCELLED):
        raise DomainError(f"Batch {b.number} is {b.status}.")
    return b


def batch_has_activity(batch):
    """True once anything was recorded against the batch (then its route and quantities are locked)."""
    return (WorkEntry.objects.filter(batch_stage__batch=batch).exists() or StageOutcome.objects.filter(batch_stage__batch=batch).exists()
            or batch.material_issues.exists() or batch.fg_receipts.exists() or batch.overcuts.exists())


@transaction.atomic
def update_batch(*, business, user, batch_id, start_date, expected_completion=None, notes="", client=None, plans=None, stages=None):
    """Edit an open batch. Client, dates, notes and fabric/GSM can always change; planned quantities and the
    workflow route only while nothing has been recorded against the batch.
    plans: [(variant, qty, fabric_type, gsm)] for the batch's existing sizes; stages as in create_batch."""
    batch = _lock_batch(business, batch_id)
    client = _check_client(business, client)
    if expected_completion and expected_completion < start_date:
        raise DomainError("Expected completion is before the start date.")
    active = batch_has_activity(batch)
    before = {"client": batch.client_id, "start": batch.start_date, "due": batch.expected_completion, "planned": batch.planned_qty}
    if plans is not None:
        existing = {p.variant_id: p for p in batch.plans.select_for_update()}
        lines = _normalise_plans(business, batch.product, plans)
        if {l["variant"].pk for l in lines} != set(existing):
            raise DomainError("Sizes cannot be added or removed from an existing batch; every planned size needs a quantity.")
        for l in lines:
            p = existing[l["variant"].pk]
            if l["qty"] != p.planned_qty and active:
                raise DomainError("Production has already been recorded; planned quantities can no longer change.")
            BatchVariantPlan.objects.filter(pk=p.pk).update(planned_qty=l["qty"], fabric_type=l["fabric_type"], gsm=l["gsm"])
        batch.planned_qty = sum(l["qty"] for l in lines)
    fp = batch.workflow_fingerprint
    if stages is not None:
        ordered = resolve_batch_stages(business, batch.workflow, stages)
        current = [(s.stage_id, s.outcome_source) for s in batch.stages.order_by("sequence")]
        if current != [(st.pk, src) for st, src in ordered]:
            if active:
                raise DomainError("Production has already been recorded; the workflow stages of this batch can no longer change.")
            batch.stages.all().delete()
            fp = _snapshot_stages(business, batch, ordered)
    Batch.objects.filter(pk=batch.pk).update(client=client, start_date=start_date, expected_completion=expected_completion,
                                             notes=notes or "", planned_qty=batch.planned_qty, workflow_fingerprint=fp)
    record_audit(business=business, user=user, action="batch.update", entity="Batch", entity_id=batch.pk, before=before,
                 after={"client": client.pk if client else None, "start": start_date, "due": expected_completion, "planned": batch.planned_qty})
    batch.refresh_from_db()
    return batch


@transaction.atomic
def delete_batch(*, business, user, batch_id):
    batch = Batch.objects.select_for_update().get(pk=batch_id, business=business)
    if batch_has_activity(batch):
        raise DomainError(f"Production, material or stock has been recorded on {batch.number}; it cannot be deleted. Cancel it instead.")
    record_audit(business=business, user=user, action="batch.delete", entity="Batch", entity_id=batch.pk, before={"number": batch.number})
    batch.plans.all().delete()
    batch.stages.all().delete()
    batch.delete()


@transaction.atomic
def cancel_batch(*, business, user, batch_id):
    batch = _lock_batch(business, batch_id)
    if batch.fg_receipts.exists():
        raise DomainError("Finished goods were already received from this batch; complete it instead of cancelling.")
    Batch.objects.filter(pk=batch.pk).update(status=Batch.CANCELLED)
    record_audit(business=business, user=user, action="batch.cancel", entity="Batch", entity_id=batch.pk, before={"status": batch.status})
    batch.refresh_from_db()
    return batch


def _sum(qs, field):
    return qs.aggregate(t=Sum(field))["t"] or ZERO


def stage_totals(batch_stage, variant):
    qs = StageOutcome.objects.filter(batch_stage=batch_stage, variant=variant)
    return {
        "processed_normal": _sum(qs.filter(is_rework_completion=False), "processed_qty"),
        "good": _sum(qs, "good_qty"),
        "rejected": _sum(qs, "rejected_worker_qty") + _sum(qs, "rejected_other_qty"),
        "rework_sent": _sum(qs, "rework_worker_qty") + _sum(qs, "rework_other_qty"),
        "rework_done": _sum(qs.filter(is_rework_completion=True), "processed_qty"),
    }


def previous_measured_stage(batch_stage):
    return (BatchStage.objects.filter(batch=batch_stage.batch, sequence__lt=batch_stage.sequence)
            .exclude(outcome_source=SRC_NONE).order_by("-sequence").first())


def available_input(batch_stage, variant):
    """Pieces this stage may still process. None = unlimited (first stage; governed by plan/over-cut)."""
    prev = previous_measured_stage(batch_stage)
    if prev is None:
        return None
    return stage_totals(prev, variant)["good"] - stage_totals(batch_stage, variant)["processed_normal"]


def final_measured_stage(batch):
    return batch.stages.exclude(outcome_source=SRC_NONE).order_by("-sequence").first()


def fg_available(batch, variant):
    final = final_measured_stage(batch)
    if final is None:
        return ZERO
    received = _sum(FinishedGoodsReceipt.objects.filter(batch=batch, variant=variant), "quantity")
    return stage_totals(final, variant)["good"] - received


# ---------------------------------------------------------------- material issue
@transaction.atomic
def issue_material(*, business, user, batch_id, variant_id, quantity, over_cut_variance=None):
    batch = _lock_batch(business, batch_id)
    v = require_variant(business, variant_id)
    if v.product.kind == ProductTemplate.FINISHED:
        raise DomainError("Only raw materials and accessories can be issued to a batch.")
    quantity = D(quantity)
    if quantity <= 0:
        raise DomainError("Issue quantity must be positive.")
    mv = post_movement(business=business, variant=v, movement_type=StockMovement.ISSUE, quantity=-quantity,
                       user=user, source_type="batch", source_id=batch.pk, note=batch.number)
    issue = MaterialIssue.objects.create(
        business=business, batch=batch, variant=v, quantity=quantity, unit_cost=mv.unit_cost, value=abs(mv.value),
        movement=mv, over_cut_variance=over_cut_variance, issue_date=timezone.localdate(), issued_by=user)
    if batch.status == Batch.PLANNED:
        Batch.objects.filter(pk=batch.pk).update(status=Batch.IN_PROGRESS)
    record_audit(business=business, user=user, action="material.issue", entity="MaterialIssue", entity_id=issue.pk,
                 after={"batch": batch.number, "sku": v.sku, "qty": quantity, "value": issue.value})
    return issue


# ---------------------------------------------------------------- work entries
def _parts(d):
    keys = ("good", "rejected_worker", "rejected_other", "rework_worker", "rework_other")
    parts = {k: D(d.get(k, 0)) for k in keys}
    if any(p < 0 for p in parts.values()):
        raise DomainError("Quantities cannot be negative.")
    return parts


def _check_reason(parts, reason, note):
    bad = parts["rejected_worker"] + parts["rejected_other"] + parts["rework_worker"] + parts["rework_other"]
    if bad > 0:
        if reason not in REASON_KEYS:
            raise DomainError("A rejection/rework reason is required.")
        if reason == "other" and not (note or "").strip():
            raise DomainError("Describe the reason when selecting 'Other'.")
    else:
        reason = ""
    return reason


@transaction.atomic
def record_work_entry(*, business, user, worker_id, operation_id, work_date, variant_id=None, product_id=None,
                      batch_id=None, batch_stage_id=None, quantity=None, good=0, rejected_worker=0,
                      rejected_other=0, rework_worker=0, rework_other=0, reason="", note="",
                      rework_links=None, over_cut_reason="", extra_materials=None):
    """One service for manual and batch-linked entries.
    - Manual (no batch_stage): payroll only, `quantity` is paid in full; no outcome, batch or stock effect.
    - Batch-linked counting operation: processed = good+rej+rework, creates a StageOutcome.
    - Batch-linked payroll-only operation: pays `quantity`, creates no outcome."""
    worker = Worker.objects.get(pk=worker_id, business=business)
    op = Operation.objects.select_related("stage").get(pk=operation_id, business=business)
    if not worker.is_active:
        raise DomainError(f"Worker {worker.name} is inactive.")
    if not op.is_active:
        raise DomainError(f"Operation {op.name} is deactivated; new work entries are blocked.")
    if work_date > timezone.localdate():
        raise DomainError("Work date cannot be in the future.")
    parts = _parts(dict(good=good, rejected_worker=rejected_worker, rejected_other=rejected_other,
                        rework_worker=rework_worker, rework_other=rework_other))
    variant = require_variant(business, variant_id) if variant_id else None
    product = ProductTemplate.objects.get(pk=product_id, business=business) if product_id else None
    bs = None
    outcome_needed = False

    if batch_stage_id:
        bs = BatchStage.objects.select_related("batch").get(pk=batch_stage_id, business=business)
        batch = _lock_batch(business, bs.batch_id)
        if variant is None:
            raise DomainError("Select the size/variant for a batch entry.")
        if not BatchVariantPlan.objects.filter(batch=batch, variant=variant).exists():
            raise DomainError(f"{variant.sku} is not part of batch {batch.number}.")
        product = batch.product
        if op.is_payroll_only:
            if any(parts.values()):
                raise DomainError("Payroll-only operations take a single quantity, no outcome breakdown.")
        else:
            if op.stage_id != bs.stage_id:
                raise DomainError(f"{op.name} does not belong to stage {bs.stage_name} of this batch.")
            if bs.outcome_source != SRC_WORK:
                raise DomainError(f"Stage {bs.stage_name} records outcomes by {bs.outcome_source}, not by operation entries.")
            outcome_needed = True
    else:
        if op.is_rework:
            raise DomainError("Rework operations must be batch-linked.")
        if variant is not None:
            product = variant.product
        if any(parts.values()):
            raise DomainError("Manual entries are payroll-only: enter a single quantity.")

    # ---- quantities
    if outcome_needed:
        processed = sum(parts.values())
        if processed <= 0:
            raise DomainError("Enter quantities for the operation.")
        reason = _check_reason(parts, reason, note)
    else:
        processed = D(quantity)
        if processed <= 0:
            raise DomainError("Quantity must be positive.")
        reason = ""

    if outcome_needed and op.is_rework:
        if parts["rework_worker"] or parts["rework_other"]:
            raise DomainError("A rework operation cannot send pieces to rework again.")
        paid = parts["good"] + parts["rejected_other"]
    elif outcome_needed:
        paid = parts["good"] + parts["rejected_other"] + parts["rework_other"]
    else:
        paid = processed

    # ---- rate + snapshot
    tier, card = select_rate(business=business, operation=op, worker=worker, product=product,
                             variant=variant, work_date=work_date)
    earnings = r2(paid * card.rate)
    entry = WorkEntry.objects.create(
        business=business, worker=worker, operation=op, work_date=work_date, is_manual=bs is None,
        batch_stage=bs, product=product, variant=variant, processed_qty=processed,
        good_qty=parts["good"], rejected_worker_qty=parts["rejected_worker"],
        rejected_other_qty=parts["rejected_other"], rework_worker_qty=parts["rework_worker"],
        rework_other_qty=parts["rework_other"], rejection_reason=reason, rejection_note=note if reason else "",
        paid_qty=paid, rate=card.rate, rate_tier=tier, rate_card=card, earnings=earnings, op_name=op.name,
        op_is_rework=op.is_rework, op_is_payroll_only=op.is_payroll_only,
        stage_name=bs.stage_name if bs else "", note=note if not reason else "", created_by=user)

    if outcome_needed:
        _apply_outcome(business, user, entry, bs, variant, parts, op, rework_links or [],
                       over_cut_reason, extra_materials or [])
        if bs.batch.status == Batch.PLANNED:
            Batch.objects.filter(pk=bs.batch_id).update(status=Batch.IN_PROGRESS)
    record_audit(business=business, user=user, action="work_entry.create", entity="WorkEntry", entity_id=entry.pk,
                 after={"worker": worker.code, "operation": op.name, "paid_qty": paid, "rate": card.rate,
                        "tier": tier, "earnings": earnings})
    return entry


def _apply_outcome(business, user, entry, bs, variant, parts, op, links, over_cut_reason, extra_materials):
    processed = entry.processed_qty
    totals = stage_totals(bs, variant)
    if op.is_rework:
        _link_rework(business, entry, bs, variant, links, processed)
    else:
        avail = available_input(bs, variant)
        if avail is not None:
            if processed > avail:
                raise DomainError(f"Only {avail} pieces of {variant.sku} are available from the previous stage.")
        else:  # first stage: plan / over-cut control
            plan = BatchVariantPlan.objects.get(batch=bs.batch, variant=variant).planned_qty
            before = totals["processed_normal"]
            if before + processed > plan:
                extra = before + processed - max(plan, before)
                if not (over_cut_reason or "").strip():
                    raise DomainError(
                        f"Over-cutting: this exceeds the planned {plan} for {variant.sku} by {extra}. "
                        "Provide a reason and the additional material issue.")
                if not extra_materials:
                    raise DomainError("Over-cutting requires an additional material issue in the same entry.")
                var = OverCutVariance.objects.create(
                    business=business, batch=bs.batch, batch_stage=bs, variant=variant, extra_qty=extra,
                    reason=over_cut_reason.strip(), work_entry=entry, created_by=user)
                for mat_variant_id, mat_qty in extra_materials:  # same transaction; stock checked inside
                    issue_material(business=business, user=user, batch_id=bs.batch_id,
                                   variant_id=mat_variant_id, quantity=mat_qty, over_cut_variance=var)
    StageOutcome.objects.create(
        business=business, batch_stage=bs, variant=variant, source=SRC_WORK, work_entry=entry,
        is_rework_completion=op.is_rework, good_qty=parts["good"], rejected_worker_qty=parts["rejected_worker"],
        rejected_other_qty=parts["rejected_other"], rework_worker_qty=parts["rework_worker"],
        rework_other_qty=parts["rework_other"], processed_qty=processed, rejection_reason=entry.rejection_reason,
        rejection_note=entry.rejection_note, outcome_date=entry.work_date, created_by=user)


def _link_rework(business, entry, bs, variant, links, processed):
    """links: [(source_work_entry_id, qty)]. Sum must equal the rework entry's completed quantity."""
    if not links:
        raise DomainError("A rework entry must be linked to the original operation entries.")
    total = ZERO
    for src_id, lq in links:
        lq = D(lq)
        if lq <= 0:
            raise DomainError("Rework link quantity must be positive.")
        src = WorkEntry.objects.select_for_update().filter(pk=src_id, business=business).first()
        if (src is None or src.batch_stage_id != bs.pk or src.variant_id != variant.pk
                or src.op_is_rework or src.op_is_payroll_only or src.is_manual):
            raise DomainError("Rework must link to a counting-operation entry of the same batch stage and size.")
        sent = src.rework_worker_qty + src.rework_other_qty
        linked = _sum(ReworkLink.objects.filter(source_entry=src), "quantity")
        if linked + lq > sent:
            raise DomainError(f"Entry #{src.pk}: only {sent - linked} pieces remain to be reworked.")
        ReworkLink.objects.create(business=business, source_entry=src, rework_entry=entry, quantity=lq)
        total += lq
    if total != processed:
        raise DomainError(f"Linked rework quantity ({total}) must equal the completed quantity ({processed}).")


@transaction.atomic
def delete_work_entry(*, business, user, entry_id):
    entry = WorkEntry.objects.select_for_update().get(pk=entry_id, business=business)
    if hasattr(entry, "wage_line"):
        raise DomainError("This work entry is on a wage statement and can no longer be changed.")
    if ReworkLink.objects.filter(source_entry=entry).exists():
        raise DomainError("Rework has been recorded against this entry; delete the rework entry first.")
    if entry.overcuts.exists():
        raise DomainError("This entry recorded an over-cut with a material issue and cannot be deleted.")
    if entry.batch_stage_id:
        _lock_batch(business, entry.batch_stage.batch_id)
        if not entry.op_is_rework and not entry.op_is_payroll_only:
            later = StageOutcome.objects.filter(batch_stage__batch=entry.batch_stage.batch,
                                                batch_stage__sequence__gt=entry.batch_stage.sequence,
                                                variant=entry.variant).exists()
            if later:
                raise DomainError("Later stages already depend on this output; it can no longer be deleted.")
            fg = FinishedGoodsReceipt.objects.filter(batch=entry.batch_stage.batch, variant=entry.variant).exists()
            if fg:
                raise DomainError("Finished goods were already received; this entry can no longer be deleted.")
    record_audit(business=business, user=user, action="work_entry.delete", entity="WorkEntry", entity_id=entry.pk,
                 before={"worker": entry.worker.code, "earnings": entry.earnings})
    entry.delete()


@transaction.atomic
def record_inspection(*, business, user, batch_stage_id, variant_id, inspected_date, good=0,
                      rejected_worker=0, rejected_other=0, reason="", note=""):
    """Direct inspection outcome. Cannot send pieces to rework (MVP). Worker-attributable rejects found
    here are unidentified and never reduce wages automatically."""
    bs = BatchStage.objects.select_related("batch").get(pk=batch_stage_id, business=business)
    batch = _lock_batch(business, bs.batch_id)
    if bs.outcome_source != SRC_INSPECTION:
        raise DomainError(f"Stage {bs.stage_name} does not record outcomes by direct inspection.")
    v = require_variant(business, variant_id)
    if not BatchVariantPlan.objects.filter(batch=batch, variant=v).exists():
        raise DomainError(f"{v.sku} is not part of batch {batch.number}.")
    parts = _parts(dict(good=good, rejected_worker=rejected_worker, rejected_other=rejected_other))
    processed = sum(parts.values())
    if processed <= 0:
        raise DomainError("Enter inspected quantities.")
    reason = _check_reason(parts, reason, note)
    avail = available_input(bs, v)
    if avail is not None and processed > avail:
        raise DomainError(f"Only {avail} pieces of {v.sku} are available for inspection.")
    if avail is None:
        raise DomainError("An inspection stage cannot be the first stage of a workflow.")
    out = StageOutcome.objects.create(
        business=business, batch_stage=bs, variant=v, source=SRC_INSPECTION, good_qty=parts["good"],
        rejected_worker_qty=parts["rejected_worker"], rejected_other_qty=parts["rejected_other"],
        processed_qty=processed, rejection_reason=reason, rejection_note=note if reason else "",
        outcome_date=inspected_date, created_by=user)
    if batch.status == Batch.PLANNED:
        Batch.objects.filter(pk=batch.pk).update(status=Batch.IN_PROGRESS)
    record_audit(business=business, user=user, action="inspection.record", entity="StageOutcome", entity_id=out.pk,
                 after={"good": parts["good"], "rejected": parts["rejected_worker"] + parts["rejected_other"]})
    return out


# ---------------------------------------------------------------- finished goods
@transaction.atomic
def receive_finished_goods(*, business, user, batch_id, variant_id, quantity):
    batch = _lock_batch(business, batch_id)
    v = require_variant(business, variant_id, kind=ProductTemplate.FINISHED)
    quantity = D(quantity)
    if quantity <= 0:
        raise DomainError("Quantity must be positive.")
    avail = fg_available(batch, v)
    if quantity > avail:
        raise DomainError(f"Only {avail} good pieces of {v.sku} are available to receive into stock.")
    mv = post_movement(business=business, variant=v, movement_type=StockMovement.FG_RECEIPT, quantity=quantity,
                       user=user, source_type="batch", source_id=batch.pk, note=batch.number)
    rec = FinishedGoodsReceipt.objects.create(business=business, batch=batch, variant=v, quantity=quantity,
                                              movement=mv, receipt_date=timezone.localdate(), received_by=user)
    record_audit(business=business, user=user, action="fg.receive", entity="FinishedGoodsReceipt", entity_id=rec.pk,
                 after={"batch": batch.number, "sku": v.sku, "qty": quantity})
    return rec


@transaction.atomic
def complete_batch(*, business, user, batch_id):
    batch = _lock_batch(business, batch_id)
    final = final_measured_stage(batch)
    for p in batch.plans.select_related("variant"):
        if fg_available(batch, p.variant) != 0:
            raise DomainError(f"{p.variant.sku}: good output has not been fully received into stock.")
    if not FinishedGoodsReceipt.objects.filter(batch=batch).exists():
        raise DomainError("Nothing has been received into finished goods yet.")
    Batch.objects.filter(pk=batch.pk).update(status=Batch.COMPLETED, actual_completion=timezone.localdate())
    record_audit(business=business, user=user, action="batch.complete", entity="Batch", entity_id=batch.pk)
    batch.refresh_from_db()
    return batch
