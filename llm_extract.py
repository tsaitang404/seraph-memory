"""Seraph LLM entity extraction.

Seraph extends holographic with an optional LLM extraction layer:
after the fast regex/known-entity pass, an LLM call extracts entities that
regex misses — Chinese names, lowercase hosts, domains, projects, etc.

Implementation: standalone OpenAI-compatible HTTP call, configured via
config.yaml `plugins.seraph.llm_extract.*` (provider/model/base_url/api_key).
Decoupled from Hermes' internal agent client so it survives version changes.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
import urllib.error
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


def extract_entities_llm(
    text: str,
    *,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> list[str]:
    """Extract entities via an OpenAI-compatible chat completion.

    Returns [] on any failure (caller falls back to regex results).
    """
    if not api_key or not base_url or not model:
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
