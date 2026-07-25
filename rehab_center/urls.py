from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from operations.api import api
from operations.views import healthcheck

urlpatterns = [
    path("healthz/", healthcheck, name="healthcheck"),
    path("admin/", admin.site.urls),
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="registration/login.html", redirect_authenticated_user=True),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("api/", api.urls),
    path("", include("operations.urls")),
]
