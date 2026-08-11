from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from .errors import RoutingBackendError
from .routing import BackendInfo, Coordinate, SnapResult

OSRM_IMAGE_REPOSITORY = "ghcr.io/project-osrm/osrm-backend"
OSRM_IMAGE_TAG = "v26.8.0-debian"
OSRM_IMAGE_DIGEST = "sha256:3ac496ff8fd7e1af53846179d73d06a97f719c8ad2217d008ed868942398665c"
OSRM_VERSION = "v26.8.0"

OSRM_IMAGE = f"{OSRM_IMAGE_REPOSITORY}@{OSRM_IMAGE_DIGEST}"

OSRM_CAR_PROFILE = "/opt/car.lua"

OSRM_MAX_TABLE_SIZE = 8000

_CONTAINER_PORT = 5000


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def write_pbf(osm_path: str | Path, destination: Path) -> Path:
    import osmium

    source = Path(osm_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix == ".pbf" or source.name.endswith(".osm.pbf"):
        shutil.copyfile(source, destination)
        return destination

    if destination.exists():
        destination.unlink()
    writer = osmium.SimpleWriter(str(destination))

    class _Copy(osmium.SimpleHandler):
        def node(self, n):  # noqa: D102 - pyosmium callback
            writer.add_node(n)

        def way(self, w):  # noqa: D102 - pyosmium callback
            writer.add_way(w)

        def relation(self, r):  # noqa: D102 - pyosmium callback
            writer.add_relation(r)

    try:
        _Copy().apply_file(str(source))
    finally:
        writer.close()
    return destination


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run_docker(
    args: list[str], *, timeout: int, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        if not check:
            return subprocess.CompletedProcess(args, returncode=1, stdout=b"", stderr=b"")
        raise RoutingBackendError(f"docker {' '.join(args[:2])} failed to run: {exc}") from exc
    if proc.returncode != 0 and check:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-8:]
        raise RoutingBackendError(
            f"docker {' '.join(args[:2])} exited {proc.returncode}:\n" + "\n".join(tail)
        )
    return proc


class OsrmBackend:

    def __init__(
        self,
        base_url: str,
        *,
        max_table_coordinates: int = OSRM_MAX_TABLE_SIZE,
        timeout_s: float = 300.0,
        image_digest: str = OSRM_IMAGE_DIGEST,
        version: str = OSRM_VERSION,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._max_table_coordinates = max_table_coordinates
        self._timeout_s = timeout_s
        self._image_digest = image_digest
        self._version = version


    @property
    def info(self) -> BackendInfo:
        return BackendInfo(
            name="osrm",
            version=self._version,
            image_digest=self._image_digest,
            packageable=True,
        )

    @property
    def max_table_coordinates(self) -> int:
        return self._max_table_coordinates


    def snap(self, lon: float, lat: float) -> SnapResult | None:
        payload = self._get(f"/nearest/v1/driving/{_coord(lon, lat)}", {"number": "1"})
        waypoints = payload.get("waypoints") or []
        if payload.get("code") != "Ok" or not waypoints:
            return None
        first = waypoints[0]
        location = first.get("location")
        distance = first.get("distance")
        if not location or distance is None:
            return None
        return SnapResult(
            lon=float(location[0]), lat=float(location[1]), distance_meters=float(distance)
        )

    def table(
        self, sources: Sequence[Coordinate], destinations: Sequence[Coordinate]
    ) -> list[list[float | None]]:
        if not sources or not destinations:
            return [[] for _ in sources]
        coords = list(sources) + list(destinations)
        if len(coords) > self._max_table_coordinates:
            raise RoutingBackendError(
                f"/table asked for {len(coords)} coordinates, over the daemon's "
                f"--max-table-size {self._max_table_coordinates}; chunk through "
                "routing.table_chunked"
            )
        path = "/table/v1/driving/" + ";".join(_coord(*c) for c in coords)
        params = {
            "annotations": "distance",
            "sources": ";".join(str(i) for i in range(len(sources))),
            "destinations": ";".join(
                str(i) for i in range(len(sources), len(sources) + len(destinations))
            ),
        }
        payload = self._get(path, params)
        if payload.get("code") != "Ok":
            raise RoutingBackendError(f"/table returned {payload.get('code')!r}")
        distances = payload.get("distances") or []
        return [[None if v is None else float(v) / 1000.0 for v in row] for row in distances]

    def route(self, a: Coordinate, b: Coordinate) -> float | None:
        path = f"/route/v1/driving/{_coord(*a)};{_coord(*b)}"
        payload = self._get(path, {"overview": "false", "alternatives": "false"})
        if payload.get("code") != "Ok":
            return None
        routes = payload.get("routes") or []
        if not routes:
            return None
        return float(routes[0]["distance"]) / 1000.0


    def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=self._timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except ValueError:
                raise RoutingBackendError(f"OSRM {path} -> HTTP {exc.code}: {body[:200]}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RoutingBackendError(f"OSRM {path} unreachable: {exc}") from exc


def _coord(lon: float, lat: float) -> str:
    return f"{lon:.6f},{lat:.6f}"


class OsrmService:

    def __init__(
        self,
        osm_path: str | Path,
        *,
        work_dir: Path | None = None,
        image: str = OSRM_IMAGE,
        profile: str = OSRM_CAR_PROFILE,
        max_table_size: int = OSRM_MAX_TABLE_SIZE,
        ready_timeout_s: float = 180.0,
        step_timeout_s: int = 1800,
        docker_runner=run_docker,
    ) -> None:
        self.osm_path = Path(osm_path)
        self.image = image
        self.profile = profile
        self.max_table_size = max_table_size
        self.ready_timeout_s = ready_timeout_s
        self.step_timeout_s = step_timeout_s
        self._explicit_work_dir = work_dir
        self._docker = docker_runner
        self._temp_dir: tempfile.TemporaryDirectory | None = None
        self._container_id: str | None = None

    def __enter__(self) -> OsrmBackend:
        if not docker_available():
            raise RoutingBackendError(
                "Docker is not available; the OSRM backend cannot start. "
                "Run the build with --fake-backend for a non-packageable "
                "development artifact, or start Docker."
            )
        if self._explicit_work_dir is not None:
            work = self._explicit_work_dir
            work.mkdir(parents=True, exist_ok=True)
        else:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="geo-build-osrm-")
            work = Path(self._temp_dir.name)
        self._work = work

        try:
            return self._start(work)
        except BaseException:
            self._teardown()
            raise

    def _start(self, work: Path) -> OsrmBackend:
        pbf = write_pbf(self.osm_path, work / "data.osm.pbf")
        mount = f"{work}:/data"
        self._docker(
            ["run", "--rm", "-v", mount, self.image,
             "osrm-extract", "-p", self.profile, f"/data/{pbf.name}"],
            timeout=self.step_timeout_s,
        )
        self._docker(
            ["run", "--rm", "-v", mount, self.image, "osrm-partition", "/data/data.osrm"],
            timeout=self.step_timeout_s,
        )
        self._docker(
            ["run", "--rm", "-v", mount, self.image, "osrm-customize", "/data/data.osrm"],
            timeout=self.step_timeout_s,
        )

        port = _free_port()
        proc = self._docker(
            ["run", "-d", "-p", f"127.0.0.1:{port}:{_CONTAINER_PORT}", "-v", mount, self.image,
             "osrm-routed", "--algorithm", "mld",
             "--max-table-size", str(self.max_table_size), "/data/data.osrm"],
            timeout=120,
        )
        self._container_id = proc.stdout.decode().strip()
        backend = OsrmBackend(
            f"http://127.0.0.1:{port}", max_table_coordinates=self.max_table_size
        )
        self._wait_ready(backend)
        return backend

    def __exit__(self, exc_type, exc, tb) -> None:
        self._teardown()

    def _teardown(self) -> None:
        if self._container_id:
            container_id, self._container_id = self._container_id, None
            self._docker(["rm", "-f", container_id], timeout=120, check=False)
        if self._temp_dir is not None:
            temp_dir, self._temp_dir = self._temp_dir, None
            temp_dir.cleanup()

    def _wait_ready(self, backend: OsrmBackend) -> None:
        deadline = time.monotonic() + self.ready_timeout_s
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                backend._get("/nearest/v1/driving/0,0", {"number": "1"})
                return
            except RoutingBackendError as exc:
                last = exc
                time.sleep(0.25)
        logs = ""
        if self._container_id:
            proc = self._docker(
                ["logs", "--tail", "20", self._container_id], timeout=60, check=False
            )
            logs = proc.stderr.decode("utf-8", "replace") + proc.stdout.decode("utf-8", "replace")
        raise RoutingBackendError(
            f"osrm-routed did not become ready within {self.ready_timeout_s}s "
            f"(last error: {last}). Container log tail:\n{logs}"
        )


__all__ = [
    "OSRM_CAR_PROFILE",
    "OSRM_IMAGE",
    "OSRM_IMAGE_DIGEST",
    "OSRM_IMAGE_REPOSITORY",
    "OSRM_IMAGE_TAG",
    "OSRM_MAX_TABLE_SIZE",
    "OSRM_VERSION",
    "OsrmBackend",
    "OsrmService",
    "docker_available",
    "run_docker",
    "write_pbf",
]
