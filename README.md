# OneERP — Manufacturing ERP for a School-Uniform Manufacturer

**OneERP** is a web-based manufacturing ERP that brings purchasing, inventory, production, worker operations, payroll, sales, finance, reporting, and customer enquiries into one application.

It is designed around the connected workflow of a small uniform manufacturer: procure raw materials, maintain stock, plan and track production batches, record worker output, calculate wages, dispatch finished goods, invoice customers, record payments, and reconcile operational records.

| Resource | Link |
|---|---|
| Live application | https://oneerp.co.in/ |
| Sign-in page | https://oneerp.co.in/accounts/login/ |
| Public business enquiry form | https://oneerp.co.in/contact/ |
| Staff enquiry inbox (login required) | https://oneerp.co.in/enquiries/ |
| Source code | https://github.com/vaishnaviuttarkar/OneERP |
| API documentation | [`docs/API.md`](docs/API.md) |

---

## 1. Start here — guide for hackathon evaluators

### Why does the ERP require login?

OneERP is an internal business operations system, not a public catalogue. It contains commercially sensitive information such as supplier bills, stock movements and values, worker rates, wages, customer invoices, receipts, expenses, and financial balances.

Users must sign in so that:

- Every action is associated with an authenticated staff account.
- Each staff member sees only the modules and information permitted by their role.
- Sensitive financial information is not exposed to production users.
- Business operations are protected by server-side permission checks, not just by hiding menu items.

The public landing page and public enquiry form can be viewed without signing in. The operational ERP workspace requires authentication.

**To evaluate the ERP, open the sign-in page:**  
https://oneerp.co.in/accounts/login/

### Evaluator credentials

The following are placeholders. The project owner will replace them with active evaluation credentials before submission.

| Account | Role | Username | Password |
|---|---|---|---|
| Owner | Full access | `REPLACE_WITH_OWNER_USERNAME` | `REPLACE_WITH_OWNER_PASSWORD` |
| Accounts / Billing | Finance, purchasing, sales and related operations | `REPLACE_WITH_ACCOUNTS_USERNAME` | `REPLACE_WITH_ACCOUNTS_PASSWORD` |
| Production | Production and stock-quantity workflows | `REPLACE_WITH_PRODUCTION_USERNAME` | `REPLACE_WITH_PRODUCTION_PASSWORD` |

Use the **Owner** account first for the broadest walkthrough. Then sign out and use the Accounts and Production accounts to verify role-based restrictions.

> **Security:** Replace every placeholder above before publishing the final submission. Do not publish real credentials in source code, `.env`, screenshots, commit history, or public issue threads. Use temporary evaluation passwords and change or disable them after judging.

### Suggested 10–15 minute evaluation walkthrough

The sidebar is grouped by business function. The visible navigation changes according to the signed-in user's role.

1. **Dashboard** — Start with the dashboard to see the business overview and operational summaries.
2. **Master data** — Open Clientele, Products, and Workers & Operations. Review customers, suppliers, product variants, workers, and production operations. Owner/Accounts users can also inspect Contractor Rate Cards.
3. **Purchasing → Inventory** — Review supplier bills and the stock position. A posted purchase bill creates the corresponding stock movements; posted bills are immutable.
4. **Production → Work Entries** — Inspect configured production workflows, batches, material issues, worker output, inspection results, rework, rejections, and finished-goods receipts.
5. **Payroll** — Review weekly worker statements, rate/earning snapshots, proposed adjustments, approvals, advance recovery, finalization, and payments.
6. **Sales** — Review delivery challans and invoices, including partial invoicing against challan quantities.
7. **Finance Overview / Receipts / Supplier Payments / Expenses / Cashbook** — Follow the financial side of sales, purchasing, payroll, and expenses.
8. **Reports** — Explore the available operational and financial reports.
9. **Reconciliation** — As Owner, open the reconciliation screen to inspect read-only integrity checks and any reported discrepancies.
10. **Enquiries** — As Owner or Accounts, open the staff enquiry inbox to review submissions, assign them, change status, add internal notes, and reply.

The application includes demo records when the demo-data command has been run. Use the existing records to explore the end-to-end flow; avoid posting or changing production data during evaluation unless you intend to demonstrate a specific transaction.

### Public enquiry form vs. staff enquiry inbox

These are two different pages:

- **Public form:** [`/contact/`](https://oneerp.co.in/contact/) — visitors can submit a business enquiry without an ERP account.
- **Staff inbox:** [`/enquiries/`](https://oneerp.co.in/enquiries/) — authenticated Owner and Accounts users can manage enquiries, assign them, update status, add internal notes, and send replies.

The `/enquiries/` page is intentionally protected because it contains customer contact details and internal correspondence.

### Email Integration Workflow

OneERP includes email integration for handling customer enquiries and staff responses.

1. **Customer submits an enquiry:** Anyone can use the public [Contact Form](https://oneerp.co.in/contact/) without logging in.
2. **Enquiry is recorded:** The enquiry is saved in the database and assigned a unique reference number.
3. **Business receives an email:** OneERP sends an email notification to the configured business mailbox, with the customer's email address as the Reply-To address.
4. **Staff manages the enquiry:** Owner and Accounts users can access the protected Enquiry Inbox to review, assign, update status, and add internal notes.
5. **Staff replies:** Staff can respond directly from OneERP. The customer receives the reply by email, and the response is recorded in the enquiry timeline.
6. **Delivery tracking:** Email delivery status is recorded. Failed notifications can be retried.

**Configuration:** Email delivery uses SMTP (configured through environment variables). Titan Mail is supported. In local development, emails are printed to the application logs if SMTP is not configured.

**Note:** Incoming customer replies are received in the business mailbox; automatic inbox synchronisation with OneERP is not implemented.
---

## 2. Modules and capabilities

### Accounts and access control
- Custom Django user model with three application roles: Owner, Accounts/Billing, and Production.
- Role checks are enforced on backend views and API endpoints.
- Users can be deactivated instead of deleted.
- Owner has access across the application; other roles receive scoped access.

### Core business records
- Business settings and financial accounts.
- Append-only audit events for important business actions.
- Document sequences scoped by business, document type, and Indian financial year (April–March).
- Row-locked numbering to protect concurrent document creation.
- Idempotency records for supported API operations.

### Parties and product catalogue
- Customer, supplier, both, and other party types.
- Products classified as finished goods, raw materials, or accessories.
- Product variants with SKU, size, colour, and unit.
- Validation for trimmed, case-insensitive party names and product/variant rules.

### Purchasing and inventory
- Supplier bills can be drafted and posted.
- Posted supplier bills are immutable.
- Posting a bill records stock movements.
- Inventory uses an immutable stock-movement ledger and a transactionally maintained stock balance.
- Weighted-average costing, precision-aware calculations, and protection against negative stock.
- Opening stock, adjustments, damage, dispatch, and supported reversals are represented as stock movements.

### Production
- Configurable production workflows and batch-specific stage snapshots.
- Size-wise production plans and material issue tracking.
- Work entries for worker output, inspections, rejection reasons, and rework.
- Rework quantities can be linked to their source work entries.
- Over-cutting requires a reason and records additional material issue in the same transaction.
- Finished-goods receipts and configurable contractor rate cards.
- Rate and earnings snapshots are retained on work entries.

### Payroll
- Weekly worker statements, with a maximum seven-day period.
- Database-enforced protection against overlapping statements for a worker.
- Work entries are linked to at most one statement.
- Accounts can propose adjustments; the Owner decides them.
- Advance recovery, immutable statement finalization, partial payments, and worker advances for excess payments.
- Payroll payments create corresponding cashbook entries.

### Sales
- Delivery challans with dispatch on posting.
- Customer invoices can include challan quantities and direct invoice lines.
- Partial invoicing is supported, with cumulative quantity checks under row locks.
- Print layouts are available for challans, invoices, and wage statements.

### Finance
- Customer receipts and allocations against invoices.
- Supplier payments and allocations against bills.
- Expenses, cashbook entries, cash/bank balances, ledgers, and cash-flow reporting.
- Database constraints enforce the cashbook source relationship.

### Reports and reconciliation
- 22 reports are implemented.
- 12 read-only reconciliation checks identify inconsistencies.
- Reconciliation results can be viewed by the Owner at `/reports/reconciliation/` or run using the management command documented below.
- Reconciliation reports discrepancies; it does not automatically repair records or block all posting operations.

### Enquiries
- Public enquiry form with validation, a honeypot field, a time-based submission check, and rate limiting.
- Owner/Accounts staff can search and filter enquiries, assign an enquiry, change its status, add internal notes, and send replies.
- Enquiry notes and replies are retained as immutable history.
- SMTP configuration is supplied through environment variables.

### REST API
- Django REST Framework API with token and session authentication.
- Role-aware access and protection of Production users from financial fields.
- `Idempotency-Key` support for documented POST operations.
- Endpoint details and example request formats: [`docs/API.md`](docs/API.md).

---

## 3. Role-based access at a glance

| Area | Owner | Accounts / Billing | Production |
|---|:---:|:---:|:---:|
| Dashboard | ✓ | ✓ | ✓ |
| Clientele | ✓ | ✓ | — |
| Products | ✓ | ✓ | ✓ |
| Workers & Operations | ✓ | ✓ | ✓ |
| Contractor Rate Cards | ✓ | ✓ | — |
| Purchasing | ✓ | ✓ | — |
| Inventory | ✓ | ✓ | ✓ |
| Production / Work Entries | ✓ | ✓ | ✓ |
| Payroll | ✓ | ✓ | — |
| Enquiries | ✓ | ✓ | — |
| Challans / Invoices | ✓ | ✓ | — |
| Receipts / Supplier Payments / Expenses / Cashbook | ✓ | ✓ | — |
| Reports | ✓ | ✓ | ✓ |
| Reconciliation | ✓ | — | — |
| User administration | ✓ | — | — |

Production users can access stock quantities needed for production, but do not receive stock values, rates, wages, advances, or financial pages/API fields. The backend enforces these restrictions.

---

## 4. Technology stack and architecture

### Technology

- **Backend:** Python, Django 5.2
- **API:** Django REST Framework
- **Database:** PostgreSQL 16
- **Application server:** Gunicorn
- **Web server / reverse proxy:** Nginx
- **Containerization:** Docker and Docker Compose
- **Frontend:** Django templates, HTMX, Bootstrap 5, Bootstrap Icons
- **Static assets:** WhiteNoise
- **Production hosting:** Linux VPS with HTTPS

### Architecture

```text
Browser
  |
  | HTTPS
  v
Nginx (reverse proxy)
  |
  v
Gunicorn
  |
  v
Django
  |-- Server-rendered views
  |-- Django REST Framework API
  |-- Role and permission checks
  |-- Domain services (business rules)
  |-- Selectors (read-only query logic)
  |
  v
Django ORM
  |
  v
PostgreSQL
```

The application is structured as a **modular monolith**. Each business domain is separated into a Django app. Business rules are primarily implemented in service modules, while read-oriented query logic is separated into selectors. The HTML interface and REST API reuse the same domain services.

### Project structure

```text
.
├── accounts/       # Users, roles, permissions
├── core/           # Business foundation, audit, numbering, shared services
├── parties/        # Customers and suppliers
├── catalogue/      # Products and variants
├── purchasing/     # Supplier bills
├── inventory/      # Stock ledger and balances
├── production/     # Workflows, batches, operations, work entries
├── payroll/        # Wage statements, adjustments and payments
├── sales/          # Challans and invoices
├── finance/        # Receipts, payments, expenses and cashbook
├── reports/        # Reports and reconciliation
├── enquiries/      # Public enquiries and staff inbox
├── config/         # Django project settings and URL configuration
├── templates/      # Shared and page templates
├── static/         # CSS, JavaScript and images
├── docs/           # API documentation
├── tests/          # Automated tests
├── scripts/        # Container startup script
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── manage.py
```

---

## 5. Run locally with Docker

### Prerequisites

- Git
- Docker Engine or Docker Desktop
- Docker Compose v2

### Setup

```bash
git clone https://github.com/vaishnaviuttarkar/OneERP.git
cd OneERP

# Create your local environment file
cp .env.example .env
```

Edit `.env` and set a unique `DJANGO_SECRET_KEY` and a strong local `POSTGRES_PASSWORD`. For local development, use the local host settings shown in `.env.example`. Do not use production secrets in a development environment.

Build and start the services:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f web
```

The web container applies existing migrations, collects static files, and starts Gunicorn. Once the containers are running, open:

- Application: http://localhost:8000/
- Login: http://localhost:8000/accounts/login/
- Public enquiry form: http://localhost:8000/contact/

### Create an Owner account

For a clean database without demo records:

```bash
docker compose exec web python manage.py create_owner --username owner
```

The command prompts for a password. It creates the initial business and basic cash/bank accounts if no business exists.

### Load sample/demo data

Alternatively, load the demonstration business and its sample operational records:

```bash
docker compose exec web python manage.py load_demo_data --password 'REPLACE_WITH_A_TEMPORARY_PASSWORD'
```

This creates the demo business and three users: `owner`, `accounts`, and `production`. The supplied password is applied to all three demo users. The command refuses to load the same demo business a second time.

**Do not run the demo-data command against an existing production database unless you have intentionally decided to seed that database.** For the public evaluation instance, use the prepared evaluation accounts listed in Section 1.

### Useful management commands

```bash
# Apply migrations
docker compose exec web python manage.py migrate

# Run the automated test suite
docker compose exec web python manage.py test -v 2

# Run read-only reconciliation checks
docker compose exec web python manage.py run_reconciliation

# Change a user's password
docker compose exec web python manage.py changepassword USERNAME

# Follow application logs
docker compose logs -f web

# Stop services without deleting the database volume
docker compose down
```

> `docker compose down -v` deletes the PostgreSQL data volume. Use it only when you intentionally want to erase the local database and all its records.

---

## 6. API

The API is available under `/api/`. It supports session or token authentication. Refer to [`docs/API.md`](docs/API.md) for the current endpoint list, role requirements, request fields, and idempotency behavior.

Example token request for a local instance:

```bash
curl -X POST http://localhost:8000/api/auth/token/ \
  -d "username=YOUR_USERNAME" \
  -d "password=YOUR_PASSWORD"
```

Use the returned token as:

```http
Authorization: Token YOUR_TOKEN
```

For supported POST operations, send a unique `Idempotency-Key` header. Repeating the same key with the same operation and payload replays the original successful response; reusing the key for a different operation or payload returns a conflict.

---

## 7. Important business rules and engineering decisions

- **Tax-inclusive purchase rate:** Purchase `unit_cost` includes tax. The recoverable tax portion is subtracted when calculating stock cost.
- **Excess wage payment:** Payment applied to the statement is capped at the outstanding amount; any excess is recorded as a worker advance.
- **Production outcome source:** The outcome source is stored on the workflow step/batch stage (`work_entry`, `inspection`, or `none`). A separate `ProductStageConfig` table was not created.
- **Pending production quantity:** Calculated as planned quantity minus final-stage good quantity and total rejected quantity, never below zero. Pending rework is shown separately.
- **Business consistency:** Cross-record business consistency is validated in services; composite foreign keys were not added.
- **Reconciliation scope:** Checks are read-only. They report discrepancies and do not repair them or automatically block every relevant posting.
- **Work-entry editing:** Before a work entry is attached to a statement, edits are performed by deleting and recreating it; there is no in-place edit.
- **Rework links:** The HTML form supports one source link per rework entry; the API supports multiple source links.
- **Idempotency:** Idempotency keys are supported by the API; UI forms use confirmation dialogs and disabled submit buttons.
- **Workflow editing:** Workflows can be created in the UI but not edited there. This avoids silently changing the definition used by existing batches.
- **Business selection:** Users are not directly tied to a business record. The current business is resolved centrally by `core.services.get_current_business()`; `DEFAULT_BUSINESS_SLUG` can optionally select it.
- **Database extension:** PostgreSQL `btree_gist` is enabled by a `pre_migrate` hook for the payroll overlap constraint.

---

## 8. Known limitations / features outside the current scope

The following were deferred and are **not implemented** in this version:

- GST workflows and GST returns
- E-invoicing
- Credit notes and debit notes
- Purchase returns
- Job work
- Multi-location inventory
- Lot/batch tracking for inventory
- Bill of Materials (BOM)
- Double-entry accounting
- Multi-tenancy
- Offline operation
- AI features
- Redis / Celery background processing

Other documented limitations include the absence of material-issue reversal, no in-place editing of work entries before statement attachment, and reconciliation checks that report rather than automatically repair or block discrepancies.

---

## 9. Tests and verification

The repository includes a Django test suite covering authentication and roles, backend access restrictions, business rules, document numbering, audit immutability, idempotency, purchasing, inventory costing, production workflows, payroll, sales, finance, reconciliation, enquiries, and page behavior.

Run the suite against the configured PostgreSQL database:

```bash
docker compose exec web python manage.py test -v 2
```

The live deployment has been started using Docker Compose with PostgreSQL, Django/Gunicorn, Nginx, and HTTPS. A successful HTTP response from the live domain confirms that the web application is reachable; it should not be interpreted as a claim that every business workflow has been independently audited by an external party.

---

## 10. Production deployment notes

The deployed application uses:

- Docker Compose for the Django/Gunicorn and PostgreSQL containers.
- A persistent Docker volume for PostgreSQL data.
- Nginx on the VPS as the public reverse proxy.
- HTTPS certificates managed by Certbot / Let's Encrypt.
- Environment variables stored in the server-side `.env` file.

For any deployment:

- Set `DJANGO_DEBUG=False`.
- Set `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` to the actual production domains.
- Use a strong, unique `DJANGO_SECRET_KEY` and database password.
- Keep `.env` out of Git and out of public downloads.
- Restrict database access to the internal Docker network.
- Change all demo passwords before exposing evaluation credentials.
- Back up the PostgreSQL volume before upgrades or destructive operations.

---

## Contact

For project enquiries, use the public form: https://oneerp.co.in/contact/

**Live application:** https://oneerp.co.in/  
**GitHub:** https://github.com/vaishnaviuttarkar/OneERP
