# REST API

Base URL `http://localhost:8000/api/` · JSON · authentication: session cookie **or** token.

```bash
curl -s -X POST localhost:8000/api/auth/token/ -d username=accounts -d password=Demo@12345      # -> {"token": "..."}
export T="Authorization: Token <token>"
```
Roles: **M** = Owner/Accounts, **P** = Owner/Accounts/Production, **O** = Owner. Production never gets money fields.
Errors: `400 {"detail": ...}` business rule or validation, `403` role, `409` idempotency conflict.

## Idempotency
Send `Idempotency-Key: <unique string>` on any POST below. Same key + same action/payload replays the original response
(header `Idempotent-Replayed: true`); same key with a different action/payload returns **409**. Only successful operations store the key
(a failed/rolled-back call does not consume it). Concurrent identical requests execute once.

| Method & path | Roles | Body (main fields) |
|---|---|---|
| GET `/stock/` | P | - (values only for M) |
| POST `/purchase-bills/` | M | `supplier_id, supplier_bill_number, bill_date, lines[{variant_id, quantity, unit_cost, tax_amount}]` -> draft |
| POST `/purchase-bills/{id}/post/` | M | posts bill, receives stock |
| POST `/batches/` | P | `product_id, start_date, expected_completion?, plans[{variant_id, quantity}]` |
| POST `/batches/{id}/material-issues/` | P | `variant_id, quantity` |
| POST `/batches/{id}/fg-receipts/` | P | `variant_id, quantity` |
| POST `/work-entries/` | P | `worker_id, operation_id, work_date, variant_id?, batch_stage_id?` + (`quantity` for manual / payroll-only) or `good, rejected_worker, rejected_other, rework_worker, rework_other, reason, note, rework_links[{source_entry_id, quantity}], over_cut_reason, extra_materials[{variant_id, quantity}]` (rate/earnings returned to M only) |
| POST `/wage-statements/` | M | `worker_id, period_start, period_end` |
| POST `/wage-statements/{id}/finalize/` | M | `advance_recovery?` |
| POST `/wage-statements/{id}/payments/` | M | `amount, account_id, payment_date?` |
| POST `/challans/` · `/challans/{id}/post/` · `/challans/{id}/cancel/` | M | `customer_id, challan_date, lines[{variant_id, quantity}]` |
| POST `/invoices/` · `/invoices/{id}/post/` · `/invoices/{id}/cancel/` | M | `customer_id, invoice_date, lines[{variant_id, quantity, rate, challan_line_id?}]` |
| POST `/receipts/` | M | `customer_id, account_id, amount, receipt_date, mode, reference, allocations[{doc_id (invoice), amount}]` |
| POST `/supplier-payments/` | M | `supplier_id, account_id, amount, payment_date, allocations[{doc_id (bill), amount}]` |
| POST `/expenses/` | M | `category, amount, account_id, expense_date, description` |
| GET `/reconciliation/` | O | list of checks with `ok` and `issues` |
