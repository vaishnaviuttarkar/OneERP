from django.urls import path

from . import views

app_name = "parties"
urlpatterns = [
    path("", views.party_list, name="list"),
    path("new/", views.party_create, name="create"),
    path("<int:pk>/", views.party_detail, name="detail"),
    path("<int:pk>/edit/", views.party_edit, name="edit"),
    path("<int:pk>/delete/", views.party_delete, name="delete"),
    path("<int:pk>/toggle/", views.party_toggle, name="toggle"),
]
