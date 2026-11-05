"""deploy/entrypoint.sh: every start writes a generation of its own under the replica base and
restores the one written most recently, so no two starts ever write one path and a start that
stopped early never hides the generation that kept the writes. Litestream and Python are stand-ins
that log what they were asked. The generation lister, backend/generations.py, runs for real
against a directory of generations (file://); its Cloud Storage listing runs against a stand-in
API."""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ENTRYPOINT = BACKEND.parent / "deploy" / "entrypoint.sh"
LISTER = BACKEND / "generations.py"
SH = shutil.which("sh")
NAME = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")

needs_sh = pytest.mark.skipif(SH is None, reason="needs a POSIX shell")

spec = importlib.util.spec_from_file_location("generations", LISTER)
generations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generations)

# A strict stand-in for Litestream 0.5.17. A generation is a directory, and its state file holds
# ok (the default), bad (fails its check) or flaky (fails once, then restores). Each restore adds
# "<url> <integrity mode>" to $RESTORES; replicate writes a snapshot into its generation.
LITESTREAM = r"""#!/bin/sh
echo "litestream $* replica=$LITESTREAM_REPLICA_URL" >> "$LOG"
dir_of() { p="${1#file://}"; case "$p" in /?:/*) p="${p#/}" ;; esac; echo "$p"; }
case "$1" in
  replicate)
    gen=$(dir_of "$LITESTREAM_REPLICA_URL")
    mkdir -p "$gen/ltx/9" && echo snapshot > "$gen/ltx/9/0000000000000001-0000000000000001.ltx"
    exit 0 ;;
  restore) ;;
  *) exit 0 ;;
esac
out=""; config=0; if_exists=0; mode=none; prev=""; url=""
for a in "$@"; do
  case "$prev" in -o) out="$a" ;; -integrity-check) mode="$a" ;; esac
  case "$a" in -config) config=1 ;; -if-replica-exists) if_exists=1 ;; esac
  prev="$a"; url="$a"
done
if [ -n "$out" ] && [ "$config" = 1 ]; then
  echo "cannot specify a replica URL and the -config flag" >&2; exit 2
fi
echo "$url $mode" >> "$RESTORES"
gen=$(dir_of "$url")
if [ -z "$(ls -A "$gen" 2>/dev/null)" ]; then
  [ "$if_exists" = 1 ] && exit 0
  echo "no matching backup files available" >&2; exit 1
fi
case "$(cat "$gen/state" 2>/dev/null || echo ok)" in
  ok) echo db > "$out" ;;
  bad) echo "post-restore integrity check: failed" >&2; exit 1 ;;
  flaky) if [ -e "$gen/tried" ]; then echo db > "$out"; else touch "$gen/tried"; exit 1; fi ;;
esac
"""

# The stand-in Python logs its calls; the generation lister runs on the real one.
PYTHON = r"""#!/bin/sh
echo "python $*" >> "$LOG"
if [ "$1" = -I ]; then exec "$REAL_PYTHON" "$@"; fi
"""


def stamp(name: str) -> float:
    """A generation name's start time, in seconds since the epoch."""
    return datetime.strptime(name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()


class Starts:
    """Instance starts one after another, each on an empty disk, sharing one replica directory."""

    def __init__(self, tmp_path: Path):
        self.tools = tmp_path / "tools"
        self.tools.mkdir()
        for name, body in (("litestream", LITESTREAM), ("python", PYTHON)):
            self.write_tool(self.tools / name, body)
        self.shims = tmp_path / "shims"  # put in front of PATH: stand-ins for date or timeout
        self.shims.mkdir()
        self.app = tmp_path / "app"
        self.app.mkdir()
        shutil.copy(LISTER, self.app / "generations.py")
        self.replica = tmp_path / "replica"
        self.replica.mkdir()
        self.base = self.replica.as_uri()
        self.db = tmp_path / "data" / "musha.db"
        self.log = tmp_path / "log.txt"
        self.restores_file = tmp_path / "restores.txt"

    @staticmethod
    def write_tool(path: Path, body: str) -> None:
        path.write_text(body, newline="\n")
        path.chmod(0o755)

    def shim(self, name: str, body: str) -> None:
        self.write_tool(self.shims / name, body)

    def generation(self, name: str, state: str = "ok", written: float | None = None) -> str:
        """A generation an earlier start left in the replica, last written at its start unless told."""
        path = self.replica / "gen" / name
        (path / "ltx" / "9").mkdir(parents=True)
        snapshot = path / "ltx" / "9" / "0000000000000001-0000000000000001.ltx"
        snapshot.write_text("snapshot")
        when = stamp(name) if written is None else written
        os.utime(snapshot, (when, when))
        (path / "state").write_text(state, newline="\n")
        return f"{self.base}/gen/{name}"

    def start(self, *, keep_disk: bool = False, **env_extra: str):
        if not keep_disk:
            shutil.rmtree(self.db.parent, ignore_errors=True)
        for f in (self.log, self.restores_file):
            f.unlink(missing_ok=True)
        env = {k: v for k, v in os.environ.items()
               if k not in ("DATABASE_URL", "K_REVISION", "MUSHA_RESTORE_FROM", "MUSHA_RESTORE_AFTER",
                            "LITESTREAM_REPLICA_URL")}
        env.update({
            "LOG": self.log.as_posix(), "RESTORES": self.restores_file.as_posix(),
            "REAL_PYTHON": Path(sys.executable).as_posix(),
            "LITESTREAM_BIN": (self.tools / "litestream").as_posix(),
            "MUSHA_PYTHON": (self.tools / "python").as_posix(),
            "MUSHA_APP_DIR": self.app.as_posix(), "MUSHA_DB_PATH": self.db.as_posix(),
            "LITESTREAM_REPLICA_URL": self.base, "K_REVISION": "musha-00009-new",
            "PATH": str(self.shims) + os.pathsep + os.environ.get("PATH", ""),
        })
        env.update(env_extra)
        env = {k: v for k, v in env.items() if v is not None}
        done = subprocess.run([SH, ENTRYPOINT.as_posix()], env=env, capture_output=True, text=True, timeout=60)
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        restores = ([tuple(line.rsplit(" ", 1)) for line in self.restores_file.read_text().splitlines()]
                    if self.restores_file.exists() else [])
        return done, lines, restores

    def replicated(self, lines: list[str]) -> str:
        """The generation this start writes, checked for its shape."""
        assert lines and lines[-1].startswith("litestream replicate -config "), lines
        url = lines[-1].rsplit("replica=", 1)[1]
        assert url.startswith(f"{self.base}/gen/") and NAME.fullmatch(url.rsplit("/", 1)[1]), url
        return url


def seeded(lines: list[str]) -> bool:
    return "python -m musha.seed --if-empty" in lines


def served(lines: list[str]) -> bool:
    return any(line.startswith("litestream replicate") for line in lines)


@pytest.fixture
def starts(tmp_path):
    return Starts(tmp_path)


@needs_sh
def test_a_start_restores_the_most_recent_generation_and_writes_one_of_its_own(starts):
    old = starts.generation("20261104T200000Z-aaaaaaaa")
    new = starts.generation("20261104T210000Z-bbbbbbbb")
    done, lines, restores = starts.start()
    assert done.returncode == 0, done.stderr
    assert restores == [(new, "full")]
    restore = next(line for line in lines if line.startswith("litestream restore"))
    assert restore.split()[2:4] == ["-o", starts.db.as_posix()] and "-config" not in restore.split()
    assert starts.replicated(lines) not in (old, new)
    assert not seeded(lines)  # the shops came back
    assert f"restoring the most recently written of 2 generations, {new}" in done.stderr


@needs_sh
def test_a_later_start_that_stopped_early_never_hides_the_generation_that_kept_the_writes(starts):
    # A failed deploy at 21:00 copied the database and stopped; the old revision, started at 20:00,
    # kept writing until 21:30. Its generation holds the later writes and is the one to restore.
    kept = starts.generation("20261104T200000Z-aaaaaaaa", written=stamp("20261104T213000Z-00000000"))
    starts.generation("20261104T210000Z-bbbbbbbb")
    done, lines, restores = starts.start()
    assert done.returncode == 0, done.stderr
    assert restores == [(kept, "full")]
    assert f"restoring the most recently written of 2 generations, {kept}" in done.stderr


@needs_sh
def test_a_restore_runs_under_a_time_limit(starts):
    starts.shim("timeout", '#!/bin/sh\necho "timeout $1" >> "$LOG"\nshift\nexec "$@"\n')
    new = starts.generation("20261104T210000Z-bbbbbbbb")
    done, lines, restores = starts.start()
    assert done.returncode == 0, done.stderr
    assert restores == [(new, "full")]
    at = lines.index("timeout 60")
    assert lines[at + 1].startswith("litestream restore -o ")


@needs_sh
def test_a_generation_caught_mid_write_restores_on_a_later_try(starts):
    starts.generation("20261104T200000Z-aaaaaaaa")
    new = starts.generation("20261104T210000Z-bbbbbbbb", state="flaky")
    done, lines, restores = starts.start()
    assert done.returncode == 0, done.stderr
    assert restores == [(new, "full")] * 2
    starts.replicated(lines)
    assert not seeded(lines)


@needs_sh
def test_a_most_recent_generation_that_never_restores_stops_the_start(starts):
    starts.generation("20261104T200000Z-aaaaaaaa")
    new = starts.generation("20261104T210000Z-bbbbbbbb", state="bad")
    done, lines, restores = starts.start()
    assert done.returncode == 1
    assert restores == [(new, "full")] * 3  # never the older generation
    assert not served(lines) and not seeded(lines)
    assert f"musha: {new} failed to restore three times" in done.stderr
    assert not starts.db.exists()


@needs_sh
def test_an_empty_listing_stops_the_start_instead_of_seeding(starts):
    # the paths from before generations sit beside gen/ and stay out of it
    for legacy in ("ltx/0", "rev/musha-00008-7z6/ltx/9"):
        (starts.replica / legacy).mkdir(parents=True)
        (starts.replica / legacy / "0000000000000001-0000000000000001.ltx").write_text("old")
    done, lines, restores = starts.start()
    assert done.returncode == 1
    assert restores == [] and not seeded(lines) and not served(lines)
    assert "no generations under" in done.stderr and "MUSHA_RESTORE_FROM=fresh" in done.stderr


@needs_sh
def test_fresh_seeds_the_first_start_alone(starts):
    # deploy.ps1's first deploy, or a laptop's first run: MUSHA_RESTORE_AFTER unset reads as none
    done, lines, restores = starts.start(MUSHA_RESTORE_FROM="fresh")
    assert done.returncode == 0, done.stderr
    assert restores == [] and seeded(lines)
    first = starts.replicated(lines)
    assert "MUSHA_RESTORE_FROM=fresh: seeding a fresh database on purpose" in done.stderr
    # the next start carries the same setting, and the first start's generation has spent it
    done, lines, restores = starts.start(MUSHA_RESTORE_FROM="fresh")
    assert done.returncode == 0, done.stderr
    assert restores == [(first, "full")] and not seeded(lines)
    assert f"MUSHA_RESTORE_FROM is spent: generation {first.rsplit('/', 1)[1]} started after it was set" in done.stderr


@needs_sh
def test_a_recovery_restores_its_source_once_and_its_line_then_ranks_first(starts):
    old = starts.generation("20261104T200000Z-aaaaaaaa")
    new = starts.generation("20261104T210000Z-bbbbbbbb")
    recovery = {"MUSHA_RESTORE_FROM": old, "MUSHA_RESTORE_AFTER": "20261104T210000Z-bbbbbbbb"}
    done, lines, restores = starts.start(**recovery)
    assert done.returncode == 0, done.stderr
    assert restores == [(old, "full")]
    assert any("generations.py" in line for line in lines)  # the request is checked against a listing
    assert f"MUSHA_RESTORE_FROM names {old}; restoring it" in done.stderr
    line = starts.replicated(lines)
    # The old line took a write after the recovery's start, in the rollout's last seconds. A later
    # start of the recovery revision, or of one after it, still restores the recovery's line.
    os.utime(next((starts.replica / "gen" / new.rsplit("/", 1)[1]).rglob("*.ltx")), (4102444800, 4102444800))
    for env in (recovery, {"MUSHA_RESTORE_AFTER": "20261104T210000Z-bbbbbbbb"}):
        done, lines, restores = starts.start(**env)
        assert done.returncode == 0, done.stderr
        assert restores == [(line, "full")] and not seeded(lines)
        line = starts.replicated(lines)


@needs_sh
def test_a_recovery_that_a_newer_start_beat_restores_the_most_recent_instead(starts):
    old = starts.generation("20261104T200000Z-aaaaaaaa")
    newer = starts.generation("20261104T220000Z-cccccccc")  # the old revision woke during the deploy
    done, lines, restores = starts.start(MUSHA_RESTORE_FROM=old, MUSHA_RESTORE_AFTER="20261104T210000Z-bbbbbbbb")
    assert done.returncode == 0, done.stderr
    assert restores == [(newer, "full")]
    assert "MUSHA_RESTORE_FROM is spent: generation 20261104T220000Z-cccccccc started after it was set" in done.stderr


@needs_sh
def test_a_recovery_from_a_generation_that_holds_nothing_stops_the_start(starts):
    starts.generation("20261104T210000Z-bbbbbbbb")
    empty = f"{starts.base}/gen/20261104T190000Z-cccccccc"
    done, lines, restores = starts.start(MUSHA_RESTORE_FROM=empty, MUSHA_RESTORE_AFTER="20261104T210000Z-bbbbbbbb")
    assert done.returncode == 1
    assert restores == [(empty, "full")] * 3
    assert not seeded(lines) and not served(lines)


@needs_sh
def test_fresh_over_generations_seeds_on_purpose(starts):
    starts.generation("20261104T210000Z-bbbbbbbb")
    done, lines, restores = starts.start(MUSHA_RESTORE_FROM="fresh", MUSHA_RESTORE_AFTER="20261104T210000Z-bbbbbbbb")
    assert done.returncode == 0, done.stderr
    assert restores == [] and seeded(lines)
    starts.replicated(lines)


@needs_sh
def test_a_malformed_restore_after_stops_the_start(starts):
    starts.generation("20261104T210000Z-bbbbbbbb")
    done, lines, restores = starts.start(MUSHA_RESTORE_FROM="fresh", MUSHA_RESTORE_AFTER="latest")
    assert done.returncode == 1
    assert "MUSHA_RESTORE_AFTER reads latest" in done.stderr
    assert restores == [] and not seeded(lines) and not served(lines)


@needs_sh
def test_each_start_replicates_to_a_new_generation(starts):
    done, first, _ = starts.start(MUSHA_RESTORE_FROM="fresh")
    assert done.returncode == 0, done.stderr
    a = starts.replicated(first)
    done, second, restores = starts.start(K_REVISION="musha-00010-two")
    assert done.returncode == 0, done.stderr
    b = starts.replicated(second)
    assert b != a and restores == [(a, "full")]  # the second start reads the first's and writes its own
    assert sorted(p.name for p in (starts.replica / "gen").iterdir()) == sorted(u.rsplit("/", 1)[1] for u in (a, b))


@needs_sh
def test_two_starts_of_one_revision_in_one_second_write_apart(starts):
    starts.shim("date", "#!/bin/sh\necho 20261105T061917Z\n")
    starts.generation("20261104T210000Z-bbbbbbbb")
    done, first, _ = starts.start()
    assert done.returncode == 0, done.stderr
    done, second, _ = starts.start()
    assert done.returncode == 0, done.stderr
    a, b = starts.replicated(first), starts.replicated(second)
    assert a.rsplit("/", 1)[1].startswith("20261105T061917Z-") and b.rsplit("/", 1)[1].startswith("20261105T061917Z-")
    assert a != b


@needs_sh
def test_a_listing_that_fails_stops_the_start(starts):
    done, lines, restores = starts.start(LITESTREAM_REPLICA_URL="s3://bucket/musha")
    assert done.returncode == 1
    assert "could not be listed" in done.stderr and "generations: s3://bucket/musha" in done.stderr
    assert restores == [] and not seeded(lines) and not served(lines)


@needs_sh
def test_a_laptop_run_writes_generations_and_keeps_a_file_on_disk(starts):
    # docker run with a file replica: no K_REVISION, the same generations
    done, lines, _ = starts.start(K_REVISION=None, MUSHA_RESTORE_FROM="fresh")
    assert done.returncode == 0, done.stderr
    first = starts.replicated(lines)
    # a file kept on disk serves as it is; Litestream's local state from the last run goes
    starts.db.parent.mkdir(parents=True, exist_ok=True)
    starts.db.write_text("db")
    meta = starts.db.parent / ".musha.db-litestream" / "ltx" / "0"
    meta.mkdir(parents=True)
    (meta / "0000000000000007-0000000000000007.ltx").write_text("old")
    done, lines, restores = starts.start(keep_disk=True, K_REVISION=None)
    assert done.returncode == 0, done.stderr
    assert restores == [] and not (starts.db.parent / ".musha.db-litestream").exists()
    assert starts.replicated(lines) != first and starts.db.read_text() == "db"


# The lister

def make(root: Path, **written: float) -> None:
    """Generations by name, each with one LTX file last written at the given time."""
    for name, when in written.items():
        ltx = root / "gen" / name / "ltx" / "0" / "0000000000000001-0000000000000001.ltx"
        ltx.parent.mkdir(parents=True)
        ltx.write_text("change")
        os.utime(ltx, (when, when))


def test_the_lister_ranks_generations_by_last_write_then_name(tmp_path):
    make(tmp_path, **{"20261104T205132Z-0f0f0f0f": 300, "20261105T000001Z-00000000": 100,
                      "20261104T205132Z-ffffffff": 300, "20251231T235959Z-12345678": 200,
                      "notes": 900, "20261104T2051Z-short": 900})
    (tmp_path / "gen" / "20261106T000000Z-eeeeeeee" / "ltx").mkdir(parents=True)  # a start that wrote nothing yet
    (tmp_path / "gen" / "20261106T000001Z-dddddddd").mkdir()
    (tmp_path / "gen" / "20261106T000001Z-dddddddd" / "state").write_text("not an LTX file")
    (tmp_path / "ltx" / "0").mkdir(parents=True)  # the layout from before generations
    base = tmp_path.as_uri()
    assert generations.generations(base + "/") == [f"{base}/gen/{n}" for n in (
        "20261104T205132Z-ffffffff", "20261104T205132Z-0f0f0f0f", "20251231T235959Z-12345678",
        "20261105T000001Z-00000000")]


def test_the_lister_puts_the_line_after_a_recovery_first(tmp_path):
    make(tmp_path, **{"20261104T200000Z-aaaaaaaa": 500, "20261104T210000Z-bbbbbbbb": 400,
                      "20261104T220000Z-cccccccc": 100, "20261104T230000Z-dddddddd": 200})
    base = tmp_path.as_uri()
    assert generations.generations(base, "20261104T210000Z-bbbbbbbb") == [f"{base}/gen/{n}" for n in (
        "20261104T230000Z-dddddddd", "20261104T220000Z-cccccccc",
        "20261104T200000Z-aaaaaaaa", "20261104T210000Z-bbbbbbbb")]
    assert generations.generations(base, "none")[0] == f"{base}/gen/20261104T200000Z-aaaaaaaa"


def test_the_lister_reads_a_directory_with_no_generations_as_none(tmp_path):
    assert generations.generations((tmp_path / "missing").as_uri()) == []
    assert generations.generations(tmp_path.as_uri()) == []


def test_the_lister_runs_isolated_and_prints_one_url_a_line(tmp_path):
    make(tmp_path, **{"20261104T200000Z-aaaaaaaa": 100, "20261104T210000Z-bbbbbbbb": 200})
    done = subprocess.run([sys.executable, "-I", str(LISTER), tmp_path.as_uri()], capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout == (f"{tmp_path.as_uri()}/gen/20261104T210000Z-bbbbbbbb\n"
                           f"{tmp_path.as_uri()}/gen/20261104T200000Z-aaaaaaaa\n").encode()
    after = subprocess.run([sys.executable, "-I", str(LISTER), tmp_path.as_uri(), "20261104T190000Z-00000000"],
                           capture_output=True, timeout=30)
    assert after.returncode == 0 and after.stdout == done.stdout
    bad = subprocess.run([sys.executable, "-I", str(LISTER), "s3://bucket/musha"], capture_output=True, text=True,
                         timeout=30)
    assert bad.returncode == 1 and bad.stdout == ""
    assert bad.stderr.startswith("generations: s3://bucket/musha: generations are listed under a gs:// or file://")
    usage = subprocess.run([sys.executable, "-I", str(LISTER), tmp_path.as_uri(), "latest"], capture_output=True,
                           text=True, timeout=30)
    assert usage.returncode == 2 and usage.stdout == "" and usage.stderr.startswith("usage:")


def test_the_lister_reads_cloud_storage_page_by_page(monkeypatch):
    calls = []

    def item(name, stamp=None, updated="2026-11-04T23:59:59.000Z"):
        return {"name": f"musha/gen/{name}", "updated": updated,
                **({"metadata": {"litestream-timestamp": stamp}} if stamp else {})}

    pages = [{"items": [item("20261104T200000Z-aaaaaaaa/ltx/0/0000000000000001-0000000000000001.ltx",
                             "2026-11-04T20:00:01.5Z"),
                        item("20261104T200000Z-aaaaaaaa/ltx/1/0000000000000002-0000000000000002.ltx",
                             "2026-11-04T21:30:00.25Z", updated="2026-11-05T03:17:19.000Z"),
                        item("notes/ltx/0/0000000000000001-0000000000000001.ltx", "2026-11-05T00:00:00Z"),
                        item("20261104T230000Z-dddddddd/state", "2026-11-05T00:00:00Z")],
              "nextPageToken": "p2"},
             {"items": [item("20261104T210000Z-bbbbbbbb/ltx/9/0000000000000001-0000000000000001.ltx",
                             "2026-11-04T21:00:02.1Z"),
                        item("20261104T190000Z-cccccccc/ltx/9/0000000000000001-0000000000000001.ltx",
                             updated="2026-11-04T19:00:03.000Z")]}]

    def fetch(url, headers):
        calls.append((url, headers))
        if url == generations.TOKEN_URL:
            assert headers == {"Metadata-Flavor": "Google"}
            return {"access_token": "tok", "expires_in": 3599}
        assert headers == {"Authorization": "Bearer tok"}
        return pages[len(calls) - 2]

    monkeypatch.setattr(generations, "fetch", fetch)
    # aaaaaaaa started first and wrote last; a compaction's upload time leaves its ranking alone
    assert generations.generations("gs://helenia-musha/musha") == [
        "gs://helenia-musha/musha/gen/20261104T200000Z-aaaaaaaa",
        "gs://helenia-musha/musha/gen/20261104T210000Z-bbbbbbbb",
        "gs://helenia-musha/musha/gen/20261104T190000Z-cccccccc"]
    first, second = (urlparse(url) for url, _ in calls[1:])
    assert first.path == "/storage/v1/b/helenia-musha/o"
    assert parse_qs(first.query) == {"prefix": ["musha/gen/"], "fields": ["items(name,updated,metadata),nextPageToken"],
                                     "maxResults": ["1000"]}
    assert parse_qs(second.query)["pageToken"] == ["p2"]


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_lister_tries_a_busy_api_again_and_names_a_refusal(monkeypatch):
    monkeypatch.setattr(generations.time, "sleep", lambda s: None)
    answers = [HTTPError("u", 503, "Service Unavailable", {}, io.BytesIO(b"")), URLError("timed out"),
               Answer(json.dumps({"items": []}).encode())]

    def urlopen(request, timeout):
        assert timeout == generations.TIMEOUT
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(generations, "urlopen", urlopen)
    assert generations.fetch("https://storage.googleapis.com/storage/v1/b/b/o?prefix=x", {}) == {"items": []}

    denied = json.dumps({"error": {"code": 403, "message": "musha-run does not have storage.objects.list access"}})

    def refuse(request, timeout):
        raise HTTPError("u", 403, "Forbidden", {}, io.BytesIO(denied.encode()))

    monkeypatch.setattr(generations, "urlopen", refuse)
    with pytest.raises(generations.ListingError) as refused:
        generations.fetch("https://storage.googleapis.com/storage/v1/b/b/o?prefix=x", {})
    assert str(refused.value) == ("https://storage.googleapis.com/storage/v1/b/b/o answered 403: "
                                  "musha-run does not have storage.objects.list access")


def test_an_object_with_no_readable_time_stops_the_listing():
    with pytest.raises(generations.ListingError, match="carries no readable time"):
        generations.seconds({"name": "musha/gen/x/ltx/0/a.ltx", "metadata": {"litestream-timestamp": "soon"}})
