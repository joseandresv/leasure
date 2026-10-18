import pytest
from pydantic import ValidationError

from config import Settings


@pytest.mark.parametrize("method, path", [("GET", "/"), ("POST", "/api/qobuz/upgrade/all")])
async def test_an_unknown_host_name_is_refused(client, method, path):
    resp = await client.request(method, path, headers={"Host": "evil.test"})

    assert resp.status_code == 400


async def test_the_loopback_address_with_a_port_is_served(client):
    resp = await client.get("/api/qobuz/status", headers={"Host": "127.0.0.1:8642"})

    assert resp.status_code == 200


@pytest.mark.parametrize("field, value", [("qobuz_max_format", 5), ("qobuz_api_calls_per_minute", 41),
                                          ("qobuz_upgrade_keep_days", 0), ("allowed_hosts", [])])
def test_unsafe_qobuz_settings_are_refused(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})
