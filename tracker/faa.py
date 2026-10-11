"""FAA airports/heliports near landing spots: linking spots to them, and creating sites for them automatically."""

from django.conf import settings
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth import get_user_model

from . import rawstore
from .geo import SiteIndex, haversine_m
from .models import Aircraft, FaaFacility, LandingCluster, Leg, Site


def faa_near(clusters):
    """{cluster pk: (FaaFacility, distance m, Site with that FAA ID or None)} for clusters within
    FAA_LINK_RADIUS_M of an FAA airport/heliport (the nearest one)."""
    if not clusters:
        return {}
    pad = 0.05
    lats, lons = [c.lat for c in clusters], [c.lon for c in clusters]
    index = SiteIndex(FaaFacility.objects.filter(lat__range=(min(lats) - pad, max(lats) + pad),
                                                 lon__range=(min(lons) - pad, max(lons) + pad)))
    out = {}
    for c in clusters:
        near = index.within(c.lat, c.lon, settings.FAA_LINK_RADIUS_M)
        if near:
            out[c.pk] = (near[0][1], round(near[0][0]), None)
    sites = {s.faa_id.upper(): s for s in Site.objects.filter(faa_id__in=[f.faa_id for f, _, _ in out.values()])}
    return {pk: (f, d, sites.get(f.faa_id.upper())) for pk, (f, d, _) in out.items()}


def auto_create_faa_sites(source):
    """Create a site for each landing spot within FAA_LINK_RADIUS_M of an FAA airport/heliport that is not a
    site yet, when a confirmed aircraft landed there. The site gets the FAA details, the spot's location
    (where the aircraft actually land), and is marked reviewed; the spots it covers leave the Cluster queue.
    Legs of the aircraft that landed there are re-derived at once, so those landings count at the new sites
    without a full rebuild. Returns the new sites."""
    from .inference.pipeline import build_legs
    from .places import clear_covered_clusters
    from .seed import default_radius

    if source != settings.PRIMARY_SOURCE:
        return []
    clusters = list(LandingCluster.objects.filter(source=source, dismissed=False))
    linked = {pk: f for pk, (f, _, site) in faa_near(clusters).items() if site is None}
    if not linked:
        return []
    unresolved = Leg.objects.filter(source=source, end_state=Leg.LANDED, end_site__isnull=True)
    confirmed = list(unresolved.filter(aircraft__status=Aircraft.CONFIRMED).values_list("end_lat", "end_lon"))
    owner = get_user_model().objects.filter(is_superuser=True).order_by("pk").first()
    created, done = [], set()
    for c in sorted(clusters, key=lambda c: -c.landing_count):
        f = linked.get(c.pk)
        if f is None or f.faa_id in done:
            continue
        n = sum(1 for lat, lon in confirmed if haversine_m(c.lat, c.lon, lat, lon) <= settings.CLUSTER_RADIUS_M)
        if not n:
            continue  # only discovered (unconfirmed) aircraft land here: leave it to an admin
        done.add(f.faa_id)
        site = Site.objects.create(
            name=f.name, site_type=f.site_type, faa_id=f.faa_id, city=f.city, state=f.state, lat=c.lat, lon=c.lon,
            elevation_ft=f.elevation_ft, radius_m=default_radius(f.site_type), origin="from_cluster", reviewed=True,
            notes=f"Created automatically: {n} landing{'s' if n != 1 else ''} by confirmed aircraft within "
                  f"{settings.CLUSTER_RADIUS_M} m, {round(haversine_m(c.lat, c.lon, f.lat, f.lon))} m from FAA "
                  f"{f.faa_id}.")
        clear_covered_clusters(site)
        if owner:
            LogEntry.objects.log_actions(user_id=owner.pk, queryset=[site], action_flag=ADDITION, single_object=True,
                                         change_message=f"Created automatically from FAA {f.faa_id}")
        created.append(site)
    if created:
        reach = {s.pk: max(s.radius_m, settings.CLUSTER_RADIUS_M) for s in created}
        hexes = {h for h, lat, lon in unresolved.values_list("hex", "end_lat", "end_lon")
                 if any(haversine_m(s.lat, s.lon, lat, lon) <= reach[s.pk] for s in created)}
        build_legs(source, hexes & rawstore.hexes(source))  # only hexes whose traces are still kept
    return created
