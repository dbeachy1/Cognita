"""Root resolver providing DOT's expected oauth2_provider namespace."""
from django.urls import include, path

urlpatterns = [
    path("", include(("cognita.oauth_service.urls", "oauth2_provider"), namespace="oauth2_provider")),
]
