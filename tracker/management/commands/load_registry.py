import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand

from tracker.registry import download_registry, load_registry


class Command(BaseCommand):
    help = "Load the FAA releasable aircraft registry (rotorcraft rows) into RegistryAircraft. Run monthly."

    def add_arguments(self, parser):
        parser.add_argument("--zip", help="Path to ReleasableAircraft.zip (downloaded if omitted)")

    def handle(self, *args, **opts):
        path = opts["zip"]
        if not path:
            path = Path(tempfile.mkdtemp()) / "ReleasableAircraft.zip"
            self.stdout.write(f"downloading registry to {path}")
            download_registry(path)
        n = load_registry(path)
        self.stdout.write(self.style.SUCCESS(f"loaded {n} rotorcraft with Mode S hex codes"))
