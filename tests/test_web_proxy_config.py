from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


def test_web_fetch_proxy_config_camel_case() -> None:
    raw = {
        "tools": {
            "policy": {"legacyCompat": True, "productionHardening": False},
            "network": {
                "search": {
                    "apiKey": "k",
                    "maxResults": 7,
                    "mode": "proxy",
                    "proxyUrl": "http://127.0.0.1:4499/v1/search",
                    "proxyHealthcheck": True,
                    "proxyFallbackToLocal": True,
                },
                "fetch": {
                    "mode": "proxy",
                    "proxyUrl": "http://127.0.0.1:4499/v1/fetch",
                    "proxyHealthcheck": True,
                    "proxyFallbackToLocal": True,
                },
            }
        }
    }
    config = Config.model_validate(convert_keys(raw))
    assert config.tools.network.search.mode == "proxy"
    assert config.tools.network.search.api_key == "k"
    assert config.tools.network.search.max_results == 7
    assert config.tools.network.search.proxy_url.endswith("/v1/search")
    assert config.tools.network.search.proxy_healthcheck is True
    assert config.tools.network.search.proxy_fallback_to_local is True
    assert config.tools.network.fetch.mode == "proxy"
    assert config.tools.network.fetch.proxy_url.endswith("/v1/fetch")
    assert config.tools.network.fetch.proxy_healthcheck is True
    assert config.tools.network.fetch.proxy_fallback_to_local is True
    assert config.tools.effective_search().mode == "proxy"
    assert config.tools.effective_fetch().mode == "proxy"
