from django.urls import path

from . import views

app_name = "purchasing"
urlpatterns = [
    path("", views.bill_list, name="list"),
    path("new/", views.bill_create, name="create"),
    path("<int:pk>/", views.bill_detail, name="detail"),
    path("<int:pk>/post/", views.bill_post, name="post"),
    path("<int:pk>/delete/", views.bill_delete, name="delete"),
]
