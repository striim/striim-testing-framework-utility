"""A copy of a file of any size, and a read of a FileWriter output, run under the transfer bound.

The default runner is ``dockercmd.run`` with ``QUERY_TIMEOUT_S`` (60 s), meant for a listing, a
checksum or a small read. A `docker cp` of a `server_files` seed or an OP jar, and the `docker exec
cat` that reads a FileWriter output back, move as many bytes as the case gives them: a multi-GB
seed on a slow Docker Desktop outlives 60 s, was stopped at rc 124 and failed the case as a
StriimFileError. Those commands now run under ``TRANSFER_TIMEOUT_S``; the small commands around
them keep the query bound.
"""
from __future__ import annotations

import types

import pytest

from livetest import dockercmd, opartifacts, striimfile

NODES = ("n1", "n2")


@pytest.fixture
def docker(monkeypatch):
    """`dockercmd.run` replaced by a recorder of (command, bound). `replies` maps a predicate on the
    command to the reply it gets; any other command succeeds with no output."""
    calls = []
    replies = {}

    def run(argv, *, timeout=dockercmd.QUERY_TIMEOUT_S, **kw):
        calls.append((list(argv), timeout))
        for match, reply in replies.items():
            if match(argv):
                return reply(argv)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(dockercmd, "run", run)
    monkeypatch.setattr(striimfile.stack, "app_nodes", lambda: NODES)
    monkeypatch.setattr(opartifacts.stack, "app_nodes", lambda: NODES)
    return types.SimpleNamespace(calls=calls, replies=replies)


def test_the_transfer_bound_is_named_and_longer_than_the_query_bound():
    assert dockercmd.TRANSFER_TIMEOUT_S == 1800.0
    assert dockercmd.TRANSFER_TIMEOUT_S > dockercmd.QUERY_TIMEOUT_S


def test_reading_a_filewriter_output_uses_the_transfer_bound_for_the_read_only(docker):
    docker.replies[lambda a: "find" in a[-1]] = lambda a: types.SimpleNamespace(
        returncode=0, stdout="/out/rows.json\n", stderr="")
    docker.replies[lambda a: a[4:5] == ["-c"] and "cat" in a[5]] = lambda a: types.SimpleNamespace(
        returncode=0, stdout="row\n\0", stderr="")
    assert striimfile.read_server_files(types.SimpleNamespace(mode="docker"), "/out/rows.json") == "row\nrow\n"
    listings = [t for argv, t in docker.calls if "find" in argv[-1]]
    reads = [t for argv, t in docker.calls if argv[4:5] == ["-c"] and "cat" in argv[5]]
    assert listings == [dockercmd.QUERY_TIMEOUT_S] * 2
    assert reads == [dockercmd.TRANSFER_TIMEOUT_S] * 2


def test_placing_a_server_file_uses_the_transfer_bound_for_the_copy_only(docker, tmp_path):
    seed = tmp_path / "seed.csv"
    seed.write_text("a,b\n")
    striimfile.place_server_file(types.SimpleNamespace(mode="docker"), seed, "/data/SLT_x/seed.csv")
    assert [(a[:2], t) for a, t in docker.calls] == [
        (["docker", "exec"], dockercmd.QUERY_TIMEOUT_S)] * 2 + [
        (["docker", "cp"], dockercmd.TRANSFER_TIMEOUT_S)] * 2


def test_uploading_an_op_jar_uses_the_transfer_bound_for_the_copy_only(docker, tmp_path):
    jar = tmp_path / "Op-5.4.jar"
    jar.write_bytes(b"jar")
    docker.replies[lambda a: "sha256sum" in a] = lambda a: types.SimpleNamespace(
        returncode=1, stdout="", stderr="no such file")
    opartifacts.upload_artifacts(types.SimpleNamespace(mode="docker"), [jar])
    for argv, t in docker.calls:
        want = dockercmd.TRANSFER_TIMEOUT_S if argv[:2] == ["docker", "cp"] else dockercmd.QUERY_TIMEOUT_S
        assert t == want, argv
    assert sum(argv[:2] == ["docker", "cp"] for argv, _ in docker.calls) == 2


def test_copying_a_kept_jar_from_the_node_that_holds_it_uses_the_transfer_bound(docker, tmp_path):
    jar = tmp_path / "Op-5.4-0123abcd.jar"
    jar.write_bytes(b"jar")
    docker.replies[lambda a: a[3:5] == ["test", "-e"]] = lambda a: types.SimpleNamespace(
        returncode=0 if a[2] == "n1" else 1, stdout="", stderr="")
    opartifacts.upload_artifacts(types.SimpleNamespace(mode="docker"), [jar], keep_existing=True)
    copies = [(argv, t) for argv, t in docker.calls if argv[:2] == ["docker", "cp"]]
    assert [argv[2].startswith("n1:") for argv, _ in copies] == [True, False]   # fetch, then publish
    assert {t for _, t in copies} == {dockercmd.TRANSFER_TIMEOUT_S}
    assert {t for argv, t in docker.calls if argv[:2] != ["docker", "cp"]} == {dockercmd.QUERY_TIMEOUT_S}


def test_placing_a_jar_on_the_agent_uses_the_transfer_bound_for_the_copy_only(docker, tmp_path):
    jar = tmp_path / "Op-5.4.jar"
    jar.write_bytes(b"jar")
    docker.replies[lambda a: "sha256sum" in a] = lambda a: types.SimpleNamespace(
        returncode=1, stdout="", stderr="no such file")
    opartifacts.place_on_agent(types.SimpleNamespace(mode="docker"), [jar])
    copies = [t for argv, t in docker.calls if argv[:2] == ["docker", "cp"]]
    assert copies == [dockercmd.TRANSFER_TIMEOUT_S]
    others = {tuple(argv[:2]): t for argv, t in docker.calls if argv[:2] != ["docker", "cp"]}
    assert set(others) == {("docker", "inspect"), ("docker", "exec"), ("docker", "restart")}
    assert set(others.values()) == {dockercmd.QUERY_TIMEOUT_S}


def test_an_injected_runner_is_still_called_with_the_command_alone(tmp_path):
    # Callers and tests that pass `run=` keep the one-argument contract for every command.
    seed = tmp_path / "seed.csv"
    seed.write_text("x\n")
    calls = []
    striimfile.place_server_file(types.SimpleNamespace(mode="docker"), seed, "/data/SLT_x/seed.csv",
                                 run=lambda argv: calls.append(argv) or types.SimpleNamespace(returncode=0))
    nodes = len(striimfile.stack.app_nodes())
    assert [c[:2] for c in calls] == [["docker", "exec"]] * nodes + [["docker", "cp"]] * nodes
