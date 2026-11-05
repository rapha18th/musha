"""The generations of Musha's database in its replica, the most recently written first.

deploy/entrypoint.sh runs this file with `python -I` at every instance start, before the app, so it
uses the standard library alone and answers within seconds. Every start writes a generation of its
own, <base>/gen/<UTC start stamp>-<random>, and restores the one listed first here.

    python -I generations.py gs://<bucket>/musha [<after>]    Cloud Storage, through its JSON API,
                                                              with the instance's token from the
                                                              metadata server
    python -I generations.py file:///replica [<after>]        a directory, for a laptop run and the tests

A generation's last write is the newest time among its LTX files. Litestream stamps each file with
the time of the change it carries, keeps that stamp through compaction, and stores it as the
litestream-timestamp metadata in Cloud Storage and as the file's mtime on disk. A snapshot carries
the time it was taken, so a start counts as a write. Ranked this way, a start that stopped early (a
failed deploy, an instance the scale-in removed) never hides a generation that an older instance
kept writing. Two generations written in the same instant go by name.

<after> is MUSHA_RESTORE_AFTER, the newest generation when the last recovery was set (none: there
was none). Generations that started after it form the database's current line and come first, so
writes the old line took during the recovery's rollout stay behind.

It prints one generation URL a line, and nothing when there are none. Names of another shape stay
out, and so does a generation with no LTX file yet. A listing that fails prints one line on stderr
and exits 1, so the start stops before it serves the wrong database.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, url2pathname, urlopen

# <UTC start stamp>-<random>, so the names sort by start time.
NAME = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]+")
TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
STORAGE = "https://storage.googleapis.com/storage/v1"
STAMP = "litestream-timestamp"  # Litestream's gs client sets it on every LTX file it uploads
TIMEOUT = 5  # seconds for each call
TRIES = 3  # a timeout, a 429 or a 5xx tries again after a short pause
PAGES = 50  # of 1,000 objects each


class ListingError(Exception):
    """A listing that could not finish. The message says why."""


def reason(e: HTTPError) -> str:
    """Cloud Storage's own words for an error answer, when it gives them."""
    try:
        return json.loads(e.read(4000))["error"]["message"]
    except (OSError, ValueError, KeyError, TypeError):
        return str(e.reason)


def fetch(url: str, headers: dict[str, str]) -> dict:
    """GET a JSON answer."""
    shown = url.split("?", 1)[0]
    for attempt in range(1, TRIES + 1):
        try:
            with urlopen(Request(url, headers=headers), timeout=TIMEOUT) as answer:
                return json.load(answer)
        except HTTPError as e:
            if not (e.code == 429 or e.code >= 500) or attempt == TRIES:
                raise ListingError(f"{shown} answered {e.code}: {reason(e)}") from None
        except (URLError, OSError) as e:  # a timeout is an OSError
            if attempt == TRIES:
                raise ListingError(f"{shown} is out of reach: {getattr(e, 'reason', e)}") from None
        except ValueError:
            raise ListingError(f"{shown} answered with something other than JSON") from None
        time.sleep(0.5 * attempt)
    raise AssertionError("each attempt returns or raises")


def seconds(item: dict) -> float:
    """When an object's change was made: Litestream's stamp, or the upload time without one."""
    for stamp in ((item.get("metadata") or {}).get(STAMP), item.get("updated")):
        try:
            return datetime.fromisoformat(stamp).timestamp()
        except (TypeError, ValueError):
            continue
    raise ListingError(f"{item.get('name')} carries no readable time")


def cloud_storage(bucket: str, prefix: str) -> dict[str, float]:
    """The last write of each generation under gs://<bucket>/<prefix>, by name."""
    token = fetch(TOKEN_URL, {"Metadata-Flavor": "Google"}).get("access_token")
    if not token:
        raise ListingError("the metadata server gave no access token")
    query = {"prefix": prefix, "fields": "items(name,updated,metadata),nextPageToken", "maxResults": "1000"}
    last: dict[str, float] = {}
    for _ in range(PAGES):
        page = fetch(f"{STORAGE}/b/{quote(bucket, safe='')}/o?{urlencode(query)}",
                     {"Authorization": f"Bearer {token}"})
        for item in page.get("items", []):
            name, _, rest = item["name"][len(prefix):].partition("/")
            if rest.startswith("ltx/"):
                last[name] = max(last.get(name, 0.0), seconds(item))
        if not page.get("nextPageToken"):
            return last
        query["pageToken"] = page["nextPageToken"]
    raise ListingError(f"gs://{bucket}/{prefix} holds more than {PAGES} pages of objects")


def directory(path: str) -> dict[str, float]:
    """The last write of each generation in <path>/gen, by name: the newest mtime under its ltx/."""
    root = os.path.join(path, "gen")
    try:
        entries = os.listdir(root)
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ListingError(f"{root}: {e.strerror}") from None
    last: dict[str, float] = {}
    for name in entries:
        for folder, _, files in os.walk(os.path.join(root, name, "ltx")):
            for file in files:
                try:
                    mtime = os.stat(os.path.join(folder, file)).st_mtime
                except FileNotFoundError:  # Litestream removed it mid-walk
                    continue
                last[name] = max(last.get(name, 0.0), mtime)
    return last


def generations(base: str, after: str = "none") -> list[str]:
    """The generation URLs under base: those that started after `after` first, then the rest, each
    group by last write, the most recent first."""
    base = base.rstrip("/")
    url = urlparse(base)
    if url.scheme == "gs" and url.netloc:
        prefix = url.path.strip("/")
        last = cloud_storage(url.netloc, f"{prefix}/gen/" if prefix else "gen/")
    elif url.scheme == "file" and url.netloc in ("", "localhost"):
        last = directory(url2pathname(url.path))
    else:
        raise ListingError(f"{base}: generations are listed under a gs:// or file:// base")
    floor = "" if after == "none" else after
    ranked = sorted((n for n in last if NAME.fullmatch(n)), key=lambda n: (n > floor, last[n], n), reverse=True)
    return [f"{base}/gen/{n}" for n in ranked]


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "none" and not NAME.fullmatch(argv[2])):
        print("usage: python -I generations.py <gs://bucket/prefix | file:///dir> [<generation name> | none]",
              file=sys.stderr)
        return 2
    try:
        found = generations(argv[1], argv[2] if len(argv) == 3 else "none")
    except ListingError as e:
        print(f"generations: {e}", file=sys.stderr)
        return 1
    sys.stdout.reconfigure(newline="\n")  # the shell reads these lines, on Windows too
    for url in found:
        print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
