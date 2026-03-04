from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


def test_web_fetch_proxy_config_camel_case() -> None:
    raw = {
        "tools": {
            "web": {
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
                }
            }
        }
    }
    config = Config.model_validate(convert_keys(raw))
    assert config.tools.web.search.mode == "proxy"
    assert config.tools.web.search.api_key == "k"
    assert config.tools.web.search.max_results == 7
    assert config.tools.web.search.proxy_url.endswith("/v1/search")
    assert config.tools.web.search.proxy_healthcheck is True
    assert config.tools.web.search.proxy_fallback_to_local is True
    assert config.tools.web.fetch.mode == "proxy"
    assert config.tools.web.fetch.proxy_url.endswith("/v1/fetch")
    assert config.tools.web.fetch.proxy_healthcheck is True
    assert config.tools.web.fetch.proxy_fallback_to_local is True
    assert config.tools.effective_search().mode == "proxy"
    assert config.tools.effective_fetch().mode == "proxy"


