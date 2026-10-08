"""Source adapters (spec section 7). Everything downstream of an adapter is source-blind."""
from .base import SourceAdapter, TracePoint, ScanStats  # noqa: F401


def get_adapter(name, **kwargs):
    if name == "adsblol":
        from .adsblol import AdsbLolAdapter

        return AdsbLolAdapter(**kwargs)
    if name == "adsbx":
        from .adsbx import AdsbxSampleAdapter

        return AdsbxSampleAdapter(**kwargs)
    raise ValueError(f"unknown source {name!r}")
