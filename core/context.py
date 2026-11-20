from accounts.models import User

ALL = {User.OWNER, User.ACCOUNTS, User.PRODUCTION}
OA = {User.OWNER, User.ACCOUNTS}

# (group, label, url name, bootstrap icon, roles)
NAV = [
    ("Overview", "Dashboard", "dashboard", "bi-grid-1x2-fill", ALL),
    ("Overview", "Finance Overview", "finance:overview", "bi-pie-chart-fill", OA),
    ("Master data", "Clientele", "parties:list", "bi-people-fill", OA),
    ("Master data", "Products", "catalogue:products", "bi-tags-fill", ALL),
    ("Master data", "Workers & Operations", "production:workers", "bi-person-vcard-fill", ALL),
    ("Master data", "Contractor Rate Cards", "production:rates", "bi-currency-rupee", OA),
    ("Operations", "Purchasing", "purchasing:list", "bi-bag-check-fill", OA),
    ("Operations", "Inventory", "inventory:stock", "bi-box-seam-fill", ALL),
    ("Operations", "Production", "production:batches", "bi-scissors", ALL),
    ("Operations", "Work Entries", "production:entries", "bi-clipboard2-check-fill", ALL),
    ("Operations", "Payroll", "payroll:statements", "bi-wallet-fill", OA),
    ("Sales & Finance", "Enquiries", "enquiries:list", "bi-envelope-paper-fill", OA),
    ("Sales & Finance", "Challans", "sales:challans", "bi-truck", OA),
    ("Sales & Finance", "Invoices", "sales:invoices", "bi-receipt-cutoff", OA),
    ("Sales & Finance", "Receipts", "finance:receipts", "bi-cash-coin", OA),
    ("Sales & Finance", "Supplier Payments", "finance:supplier_payments", "bi-bank2", OA),
    ("Sales & Finance", "Expenses", "finance:expenses", "bi-credit-card-2-front-fill", OA),
    ("Sales & Finance", "Cashbook", "finance:cashbook", "bi-journal-richtext", OA),
    ("Insights", "Reports", "reports:index", "bi-bar-chart-line-fill", ALL),
    ("Insights", "Reconciliation", "reports:reconciliation", "bi-shield-check", {User.OWNER}),
    ("Admin", "Users", "accounts:users", "bi-person-gear", {User.OWNER}),
]


def erp_context(request):
    u = getattr(request, "user", None)
    if not u or not u.is_authenticated:
        return {}
    role = u.effective_role
    groups = {}
    for group, name, url, icon, roles in NAV:
        if role in roles:
            groups.setdefault(group, []).append((name, url, icon))
    new_enquiries = 0
    if role in OA:
        try:      # unread-enquiry badge in the sidebar
            from enquiries.models import Enquiry
            from .services import get_current_business
            new_enquiries = Enquiry.objects.filter(business=get_current_business(), status="new").count()
        except Exception:      # noqa: BLE001 - never break a page because of a badge
            new_enquiries = 0
    return {"nav_groups": list(groups.items()), "user_role": role, "new_enquiry_count": new_enquiries}
