from django.urls import path

from . import views

app_name = "enquiries"
urlpatterns = [
    path("", views.enquiry_list, name="list"),
    path("<int:pk>/", views.enquiry_detail, name="detail"),
    path("<int:pk>/status/", views.set_status, name="status"),
    path("<int:pk>/assign/", views.set_assignee, name="assign"),
    path("<int:pk>/note/", views.post_note, name="note"),
    path("<int:pk>/reply/", views.post_reply, name="reply"),
    path("<int:pk>/resend/", views.resend, name="resend"),
]
