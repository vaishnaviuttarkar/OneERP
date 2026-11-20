from django import forms
from django.contrib.auth.forms import UserCreationForm

from core.forms import BootstrapMixin
from .models import User


class UserCreateForm(BootstrapMixin, UserCreationForm):
    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "role"]
