"""Facebook / Instagram page links set by admins on an aircraft, its program or its operator."""

from urllib.parse import urlparse

from django.core.exceptions import ValidationError

SITES = {"facebook_url": ("facebook", ("facebook.com", "fb.com")), "instagram_url": ("instagram", ("instagram.com",))}


def normalize(field, value):
    """Full https link for a typed page: accepts full links, 'facebook.com/page' and '@handle'. Raises
    ValidationError for anything that is not the right site."""
    value = (value or "").strip()
    if not value:
        return ""
    site, hosts = SITES[field]
    if value.startswith("@"):
        value = f"https://www.{site}.com/{value[1:].strip('/')}"
    elif "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not any(host == h or host.endswith("." + h) for h in hosts):
        raise ValidationError({field: f"Enter a {site.title()} page link (e.g. https://www.{site}.com/pagename) "
                                      f"or @pagename."})
    if not parsed.path.strip("/"):
        raise ValidationError({field: f"That is the {site.title()} home page; enter the page's own link."})
    return value


class SocialPagesMixin:
    """Model mixin: clean() normalises facebook_url / instagram_url."""

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
    """{field: (url, where it was set)} using the aircraft's own link, else its program's, else its operator's."""
    out = {}
    for field in SITES:
        for obj, where in ((aircraft, "this aircraft"), (aircraft.program, "its program"),
                           (aircraft.operator, "its operator")):
            if obj is not None and getattr(obj, field, ""):
                out[field] = (getattr(obj, field), where)
                break
    return out
