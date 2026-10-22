"""server_info reports the local_mcpconnector plugin version and compatibility."""

import asyncio

import httpx
import pytest

from fakes import site_info
from moodle_mcp_connector import server

GET_INFO = "local_mcpconnector_get_info"
MIN = server.MIN_PLUGIN_VERSION


def run_server_info() -> dict:
    return asyncio.run(server.server_info())


def test_minimum_plugin_version():
    # The first plugin release that provides every function this client calls.
    assert server.MIN_PLUGIN_VERSION == 2026102123


def test_phase1_plugin_is_now_incompatible(fake_call):
    fake_call({
        "core_webservice_get_site_info": site_info(GET_INFO),
        GET_INFO: {"component": "local_mcpconnector", "version": 2026102110, "release": "0.1.0"},
    })
    info = run_server_info()
    assert info["plugin_compatible"] is False
    assert info["plugin_min_version"] == 2026102123


def test_compatible_plugin(fake_call):
    fake = fake_call({
        "core_webservice_get_site_info": site_info("local_mcpconnector_create_page", GET_INFO),
        GET_INFO: {"component": "local_mcpconnector", "version": MIN, "release": "0.2.0"},
    })
    info = run_server_info()
    assert info["connected"] is True
    assert info["plugin_installed"] is True
    assert info["plugin_version"] == MIN
    assert info["plugin_release"] == "0.2.0"
    assert info["plugin_compatible"] is True
    assert "plugin_error" not in info
    assert fake.calls[-1] == (GET_INFO, {})  # called with no params


def test_newer_plugin_is_compatible_and_string_version_accepted(fake_call):
    fake_call({
        "core_webservice_get_site_info": site_info(GET_INFO),
        GET_INFO: {"component": "local_mcpconnector", "version": "2027010100", "release": "1.0"},
    })
    info = run_server_info()
    assert info["plugin_version"] == 2027010100
    assert info["plugin_compatible"] is True


def test_old_plugin_is_incompatible(fake_call):
    fake_call({
        "core_webservice_get_site_info": site_info(GET_INFO),
        GET_INFO: {"component": "local_mcpconnector", "version": MIN - 1, "release": "0.1.0"},
    })
    info = run_server_info()
    assert info["plugin_version"] == MIN - 1
    assert info["plugin_compatible"] is False


def test_get_info_failure_is_reported_not_raised(fake_call):
    for failure in (
        server.MoodleError("Moodle error [accessexception]: Access control exception"),
        httpx.ConnectError("connection refused"),
    ):
        fake_call({
            "core_webservice_get_site_info": site_info("local_mcpconnector_create_page"),
            GET_INFO: failure,
        })
        info = run_server_info()
        assert info["connected"] is True
        assert info["plugin_installed"] is True
        assert info["plugin_compatible"] is False
        assert info["plugin_version"] is None
        assert str(failure) in info["plugin_error"]


def test_malformed_get_info_is_reported_not_raised(fake_call):
    fake_call({
        "core_webservice_get_site_info": site_info(GET_INFO),
        GET_INFO: {"component": "local_mcpconnector"},  # no version
    })
    info = run_server_info()
    assert info["plugin_compatible"] is False
    assert "plugin_error" in info


def test_no_plugin_skips_get_info(fake_call):
    fake = fake_call({"core_webservice_get_site_info": site_info("core_course_get_contents")})
    info = run_server_info()
    assert info["plugin_installed"] is False
    assert info["plugin_compatible"] is False
    assert not fake.called(GET_INFO)


@pytest.mark.parametrize("failure,expected", [
    (httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
     "ConnectError: [Errno 11001] getaddrinfo failed"),
    (httpx.ReadTimeout(""), "ReadTimeout"),
])
def test_unreachable_site_reports_disconnected(fake_call, failure, expected):
    fake = fake_call({"core_webservice_get_site_info": failure})
    info = run_server_info()
    assert info["connected"] is False
    assert info["error"] == expected
    assert info["server_version"] == server.SERVER_VERSION  # rest of the report intact
    assert not fake.called(GET_INFO)


def test_http_error_status_reports_disconnected(monkeypatch):
    # The real _call, against a server answering 503 to everything.
    real = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    info = run_server_info()
    assert info["connected"] is False
    assert info["error"].startswith("HTTPStatusError:") and "503" in info["error"]
    assert "SECRET-TEST-TOKEN" not in info["error"]


def test_site_info_failure_still_reports_disconnected(fake_call):
    fake = fake_call({"core_webservice_get_site_info": server.MoodleError("bad token")})
    info = run_server_info()
    assert info["connected"] is False
    assert "bad token" in info["error"]
    assert not fake.called(GET_INFO)
