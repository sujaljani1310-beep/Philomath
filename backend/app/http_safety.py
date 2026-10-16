"""Stateless HTTP clients and safe redirect policy for credentialed APIs."""

from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx


class _RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class _StatelessCookieJar(CookieJar):
    def set_cookie(self, cookie):
        # HTTPX's request builder copies a nonempty jar into a default-policy
        # jar. Prevent explicit SDK cookie writes as well as Set-Cookie storage.
        return None


def stateless_cookies() -> CookieJar:
    """Return a fresh cookie jar that never retains upstream session state."""
    return _StatelessCookieJar(policy=_RejectCookies())


def reject_unsafe_redirects(response: httpx.Response) -> None:
    """Permit redirects only within the same HTTPS origin.

    An HTTPX response hook runs before redirect follow-up requests, including
    307/308 body replay. Configured initial gateways remain valid destinations.
    Error messages deliberately omit URLs, headers, and response content.
    """
    if not response.has_redirect_location:
        return
    source = response.request.url
    try:
        destination = source.join(response.headers["location"])
    except (httpx.InvalidURL, UnicodeError, ValueError):
        raise httpx.ProtocolError(
            "Refusing an invalid upstream redirect.", request=response.request
        ) from None
    if (
        destination.scheme != "https"
        or source.scheme != "https"
        or (destination.scheme, destination.host, destination.port)
        != (source.scheme, source.host, source.port)
        or destination.username
        or destination.password
    ):
        raise httpx.ProtocolError(
            "Refusing an unsafe upstream redirect.", request=response.request
        )
