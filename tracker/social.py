"""Facebook / Instagram pages set by admins on an aircraft, its program or its operator.

Stored as the bare username (or "id:<number>" for Facebook pages without one); the links are built here from one
pattern per site, so a change in a site's URL structure is a one-line fix that applies everywhere."""

import re
import urllib.request
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ValidationError

# field -> (site name, hosts, link pattern for a username)
SITES = {
    "facebook": ("Facebook", ("facebook.com", "fb.com", "fb.me"), "https://www.facebook.com/{}"),
    "instagram": ("Instagram", ("instagram.com",), "https://www.instagram.com/{}/"),
}
FACEBOOK_ID_URL = "https://www.facebook.com/profile.php?id={}"
USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
NOT_A_PAGE = {"search", "explore", "home.php", "watch", "groups", "events", "marketplace", "hashtag", "p", "reel",
              "reels", "stories", "share", "sharer.php", "login", "accounts", "tags"}


def page_url(field, value):
    """The link for a stored username (or Facebook "id:<number>")."""
    if not value:
        return ""
    if field == "facebook" and value.startswith("id:"):
        return FACEBOOK_ID_URL.format(value[3:])
    return SITES[field][2].format(value)


USER_AGENT = "Mozilla/5.0 (compatible; helopath/1.0; +page lookup)"
CANONICAL = re.compile(r"""<(?:link[^>]+rel=["']canonical["'][^>]*href|meta[^>]+property=["']og:url["'][^>]*content)=["']([^"']+)""",
                       re.I)


def resolve_redirect(url, opener=urllib.request.urlopen):
    """Where a Facebook share / short link leads: the final address after redirects, or the page's canonical link
    if it lands on an interstitial. Raises on network errors."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with opener(req, timeout=8) as r:
        final = r.geturl()
        body = r.read(300_000).decode("utf-8", "replace")
    if _is_share(urlparse(final)):
        match = CANONICAL.search(body)
        if match:
            return match.group(1).replace("&amp;", "&")
    return final


def _is_share(url):
    host = (url.hostname or "").lower()
    parts = [p for p in url.path.split("/") if p]
    return host == "fb.me" or host.endswith(".fb.me") or parts[:1] == ["share"]


def normalize(field, value, _depth=0):
    """Username to store from whatever was typed: a full link, 'facebook.com/page', '@handle' or 'handle'.
    Raises ValidationError for another site, a site page that is not a profile, or an invalid name."""
    value = (value or "").strip()
    if not value:
        return ""
    name, hosts, _ = SITES[field]
    bad = ValidationError({field: f"Enter a {name} username (e.g. osflifeflight), @username, or a link to the "
                                  f"{name} page."})
    if value.startswith("@"):
        value = value[1:].strip()
    elif "://" in value or "/" in value or any(h in value.lower() for h in hosts):
        url = urlparse(value if "://" in value else "https://" + value)
        host = (url.hostname or "").lower()
        if url.scheme not in ("http", "https") or not any(host == h or host.endswith("." + h) for h in hosts):
            raise bad
        parts = [p for p in url.path.split("/") if p]
        if field == "facebook" and _is_share(url):
            # A share link (facebook.com/share/<code>/, fb.me/<code>) has no username: follow it to the page.
            failed = ValidationError({field: "Couldn't read the page from that share link. Open it in your browser "
                                             "and copy the address bar link instead (e.g. facebook.com/osflifeflight)."})
            if _depth:
                raise failed
            try:
                final = urlparse(resolve_redirect(value if "://" in value else "https://" + value))
            except Exception:
                raise failed
            if (final.path.strip("/").split("/")[:1] == ["login"]) and parse_qs(final.query).get("next"):
                final = urlparse(parse_qs(final.query)["next"][0])  # login wall: the page is in ?next=
            try:
                return normalize(field, final.geturl(), _depth=1)
            except ValidationError:
                raise failed
        if field == "facebook" and parts[:1] == ["profile.php"]:
            page_id = (parse_qs(url.query).get("id") or [""])[0]
            if page_id.isdigit():
                return f"id:{page_id}"
            raise bad
        if field == "facebook" and parts[:1] == ["pages"] and parts[-1].isdigit():
            return f"id:{parts[-1]}"  # facebook.com/pages/Some-Name/123456
        if not parts or parts[0].lower() in NOT_A_PAGE:
            raise ValidationError({field: f"That {name} link is not a page; use the page's own link or username."})
        value = parts[0]
    if field == "facebook" and value.startswith("id:") and value[3:].isdigit():
        return value
    if not USERNAME.match(value):
        raise bad
    return value


class SocialPagesMixin:
    """Model mixin: clean() turns whatever was typed in facebook / instagram into the stored username."""

    def social_links(self):
        """[(field, site name, url)] for the pages set on this object."""
        return [(f, SITES[f][0], page_url(f, getattr(self, f))) for f in SITES if getattr(self, f, "")]

    def clean(self):
        super().clean()
        errors = {}
        for field in SITES:
            try:
                setattr(self, field, normalize(field, getattr(self, field)))
            except ValidationError as e:
                errors.update(e.message_dict)
        if errors:
            raise ValidationError(errors)


def page_links(aircraft):
    """{field: (url, where it was set)} using the aircraft's own page, else its program's, else its operator's."""
    out = {}
    for field in SITES:
        for obj, where in ((aircraft, "this aircraft"), (aircraft.program, "its program"),
                           (aircraft.operator, "its operator")):
            if obj is not None and getattr(obj, field, ""):
                out[field] = (page_url(field, getattr(obj, field)), where)
                break
    return out
