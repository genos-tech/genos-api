"""OAuth 2.1 authorization server routes for MCP (MIP-1077).

    GET   /.well-known/oauth-authorization-server[/<path>]   RFC 8414 metadata
    GET   /.well-known/oauth-protected-resource[/<path>]     RFC 9728 metadata
    GET   /oauth/authorize/    validate, then to the consent page of the Genos app
    POST  /oauth/token/        authorization code and refresh, PKCE
    POST  /oauth/revoke_token/ RFC 7009
    POST  /oauth/register/     RFC 7591 (policy in services/mcp_oauth.py)

The metadata lives at the ORIGIN's `.well-known`, as the RFCs require, so these are mounted at the
root, not under a prefix. Registered under django-oauth-toolkit's own namespace (`oauth2_provider`)
because its metadata view finds the endpoints to advertise by reversing those names: only these
four are mounted, so only these four are advertised. Its management pages, device flow,
introspection and OIDC are not mounted at all. Everything answers 404 until `MCP_OAUTH_ENABLED`.
"""

from django.urls import path

from origin.views.public import oauth_views as views

app_name = "oauth2_provider"

urlpatterns = [
    path(
        ".well-known/oauth-authorization-server",
        views.server_metadata_view,
        name="oauth-server-metadata",
    ),
    path(
        ".well-known/oauth-authorization-server/<path:issuer_path>",
        views.server_metadata_view,
        name="oauth-server-metadata-issuer",
    ),
    path(
        ".well-known/oauth-protected-resource",
        views.resource_metadata_view,
        name="oauth-resource-metadata",
    ),
    path(
        ".well-known/oauth-protected-resource/<path:resource_path>",
        views.resource_metadata_view,
        name="oauth-resource-metadata-path",
    ),
    path("oauth/authorize/", views.authorize_view, name="authorize"),
    path("oauth/token/", views.token_view, name="token"),
    path("oauth/revoke_token/", views.revoke_view, name="revoke-token"),
    path("oauth/register/", views.register_view, name="dcr-register"),
    # Named only because the library reverses it when it answers a registration (RFC 7592
    # management is not offered; the view is a 404 and the response leaves the link out).
    path(
        "oauth/register/<str:client_id>/",
        views.registration_management_view,
        name="dcr-register-management",
    ),
]
