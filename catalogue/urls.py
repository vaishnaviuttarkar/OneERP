from django.urls import path

from . import views

app_name = "catalogue"
urlpatterns = [
    path("products/", views.products, name="products"),
    path("products/new/", views.product_create, name="product_create"),
    path("products/<int:pk>/edit/", views.product_edit, name="product_edit"),
    path("products/<int:pk>/delete/", views.product_delete, name="product_delete"),
    path("variants/new/", views.variant_create, name="variant_create"),
    path("variants/<int:pk>/edit/", views.variant_edit, name="variant_edit"),
    path("variants/<int:pk>/delete/", views.variant_delete, name="variant_delete"),
]
