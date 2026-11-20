import time
from decimal import Decimal as D

from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse

from accounts.models import User
from core.models import AuditEvent, Business
from core.exceptions import DomainError, ImmutableRecordError
from enquiries import forms as F
from enquiries import services
from enquiries.models import Enquiry, EnquiryNote, EnquiryReply
from .base import ERPTestCase

SETTINGS = dict(BUSINESS_MAILBOX="office@example.com", DEFAULT_FROM_EMAIL="Office <office@example.com>",
                ENQUIRY_RATE_LIMIT_PER_HOUR=3, ENQUIRY_MIN_FILL_SECONDS=3)
FAILING = "tests.failing_backend.FailingBackend"


def payload(**kw):
    data = {"name": "Priya Sharma", "company": "Green Valley School", "email": "priya@school.example", "phone": "+91 98200 12345",
            "enquiry_type": "pricing", "subject": "Price for 300 shirts", "message": "We need 300 white shirts in sizes 28 to 34 before June.",
            "website": "", "token": F.new_form_token(issued_at=time.time() - 30)}
    data.update(kw)
    return data


def make_enquiry(test, **kw):
    data = {k: v for k, v in payload(**kw).items() if k not in ("website", "token")}
    return services.submit_enquiry(business=test.b, data=data, ip=kw.get("ip"))[0]


@override_settings(**SETTINGS)
class PublicContact(ERPTestCase):
    def test_page_loads_for_anonymous_visitors(self):
        r = Client().get("/contact/")
        self.assertEqual(r.status_code, 200)
        for needle in ('name="name"', 'name="company"', 'name="email"', 'name="phone"', 'name="enquiry_type"', 'name="subject"', 'name="message"', 'name="website"'):
            self.assertContains(r, needle)
        for label in ("Sales", "Product", "Pricing", "Partnership", "Support", "Other"):
            self.assertContains(r, label)

    def test_valid_submission_stores_enquiry_sends_mail_and_shows_reference(self):
        c = Client()
        r = c.post("/contact/", payload())
        self.assertRedirects(r, "/contact/sent/")
        e = Enquiry.objects.get()
        self.assertTrue(e.reference.startswith("ENQ/") and e.status == "new")
        self.assertEqual((e.name, e.company, e.enquiry_type), ("Priya Sharma", "Green Valley School", "pricing"))
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["office@example.com"])
        self.assertEqual(m.reply_to, ["priya@school.example"])
        self.assertIn(e.reference, m.subject)
        self.assertIn("300 white shirts", m.body)
        self.assertIsNotNone(e.notified_at)
        self.assertEqual(e.notify_error, "")
        self.assertContains(c.get("/contact/sent/"), e.reference)

    def test_reference_numbers_are_unique_and_sequential(self):
        c = Client()
        c.post("/contact/", payload(subject="First enquiry"))
        c.post("/contact/", payload(subject="Second enquiry"))
        refs = list(Enquiry.objects.order_by("id").values_list("reference", flat=True))
        self.assertEqual(len(set(refs)), 2)
        self.assertTrue(refs[0].endswith("00001") and refs[1].endswith("00002"))

    def test_validation_errors_are_reported_and_nothing_is_stored(self):
        bad = {"email": "not-an-email", "message": "short", "enquiry_type": "", "phone": "abc", "subject": "x", "name": "A"}
        for field, value in bad.items():
            r = Client().post("/contact/", payload(**{field: value}))
            self.assertEqual(r.status_code, 400, field)
            self.assertIn(field, r.context["form"].errors, field)
        self.assertEqual(Enquiry.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_unknown_enquiry_type_rejected(self):
        self.assertEqual(Client().post("/contact/", payload(enquiry_type="hack")).status_code, 400)

    def test_too_many_links_rejected_as_spam(self):
        spam = "Buy now http://a.example http://b.example http://c.example http://d.example cheap"
        r = Client().post("/contact/", payload(message=spam))
        self.assertEqual(r.status_code, 400)
        self.assertIn("message", r.context["form"].errors)

    def test_honeypot_filled_stores_and_sends_nothing(self):
        r = Client().post("/contact/", payload(website="http://spam.example"))
        self.assertRedirects(r, "/contact/sent/")
        self.assertEqual((Enquiry.objects.count(), len(mail.outbox)), (0, 0))

    def test_too_fast_expired_and_forged_tokens_are_rejected(self):
        for token in (F.new_form_token(), F.new_form_token(issued_at=time.time() - 3 * 24 * 3600), "forged:token", ""):
            r = Client().post("/contact/", payload(token=token))
            self.assertEqual(r.status_code, 400, token)
        self.assertEqual(Enquiry.objects.count(), 0)

    def test_csrf_is_enforced(self):
        r = Client(enforce_csrf_checks=True).post("/contact/", payload())
        self.assertEqual(r.status_code, 403)

    def test_rate_limit_per_ip(self):
        c = Client()
        for i in range(3):
            self.assertEqual(c.post("/contact/", payload(subject=f"Enquiry number {i}")).status_code, 302)
        r = c.post("/contact/", payload(subject="Enquiry number 4"))
        self.assertEqual(r.status_code, 429)
        self.assertContains(r, "Too many enquiries", status_code=429)
        self.assertEqual(Enquiry.objects.count(), 3)

    def test_double_submit_returns_same_reference_and_sends_one_mail(self):
        c = Client()
        c.post("/contact/", payload())
        c.post("/contact/", payload())
        self.assertEqual((Enquiry.objects.count(), len(mail.outbox)), (1, 1))

    def test_header_injection_is_neutralised(self):
        Client().post("/contact/", payload(subject="Hello\nBcc: evil@example.com"))
        m = mail.outbox[0]
        self.assertNotIn("\n", m.subject)
        self.assertEqual(m.to, ["office@example.com"])
        self.assertEqual(m.bcc, [])

    def test_unavailable_when_no_business(self):
        Business.objects.update(is_active=False)
        self.assertEqual(Client().get("/contact/").status_code, 503)

    def test_get_does_not_require_login_but_staff_pages_do(self):
        self.assertEqual(Client().get("/enquiries/").status_code, 302)


@override_settings(**SETTINGS)
class EmailFailureHandling(ERPTestCase):
    @override_settings(EMAIL_BACKEND=FAILING, EMAIL_HOST_PASSWORD="hunter2secret", EMAIL_HOST_USER="mailbox@example.com")
    def test_smtp_failure_keeps_the_enquiry_and_never_leaks_credentials(self):
        with self.assertLogs("enquiries.mail", level="ERROR") as logs:
            r = Client().post("/contact/", payload())
        self.assertRedirects(r, "/contact/sent/")           # the customer's message is safe in the ERP
        e = Enquiry.objects.get()
        self.assertIsNone(e.notified_at)
        self.assertTrue(e.notify_error)
        self.assertNotIn("hunter2secret", e.notify_error)
        self.assertNotIn("hunter2secret", " ".join(logs.output))
        staff = Client()
        staff.login(username="acct", password="x")
        page = staff.get(f"/enquiries/{e.pk}/")
        self.assertContains(page, "notification e-mail")
        self.assertContains(page, "Retry")
        self.assertNotContains(page, "hunter2secret")

    @override_settings(BUSINESS_MAILBOX="")
    def test_missing_mailbox_is_reported_not_crashed(self):
        Client().post("/contact/", payload())
        self.assertIn("not configured", Enquiry.objects.get().notify_error)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST_USER="", EMAIL_HOST_PASSWORD="")
    def test_missing_smtp_credentials_do_not_attempt_a_connection(self):
        Client().post("/contact/", payload())
        self.assertIn("credentials", Enquiry.objects.get().notify_error)

    def test_resend_notification_after_fixing_the_mail_settings(self):
        with override_settings(EMAIL_BACKEND=FAILING):
            e = make_enquiry(self)
        self.assertTrue(e.notify_error)
        r = services.resend_notification(business=self.b, user=self.acct, enquiry_id=e.pk)
        self.assertTrue(r.ok)
        e.refresh_from_db()
        self.assertEqual((e.notify_error, len(mail.outbox)), ("", 1))

    def test_credentials_are_only_read_from_settings_never_hardcoded(self):
        import pathlib, re
        root = pathlib.Path(__file__).resolve().parent.parent
        pattern = re.compile(r"EMAIL_HOST_PASSWORD\s*=\s*['\"][^'\"]+['\"]")
        for path in list(root.rglob("*.py")) + list(root.rglob("*.html")) + [root / ".env.example"]:
            if "tests" in path.parts:
                continue
            self.assertIsNone(pattern.search(path.read_text()), f"hard-coded credential in {path.name}")
        for path in (root / "templates").rglob("*.html"):
            self.assertNotIn("EMAIL_HOST_PASSWORD", path.read_text(), path.name)


@override_settings(**SETTINGS)
class EnquiryManagement(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.e1 = make_enquiry(self, subject="Pricing for shirts", enquiry_type="pricing", name="Priya Sharma")
        self.e2 = make_enquiry(self, subject="Partnership proposal", enquiry_type="partnership", name="Rahul Verma", company="Textile Hub", email="rahul@hub.example")
        self.e3 = make_enquiry(self, subject="Order support needed", enquiry_type="support", name="Meena Iyer", company="", email="meena@x.example")
        mail.outbox.clear()
        self.client_acct = Client()
        self.client_acct.login(username="acct", password="x")

    def test_access_control(self):
        self.assertEqual(Client().get("/enquiries/").status_code, 302)
        prod = Client()
        prod.login(username="prod", password="x")
        self.assertEqual(prod.get("/enquiries/").status_code, 403)
        self.assertEqual(prod.get(f"/enquiries/{self.e1.pk}/").status_code, 403)
        for action in ("status", "assign", "note", "reply", "resend"):
            self.assertEqual(prod.post(f"/enquiries/{self.e1.pk}/{action}/", {}).status_code, 403, action)
        owner = Client()
        owner.login(username="owner", password="x")
        self.assertEqual(owner.get("/enquiries/").status_code, 200)
        self.assertEqual(self.client_acct.get(f"/enquiries/{self.e1.pk}/").status_code, 200)

    def test_list_search_and_filters(self):
        c = self.client_acct
        r = c.get("/enquiries/")
        for e in (self.e1, self.e2, self.e3):
            self.assertContains(r, e.reference)
        r = c.get("/enquiries/", {"q": "textile"})
        self.assertContains(r, self.e2.reference)
        self.assertNotContains(r, self.e1.reference)
        r = c.get("/enquiries/", {"type": "support"})
        self.assertContains(r, self.e3.reference)
        self.assertNotContains(r, self.e2.reference)
        services.change_status(business=self.b, user=self.acct, enquiry_id=self.e2.pk, status="closed")
        r = c.get("/enquiries/", {"status": "closed"})
        self.assertContains(r, self.e2.reference)
        self.assertNotContains(r, self.e1.reference)
        services.assign(business=self.b, user=self.acct, enquiry_id=self.e1.pk, assignee=self.acct)
        self.assertContains(c.get("/enquiries/", {"assigned": "me"}), self.e1.reference)
        self.assertNotContains(c.get("/enquiries/", {"assigned": "me"}), self.e3.reference)
        self.assertNotContains(c.get("/enquiries/", {"assigned": "unassigned"}), self.e1.reference)
        self.assertContains(c.get("/enquiries/", {"assigned": "unassigned"}), self.e3.reference)
        r = c.get("/enquiries/", {"status": "new", "type": "support", "q": "order"})      # combined
        self.assertContains(r, self.e3.reference)
        self.assertNotContains(r, self.e1.reference)

    def test_detail_shows_contact_details_message_and_date(self):
        r = self.client_acct.get(f"/enquiries/{self.e1.pk}/")
        for text in ("Priya Sharma", "priya@school.example", "+91 98200 12345", "Green Valley School", "Pricing", "300 white shirts"):
            self.assertContains(r, text)

    def test_status_workflow(self):
        c = self.client_acct
        for status, label in (("in_progress", "In Progress"), ("responded", "Responded"), ("closed", "Closed")):
            c.post(f"/enquiries/{self.e1.pk}/status/", {"status": status})
            self.e1.refresh_from_db()
            self.assertEqual(self.e1.status, status)
        self.assertIsNotNone(self.e1.closed_at)
        c.post(f"/enquiries/{self.e1.pk}/status/", {"status": "in_progress"})      # reopen
        self.e1.refresh_from_db()
        self.assertEqual((self.e1.status, self.e1.closed_at), ("in_progress", None))
        with self.assertRaises(DomainError):
            services.change_status(business=self.b, user=self.acct, enquiry_id=self.e1.pk, status="in_progress")
        with self.assertRaises(DomainError):
            services.change_status(business=self.b, user=self.acct, enquiry_id=self.e1.pk, status="bogus")
        self.assertTrue(AuditEvent.objects.filter(action="enquiry.status", entity_id=str(self.e1.pk)).exists())

    def test_assignment(self):
        c = self.client_acct
        c.post(f"/enquiries/{self.e3.pk}/assign/", {"assignee": self.owner.pk})
        self.e3.refresh_from_db()
        self.assertEqual((self.e3.assigned_to, self.e3.status), (self.owner, "in_progress"))     # first assignment starts work
        with self.assertRaises(DomainError):
            services.assign(business=self.b, user=self.acct, enquiry_id=self.e3.pk, assignee=self.prodn)
        c.post(f"/enquiries/{self.e3.pk}/assign/", {"assignee": ""})
        self.e3.refresh_from_db()
        self.assertIsNone(self.e3.assigned_to)
        self.assertEqual(c.post(f"/enquiries/{self.e3.pk}/assign/", {"assignee": self.prodn.pk}).status_code, 302)   # form rejects non-staff
        self.e3.refresh_from_db()
        self.assertIsNone(self.e3.assigned_to)

    def test_internal_notes_are_stored_immutable_and_never_emailed(self):
        self.client_acct.post(f"/enquiries/{self.e1.pk}/note/", {"body": "Called the school, wants a sample first."})
        note = EnquiryNote.objects.get()
        self.assertEqual(note.author, self.acct)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(self.client_acct.get(f"/enquiries/{self.e1.pk}/"), "wants a sample first")
        note.body = "changed"
        with self.assertRaises(ImmutableRecordError):
            note.save()
        with self.assertRaises(DomainError):
            services.add_note(business=self.b, user=self.acct, enquiry_id=self.e1.pk, body="   ")

    def test_reply_is_sent_from_business_mailbox_and_recorded(self):
        r = self.client_acct.post(f"/enquiries/{self.e1.pk}/reply/", {"subject": f"Re: [{self.e1.reference}] Pricing for shirts",
                                                                      "body": "Dear Priya, our price is Rs 450 per shirt."}, follow=True)
        self.assertContains(r, "Reply sent")
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual((m.to, m.reply_to, m.from_email), (["priya@school.example"], ["office@example.com"], "Office <office@example.com>"))
        self.assertIn("Rs 450", m.body)
        self.assertIn(self.b.name, m.body)
        reply = EnquiryReply.objects.get()
        self.assertTrue(reply.delivered)
        self.assertEqual(reply.sent_by, self.acct)
        self.e1.refresh_from_db()
        self.assertEqual(self.e1.status, "responded")
        self.assertContains(self.client_acct.get(f"/enquiries/{self.e1.pk}/"), "Rs 450 per shirt")

    @override_settings(EMAIL_BACKEND=FAILING, EMAIL_HOST_PASSWORD="hunter2secret")
    def test_failed_reply_is_recorded_without_advancing_status_or_leaking_secrets(self):
        r = self.client_acct.post(f"/enquiries/{self.e1.pk}/reply/", {"subject": "Re: pricing", "body": "Here is our quote."}, follow=True)
        self.assertContains(r, "could not be e-mailed")
        self.assertNotContains(r, "hunter2secret")
        reply = EnquiryReply.objects.get()
        self.assertFalse(reply.delivered)
        self.assertTrue(reply.error)
        self.e1.refresh_from_db()
        self.assertEqual(self.e1.status, "new")
        self.assertContains(self.client_acct.get(f"/enquiries/{self.e1.pk}/"), "NOT delivered")

    def test_reply_validation(self):
        with self.assertRaises(DomainError):
            services.send_reply(business=self.b, user=self.acct, enquiry_id=self.e1.pk, subject="", body="x")
        r = self.client_acct.post(f"/enquiries/{self.e1.pk}/reply/", {"subject": "", "body": ""}, follow=True)
        self.assertContains(r, "Please enter a subject and a message")
        self.assertEqual(EnquiryReply.objects.count(), 0)

    def test_conversation_history_is_chronological(self):
        c = self.client_acct
        c.post(f"/enquiries/{self.e1.pk}/assign/", {"assignee": self.acct.pk})
        c.post(f"/enquiries/{self.e1.pk}/note/", {"body": "Note one"})
        c.post(f"/enquiries/{self.e1.pk}/reply/", {"subject": "Re: x", "body": "Reply one"})
        c.post(f"/enquiries/{self.e1.pk}/note/", {"body": "Note two"})
        html = c.get(f"/enquiries/{self.e1.pk}/").content.decode()
        order = [html.index(t) for t in ("Enquiry received", "Assignment changed", "Note one", "Reply one", "Note two")]
        self.assertEqual(order, sorted(order))

    def test_data_is_scoped_to_the_business(self):
        other = Business.objects.create(name="Other Co", slug="other-co")
        foreign = Enquiry.objects.create(business=other, reference="ENQ/X/00001", name="Z", email="z@z.example", enquiry_type="sales",
                                         subject="Foreign", message="Should never be visible here")
        self.assertNotContains(self.client_acct.get("/enquiries/"), "Foreign")
        self.assertEqual(self.client_acct.get(f"/enquiries/{foreign.pk}/").status_code, 404)

    def test_sidebar_badge_counts_new_enquiries_for_staff_only(self):
        owner = Client()
        owner.login(username="owner", password="x")
        self.assertContains(owner.get("/"), 'count-pill">3<')
        services.change_status(business=self.b, user=self.acct, enquiry_id=self.e1.pk, status="in_progress")
        self.assertContains(owner.get("/"), 'count-pill">2<')
        prod = Client()
        prod.login(username="prod", password="x")
        self.assertNotContains(prod.get("/"), "count-pill")

    def test_enquiries_sort_with_filters(self):
        r = self.client_acct.get("/enquiries/", {"sort": "2", "dir": "asc", "type": "pricing"})
        self.assertContains(r, self.e1.reference)
        self.assertNotContains(r, self.e3.reference)
