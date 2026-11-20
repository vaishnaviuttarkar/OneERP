import re
import time

from django import forms
from django.conf import settings
from django.core import signing
from django.core.validators import RegexValidator

from accounts.models import User
from core.forms import BootstrapMixin
from .models import Enquiry

_signer = signing.TimestampSigner(salt="enquiry-form")
_URL = re.compile(r"https?://|www\.", re.I)


def new_form_token(issued_at=None):
    return _signer.sign(str(int(issued_at or time.time())))


def _squash(value):
    return " ".join((value or "").split())      # single line: also blocks e-mail header injection


class ContactForm(BootstrapMixin, forms.Form):
    name = forms.CharField(label="Your name", min_length=2, max_length=100)
    company = forms.CharField(label="Company name", required=False, max_length=150)
    email = forms.EmailField(label="Email address", max_length=254)
    phone = forms.CharField(label="Phone", required=False, max_length=30,
                            validators=[RegexValidator(r"^[0-9+()\-.\s]{6,30}$", "Enter a valid phone number.")])
    enquiry_type = forms.ChoiceField(label="Enquiry type", choices=[("", "Select…")] + Enquiry.TYPES)
    subject = forms.CharField(min_length=3, max_length=150)
    message = forms.CharField(min_length=10, max_length=5000, widget=forms.Textarea(attrs={"rows": 6}))
    website = forms.CharField(required=False, label="Leave this field empty",
                              widget=forms.TextInput(attrs={"tabindex": "-1", "autocomplete": "off"}))   # honeypot
    token = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean_name(self):
        return _squash(self.cleaned_data["name"])

    def clean_company(self):
        return _squash(self.cleaned_data.get("company"))

    def clean_phone(self):
        return _squash(self.cleaned_data.get("phone"))

    def clean_subject(self):
        return _squash(self.cleaned_data["subject"])

    def clean_enquiry_type(self):
        v = self.cleaned_data["enquiry_type"]
        if not v:
            raise forms.ValidationError("Please choose an enquiry type.")
        return v

    def clean_message(self):
        m = self.cleaned_data["message"].replace("\r\n", "\n").strip()
        if len(m) < 10:
            raise forms.ValidationError("Please write a little more so we can help.")
        if len(_URL.findall(m)) > 3:
            raise forms.ValidationError("Your message contains too many links.")
        return m

    @property
    def is_bot(self):
        return bool(self.cleaned_data.get("website"))

    def clean(self):
        data = super().clean()
        # time trap: the form must have been rendered a few seconds (and at most a day) before submission
        try:
            issued = int(_signer.unsign(data.get("token") or "", max_age=24 * 3600))
        except (signing.BadSignature, ValueError):
            raise forms.ValidationError("Your session expired. Please reload the page and submit again.")
        if time.time() - issued > 24 * 3600:
            raise forms.ValidationError("Your session expired. Please reload the page and submit again.")
        if time.time() - issued < settings.ENQUIRY_MIN_FILL_SECONDS:
            raise forms.ValidationError("That was a little too fast. Please check your details and submit again.")
        return data


class ReplyForm(BootstrapMixin, forms.Form):
    subject = forms.CharField(max_length=200)
    body = forms.CharField(min_length=2, max_length=10000, widget=forms.Textarea(attrs={"rows": 7}), label="Message")


class NoteForm(BootstrapMixin, forms.Form):
    body = forms.CharField(max_length=3000, widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Internal note - never e-mailed"}), label="Internal note")


class StatusForm(BootstrapMixin, forms.Form):
    status = forms.ChoiceField(choices=Enquiry.STATUSES)


class AssignForm(BootstrapMixin, forms.Form):
    assignee = forms.ModelChoiceField(queryset=User.objects.none(), required=False, empty_label="Unassigned")

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["assignee"].queryset = User.objects.filter(is_active=True, role__in=["owner", "accounts"]).order_by("username")
        self.fields["assignee"].label_from_instance = lambda u: u.get_full_name() or u.username
