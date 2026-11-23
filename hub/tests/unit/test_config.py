from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hub.config import ConfigError, check_runtime, load_config, parse_config
from hub.paths import default_config_dir
from hub.settings import Settings


def _settings(**kw: Any) -> Settings:
    return Settings.model_validate(kw)  # bypasses .env: fields set explicitly


def test_shipped_config_is_valid() -> None:
    cfg = load_config(default_config_dir())
    assert cfg.routes.routes["private"].local_only
    assert cfg.hub.server.host == "127.0.0.1"


def test_shipped_prices_are_placeholders_for_cloud_models() -> None:
    """We must not ship invented prices for cloud models."""
    cfg = load_config(default_config_dir())
    for spec in cfg.routes.models.values():
        if spec.provider != "ollama":
            assert not spec.price.configured, f"{spec.alias} ships a price"


def test_valid_minimal(routes_dict: dict[str, Any]) -> None:
    cfg = parse_config({}, routes_dict)
    assert cfg.routes.models["claude"].alias == "claude"
    assert cfg.routes.routes["default"].chain == ["claude", "gemini", "local"]


@pytest.mark.parametrize("cloud", ["claude", "gemini", "muse"])
def test_private_route_rejects_cloud_fallback(routes_dict: dict[str, Any], cloud: str) -> None:
    routes_dict["routes"]["private"]["fallback"] = [cloud]
    with pytest.raises(ConfigError, match="not local"):
        parse_config({}, routes_dict)


def test_private_route_rejects_cloud_primary(routes_dict: dict[str, Any]) -> None:
    routes_dict["routes"]["private"]["primary"] = "claude"
    with pytest.raises(ConfigError, match="not local"):
        parse_config({}, routes_dict)


@pytest.mark.parametrize("tag", ["gpt-oss:120b-cloud", "glm-4.6:cloud", "qwen3-coder:480B-Cloud"])
def test_private_route_rejects_ollama_cloud_models(routes_dict: dict[str, Any], tag: str) -> None:
    routes_dict["models"]["local"]["model"] = tag
    with pytest.raises(ConfigError, match="not local"):
        parse_config({}, routes_dict)


def test_route_named_private_must_be_local_only(routes_dict: dict[str, Any]) -> None:
    routes_dict["routes"]["private"]["local_only"] = False
    with pytest.raises(ConfigError, match="must set local_only"):
        parse_config({}, routes_dict)


def test_any_local_only_route_is_checked(routes_dict: dict[str, Any]) -> None:
    routes_dict["routes"]["secret_stuff"] = {
        "primary": "local",
        "fallback": ["muse"],
        "local_only": True,
    }
    with pytest.raises(ConfigError, match="secret_stuff"):
        parse_config({}, routes_dict)


def test_unknown_model_reference(routes_dict: dict[str, Any]) -> None:
    routes_dict["routes"]["default"]["fallback"] = ["nope"]
    with pytest.raises(ConfigError, match="unknown model 'nope'"):
        parse_config({}, routes_dict)


def test_duplicate_in_chain(routes_dict: dict[str, Any]) -> None:
    routes_dict["routes"]["default"]["fallback"] = ["claude"]
    with pytest.raises(ConfigError, match="more than once"):
        parse_config({}, routes_dict)


def test_default_route_required(routes_dict: dict[str, Any]) -> None:
    del routes_dict["routes"]["default"]
    with pytest.raises(ConfigError, match="'default' is required"):
        parse_config({}, routes_dict)


def test_auto_is_reserved(routes_dict: dict[str, Any]) -> None:
    routes_dict["routes"]["auto"] = {"primary": "claude"}
    with pytest.raises(ConfigError, match="reserved"):
        parse_config({}, routes_dict)


def test_placeholder_model_id_rejected(routes_dict: dict[str, Any]) -> None:
    routes_dict["models"]["claude"]["model"] = "<set in config>"
    with pytest.raises(ConfigError, match="placeholder"):
        parse_config({}, routes_dict)


def test_unknown_provider_rejected(routes_dict: dict[str, Any]) -> None:
    routes_dict["models"]["claude"]["provider"] = "openai"
    with pytest.raises(ConfigError, match="provider"):
        parse_config({}, routes_dict)


def test_extra_keys_rejected(routes_dict: dict[str, Any]) -> None:
    """A typo such as 'fallbacks' must not silently mean 'no fallback'."""
    routes_dict["routes"]["default"]["fallbacks"] = ["gemini"]
    with pytest.raises(ConfigError, match="fallbacks"):
        parse_config({}, routes_dict)


def test_negative_price_rejected(routes_dict: dict[str, Any]) -> None:
    routes_dict["models"]["claude"]["price"] = {"input_per_mtok": -1, "output_per_mtok": 1}
    with pytest.raises(ConfigError):
        parse_config({}, routes_dict)


def test_classifier_rule_needs_condition(routes_dict: dict[str, Any]) -> None:
    routes_dict["classifier"]["rules"].append({"route": "default"})
    with pytest.raises(ConfigError, match="no conditions"):
        parse_config({}, routes_dict)


def test_classifier_unknown_route(routes_dict: dict[str, Any]) -> None:
    routes_dict["classifier"]["rules"].append({"route": "nope", "any": ["x"]})
    with pytest.raises(ConfigError, match="unknown route"):
        parse_config({}, routes_dict)


def test_lan_bind_requires_opt_in(routes_dict: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match="allow_lan"):
        parse_config({"server": {"host": "0.0.0.0"}}, routes_dict)


def test_lan_bind_requires_token(routes_dict: dict[str, Any]) -> None:
    cfg = parse_config({"server": {"host": "0.0.0.0", "allow_lan": True}}, routes_dict)
    with pytest.raises(ConfigError, match="HUB_API_TOKEN"):
        check_runtime(cfg, _settings())
    check_runtime(cfg, _settings(HUB_API_TOKEN="t"))  # ok with token


def test_host_override_also_requires_token(routes_dict: dict[str, Any]) -> None:
    cfg = parse_config({}, routes_dict)
    with pytest.raises(ConfigError, match="HUB_API_TOKEN"):
        check_runtime(cfg, _settings(HUB_HOST="0.0.0.0"))


@pytest.mark.parametrize("base", ["https://ollama.com", "http://8.8.8.8:11434"])
def test_public_ollama_base_rejected_when_private_route_exists(
    routes_dict: dict[str, Any], base: str
) -> None:
    cfg = parse_config({}, routes_dict)
    with pytest.raises(ConfigError, match="OLLAMA_API_BASE"):
        check_runtime(cfg, _settings(OLLAMA_API_BASE=base))


def test_missing_device_token_warns(routes_dict: dict[str, Any]) -> None:
    cfg = parse_config({"devices": [{"id": "cores3-desk", "kind": "cores3"}]}, routes_dict)
    warnings = check_runtime(cfg, _settings())
    assert any("cores3-desk" in w for w in warnings)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path)


def test_invalid_yaml(tmp_path: Path) -> None:
    (tmp_path / "hub.yaml").write_text("server: [unclosed")
    (tmp_path / "routes.yaml").write_text("")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(tmp_path)


# ------------------------------------------------------------------ memory


def test_memory_defaults(routes_dict: dict[str, Any]) -> None:
    mem = parse_config({}, routes_dict).hub.memory
    assert mem.enabled
    assert mem.mcp.max_sensitivity == "personal"
    assert mem.embedding.model == "nomic-embed-text"


def test_mcp_cannot_serve_private(routes_dict: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match="max_sensitivity"):
        parse_config({"memory": {"mcp": {"max_sensitivity": "private"}}}, routes_dict)


def _without_private_route(routes_dict: dict[str, Any]) -> dict[str, Any]:
    """Isolate the embedder check from the local_only-route endpoint check."""
    del routes_dict["routes"]["private"]
    rules = routes_dict["classifier"]["rules"]
    routes_dict["classifier"]["rules"] = [r for r in rules if r["route"] != "private"]
    return routes_dict


@pytest.mark.parametrize(
    ("model", "base"),
    [
        ("nomic-embed-text:cloud", "http://127.0.0.1:11434"),
        ("nomic-embed-text", "https://ollama.com"),
    ],
)
def test_embedder_must_be_local(routes_dict: dict[str, Any], model: str, base: str) -> None:
    cfg = parse_config(
        {"memory": {"embedding": {"model": model}}}, _without_private_route(routes_dict)
    )
    with pytest.raises(ConfigError, match="notes would leave the machine"):
        check_runtime(cfg, _settings(OLLAMA_API_BASE=base))


def test_embedder_check_skipped_when_memory_disabled(routes_dict: dict[str, Any]) -> None:
    cfg = parse_config({"memory": {"enabled": False}}, _without_private_route(routes_dict))
    check_runtime(cfg, _settings(OLLAMA_API_BASE="https://ollama.com"))
