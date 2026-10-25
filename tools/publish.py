#!/usr/bin/env python3
"""Publish an HTML file as an artifact over the REST API (what Claude's tool does).

    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=<long-lived access token>
    tools/publish.py pesto-tilapia examples/cook-timer.html \
        --title "Pesto Tilapia" --caps '{"notify": true}'

It reads the current version first and sends it as base_version, so it never
overwrites a hand edit without you seeing the 409.

    tools/publish.py window-vent page.html --assets build/assets

uploads every file under build/assets as the artifact's asset files (paths kept,
e.g. build/assets/img/plan.webp -> img/plan.webp), skipping files whose sha256
already matches.
"""
import hashlib
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def call(method, url, token, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("artifact_id")
    parser.add_argument("html_file")
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument("--caps", help='capabilities JSON, e.g. \'{"notify": true, "entities": ["sensor.*"]}\'')
    parser.add_argument("--assets", help="folder whose files become the artifact's asset files")
    args = parser.parse_args()

    base = os.environ.get("HA_URL", "").rstrip("/")
    token = os.environ.get("HA_TOKEN", "")
    if not base or not token:
        sys.exit("Set HA_URL and HA_TOKEN")
    url = f"{base}/api/html_artifacts/{args.artifact_id}"

    body = {"html": open(args.html_file, encoding="utf-8").read()}
    for key in ("title", "description"):
        if getattr(args, key):
            body[key] = getattr(args, key)
    if args.caps:
        body["capabilities"] = json.loads(args.caps)

    status, current = call("GET", url, token)
    if status == 200:
        body["base_version"] = current["version"]
    status, result = call("PUT", url, token, body)
    if status == 409:
        sys.exit(f"Conflict: version {result['current']['version']} was edited by {result['current']['updated_by']}. Merge and retry.")
    if status != 200:
        sys.exit(f"Failed ({status}): {result}")
    print(f"Published {args.artifact_id} v{result['version']} -> {base}/html-artifacts")
    if args.assets:
        upload_assets(url, token, args.assets)


def upload_assets(url, token, folder):
    status, listing = call("GET", f"{url}/assets", token)
    have = {f["path"]: f["sha256"] for f in listing.get("files", [])} if status == 200 else {}
    sent = skipped = 0
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            data = open(full, "rb").read()
            digest = hashlib.sha256(data).hexdigest()
            if have.get(rel) == digest:
                skipped += 1
                continue
            quoted = urllib.parse.quote(rel)
            status, result = call("PUT", f"{url}/assets/{quoted}?sha256={digest}", token, raw=data)
            if status != 200:
                sys.exit(f"Asset {rel} failed ({status}): {result}")
            sent += 1
    print(f"Assets: {sent} uploaded, {skipped} unchanged")


if __name__ == "__main__":
    main()
