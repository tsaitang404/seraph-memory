"""Seraph LLM entity extraction.

Seraph extends holographic with an optional LLM extraction layer:
after the fast regex/known-entity pass, an LLM call extracts entities that
regex misses — Chinese names, lowercase hosts, domains, projects, etc.

Implementation: standalone OpenAI-compatible HTTP call that REUSES Hermes'
provider machinery (hermes_cli.providers) to resolve base_url + api_key for
the configured model provider. No separate LLM credentials needed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You extract named entities from a fact statement. "
    "An entity is any concrete, referenceable thing: people, machines/servers, "
    "hostnames, domain names, projects, repositories, services, tools, "
    "cloud providers, storage backends, companies, cities, etc. "
    "Include lowercase names, Chinese names, domain names, and compound names. "
    "EXCLUDE pure numbers, port numbers, timestamps, and generic words. "
    "Return ONLY a JSON array of strings, e.g. [\"sad\", \"tm.aketer.me\", \"poto\"]. "
    "No explanation, no markdown."
)


def _resolve_endpoint() -> tuple[str, str, str]:
    """Resolve (base_url, api_key, model) for the configured provider via Hermes.

    Uses hermes_cli.providers.get_provider() to read the provider's
    base_url_env_var, then reads that env var (or .env) for the value.
    Falls back to deepseek if resolution fails.
    """
    try:
        from hermes_cli.config import load_config_readonly, cfg_get
        from hermes_cli.providers import get_provider

        all_config = load_config_readonly()
        model = cfg_get(all_config, "model", "default", default="") or ""
        provider = cfg_get(all_config, "model", "provider", default="") or ""
        if not model or not provider:
            return "", "", ""

        # Get provider def (base_url_env_var + api key env var)
        pdef = get_provider(provider)
        base_url_env = pdef.base_url_env_var if pdef else ""
        api_key_envs = pdef.api_key_env_vars if pdef else ()

        base_url = ""
        if base_url_env:
            base_url = os.environ.get(base_url_env, "")
            if not base_url:
                base_url = _read_env_file(base_url_env)
        api_key = ""
        for env_var in api_key_envs:
            api_key = os.environ.get(env_var, "") or _read_env_file(env_var)
            if api_key:
                break

        # opencode-go/zen are aggregators without a fixed base URL — fall back
        # to deepseek for extraction in that case.
        if provider in ("opencode-go", "opencode-zen") or not base_url or not api_key:
            return _fallback_deepseek()

        return base_url, api_key, model
    except Exception as e:
        logger.debug("Seraph provider resolution failed: %s", e)
        return _fallback_deepseek()


def _fallback_deepseek() -> tuple[str, str, str]:
    """Fallback: deepseek-chat via api.deepseek.com (env DEEPSEEK_API_KEY)."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "") or _read_env_file("DEEPSEEK_API_KEY")
    if not api_key:
        return "", "", ""
    return "https://api.deepseek.com/v1", api_key, "deepseek-chat"


def _read_env_file(key: str) -> str:
    try:
        hermes_home = os.path.expanduser("~/.hermes")
        with open(os.path.join(hermes_home, ".env")) as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def extract_entities_llm(text: str) -> list[str]:
    """Extract entities via the configured Hermes model.

    Returns [] on any failure (caller falls back to regex results).
    """
    base_url, api_key, model = _resolve_endpoint()
    if not base_url or not api_key or not model:
        return []

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Extract entities from: {text}"},
        ],
        "temperature": 0.1,
        "max_tokens": 300,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        raw = data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.debug("Seraph LLM extraction failed: %s", e)
        return []

    if not raw:
        return []
    raw = raw.strip()
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        if isinstance(data, list):
            out: list[str] = []
            for item in data:
                if isinstance(item, str):
                    item = item.strip()
                    if item and item not in out:
                        out.append(item)
            return out
    except Exception as e:
        logger.debug("Seraph LLM JSON parse failed: %s", e)
    return []
