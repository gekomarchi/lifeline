"""Chain and subject configuration (`config/network.json`) and the shared `--rpc` argument."""

from __future__ import annotations

import json
from pathlib import Path

from .reader import ChainReader

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "network.json"
ROLES = ("reactive", "destination")


class ConfigError(Exception):
    """The configuration or a command argument is not usable."""


def load_config(path=None) -> dict:
    path = Path(path or DEFAULT_CONFIG)
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ConfigError(f"{path}: {error}") from None
    chains = config.get("chains") or {}
    for name, chain in chains.items():
        for key in ("chainId", "role", "ledger"):
            if key not in chain:
                raise ConfigError(f"{path}: chains.{name}.{key} is missing")
        if chain["role"] not in ROLES:
            raise ConfigError(f"{path}: chains.{name}.role must be one of {ROLES}")
    if [chain["role"] for chain in chains.values()].count("reactive") != 1:
        raise ConfigError(f"{path}: exactly one chain must have the role reactive")
    config.setdefault("subjects", {})
    return config


def parse_rpc(values) -> dict:
    """Turn repeated `<chain>=<url>` arguments into {chain: [url, ...]}, in the given order."""
    urls = {}
    for value in values or []:
        chain, separator, url = value.partition("=")
        if not separator or not chain or not url:
            raise ConfigError(f"--rpc needs <chain>=<url>, got {value!r}")
        urls.setdefault(chain, []).append(url)
    return urls


def open_readers(config, rpc, cache_dir=".cache/chain", **options) -> dict:
    """One `ChainReader` for each configured chain. `--rpc` URLs replace the configured list."""
    unknown = set(rpc) - set(config["chains"])
    if unknown:
        raise ConfigError(f"--rpc names a chain that is not configured: {sorted(unknown)}")
    readers = {}
    for name, chain in config["chains"].items():
        urls = rpc.get(name) or chain.get("rpc") or []
        if not urls:
            raise ConfigError(f"no RPC URL for chain {name}: use --rpc {name}=<url>")
        readers[name] = ChainReader(name, urls, cache_dir, chain_id=chain["chainId"], **options)
    return readers
