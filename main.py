"""Google cloud function entry points."""

from probe_scraper.glean_push import main as glean_push

__all__ = ["glean_push"]
