"""URL surface for the loopback DOT child."""

from django.urls import path
from oauth2_provider.views import RevokeTokenView, TokenView

from .views import (
    CognitaAuthorizationView,
    CognitaDCRManagementView,
    CognitaDCRView,
    ConnectionsView,
    IntrospectionView,
    LoginView,
    ReadyView,
    ServiceMetadataView,
)

urlpatterns = [
    path(".well-known/oauth-authorization-server", ServiceMetadataView.as_view(), name="oauth-server-metadata"),
    path(".well-known/oauth-authorization-server/", ServiceMetadataView.as_view()),
    path("oauth/login", LoginView.as_view()),
    path("oauth/authorize", CognitaAuthorizationView.as_view(), name="authorize"),
    path("oauth/authorize/", CognitaAuthorizationView.as_view()),
    path("oauth/token", TokenView.as_view(), name="token"),
    path("oauth/token/", TokenView.as_view()),
    path("oauth/revoke", RevokeTokenView.as_view(), name="revoke-token"),
    path("oauth/revoke/", RevokeTokenView.as_view()),
    path("oauth/register", CognitaDCRView.as_view(), name="dcr-register"),
    path("oauth/register/", CognitaDCRView.as_view()),
    path("oauth/register/<str:client_id>", CognitaDCRManagementView.as_view(), name="dcr-register-management"),
    path("oauth/register/<str:client_id>/", CognitaDCRManagementView.as_view()),
    path("_cognita/ready", ReadyView.as_view()),
    path("_cognita/introspect", IntrospectionView.as_view()),
    path("_cognita/connections", ConnectionsView.as_view()),
    path("_cognita/connections/<str:connection_id>", ConnectionsView.as_view()),
]
