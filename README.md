# OneERP - Manufacturing ERP for a small school-uniform manufacturer

Django 5.2 · DRF · PostgreSQL 16 · Django templates + HTMX + Bootstrap 5 · Docker Compose.
Modular monolith: `accounts core parties catalogue inventory purchasing production payroll sales finance reports`.

> ## READ THIS FIRST - verification status (honest)
> This project was written in an environment **without Django, PostgreSQL or Docker and without network access**.
> What was actually verified there: every Python file compiles (`py_compile`), and a custom AST check found no undefined
> names or broken cross-module imports. **Nothing has been executed.** Migrations were **not** generated, the test suite
> was **not** run, and Docker Compose was **not** started. Expect to fix a few small bugs on the first run.
> Treat the test suite as the first thing to run (step 6) - it was written to catch exactly that.

Architecture: `Browser -> Django views / DRF APIs -> services -> ORM -> PostgreSQL`. Business rules live in
`<app>/services.py`; read-only queries in `selectors.py`. Both the HTML UI and the REST API call the same services.

## Quick start (macOS 12.6.5, Docker 26 / Compose 2.26)

```bash
# 1. extract
unzip uniform-erp.zip && cd erp

# 2. environment (development-only values; edit if you like)
cp .env.example .env

# 3. generate migrations ONCE (they are produced by Django itself, never handwritten) and build the images
docker compose build
docker compose run --rm web python manage.py makemigrations accounts core parties catalogue inventory purchasing production payroll sales finance reports

# 4. start PostgreSQL 16 + web (applies migrations, collects static files)
docker compose up -d
docker compose logs -f web          # wait for "Listening at: http://0.0.0.0:8000"

# 5. create the Owner (prompts for a password) and optionally load demo data
docker compose exec web python manage.py create_owner --username owner      # skip if you load demo data
docker compose exec web python manage.py load_demo_data                     # creates demo business + users

# 6. run the tests (needs PostgreSQL; the test DB is created automatically)
docker compose exec web python manage.py test -v 2

# 7. stop
docker compose down            # keeps the database volume
```

Open **http://localhost:8000** .

(`docker compose up` also generates migrations automatically on the first start if they are missing, via
`scripts/entrypoint.sh`. Commit the generated `*/migrations/0001_initial.py` files to Git afterwards.)

### Demo logins (development only - never deploy these)
| user | role | password |
|---|---|---|
| `owner` | Owner | `Demo@12345` |
| `accounts` | Accounts/Billing | `Demo@12345` |
| `production` | Production | `Demo@12345` |

Override with `load_demo_data --password '...'`. `load_demo_data` refuses to run twice; reset the dev DB first.
Change passwords with `docker compose exec web python manage.py changepassword <user>`.

### Useful commands
```bash
docker compose exec web python manage.py migrate                 # run migrations
docker compose exec web python manage.py run_reconciliation      # read-only integrity checks (exit 1 on failure)
docker compose logs -f web db                                    # logs
docker compose down -v                                           # RESET the dev database (deletes all data!)
```
After `down -v` run steps 4-5 again (migration files are kept).

## What is implemented
* **accounts** custom `User` (first migration), roles Owner / Accounts-Billing / Production, deactivate-not-delete, backend role checks on every view and API endpoint.
* **core** Business, BusinessSettings, FinancialAccount, append-only AuditEvent, DocumentSequence (row-locked, per business/type/financial year Apr-Mar, never reused), IdempotencyKey, central `get_current_business()`.
* **parties / catalogue** customers, suppliers, both, other (other cannot be used in documents); case-insensitive unique trimmed names; products (finished / raw / accessory) with variants (SKU, size, colour, unit); raw/accessory = exactly one blank variant; finished goods require a default workflow.
* **purchasing** draft/post bills, supplier-bill-number unique per supplier, posted immutable, stock cost = `line_amount - tax_amount`.
* **inventory** immutable `StockMovement` ledger + transactionally maintained `StockBalance`, weighted-average cost (full precision, half-up 2dp, final issue takes exact remaining value), no negative stock (service + DB check), no backdating, opening/adjustment/damage/dispatch/reversal.
* **production** configurable workflows (stage once per workflow), batches with immutable stage snapshots + SHA-256 fingerprint (stage ids, sequence, outcome source; names excluded), size-wise plans, material issue, counting-operation entries and direct inspection, rejection reasons, worker-attributable vs other, rework with quantity links, over-cutting (reason + extra material issue in the same transaction), finished-goods receipts, rate cards with the 6-tier precedence, immutable cards, backdating Owner-only with reason, rate/earnings snapshots on work entries, manual payroll-only entries, payroll-only operations.
* **payroll** weekly statements (per worker, max 7 days, PostgreSQL `btree_gist` exclusion constraint against overlap), one statement per work entry (`OneToOne`), adjustments proposed by Accounts and decided by the Owner (immutable decision, self-approval flagged), advance recovery, finalization (immutable), partial payments, excess payment -> worker advance, one cashbook entry per payment.
* **sales** challans (dispatch once on post), invoices (partial invoicing across challans + direct lines, cumulative limit under row locks, whole-rupee rounding, cancellation rules), print layouts for challan / invoice / wage statement.
* **finance** receipts + allocations, supplier payments + allocations (explicit FKs), expenses, cashbook (exactly-one-source DB constraint), cash/bank balances, ledgers, cash flow.
* **reports** 22 reports + 12 read-only reconciliation checks (Owner screen at `/reports/reconciliation/` and `manage.py run_reconciliation`).
* **UI** sidebar by role, dashboard from live DB values, HTMX live search, filters, pagination, confirmation dialogs, badges, empty states, messages.
* **API** DRF + token/session auth, `Idempotency-Key` support - see `docs/API.md`.

## Design decisions & documented deviations (please read)
1. **Tax-inclusive purchase rate.** `unit_cost` is tax-inclusive: `line_amount = qty x unit_cost`, `tax_amount` is the recoverable part inside it; stock cost = `line_amount - tax_amount`.
2. **Payment above outstanding wages.** The spec says both "cannot exceed outstanding" and "excess becomes an advance". Resolution: the payment is accepted, `applied_amount` is capped at outstanding and the excess is recorded as a `WorkerAdvance`.
3. **Outcome source is stored on the workflow step / batch stage** (`work_entry`, `inspection`, `none`) instead of a separate `ProductStageConfig` table (not created). "Final stage" for finished-goods receipts = the last stage whose outcome source is not `none`.
4. **`pending`** = planned - final-stage good - total rejected (never below 0). Rework still pending is shown separately.
5. **Business consistency** (e.g. a variant belongs to the same business as its product) is enforced in services; **composite foreign keys were not created**.
6. **Reconciliation does not yet block posting.** It reports discrepancies and never repairs them; automatic blocking of relevant postings is not implemented.
7. Work entries are edited by **delete + re-create** before they are attached to a statement (no in-place edit). Material-issue reversal is not implemented.
8. The HTML form accepts **one rework source link per rework entry**; the API accepts several.
9. UI forms rely on confirm dialogs and disabled submit buttons; **idempotency keys are an API feature**.
10. Workflows can be created but not edited in the UI (editing would only affect future batches by design). No password-change screen (use `changepassword`).
11. Bootstrap, Bootstrap Icons and HTMX load from public CDNs - the UI needs internet access in the browser.
12. Production users see stock **quantities** (needed for issuing materials) but never values, rates, wages, advances or any financial page/API field.
13. Users are not tied to a business (kept out of the first migration to avoid a user<->core cycle); the single current business is resolved by `core.services.get_current_business()` (optionally `DEFAULT_BUSINESS_SLUG`).
14. btree_gist is enabled by a `pre_migrate` hook in `core/apps.py` (infrastructure only), so it also exists in the test database. The `postgres` image user is a superuser, so this works in Docker.

## Not implemented (deferred by the brief)
GST, e-invoicing, credit/debit notes, purchase returns, job work, multi-location, lot tracking, BOM, double-entry accounting, multi-tenancy, offline, AI, Redis/Celery.

## Tests
`tests/` contains 101 tests (Django `TestCase`, run against PostgreSQL): auth and roles, backend 403s, party/product rules,
numbering, audit immutability, idempotency (including concurrent threads), purchasing and stock, weighted average and rounding,
negative stock and DB constraints, workflow snapshots, outcomes, rejections/rework/links, over-cutting rollback, rate precedence and
snapshots, manual/payroll-only entries, payroll statements, overlap constraint, adjustments and approval flags, advances, payments,
challan/invoice dispatch rules, partial invoicing, cancellations, receipts and supplier-payment allocations, reconciliation and
page smoke tests on the demo data.

## Security notes
Secrets come from `.env` (not committed); `DJANGO_DEBUG` defaults to false and the app refuses to start without a secret key then;
CSRF on; Django password hashing; ORM only; role checks on every view and API. For real deployment: set `DJANGO_DEBUG=False`,
a long `DJANGO_SECRET_KEY`, `DJANGO_SECURE_COOKIES=True`, put it behind HTTPS and **delete the demo users**.


---

# Update: Enquiries, working list filters, mobile layout

## 1. Contact & Enquiries
**Public page:** `/contact/` (no login). Fields: name, company, email, phone, enquiry type (Sales, Product, Pricing, Partnership, Support, Other),
subject, message. Each submission gets a reference such as `ENQ/2026-27/00001` (same locked, never-reused numbering as other documents).

**What happens on submit:** the enquiry is validated, **saved first**, then e-mailed to the business mailbox with *Reply-To* set to the sender.
If SMTP fails the customer's message is still safe in the ERP; the enquiry is flagged ("notification failed") and staff get a **Retry** button.

**Abuse controls (no extra dependencies):** CSRF; hidden honeypot field; signed time-trap token (rejects forms submitted in under
`ENQUIRY_MIN_FILL_SECONDS` seconds, forged or older than a day); per-IP limit (`ENQUIRY_RATE_LIMIT_PER_HOUR`, default 5, stored in the DB so it holds across workers);
duplicate suppression (same sender/subject/message within 10 minutes returns the original reference); more than 3 links is rejected; single-line fields are
collapsed to block e-mail header injection. Set `TRUST_PROXY_HEADERS=True` only if you run behind your own reverse proxy.

**Staff side (Owner & Accounts):** sidebar **Enquiries** (with a badge for *New*), search (reference, name, company, email, subject, message),
filters (status, type, assignee), sortable columns, detail page with contact details, message, assign, status (New / In Progress / Responded / Closed),
internal notes (never e-mailed), **reply from the business mailbox**, and a chronological conversation history (replies incl. failed ones, notes, status and
assignment changes). Notes and replies are insert-only. Production users cannot open any of it (403).

## 2. SMTP with your GoDaddy Titan mailbox
Put these in **your `.env`** (never in Git; `.env.example` ships them blank):
```
EMAIL_HOST=smtp.titan.email
EMAIL_PORT=465
EMAIL_USE_SSL=True
EMAIL_USE_TLS=False
EMAIL_HOST_USER=you@yourdomain.com
EMAIL_HOST_PASSWORD=<mailbox password>
BUSINESS_MAILBOX=you@yourdomain.com        # where enquiries arrive (defaults to EMAIL_HOST_USER)
DEFAULT_FROM_EMAIL=Your Company <you@yourdomain.com>   # optional
```
Alternative: port `587` with `EMAIL_USE_TLS=True` and `EMAIL_USE_SSL=False`. Confirm the host/port in your GoDaddy/Titan account if sending fails.
With `EMAIL_HOST` empty (development) messages are printed to `docker compose logs web` instead of being sent.
After editing `.env`: `docker compose up -d --force-recreate web`.

Credentials are read only from the environment, never rendered in a template, stored in the database or written to logs: delivery errors are mapped to a short
generic message (e.g. "The mail server rejected the mailbox login") and any exception text is redacted before logging.
Replies you send from the ERP appear as "Sent" in the mailbox only if Titan's SMTP stores copies; customers' answers arrive in the mailbox itself
(reading incoming mail into the ERP is **not** implemented).

## 3. Filters fixed (root cause)
The server-side filtering was correct; the shared list template was not:
1. `hx-get=""` made HTMX re-send the *whole current URL* plus the form, so parameters were duplicated and a stale `?page=2` survived a filter change;
2. the default swap put a second `#results` inside the first (nested cards, duplicate ids);
3. no sorting existed at all.
Now the form targets the page path, swaps with `outerHTML`, never carries `page`, works without JavaScript, preserves search + filters + sort + direction in
the form, header links and pager links, and shows a **Reset** link only when something is filtered. Column headers are sortable (`?sort=<column>&dir=asc|desc`, numeric-aware, invalid values ignored).

## 4. Mobile / tablet
Sidebar becomes a slide-in drawer; tables collapse into labelled cards under 768 px (list, detail and form line tables); filters, buttons and page headers wrap to
full-width touch targets (44 px, 16 px inputs to avoid iOS zoom); dashboard cards are two-up on phones and charts resize; the public contact page and enquiry screens are fully responsive.

## Applying this update to an existing install
```bash
unzip -o ~/Downloads/enquiries-update.zip          # from the folder that contains erp/
cd erp
# append the new variables from .env.example to YOUR .env, then:
docker compose up -d --force-recreate web          # entrypoint generates enquiries migrations, migrates, restarts
docker compose exec web python manage.py test tests.test_enquiries tests.test_list_filters -v 2
```
Existing tables and data are untouched; only the new `enquiries_*` tables are added. Commit the generated `enquiries/migrations/0001_initial.py`.
