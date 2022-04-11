"""Google Cloud Function for scraping glean probes from a single commit."""
import argparse
import json
import os
import tempfile
from unittest.mock import Mock

from flask import Request, Response

from . import runner


def main(request: Request) -> Response:
    """Scrape probes from a single glean commit."""
    output_bucket = os.environ.get(
        "OUTPUT_BUCKET",
        # "s3://net-mozaws-prod-us-west-2-data-pitmo",
        "gs://relud1-probe-scraper/gcf_output",
    )

    args = request.get_json(force=True)
    if not isinstance(args, dict):
        return Response(f"request body must be a JSON object but got: {args}", 400)
    try:
        url = args["url"]
        commit = args["commit"]
        branch = args["branch"]
    except KeyError as e:
        return Response(f"request JSON missing key: {e}\n", 400)

    if not isinstance(url, str):
        return Response("Error: url must be a string\n", 400)
    if not isinstance(commit, str):
        return Response("Error: commit must be a string\n", 400)
    if not isinstance(branch, str):
        return Response("Error: branch must be a string\n", 400)

    with tempfile.TemporaryDirectory() as tmpdirname:
        out_dir = os.path.join(tmpdirname, "output")
        cache_dir = os.path.join(tmpdirname, "cache")
        os.mkdir(out_dir)
        os.mkdir(cache_dir)
        updated_paths = runner.main(
            cache_dir=cache_dir,
            out_dir=out_dir,
            firefox_version=None,
            min_firefox_version=None,
            process_moz_central_probes=False,
            process_glean_metrics=True,
            repositories_file="repositories.yaml",
            send_emails=False,
            glean_repos=None,
            firefox_channel=None,
            output_bucket=output_bucket,
            cache_bucket=None,
            env="prod",
            bugzilla_api_key=None,
            update_mode=True,
            commit=commit,
            branch=branch,
            glean_repo_url=url,
        )
        if updated_paths:
            updates = [p.relative_to(out_dir) for p in updated_paths]
            return Response(
                f"update published for {', '.join(updates)}\n", 200
            )
        return Response("update is valid, but not published\n", 200)


if __name__ == "__main__":
    _parser = argparse.ArgumentParser()
    _parser.add_argument(
        "data",
        help="JSON format data describing the glean commit or branch to push",
        type=str,
    )
    _args = _parser.parse_args()
    _data = json.loads(_args.data)
    _request = Mock(get_json=Mock(return_value=_data), args=_data)
    _response = main(_request)
    print(f"HTTP {_response.status_code}: {_response.data.decode()}")
