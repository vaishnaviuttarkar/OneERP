from django.urls import path

from . import views

app_name = "inventory"
urlpatterns = [
    path("", views.stock, name="stock"),
    path("movements/", views.movements, name="movements"),
    path("adjust/", views.adjust, name="adjust"),
]
