"""Every tunable number in one place (spec section 11).

Changing anything here takes effect after `manage.py rebuild --source <source>`.
Values can be overridden with environment variables of the same name prefixed HELOPATH_.
"""
import os


def _env(name, default):
    raw = os.environ.get(f"HELOPATH_{name}")
    return type(default)(raw) if raw is not None else default


# Region of interest: Illinois plus border receiving areas.
BBOX_LAT_MIN = _env("BBOX_LAT_MIN", 36.5)
BBOX_LAT_MAX = _env("BBOX_LAT_MAX", 43.0)
BBOX_LON_MIN = _env("BBOX_LON_MIN", -92.0)
BBOX_LON_MAX = _env("BBOX_LON_MAX", -87.0)

# Retention.
BACKFILL_DAYS = _env("BACKFILL_DAYS", 90)
UNDISCOVERED_RETENTION_DAYS = _env("UNDISCOVERED_RETENTION_DAYS", 120)

# Leg segmentation.
LEG_GAP_MINUTES = _env("LEG_GAP_MINUTES", 10.0)
LEG_MIN_AIRBORNE_POINTS = _env("LEG_MIN_AIRBORNE_POINTS", 3)
LEG_MIN_DURATION_S = _env("LEG_MIN_DURATION_S", 60.0)
LEG_TRACK_MAX_POINTS = _env("LEG_TRACK_MAX_POINTS", 200)

# Leg end state.
SITE_DEFAULT_RADIUS_M = _env("SITE_DEFAULT_RADIUS_M", 300)
LANDED_MAX_AGL_FT = _env("LANDED_MAX_AGL_FT", 200.0)
LANDED_MAX_GS_KT = _env("LANDED_MAX_GS_KT", 20.0)
INFERRED_MAX_DIST_M = _env("INFERRED_MAX_DIST_M", 3000.0)
INFERRED_MAX_AGL_FT = _env("INFERRED_MAX_AGL_FT", 1500.0)
INFERRED_DESCENT_LOOKBACK_S = _env("INFERRED_DESCENT_LOOKBACK_S", 120.0)
# Low-and-slow at signal loss: many hospital pads are on rooftops or parking structures, where
# the transponder rarely reports "ground" and coverage often drops on short final. If the last
# point is this slow and this low near a site, assume the aircraft landed there. The altitude
# limit is generous because a rooftop pad can sit a few hundred feet above the site elevation.
LOW_SLOW_MAX_GS_KT = _env("LOW_SLOW_MAX_GS_KT", 40.0)
LOW_SLOW_MAX_AGL_FT = _env("LOW_SLOW_MAX_AGL_FT", 500.0)
LOW_SLOW_RADIUS_M = _env("LOW_SLOW_RADIUS_M", 1000.0)

# Confidence (starts at 1.0 and is reduced by each penalty).
CONF_INFERRED_CAP = _env("CONF_INFERRED_CAP", 0.9)
CONF_LOW_SLOW_CAP = _env("CONF_LOW_SLOW_CAP", 0.85)
CONF_LOW_SLOW_DIST_PENALTY = _env("CONF_LOW_SLOW_DIST_PENALTY", 0.2)  # at LOW_SLOW_RADIUS_M
CONF_DIST_PENALTY = _env("CONF_DIST_PENALTY", 0.4)        # at INFERRED_MAX_DIST_M
CONF_ALT_PENALTY = _env("CONF_ALT_PENALTY", 0.3)          # at INFERRED_MAX_AGL_FT
CONF_NEIGHBOR_PENALTY = _env("CONF_NEIGHBOR_PENALTY", 0.2)
CONF_NEIGHBOR_RADIUS_M = _env("CONF_NEIGHBOR_RADIUS_M", 1000.0)

# Auto-discovery (spec section 8).
DISCOVERY_MIN_VISITS = _env("DISCOVERY_MIN_VISITS", 3)
DISCOVERY_MIN_DAYS = _env("DISCOVERY_MIN_DAYS", 2)
DISCOVERY_WINDOW_DAYS = _env("DISCOVERY_WINDOW_DAYS", 30)
DISCOVERY_MIN_INFERRED_CONF = _env("DISCOVERY_MIN_INFERRED_CONF", 0.6)

# Landing clusters.
CLUSTER_RADIUS_M = _env("CLUSTER_RADIUS_M", 150.0)
BASE_POINT_RADIUS_M = _env("BASE_POINT_RADIUS_M", 300.0)  # base that is not (yet) a Site

# Mission chaining.
MISSION_MAX_LEG_GAP_HOURS = _env("MISSION_MAX_LEG_GAP_HOURS", 12.0)
MISSION_LOST_SIGNAL_BRIDGE_HOURS = _env("MISSION_LOST_SIGNAL_BRIDGE_HOURS", 2.0)
MISSION_UNKNOWN_CONF = _env("MISSION_UNKNOWN_CONF", 0.3)

# Data quality: the "local day" used for inferred base and monthly buckets.
LOCAL_TZ = _env("LOCAL_TZ", "America/Chicago")

# The source that drives discovery and the Aircraft table. Other sources (adsbx) are
# evaluation-only: they get legs and a coverage report, never Aircraft rows.
PRIMARY_SOURCE = "adsblol"
