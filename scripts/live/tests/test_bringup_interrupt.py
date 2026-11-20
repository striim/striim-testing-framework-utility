"""Ctrl-C during a service bring-up: the containers `docker compose up` already created are still torn down
at session end, because the service joins the session's teardown set before the bring-up starts."""
from __future__ import annotations

import pytest

from livetest import services


def _container(name):
    return services.load_service(name).container


@pytest.mark.parametrize("where", ["compose_up", "post_up"])
def test_an_interrupted_bring_up_leaves_the_container_queued_for_teardown(monkeypatch, where):
    import dataclasses
    defn = dataclasses.replace(services.load_service("postgres"), post_up="/opt/postup.sh")
    monkeypatch.setattr(services, "load_service", lambda name: defn)
    started = set()

    def interrupt(d):
        raise KeyboardInterrupt

    up = interrupt if where == "compose_up" else (lambda d: None)
    post_up = interrupt if where == "post_up" else (lambda d: None)
    with pytest.raises(KeyboardInterrupt):
        services.resolve("postgres", {}, started, compose_up=up, post_up=post_up)
    assert started == {defn.container}


def test_a_failed_bring_up_is_retried_by_the_next_case():
    started = set()

    def refused(defn):
        raise services.ServiceError("docker compose up failed for postgres")

    with pytest.raises(services.ServiceError):
        services.resolve("postgres", {}, started, compose_up=refused, post_up=lambda d: None)
    assert started == set()
    calls = []
    r = services.resolve("postgres", {}, started, compose_up=calls.append, post_up=lambda d: None)
    assert r.started is True and len(calls) == 1 and started == {_container("postgres")}
