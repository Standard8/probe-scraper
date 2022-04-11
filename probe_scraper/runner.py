# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import datetime
import errno
import json
import os
import sys
import tempfile
import traceback
from collections import defaultdict
from pathlib import Path
from typing import List, Optional

from dateutil.tz import tzlocal

from . import fog_checks, glean_checks, transform_probes, transform_revisions
from .emailer import send_ses
from .remote_storage import remote_storage_pull, remote_storage_push
from .parsers.events import EventsParser
from .parsers.histograms import HistogramsParser
from .parsers.metrics import GleanMetricsParser
from .parsers.pings import GleanPingsParser
from .parsers.repositories import RepositoriesParser
from .parsers.scalars import ScalarsParser
from .parsers.tags import GleanTagsParser
from .scrapers import git_scraper, moz_central_scraper


class DummyParser:
    def parse(self, files):
        return {}


FROM_EMAIL = "telemetry-alerts@mozilla.com"
DEFAULT_TO_EMAIL = "glean-team@mozilla.com"
FIRST_APPEARED_DATE_KEY = "first_added"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


PARSERS = {
    # This lists the available probe registry parsers:
    # parser type -> parser
    "event": EventsParser(),
    "histogram": HistogramsParser(),
    "scalar": ScalarsParser(),
}

GLEAN_PARSER = GleanMetricsParser()
GLEAN_PINGS_PARSER = GleanPingsParser()
GLEAN_TAGS_PARSER = GleanTagsParser()
GLEAN_METRICS_FILENAME = "metrics.yaml"
GLEAN_PINGS_FILENAME = "pings.yaml"
GLEAN_TAGS_FILENAME = "tags.yaml"


def general_data():
    return {
        "lastUpdate": datetime.datetime.now(tzlocal()).isoformat(),
    }


def dump_json(data, out_dir: Path, file_name: str) -> Path:
    # Make sure that the output directory exists. This also creates
    # intermediate directories if needed.
    try:
        os.makedirs(out_dir)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    def date_serializer(o):
        if isinstance(o, datetime.datetime):
            return o.isoformat()

    path = out_dir / file_name
    with open(path, "w") as f:
        print("  " + path)
        json.dump(
            data,
            f,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
            default=date_serializer,
        )
    return path


def write_moz_central_probe_data(probe_data, revisions, out_dir: Path) -> List[Path]:
    # Save all our files to "outdir/firefox/..." to mimic a REST API.
    base_dir = out_dir / "firefox"

    print("\nwriting output:")
    dump_json(general_data(), base_dir, "general")
    dump_json(revisions, base_dir, "revisions")

    # Break down the output by channel. We don't need to write a revisions
    # file in this case, the probe data will contain human readable version
    # numbers along with revision numbers.
    for channel, channel_probes in probe_data.items():
        data_dir = os.path.join(base_dir, channel, "main")
        dump_json(channel_probes, data_dir, "all_probes")

    return [base_dir]


def write_general_data(out_dir):
    general_path = dump_json(general_data(), out_dir, "general")
    index_path = out_dir / "index.html"
    with open(index_path, "w") as f:
        f.write(
            """
            <html><head><title>Mozilla Probe Info</title></head>
            <body>This site contains metadata used by Mozilla's data collection
            infrastructure, for more information see
            <a href=\"https://mozilla.github.io/probe-scraper/\">the generated documentation</a>.
            </body></html>
            """
        )
    return [general_path, index_path]


def write_glean_metric_data(metrics, dependencies, out_dir):
    # Save all our files to "outdir/glean/<repo>/..." to mimic a REST API.
    for repo, metrics_data in metrics.items():
        dependencies_data = dependencies[repo]

        base_dir = out_dir / "glean" / repo

        dump_json(general_data(), base_dir, "general")
        dump_json(metrics_data, base_dir, "metrics")
        dump_json(dependencies_data, base_dir, "dependencies")


def write_glean_tag_data(tags, out_dir):
    # Save all our files to "outdir/glean/<repo>/..." to mimic a REST API.
    for repo, tags_data in tags.items():
        base_dir = out_dir / "glean" / repo
        dump_json(tags_data, base_dir, "tags")


def write_glean_ping_data(pings, out_dir):
    # Save all our files to "outdir/glean/<repo>/..." to mimic a REST API.
    for repo, pings_data in pings.items():
        base_dir = out_dir / "glean" / repo
        dump_json(pings_data, base_dir, "pings")


def write_repositories_data(repos, out_dir) -> List[Path]:
    json_data = [r.to_dict() for r in repos]
    file_path = dump_json(json_data, out_dir, "repositories")
    return [file_path]


def write_v2_data(repos, out_dir) -> List[Path]:
    base_dir = out_dir / "v2" / "glean"
    dump_json(
        repos["app-listings"], base_dir, "app-listings"
    )
    dump_json(
        repos["library-variants"],
        base_dir,
        "library-variants",
    )
    return [base_dir]


def parse_moz_central_probes(scraped_data):
    """
    Parse probe data from files into the form:
    channel_name: {
      node_id: {
        histogram: {
          name: ...,
          ...
        },
        scalar: {
          ...
        },
      },
      ...
    }
    """

    lookup_table = {}

    def dedupe_probes(results):
        # Most probes have exactly the same contents across revisions, so we
        # can get significant memory savings by deduplicating them across the
        # entire history.
        deduped = {}
        for key, value in results.items():
            # Get a stable hash for a dict, by sorting the keys when writing
            # out values.
            probe_hash = hash(json.dumps(value, sort_keys=True))
            lookup_for_name = lookup_table.get(key, None)
            if lookup_for_name is None:
                lookup_table[key] = {probe_hash: value}
                deduped[key] = value
            else:
                existing_probe = lookup_for_name.get(probe_hash, None)
                if existing_probe is None:
                    lookup_for_name[probe_hash] = value
                    deduped[key] = value
                else:
                    deduped[key] = existing_probe

        return deduped

    probes = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for channel, revisions in scraped_data.items():
        for revision, details in revisions.items():
            for probe_type, paths in details["registries"].items():
                results = PARSERS[probe_type].parse(paths, details["version"], channel)
                deduped = dedupe_probes(results)
                probes[channel][revision][probe_type] = deduped

    return probes


def add_first_appeared_dates(probes_by_channel, first_appeared_dates):
    for channel, probes in probes_by_channel.items():
        for probe_id, info in probes.items():
            if channel == "all":
                dates = first_appeared_dates[probe_id]
            else:
                dates = {
                    k: v
                    for k, v in first_appeared_dates[probe_id].items()
                    if k == channel
                }

            dates = {k: v.strftime(DATE_FORMAT) for k, v in dates.items()}
            probes_by_channel[channel][probe_id][FIRST_APPEARED_DATE_KEY] = dates

    return probes_by_channel


def load_moz_central_probes(
    cache_dir, out_dir, fx_version, min_fx_version, firefox_channel,
    update_mode: bool = False,
):
    if update_mode:
        raise NotImplementedError("update mode not implemented for moz central")

    if fx_version:
        min_fx_version = fx_version
        max_fx_version = fx_version
    else:
        max_fx_version = None

    if firefox_channel:
        channels = [firefox_channel]
    else:
        channels = None

    # Scrape all revisions from buildhub
    revision_data = moz_central_scraper.scrape_channel_revisions(
        cache_dir,
        min_fx_version=min_fx_version,
        max_fx_version=max_fx_version,
        channels=channels,
    )
    revision_probes = parse_moz_central_probes(revision_data)

    # Get the minimum revision and date per probe-channel
    revision_dates = transform_revisions.transform(revision_data)
    first_appeared_dates = transform_probes.get_minimum_date(
        revision_probes, revision_data, revision_dates
    )

    probes_by_channel = transform_probes.transform(
        revision_probes,
        revision_data,
        break_by_channel=True,
        revision_dates=revision_dates,
    )
    probes_by_channel["all"] = transform_probes.transform(
        revision_probes,
        revision_data,
        break_by_channel=False,
        revision_dates=revision_dates,
    )

    # Add in the first appeared dates
    probes_by_channel_with_dates = add_first_appeared_dates(
        probes_by_channel, first_appeared_dates
    )

    # Serialize the probe data to disk.
    update_paths = write_moz_central_probe_data(probes_by_channel_with_dates, revision_dates, out_dir)

    return update_paths


def load_glean_metrics(
    cache_dir,
    out_dir,
    repositories_file,
    dry_run,
    glean_repos,
    bugzilla_api_key: Optional[str],
    update_mode: bool = False,
    commit: Optional[str] = None,
    branch: Optional[str] = None,
    glean_repo_url: Optional[str] = None,
    output_bucket: Optional[str] = None,
):
    all_repos = RepositoriesParser().parse(repositories_file)
    if glean_repo_url:
        if glean_repos:
            raise ValueError("Must not specify both glean_repos and glean_repo_url")
        repositories = [r for r in all_repos if r.url == glean_repo_url]
    elif glean_repos:
        repositories = [r for r in all_repos if r.name in glean_repos]
    else:
        repositories = all_repos
    if commit and not update_mode:
        raise ValueError("Must not specify commit without update_mode")
    commit_timestamps, repos_metrics_data, emails, prod_repos = git_scraper.scrape(
        cache_dir, repositories, commit, branch
    )

    glean_checks.check_glean_metric_structure(repos_metrics_data)

    abort_after_emails = False

    update_paths = []
    if not update_mode or commit:
        # write pre-repo data outside update mode and when updating with a commit
        # Parse metric data from files into the form:
        # <repo_name>:  {
        #   <commit-hash>:  {
        #     <metric-name>: {
        #       ...
        #     },
        #   },
        #   ...
        # }
        tags = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        metrics = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        pings = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

        metrics_by_repo = {}
        pings_by_repo = {}
        tags_by_repo = {}
        # TODO load dependencies for glean_checks.check_for_duplicate_metrics
        repos_and_dependencies = [repo for repo in repos_metrics_data]
        for repo in repos_and_dependencies:
            metrics_by_repo[repo] = {}
            pings_by_repo[repo] = {}
            tags_by_repo[repo] = {}
            if update_mode:
                # initialize state from prior output
                base_dir = out_dir / "glean" / repo
                src = f"{output_bucket}/{base_dir.relative_to(out_dir)}"
                remote_storage_pull(src, base_dir, decompress=True)
                try:
                    with open(os.path.join(base_dir, "metrics"), "r") as metrics_file:
                        metrics_by_repo[repo] = json.load(metrics_file)
                except FileNotFoundError:
                    pass  # this is fine
                try:
                    with open(os.path.join(base_dir, "pings"), "r") as pings_file:
                        pings_by_repo[repo] = json.load(pings_file)
                except FileNotFoundError:
                    pass  # this is fine
                try:
                    with open(os.path.join(base_dir, "tags"), "r") as tags_file:
                        tags_by_repo[repo] = json.load(tags_file)
                except FileNotFoundError:
                    pass  # this is fine

        for repo_name, commits in repos_metrics_data.items():
            for commit_hash, paths in commits.items():
                tags_files = [p for p in paths if p.endswith(GLEAN_TAGS_FILENAME)]
                metrics_files = [p for p in paths if p.endswith(GLEAN_METRICS_FILENAME)]
                pings_files = [p for p in paths if p.endswith(GLEAN_PINGS_FILENAME)]

                try:
                    config = {"allow_reserved": repo_name.startswith("glean")}
                    repo = next(r for r in repositories if r.name == repo_name).to_dict()

                    if tags_files:
                        results, errs = GLEAN_TAGS_PARSER.parse(
                            tags_files, config, repo["url"], commit_hash
                        )
                        tags[repo_name][commit_hash] = results

                    if metrics_files:
                        # TODO preserve existing errs
                        results, errs = GLEAN_PARSER.parse(
                            metrics_files, config, repo["url"], commit_hash
                        )
                        metrics[repo_name][commit_hash] = results

                    if pings_files:
                        results, errs = GLEAN_PINGS_PARSER.parse(
                            pings_files, config, repo["url"], commit_hash
                        )
                        pings[repo_name][commit_hash] = results
                except Exception:
                    if commit:
                        # TODO pass error email content back to glean push
                        raise
                    files = tags_files + metrics_files + pings_files
                    msg = "Improper file in {}\n{}".format(
                        ", ".join(files), traceback.format_exc()
                    )
                    emails[repo_name]["emails"].append(
                        {"subject": "Probe Scraper: Improper File", "message": msg}
                    )
                else:
                    if errs:
                        msg = ("Error in processing commit {}\n" "Errors: [{}]").format(
                            commit_hash, ".".join(errs)
                        )
                        if commit:
                            # TODO pass error email content back to glean push
                            raise ValueError(msg)
                        emails[repo_name]["emails"].append(
                            {
                                "subject": "Probe Scraper: Error on parsing metric or ping files",
                                "message": msg,
                            }
                        )

        tags_by_repo.update(
            transform_probes.transform_tags_by_hash(commit_timestamps, tags)
        )

        metrics_by_repo.update(
            transform_probes.transform_metrics_by_hash(commit_timestamps, metrics)
        )

        pings_by_repo.update(
            transform_probes.transform_pings_by_hash(commit_timestamps, pings)
        )

        dependencies_by_repo = {}
        for repo in repositories:
            dependencies = {}
            for dependency in repo.dependencies:
                dependencies[dependency] = {"type": "dependency", "name": dependency}
            dependencies_by_repo[repo.name] = dependencies

        if glean_repos is None or len(glean_repos) > 1:
            # Don't check for duplicate metrics if we're only parsing
            # one glean repository (this will almost always crash, since
            # the libraries that most repositories depend on will not
            # be specified)
            abort_after_emails |= glean_checks.check_for_duplicate_metrics(
                repositories, metrics_by_repo, emails
            )
        # TODO raise exceptions?
        glean_checks.check_for_expired_metrics(
            repositories, metrics, commit_timestamps, emails, dry_run=dry_run
        )

        # FOG repos (e.g. firefox-desktop, gecko) use a different expiry mechanism.
        # Also, expired metrics in FOG repos can have bugs auto-filed for them.
        fog_emails_by_repo = fog_checks.file_bugs_and_get_emails_for_expiring_metrics(
            repositories, metrics, commit_timestamps, bugzilla_api_key, dry_run
        )
        if fog_emails_by_repo is not None:
            emails.update(fog_emails_by_repo)

        print("\nwriting output:")
        write_glean_tag_data(tags_by_repo, out_dir)
        write_glean_metric_data(metrics_by_repo, dependencies_by_repo, out_dir)
        write_glean_ping_data(pings_by_repo, out_dir)
        # in update mode, only write changes for prod repos
        update_paths += [out_dir / "glean" / repo for repo in prod_repos]

    if not update_mode or not commit:
        # write generic data outside update mode and when updating without a commit
        update_paths += write_repositories_data(repositories, out_dir)
        update_paths += write_general_data(out_dir)

        repos_v2 = RepositoriesParser().parse_v2(repositories_file)
        update_paths += write_v2_data(repos_v2, out_dir)

    for repo_name, email_info in list(emails.items()):
        addresses = email_info["addresses"] + [DEFAULT_TO_EMAIL]
        for email in email_info["emails"]:
            send_ses(
                FROM_EMAIL,
                email["subject"],
                email["message"],
                addresses,
                dryrun=dry_run,
            )

    if abort_after_emails:
        raise ValueError("Errors processing Glean metrics")

    return update_paths


def setup_output_and_cache_dirs(output_bucket: str, cache_bucket: str, out_dir: Path, cache_dir: Path):
    # Create the output directory
    os.mkdir(out_dir)

    # Sync the cache directory
    print(f"Syncing cache from {cache_bucket} with {cache_dir}")
    remote_storage_pull(cache_bucket, cache_dir)


def push_output_and_cache_dirs(
    output_bucket: str, cache_bucket: str, out_dir: Path, cache_dir: Path, update_mode: bool, update_paths: Optional[List[str]]
):
    # Check output dir and then sync with cloudfront
    if not os.listdir(out_dir):
        print("{} is empty".format(out_dir))
        sys.exit(1)

    output_kwargs = {"compress": True, "delete": True}
    if update_mode:
        for src in update_paths:
            dst = f"{output_bucket}/{src.relative_to(out_dir)}"
            remote_storage_push(src, dst, **output_kwargs)
    else:
        remote_storage_push(src=out_dir, dst=output_bucket, **output_kwargs)
    remote_storage_push(src=cache_dir, dst=cache_bucket, exclude="*.git/*")


def main(
    cache_dir,
    out_dir,
    firefox_version,
    min_firefox_version,
    process_moz_central_probes,
    process_glean_metrics,
    repositories_file,
    send_emails,
    glean_repos,
    firefox_channel,
    output_bucket,
    cache_bucket,
    env,
    bugzilla_api_key: Optional[str],
    update_mode: bool = False,
    commit: Optional[str] = None,
    branch: Optional[str] = None,
    glean_repo_url: Optional[str] = None,
):
    if not (process_moz_central_probes or process_glean_metrics):
        process_moz_central_probes = process_glean_metrics = True

    # Pull cache if we are not running pytest or local dryruns or update mode
    if env == "prod" and not update_mode:
        setup_output_and_cache_dirs(
            output_bucket, cache_bucket, out_dir, cache_dir
        )

    update_paths = []
    if process_moz_central_probes:
        update_paths += load_moz_central_probes(
            cache_dir,
            out_dir,
            firefox_version,
            min_firefox_version,
            firefox_channel,
            update_mode,
        )

    if process_glean_metrics:
        update_paths += load_glean_metrics(
            cache_dir,
            out_dir,
            repositories_file,
            send_emails,
            glean_repos,
            bugzilla_api_key,
            update_mode,
            commit,
            branch,
            glean_repo_url,
            output_bucket,
        )

    # Sync results if we are not running pytest or local dryruns
    if env == "prod":
        push_output_and_cache_dirs(output_bucket, cache_bucket, out_dir, cache_dir, update_mode, update_paths)

    return update_paths


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cache-dir",
        help="Cache directory. If empty, will be filled with the probe files.",
        action="store",
        default=tempfile.mkdtemp(),
    )
    parser.add_argument(
        "--out-dir",
        help="Directory to store output files in.",
        action="store",
        default=".",
    )
    parser.add_argument(
        "--repositories-file",
        help="Repositories YAML file location.",
        action="store",
        default="repositories.yaml",
    )
    parser.add_argument(
        "--dry-run", help="Whether emails should be sent.", action="store_true"
    )
    parser.add_argument(
        "--glean-repo",
        help="The Glean Repositories to scrape (may be specified multiple times). "
        "If unspecified, scrapes all.",
        type=str,
        dest="glean_repos",
        action="append",
        required=False,
    )
    parser.add_argument(
        "--firefox-channel",
        help="The Fx channel to scrape. If unspecified, scrapes all.",
        type=str,
        required=False,
    )
    parser.add_argument(
        "--output-bucket",
        help="The output s3 cloudfront bucket where out-dir will be syncd.",
        type=str,
        default="s3://net-mozaws-prod-us-west-2-data-pitmo",
    )
    parser.add_argument(
        "--cache-bucket",
        help="The cache bucket for probe scraper.",
        type=str,
        default="s3://telemetry-airflow-cache/cache/probe-scraper",
    )
    parser.add_argument(
        "--env",
        help="We set this to 'prod' when we need to run actual s3 syncs",
        type=str,
        choices=["dev", "prod"],
        default="dev",
    )
    parser.add_argument(
        "--bugzilla-api-key",
        help="The bugzilla API key used to find and file bugs for FOG repos."
        "If not provided, no bugs will be filed.",
        type=str,
        required=False,
    )

    application = parser.add_mutually_exclusive_group()
    application.add_argument(
        "--moz-central", help="Only scrape moz-central probes", action="store_true"
    )
    application.add_argument(
        "--glean", help="Only scrape metrics in remote glean repos", action="store_true"
    )

    versions = parser.add_mutually_exclusive_group()
    versions.add_argument(
        "--firefox-version",
        help="Version of Firefox to scrape",
        action="store",
        type=int,
        required=False,
    )
    versions.add_argument(
        "--min-firefox-version",
        help="Min version of Firefox to scrape",
        action="store",
        type=int,
        required=False,
    )

    args = parser.parse_args()

    main(
        Path(args.cache_dir),
        Path(args.out_dir),
        args.firefox_version,
        args.min_firefox_version,
        args.moz_central,
        args.glean,
        args.repositories_file,
        args.dry_run,  # send_emails==dry_run
        args.glean_repos,
        args.firefox_channel,
        args.output_bucket,
        args.cache_bucket,
        args.env,
        args.bugzilla_api_key,
    )
