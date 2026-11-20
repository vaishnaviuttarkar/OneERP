import json

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils.html import format_html, format_html_join
from django.views.decorators.http import require_POST

from accounts.permissions import ACCOUNTS, OWNER, PRODUCTION, role_required
from core.services import get_current_business
from core.viewkit import (badge, detail_page, form_page, handle_post, inr, link, post_btn_csrf, qf, render_list,
                          row_actions, search_filter)
from parties.models import Party
from reports.selectors import batch_summary, stage_output_rows, stage_progress
from . import forms as F
from . import services
from .models import (Batch, BatchVariantPlan, FinishedGoodsReceipt, MaterialIssue, Operation, ProductWorkflow, RateCard,
                     StageDefinition, Worker, Workflow, WorkEntry)

PROD = (OWNER, PRODUCTION, ACCOUNTS)
MONEY_ROLES = (OWNER, ACCOUNTS)
OPEN = (Batch.PLANNED, Batch.IN_PROGRESS)


def _money(request):
    return request.user.effective_role in MONEY_ROLES


@role_required(*PROD)
def batches(request):
    b = get_current_business()
    qs = search_filter(Batch.objects.filter(business=b).select_related("product", "client"), request.GET.get("q", "").strip(),
                       "number", "product__name", "client__name", "notes")
    st, client = request.GET.get("status", ""), request.GET.get("client", "")
    if st:
        qs = qs.filter(status=st)
    if client == "none":
        qs = qs.filter(client__isnull=True)
    elif client.isdigit():
        qs = qs.filter(client_id=int(client))
    rows = []
    for x in qs:
        s = batch_summary(x)
        acts = row_actions(request, edit=f"/production/batches/{x.pk}/edit/" if x.status in OPEN else None,
                           delete=f"/production/batches/{x.pk}/delete/" if not services.batch_has_activity(x) else None,
                           confirm=f"Delete batch {x.number}? Only batches with nothing recorded can be deleted.")
        rows.append([link(f"/production/batches/{x.pk}/", x.number), x.client.name if x.client else "-", x.product.name, qf(s["planned"]),
                     qf(s["completed"]), qf(s["pending"]), qf(s["rejected"]), s["current_stage"],
                     x.expected_completion or "-", badge("delayed", "danger") if s["delayed"] else badge(x.status), acts])
    clients = [(str(p.pk), p.name) for p in Party.objects.filter(business=b, batches__isnull=False).distinct().order_by("name")]
    return render_list(request, title="Production batches",
                       headers=["Batch", "Client", "Product", "Planned", "Completed", "Pending", "Rejected", "Stage", "Due", "Status", ""],
                       rows=rows, add_url="/production/batches/new/", add_label="New batch",
                       filters=[{"name": "status", "label": "Status", "current": st, "options": Batch.STATUSES},
                                {"name": "client", "label": "Client", "current": client, "options": clients + [("none", "No client")]}],
                       extra_actions=[("Work entry", "/production/entries/new/"), ("Issue material", "/production/issue/"), ("Inspection", "/production/inspection/"), ("Receive FG", "/production/fg-receipt/")])


def _batch_form_context(b, form):
    """Stage descriptions, each product's default stage ids (pre-ticks the checklist) and fabric suggestions."""
    defaults = {str(pw.product_id): list(pw.workflow.steps.values_list("stage_id", flat=True))
                for pw in ProductWorkflow.objects.filter(business=b).select_related("workflow")}
    meta = {st.pk: st.get_default_outcome_source_display() for st in form.fields["stages"].queryset}
    fabrics = sorted(set(BatchVariantPlan.objects.filter(business=b).exclude(fabric_type="").values_list("fabric_type", flat=True)))
    return {"product_defaults_json": json.dumps(defaults), "stage_meta": meta,
            "fabric_suggestions": fabrics or ["Poly-cotton", "Cotton", "Polyester", "Terry cotton", "Denim", "Pique knit"]}


def _plans(formset):
    return [(c["variant"], c["quantity"], c.get("fabric_type") or "", c.get("gsm"))
            for c in formset.cleaned_data if c.get("variant") and c.get("quantity")]


@role_required(*PROD)
def batch_create(request):
    b = get_current_business()
    form = F.BatchForm(request.POST or None, business=b)
    fs = F.PlanFormSet(request.POST or None, form_kwargs={"business": b})

    def handler(f, formsets):
        d = f.cleaned_data
        return services.create_batch(business=b, user=request.user, product=d["product"], plans=_plans(formsets["Size-wise plan"]),
                                     start_date=d["start_date"], expected_completion=d["expected_completion"], notes=d["notes"],
                                     client=d["client"], stages=list(d["stages"]))
    return form_page(request, form, "New production batch", handler, lambda x: f"/production/batches/{x.pk}/",
                     formsets={"Size-wise plan": fs}, back="/production/batches/", template="production/batch_form.html",
                     context=_batch_form_context(b, form), submit="Create batch",
                     note="Pick the client, plan each size with its fabric and GSM, then tick the workflow stages this order needs. "
                          "The selected route is snapshotted into the batch; later workflow edits will not affect it.")


@role_required(*PROD)
def batch_edit(request, pk):
    b = get_current_business()
    batch = get_object_or_404(Batch.objects.select_related("product", "client"), pk=pk, business=b)
    if batch.status not in OPEN:
        messages.error(request, f"Batch {batch.number} is {batch.get_status_display().lower()} and can no longer be edited.")
        return redirect(f"/production/batches/{batch.pk}/")
    locked = services.batch_has_activity(batch)
    initial = {"client": batch.client_id, "start_date": batch.start_date, "expected_completion": batch.expected_completion,
               "notes": batch.notes, "stages": list(batch.stages.values_list("stage_id", flat=True))}
    form = F.BatchForm(request.POST or None, business=b, batch=batch, initial=initial)
    if locked:
        form.fields["stages"].disabled = True     # disabled fields keep their initial value on POST
    plan_initial = [{"variant": p.variant_id, "quantity": p.planned_qty, "fabric_type": p.fabric_type, "gsm": p.gsm}
                    for p in batch.plans.order_by("id")]
    fs = F.PlanEditFormSet(request.POST or None, initial=plan_initial, form_kwargs={"business": b, "product": batch.product})
    for f_ in fs.forms:
        f_.fields["variant"].disabled = True       # sizes are fixed once the batch exists
        if locked:
            f_.fields["quantity"].disabled = True

    def handler(f, formsets):
        d = f.cleaned_data
        return services.update_batch(business=b, user=request.user, batch_id=batch.pk, start_date=d["start_date"],
                                     expected_completion=d["expected_completion"], notes=d["notes"], client=d["client"],
                                     plans=_plans(formsets["Size-wise plan"]), stages=list(d["stages"]))
    return form_page(request, form, f"Edit batch {batch.number}", handler, lambda x: f"/production/batches/{x.pk}/",
                     formsets={"Size-wise plan": fs}, back=f"/production/batches/{batch.pk}/", template="production/batch_form.html",
                     context={**_batch_form_context(b, form), "stages_locked": locked}, submit="Save changes", success_msg="Batch updated.",
                     note=("Production has been recorded, so planned quantities and workflow stages are locked. Client, dates, notes, "
                           "fabric type and GSM can still be changed.") if locked else
                          "Product and sizes are fixed. Everything else, including the workflow stages, can change until production starts.")


@role_required(*PROD)
@require_POST
def batch_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_batch(business=b, user=request.user, batch_id=pk), "/production/batches/", "Batch deleted.")


@role_required(*PROD)
@require_POST
def batch_cancel(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.cancel_batch(business=b, user=request.user, batch_id=pk), f"/production/batches/{pk}/", "Batch cancelled.")


def _workflow_html(batch):
    """Stage stepper for the batch page: done / current / upcoming with good output and progress per stage."""
    steps = stage_progress(batch)
    if not steps:
        return format_html('<div class="text-muted">No stages.</div>')
    items = format_html_join("", '<li class="wf-step {}"><span class="dot">{}</span><div class="wf-body"><b>{}</b><small>{}</small>'
                                 '<div class="wf-bar"><i style="width:{}%"></i></div><small class="wf-qty">{}</small></div></li>',
                             ((s["state"], format_html('<i class="bi bi-check-lg"></i>') if s["state"] == "done" else s["seq"], s["name"],
                               s["source_label"], s["pct"], s["qty_label"]) for s in steps))
    return format_html('<ol class="wf-steps">{}</ol>', items)


@role_required(*PROD)
def batch_detail(request, pk):
    b = get_current_business()
    batch = get_object_or_404(Batch.objects.select_related("product", "client"), pk=pk, business=b)
    s = batch_summary(batch)
    money = _money(request)
    out_rows = [[r["stage"], r["variant"].sku, r["source"].replace("_", " "), qf(r["processed_normal"]), qf(r["good"]), qf(r["rejected"]),
                 qf(r["rework_sent"]), qf(r["rework_done"])] for r in stage_output_rows(batch)]
    issues = [[i.issue_date, i.variant.label, qf(i.quantity)] + ([inr(i.value)] if money else []) + ["Over-cut" if i.over_cut_variance_id else ""]
              for i in batch.material_issues.select_related("variant__product")]
    entries = []
    for e in WorkEntry.objects.filter(batch_stage__batch=batch).select_related("worker", "variant"):
        row = [e.work_date, e.worker.name, e.op_name, e.variant.sku if e.variant else "-", qf(e.good_qty), qf(e.rejected_worker_qty + e.rejected_other_qty),
               qf(e.rework_worker_qty + e.rework_other_qty)]
        if money:
            row += [qf(e.paid_qty), inr(e.rate), inr(e.earnings)]
        if not hasattr(e, "wage_line"):
            row.append(post_btn_csrf(request, f"/production/entries/{e.pk}/delete/", "Delete", "Delete this entry?", "btn-outline-danger"))
        else:
            row.append("Locked")
        entries.append(row)
    over = [[o.batch_stage.stage_name, o.variant.sku, qf(o.extra_qty), o.reason] for o in batch.overcuts.select_related("variant", "batch_stage")]
    fgr = [[r.receipt_date, r.variant.sku, qf(r.quantity)] for r in FinishedGoodsReceipt.objects.filter(batch=batch).select_related("variant")]
    plan = [[p.variant.sku, p.variant.size or "-", qf(p.planned_qty), p.fabric_type or "-", p.gsm or "-"]
            for p in batch.plans.select_related("variant").order_by("id")]
    ent_headers = ["Date", "Worker", "Operation", "Size", "Good", "Rejected", "Rework"] + (["Paid qty", "Rate", "Earnings"] if money else []) + [""]
    buttons = []
    if batch.status in OPEN:
        buttons = [format_html('<a class="btn btn-sm btn-outline-secondary" href="/production/batches/{}/edit/"><i class="bi bi-pencil-square me-1"></i>Edit</a>', batch.pk),
                   post_btn_csrf(request, f"/production/batches/{batch.pk}/complete/", "Complete batch", "Mark this batch complete?", "btn-success")]
        if not batch.fg_receipts.exists():
            buttons.append(post_btn_csrf(request, f"/production/batches/{batch.pk}/cancel/", "Cancel batch", f"Cancel batch {batch.number}?", "btn-outline-danger"))
    if not services.batch_has_activity(batch):
        buttons.append(post_btn_csrf(request, f"/production/batches/{batch.pk}/delete/", "Delete", f"Delete batch {batch.number}? This cannot be undone.",
                                     "btn-outline-danger", icon="bi-trash3"))
    client = link(f"/parties/{batch.client_id}/", batch.client.name) if batch.client and _money(request) else (batch.client.name if batch.client else "-")
    route = " → ".join(st.stage_name for st in batch.stages.all())
    return detail_page(request, title=batch.number, badge_html=badge("delayed", "danger") if s["delayed"] else badge(batch.status),
                       subtitle=f"{batch.product.name} · {batch.client.name if batch.client else 'no client'}" + (f" · {batch.notes}" if batch.notes else ""),
                       facts=[("Client", client), ("Planned", qf(s["planned"])), ("Completed (final stage good)", qf(s["completed"])), ("Pending", qf(s["pending"])),
                              ("Rejected", qf(s["rejected"])), ("Rework pending", qf(s["rework_pending"])), ("Received into stock", qf(s["received"])),
                              ("Current stage", s["current_stage"]), ("Start / expected end", f"{batch.start_date} / {batch.expected_completion or '-'}"),
                              ("Progress", f"{s['progress']}%")],
                       sections=[{"title": format_html('Workflow <small class="text-muted fw-normal">· {} stages · {}</small>', batch.stages.count(), route),
                                  "html": _workflow_html(batch)},
                                 {"title": "Size-wise plan", "headers": ["SKU", "Size", "Planned", "Fabric type", "GSM"], "rows": plan},
                                 {"title": "Stage-wise output", "headers": ["Stage", "Size", "Source", "Processed", "Good", "Rejected", "Sent to rework", "Rework done"], "rows": out_rows},
                                 {"title": "Material issued", "headers": ["Date", "Material", "Qty"] + (["Cost"] if money else []) + [""], "rows": issues},
                                 {"title": "Work entries", "headers": ent_headers, "rows": entries},
                                 {"title": "Over-cutting variance", "headers": ["Stage", "Size", "Extra qty", "Reason"], "rows": over},
                                 {"title": "Finished goods receipts", "headers": ["Date", "Size", "Qty"], "rows": fgr}],
                       buttons=buttons, back="/production/batches/")


@role_required(*PROD)
@require_POST
def batch_complete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.complete_batch(business=b, user=request.user, batch_id=pk), f"/production/batches/{pk}/", "Batch completed.")


@role_required(*PROD)
def entry_new(request):
    b = get_current_business()
    batch = Batch.objects.filter(business=b, pk=request.GET.get("batch")).first()
    form = F.WorkEntryForm(request.POST or None, business=b, batch=batch)

    def handler(f, fs):
        d = f.cleaned_data
        links = [(d["rework_source"].pk, (d["good"] or 0) + (d["rejected_worker"] or 0) + (d["rejected_other"] or 0))] if d["rework_source"] else []
        extras = [(d[f"extra_material_{i}"].pk, d[f"extra_qty_{i}"]) for i in (1, 2) if d[f"extra_material_{i}"] and d[f"extra_qty_{i}"]]
        return services.record_work_entry(
            business=b, user=request.user, worker_id=d["worker"].pk, operation_id=d["operation"].pk, work_date=d["work_date"],
            variant_id=d["variant"].pk, batch_id=d["batch_stage"].batch_id, batch_stage_id=d["batch_stage"].pk, quantity=d["quantity"],
            good=d["good"] or 0, rejected_worker=d["rejected_worker"] or 0, rejected_other=d["rejected_other"] or 0,
            rework_worker=d["rework_worker"] or 0, rework_other=d["rework_other"] or 0, reason=d["reason"], note=d["note"],
            rework_links=links, over_cut_reason=d["over_cut_reason"], extra_materials=extras)
    return form_page(request, form, "Batch work entry", handler, lambda e: f"/production/batches/{e.batch_stage.batch_id}/",
                     back="/production/batches/",
                     note="Processed = good + rejected + sent to rework. Payroll-only operations use the single Quantity field instead. "
                          "Rework entries must pick the original entry being reworked.")


@role_required(*PROD)
def manual_entry_new(request):
    b = get_current_business()
    form = F.ManualEntryForm(request.POST or None, business=b)

    def handler(f, fs):
        d = f.cleaned_data
        return services.record_work_entry(business=b, user=request.user, worker_id=d["worker"].pk, operation_id=d["operation"].pk,
                                          work_date=d["work_date"], variant_id=d["variant"].pk if d["variant"] else None,
                                          quantity=d["quantity"], note=d["note"])
    return form_page(request, form, "Manual work entry (payroll only)", handler, "/production/entries/", back="/production/entries/",
                     note="Manual entries affect payroll only - never batches, outcomes or stock.")


@role_required(*PROD)
def entries(request):
    b = get_current_business()
    money = _money(request)
    qs = WorkEntry.objects.filter(business=b).select_related("worker", "batch_stage__batch", "variant")
    qs = search_filter(qs, request.GET.get("q", "").strip(), "worker__name", "op_name", "batch_stage__batch__number")
    rows = []
    for e in qs[:300]:
        on_statement = hasattr(e, "wage_line")
        batch_cell = "Manual" if e.is_manual else link(f"/production/batches/{e.batch_stage.batch_id}/", e.batch_stage.batch.number)
        row = [e.work_date, e.worker.name, e.op_name, batch_cell, qf(e.processed_qty)]
        if money:
            row += [qf(e.paid_qty), inr(e.rate), e.rate_tier, inr(e.earnings), "On statement" if on_statement else "Open"]
        # work entries are corrected by delete + re-enter (outcomes, rates and payroll are snapshotted on entry)
        row.append(row_actions(request, delete=None if on_statement else f"/production/entries/{e.pk}/delete/",
                               confirm="Delete this work entry? Re-enter it if it needs correcting."))
        rows.append(row)
    return render_list(request, title="Work entries", headers=["Date", "Worker", "Operation", "Batch", "Processed"] + (["Paid qty", "Rate", "Rate tier", "Earnings", "Payroll"] if money else []) + [""],
                       rows=rows, add_url="/production/entries/new/", add_label="Batch entry",
                       subtitle="To correct an open entry, delete it and enter it again. Entries on a wage statement are locked.",
                       extra_actions=[("Manual entry", "/production/entries/manual/")])


@role_required(*PROD)
@require_POST
def entry_delete(request, pk):
    b = get_current_business()
    e = get_object_or_404(WorkEntry, pk=pk, business=b)
    back = f"/production/batches/{e.batch_stage.batch_id}/" if e.batch_stage_id else "/production/entries/"
    return handle_post(request, lambda: services.delete_work_entry(business=b, user=request.user, entry_id=pk), back, "Entry deleted.")


@role_required(*PROD)
def inspection_new(request):
    b = get_current_business()
    form = F.InspectionForm(request.POST or None, business=b)

    def handler(f, fs):
        d = f.cleaned_data
        return services.record_inspection(business=b, user=request.user, batch_stage_id=d["batch_stage"].pk, variant_id=d["variant"].pk,
                                          inspected_date=d["inspected_date"], good=d["good"] or 0, rejected_worker=d["rejected_worker"] or 0,
                                          rejected_other=d["rejected_other"] or 0, reason=d["reason"], note=d["note"])
    return form_page(request, form, "Direct inspection", handler, lambda o: f"/production/batches/{o.batch_stage.batch_id}/", back="/production/batches/",
                     note="Direct inspection cannot send pieces to rework, and unidentified worker rejects never reduce wages automatically.")


@role_required(*PROD)
def issue_material(request):
    b = get_current_business()
    form = F.MaterialIssueForm(request.POST or None, business=b)
    return form_page(request, form, "Issue material to batch",
                     lambda f, fs: services.issue_material(business=b, user=request.user, batch_id=f.cleaned_data["batch"].pk,
                                                          variant_id=f.cleaned_data["variant"].pk, quantity=f.cleaned_data["quantity"]),
                     lambda i: f"/production/batches/{i.batch_id}/", back="/production/batches/")


@role_required(*PROD)
def fg_receive(request):
    b = get_current_business()
    form = F.FGReceiptForm(request.POST or None, business=b)
    return form_page(request, form, "Receive finished goods into stock",
                     lambda f, fs: services.receive_finished_goods(business=b, user=request.user, batch_id=f.cleaned_data["batch"].pk,
                                                                   variant_id=f.cleaned_data["variant"].pk, quantity=f.cleaned_data["quantity"]),
                     lambda r: f"/production/batches/{r.batch_id}/", back="/production/batches/")


# ---------------------------------------------------------------- masters
@role_required(*PROD)
def workers(request):
    b = get_current_business()
    qs = search_filter(Worker.objects.filter(business=b), request.GET.get("q", "").strip(), "name", "code")
    rows = []
    for w in qs:
        toggle = post_btn_csrf(request, f"/production/workers/{w.pk}/toggle/", "Deactivate" if w.is_active else "Activate",
                               f"{'Deactivate' if w.is_active else 'Activate'} {w.name}?", "btn-outline-secondary act-btn")
        rows.append([w.code, w.name, w.phone or "-", badge("Active" if w.is_active else "Inactive"),
                     row_actions(request, edit=f"/production/workers/{w.pk}/edit/", delete=f"/production/workers/{w.pk}/delete/", extra=[toggle],
                                 confirm=f"Delete worker {w.name}? Workers with recorded work cannot be deleted - deactivate them instead.")])
    return render_list(request, title="Workers", headers=["Code", "Name", "Phone", "Status", ""], rows=rows, add_url="/production/workers/new/",
                       add_label="New worker", extra_actions=[("Operations", "/production/operations/"), ("Stages & workflows", "/production/workflows/")])


@role_required(*PROD)
def worker_new(request):
    b = get_current_business()
    form = F.WorkerForm(request.POST or None)
    return form_page(request, form, "New worker", lambda f, fs: services.create_worker(business=b, user=request.user, **f.cleaned_data),
                     "/production/workers/", back="/production/workers/")


@role_required(*PROD)
def worker_edit(request, pk):
    b = get_current_business()
    w = get_object_or_404(Worker, pk=pk, business=b)
    form = F.WorkerForm(request.POST or None, initial={"name": w.name, "code": w.code, "phone": w.phone})
    return form_page(request, form, f"Edit worker {w.name}",
                     lambda f, fs: services.update_worker(business=b, user=request.user, worker_id=w.pk, **f.cleaned_data),
                     "/production/workers/", back="/production/workers/", submit="Save changes", success_msg="Worker updated.")


@role_required(*PROD)
@require_POST
def worker_toggle(request, pk):
    b = get_current_business()
    w = get_object_or_404(Worker, pk=pk, business=b)
    return handle_post(request, lambda: services.set_worker_active(business=b, user=request.user, worker_id=pk, active=not w.is_active),
                       "/production/workers/", "Worker updated.")


@role_required(*PROD)
@require_POST
def worker_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_worker(business=b, user=request.user, worker_id=pk), "/production/workers/", "Worker deleted.")


@role_required(*PROD)
def operations(request):
    b = get_current_business()
    rows = []
    for o in Operation.objects.filter(business=b).select_related("stage"):
        kind = "Payroll-only" if o.is_payroll_only else ("Rework" if o.is_rework else "Counting")
        toggle = post_btn_csrf(request, f"/production/operations/{o.pk}/toggle/", "Deactivate" if o.is_active else "Activate", "Change status?",
                               "btn-outline-secondary act-btn")
        rows.append([o.name, o.stage.name if o.stage else "-", kind, badge("Active" if o.is_active else "Inactive"),
                     row_actions(request, edit=f"/production/operations/{o.pk}/edit/", delete=f"/production/operations/{o.pk}/delete/", extra=[toggle],
                                 confirm=f"Delete operation {o.name}? Operations with rate cards or work cannot be deleted - deactivate them instead.")])
    return render_list(request, title="Operations", headers=["Operation", "Stage", "Type", "Status", ""], rows=rows, add_url="/production/operations/new/",
                       add_label="New operation", search=False)


@role_required(*PROD)
def operation_new(request):
    b = get_current_business()
    form = F.OperationForm(request.POST or None, business=b)
    return form_page(request, form, "New operation", lambda f, fs: services.create_operation(business=b, user=request.user, **f.cleaned_data),
                     "/production/operations/", back="/production/operations/")


@role_required(*PROD)
def operation_edit(request, pk):
    b = get_current_business()
    op = get_object_or_404(Operation, pk=pk, business=b)
    form = F.OperationForm(request.POST or None, business=b, initial={"name": op.name, "stage": op.stage_id, "is_rework": op.is_rework,
                                                                       "is_payroll_only": op.is_payroll_only})
    return form_page(request, form, f"Edit operation {op.name}",
                     lambda f, fs: services.update_operation(business=b, user=request.user, operation_id=op.pk, **f.cleaned_data),
                     "/production/operations/", back="/production/operations/", submit="Save changes", success_msg="Operation updated.")


@role_required(*PROD)
@require_POST
def operation_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_operation(business=b, user=request.user, operation_id=pk), "/production/operations/", "Operation deleted.")


@role_required(*PROD)
@require_POST
def operation_toggle(request, pk):
    b = get_current_business()
    op = get_object_or_404(Operation, pk=pk, business=b)
    return handle_post(request, lambda: services.set_operation_active(business=b, user=request.user, operation_id=pk, active=not op.is_active),
                       "/production/operations/", "Operation updated.")


@role_required(OWNER, ACCOUNTS)
def workflows(request):
    b = get_current_business()
    rows = [[w.name, " → ".join(s.stage.name for s in w.steps.select_related("stage")),
             row_actions(request, edit=f"/production/workflows/{w.pk}/edit/", delete=f"/production/workflows/{w.pk}/delete/",
                         confirm=f"Delete workflow {w.name}? Workflows used by a product or batch cannot be deleted.")]
            for w in Workflow.objects.filter(business=b)]
    return render_list(request, title="Workflows", headers=["Workflow", "Stages", ""], rows=rows, add_url="/production/workflows/new/", add_label="New workflow",
                       extra_actions=[("Stages", "/production/stages/"), ("New stage", "/production/stages/new/")], search=False,
                       subtitle="A workflow is a product's default route. Each batch ticks the stages it actually needs.")


@role_required(OWNER, ACCOUNTS)
def stages(request):
    b = get_current_business()
    rows = [[s.sequence, s.name, s.code, s.get_default_outcome_source_display(), badge("Active" if s.is_active else "Inactive"),
             row_actions(request, edit=f"/production/stages/{s.pk}/edit/", delete=f"/production/stages/{s.pk}/delete/",
                         confirm=f"Delete stage {s.name}? Stages used by workflows, operations or batches cannot be deleted.")]
            for s in StageDefinition.objects.filter(business=b)]
    return render_list(request, title="Production stages", headers=["Order", "Stage", "Code", "Records output by", "Status", ""], rows=rows,
                       add_url="/production/stages/new/", add_label="New stage", search=False, extra_actions=[("Workflows", "/production/workflows/")],
                       subtitle="These stages appear as the workflow checklist on every batch, in this order.")


def _stage_form_page(request, b, stage=None):
    initial = {"name": stage.name, "code": stage.code, "default_outcome_source": stage.default_outcome_source, "sequence": stage.sequence} if stage else None
    form = F.StageForm(request.POST or None, initial=initial)
    return form_page(request, form, f"Edit stage {stage.name}" if stage else "New stage",
                     lambda f, fs: services.save_stage(business=b, user=request.user, stage_id=stage.pk if stage else None, **f.cleaned_data),
                     "/production/stages/", back="/production/stages/", submit="Save changes" if stage else "Save")


@role_required(OWNER, ACCOUNTS)
def stage_new(request):
    return _stage_form_page(request, get_current_business())


@role_required(OWNER, ACCOUNTS)
def stage_edit(request, pk):
    b = get_current_business()
    return _stage_form_page(request, b, get_object_or_404(StageDefinition, pk=pk, business=b))


@role_required(OWNER, ACCOUNTS)
@require_POST
def stage_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_stage(business=b, user=request.user, stage_id=pk), "/production/stages/", "Stage deleted.")


def _steps(formset):
    return [(c["stage"], c["outcome_source"] or c["stage"].default_outcome_source) for c in formset.cleaned_data if c.get("stage")]


@role_required(OWNER, ACCOUNTS)
def workflow_new(request):
    b = get_current_business()
    form = F.WorkflowForm(request.POST or None)
    fs = F.StepFormSet(request.POST or None, form_kwargs={"business": b})

    def handler(f, formsets):
        return services.create_workflow(business=b, user=request.user, name=f.cleaned_data["name"], steps=_steps(formsets["Stages in order"]))
    return form_page(request, form, "New workflow", handler, "/production/workflows/", formsets={"Stages in order": fs}, back="/production/workflows/",
                     note="Existing batches keep their own snapshot; a new workflow only affects products you assign it to and future batches.")


@role_required(OWNER, ACCOUNTS)
def workflow_edit(request, pk):
    b = get_current_business()
    wf = get_object_or_404(Workflow, pk=pk, business=b)
    form = F.WorkflowForm(request.POST or None, initial={"name": wf.name})
    initial = [{"stage": s.stage_id, "outcome_source": s.outcome_source} for s in wf.steps.order_by("sequence")]
    fs = F.StepFormSet(request.POST or None, initial=initial, form_kwargs={"business": b})

    def handler(f, formsets):
        return services.update_workflow(business=b, user=request.user, workflow_id=wf.pk, name=f.cleaned_data["name"], steps=_steps(formsets["Stages in order"]))
    return form_page(request, form, f"Edit workflow {wf.name}", handler, "/production/workflows/", formsets={"Stages in order": fs},
                     back="/production/workflows/", submit="Save changes", success_msg="Workflow updated.",
                     note="Existing batches keep their own stage snapshot; changes only affect batches created from now on.")


@role_required(OWNER, ACCOUNTS)
@require_POST
def workflow_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_workflow(business=b, user=request.user, workflow_id=pk), "/production/workflows/", "Workflow deleted.")


@role_required(*MONEY_ROLES)
def rates(request):
    b = get_current_business()
    qs = search_filter(RateCard.objects.filter(business=b).select_related("operation", "worker", "product", "variant"), request.GET.get("q", "").strip(), "operation__name", "worker__name", "product__name")
    rows = [[r.operation.name, r.worker.name if r.worker else "any", r.product.name if r.product else "any", r.variant.sku if r.variant else "any",
             inr(r.rate), r.effective_from, ("Backdated: " + r.reason) if r.is_backdated else ""] for r in qs.order_by("-effective_from", "-id")[:300]]
    return render_list(request, title="Rate cards", headers=["Operation", "Worker", "Product", "Variant", "Rate", "Effective", "Note"], rows=rows,
                       add_url="/production/rates/new/", add_label="New rate card", subtitle="Rate cards are immutable; create a new card to change a rate.")


@role_required(*MONEY_ROLES)
def rate_new(request):
    b = get_current_business()
    form = F.RateCardForm(request.POST or None, business=b)
    return form_page(request, form, "New rate card",
                     lambda f, fs: services.create_rate_card(business=b, user=request.user, **f.cleaned_data), "/production/rates/", back="/production/rates/",
                     note="Precedence: worker+product+variant, worker+product, worker, product+variant, product, default (all blank).")
