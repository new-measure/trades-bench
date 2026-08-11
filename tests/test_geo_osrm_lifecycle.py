from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from geo_build import osrm as osrm_module  # noqa: E402
from geo_build.errors import RoutingBackendError  # noqa: E402
from geo_build.osrm import OsrmService  # noqa: E402


class _RecordingDocker:

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, *, timeout, check=True):
        self.calls.append(list(args))
        stdout = b"container-abc123\n" if args[:2] == ["run", "-d"] else b""
        return subprocess.CompletedProcess(args, returncode=0, stdout=stdout, stderr=b"")

    @property
    def teardown_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["rm", "-f"]]


@pytest.fixture
def stub_environment(monkeypatch):
    monkeypatch.setattr(osrm_module, "docker_available", lambda: True)
    monkeypatch.setattr(osrm_module, "write_pbf", lambda src, dest: Path(dest))


def _service(recorder, tmp_path=None) -> OsrmService:
    return OsrmService("unused.osm", work_dir=tmp_path, docker_runner=recorder)


def test_a_failing_readiness_wait_removes_the_container(stub_environment, monkeypatch):
    def boom(self, backend):
        raise RoutingBackendError("osrm-routed did not become ready")

    monkeypatch.setattr(OsrmService, "_wait_ready", boom)
    recorder = _RecordingDocker()
    service = _service(recorder)

    with pytest.raises(RoutingBackendError, match="did not become ready"):
        service.__enter__()

    assert recorder.teardown_calls == [["rm", "-f", "container-abc123"]]
    assert service._container_id is None


def test_a_keyboard_interrupt_between_launch_and_ready_still_tears_down(
    stub_environment, monkeypatch
):

    def interrupt(self, backend):
        raise KeyboardInterrupt

    monkeypatch.setattr(OsrmService, "_wait_ready", interrupt)
    recorder = _RecordingDocker()
    service = _service(recorder)

    with pytest.raises(KeyboardInterrupt):
        service.__enter__()

    assert recorder.teardown_calls == [["rm", "-f", "container-abc123"]]


def test_a_failing_preprocessing_step_cleans_up_the_work_directory(stub_environment):
    class _FailingExtract(_RecordingDocker):
        def __call__(self, args, *, timeout, check=True):
            self.calls.append(list(args))
            if "osrm-extract" in args:
                raise RoutingBackendError("docker run exited 1")
            return subprocess.CompletedProcess(args, returncode=0, stdout=b"", stderr=b"")

    recorder = _FailingExtract()
    service = _service(recorder)
    with pytest.raises(RoutingBackendError, match="exited 1"):
        service.__enter__()

    assert recorder.teardown_calls == []
    assert service._temp_dir is None


def test_teardown_is_idempotent(stub_environment, monkeypatch):
    monkeypatch.setattr(
        OsrmService, "_wait_ready", lambda self, backend: (_ for _ in ()).throw(RuntimeError("x"))
    )
    recorder = _RecordingDocker()
    service = _service(recorder)

    with pytest.raises(RuntimeError):
        service.__enter__()
    service.__exit__(None, None, None)
    service.__exit__(None, None, None)

    assert recorder.teardown_calls == [["rm", "-f", "container-abc123"]]


def test_a_successful_start_tears_down_on_exit(stub_environment, monkeypatch):
    monkeypatch.setattr(OsrmService, "_wait_ready", lambda self, backend: None)
    recorder = _RecordingDocker()
    service = _service(recorder)

    with service as backend:
        assert backend.base_url.startswith("http://127.0.0.1:")
        assert recorder.teardown_calls == []
    assert recorder.teardown_calls == [["rm", "-f", "container-abc123"]]


def test_the_launch_sequence_is_the_documented_mld_pipeline(stub_environment, monkeypatch):
    monkeypatch.setattr(OsrmService, "_wait_ready", lambda self, backend: None)
    recorder = _RecordingDocker()
    with _service(recorder):
        pass
    binaries = [next((a for a in call if a.startswith("osrm-")), None) for call in recorder.calls]
    assert [b for b in binaries if b] == [
        "osrm-extract",
        "osrm-partition",
        "osrm-customize",
        "osrm-routed",
    ]
    routed = next(call for call in recorder.calls if "osrm-routed" in call)
    assert "--algorithm" in routed and routed[routed.index("--algorithm") + 1] == "mld"


def test_run_docker_with_check_false_never_raises(monkeypatch):

    def explode(*args, **kwargs):
        raise OSError("docker daemon went away")

    monkeypatch.setattr(subprocess, "run", explode)
    result = osrm_module.run_docker(["rm", "-f", "gone"], timeout=1, check=False)
    assert result.returncode == 1
    with pytest.raises(RoutingBackendError):
        osrm_module.run_docker(["ps"], timeout=1)
