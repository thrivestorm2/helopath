"""FAA airports/heliports near landing spots: linking spots to them, and creating sites for them automatically."""

from django.conf import settings
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
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



def _keeper_rank(site, arrivals):
    """Which duplicate to keep: reviewed first, then one an admin made (not the FAA seed), then the busiest."""
    return (not site.reviewed, site.origin == "faa_seed", -arrivals.get(site.pk, 0), site.pk)


def merge_duplicate_faa_sites():
    """Sites sharing an FAA ID (ignoring case and spaces) are merged into one: everything that points at a
    duplicate (legs, missions, aircraft bases, hangars, totals, landing spots) is moved to the kept site, its
    blank fields are filled from the duplicate, the larger radius is kept, and the duplicate is deleted.
    Returns [(kept site, [removed names])]."""
    from collections import defaultdict

    from django.db import transaction
    from django.db.models import Count

    groups = defaultdict(list)
    for site in Site.objects.exclude(faa_id=""):
        groups[site.faa_id.strip().upper()].append(site)
    groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not groups:
        return []
    ids = [s.pk for sites in groups.values() for s in sites]
    arrivals = dict(Leg.objects.filter(end_site_id__in=ids).values_list("end_site").annotate(n=Count("pk")))
    relations = [f for f in Site._meta.get_fields(include_hidden=True)
                 if f.auto_created and not f.concrete and (f.one_to_many or f.one_to_one)]
    owner = get_user_model().objects.filter(is_superuser=True).order_by("pk").first()
    merged = []
    for code, sites in groups.items():
        sites.sort(key=lambda s: _keeper_rank(s, arrivals))
        keep, dups = sites[0], sites[1:]
        with transaction.atomic():
            for dup in dups:
                for rel in relations:
                    # A site whose weather hangar is the duplicate now uses the kept site.
                    rel.related_model.objects.filter(**{rel.field.name: dup}).update(**{rel.field.name: keep})
                for field in ("city", "state", "faa_id"):
                    if not getattr(keep, field):
                        setattr(keep, field, getattr(dup, field))
                keep.radius_m = max(keep.radius_m, dup.radius_m)
                keep.reviewed = keep.reviewed or dup.reviewed
                keep.is_active = keep.is_active or dup.is_active
                keep.notes = "\n".join(n for n in (keep.notes, f"Merged with duplicate '{dup.name}' (same FAA ID "
                                                    f"{code}); its notes: {dup.notes or 'none'}") if n)
            keep.faa_id = code
            if keep.weather_hangar_id in {d.pk for d in dups}:
                keep.weather_hangar = keep
            keep.save()
            names = [d.name for d in dups]
            Site.objects.filter(pk__in=[d.pk for d in dups]).delete()
        if owner:
            LogEntry.objects.log_actions(user_id=owner.pk, queryset=[keep], action_flag=CHANGE, single_object=True,
                                         change_message=f"Merged duplicate sites with FAA ID {code}: "
                                                        + ", ".join(names))
        merged.append((keep, names))
    return merged
