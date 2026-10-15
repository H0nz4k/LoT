"""Optional Cloudflare Access authentication for remote device controls."""
import os
import re
from threading import Lock
from urllib.parse import urlsplit

import jwt


class RemoteAccess:
    def __init__(self, team_domain="", audience="", origin="", emails=""):
        team = team_domain.removeprefix("https://").rstrip("/").lower()
        self.audience = audience.strip()
        self.origin = origin.rstrip("/")
        self.emails = {email.strip().casefold() for email in emails.split(",") if email.strip()}
        self.lock = Lock()
        self.client = None
        self.issuer = "https://" + team
        try:
            parsed = urlsplit(self.origin)
            valid_origin = (parsed.scheme == "https" and bool(parsed.hostname)
                            and not parsed.username and not parsed.password
                            and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            valid_origin = False
        self.enabled = bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.cloudflareaccess\.com", team)
                            and self.audience and valid_origin)
        if self.enabled:
            self.client = jwt.PyJWKClient(self.issuer + "/cdn-cgi/access/certs",
                                         cache_jwk_set=True, lifespan=300, timeout=5)

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("HANZHUB_ACCESS_TEAM_DOMAIN", ""),
                   os.environ.get("HANZHUB_ACCESS_AUD", ""),
                   os.environ.get("HANZHUB_ACCESS_ORIGIN", ""),
                   os.environ.get("HANZHUB_ACCESS_ALLOWED_EMAILS", ""))

    def verify(self, token):
        if not self.enabled or not token or len(token) > 16384:
            return False
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                return False
            if not 0 < len(header["kid"]) <= 256:
                return False
            # Key URL comes only from trusted server configuration, never JWT jku/x5u.
            with self.lock:
                signing_key = self.client.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, signing_key.key, algorithms=["RS256"],
                                issuer=self.issuer, audience=self.audience,
                                options={"require": ["exp", "iat", "iss", "aud", "sub", "email"]})
            email = claims.get("email")
            return bool(claims.get("sub") and isinstance(email, str) and email.strip()
                        and (not self.emails or email.casefold() in self.emails))
        except (jwt.PyJWTError, ValueError, TypeError, OSError):
            # Fail closed, including expired tokens, wrong audience and JWKS outages.
            return False


def remote_route_allowed(method, path):
    if method == "GET" and path in ("/api/iot/info", "/api/iot/health", "/api/iot/devices",
                                    "/api/iot/panel", "/api/iot/events"):
        return True
    if method == "GET" and re.fullmatch(r"/api/iot/devices/[a-f0-9]{12}/state", path):
        return True
    return method == "POST" and bool(re.fullmatch(r"/api/iot/devices/[a-f0-9]{12}/command", path))
