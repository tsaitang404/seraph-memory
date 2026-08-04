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
    "STRICT EXCLUSIONS — do NOT extract: "
    "DNS record types (A, AAAA, CNAME, MX, TXT, SPF, DKIM, DMARC, NS), "
    "port numbers or IP addresses (38.76.190.84, 104.21.x, 172.67.x), "
    "generic table/database names (images, configuration, users, data), "
    "single generic words (web, ai, r2, d1, open, astr, tail, pic), "
    "bucket names alone (web, lobechat) unless clearly a named resource, "
    "timestamps, numbers, or vague categories. "
    "When a short name is ambiguous, prefer the full form (e.g. 'hermes dashboard' not 'dashboard'). "
    "Return ONLY a JSON array of strings, e.g. [\"sad\", \"tm.aketer.me\", \"poto\"]. "
    "No explanation, no markdown."
)

_RELATION_PROMPT = (
    "You extract relationships between named entities from a fact statement. "
    "A relationship is a triplet: (source_entity, relation, target_entity) where "
    "the relation is a short semantic verb like runs_on, resolves_to, proxies_to, "
    "deployed_on, stores_in, depends_on, connects_to, part_of, uses, hosts, etc. "
    "Only include relations explicitly implied by the statement. "
    "Include lowercase, Chinese, and domain names as entities. "
    "STRICT EXCLUSIONS — do NOT use as entities: "
    "DNS record types (A, CNAME, MX, TXT, SPF, DKIM, DMARC), "
    "port numbers or IP addresses, "
    "generic table/database names (images, configuration, users), "
    "single generic words (web, ai, r2, d1), timestamps, or numbers. "
    "Return ONLY a JSON array of triplets, e.g. "
    "[[\"tm.aketer.me\", \"resolves_to\", \"sad\"], [\"sad\", \"hosts\", \"trilium\"]]. "
    "No explanation, no markdown."
)

_DESCRIPTION_PROMPT = (
    "You write a concise structured description for ONE entity based on the "
    "facts and relations associated with it. "
    "Output plain text (no markdown, no JSON), 3-8 lines, covering: "
    "what it is (type/role), key attributes (IP, user, system, ports, URLs), "
    "and its important connections. "
    "Use the entity's own language (Chinese if the facts are Chinese). "
    "Only state what the facts support — do not invent details."
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
    raw = _chat(text, _SYSTEM_PROMPT)
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


def extract_relations_llm(text: str) -> list[tuple[str, str, str]]:
    """Extract (source, relation, target) triplets via the configured LLM.

    Returns [] on any failure.
    """
    raw = _chat(text, _RELATION_PROMPT)
    if not raw:
        return []
    raw = raw.strip()
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        if isinstance(data, list):
            out: list[tuple[str, str, str]] = []
            for item in data:
                if (
                    isinstance(item, list)
                    and len(item) == 3
                    and all(isinstance(x, str) for x in item)
                ):
                    s, r, t = (x.strip() for x in item)
                    if s and r and t and (s, r, t) not in out:
                        out.append((s, r, t))
            return out
    except Exception as e:
        logger.debug("Seraph LLM relation JSON parse failed: %s", e)
    return []


def generate_entity_description(entity: str, facts: list[str], relations: list[tuple[str, str, str]]) -> str:
    """Generate a concise description for an entity from its facts + relations.

    Returns "" on any failure (caller keeps existing description).
    """
    if not facts:
        return ""
    facts_text = "\n".join(f"- {f[:150]}" for f in facts[:12])
    rel_text = "\n".join(f"- {s} {r} {t}" for s, r, t in relations[:12])
    prompt = (
        f"Entity: {entity}\n\nAssociated facts:\n{facts_text}\n"
        + (f"\nRelations:\n{rel_text}" if rel_text else "")
    )
    raw = _chat(prompt, _DESCRIPTION_PROMPT)
    if not raw:
        return ""
    desc = raw.strip()
    # Strip markdown code fences if present
    if desc.startswith("```"):
        desc = desc.split("```", 2)[1] if "```" in desc[3:] else desc
        desc = desc.strip()
    return desc[:800]


def _chat(text: str, system_prompt: str) -> str:
    """Send a chat completion to the configured Hermes model, return raw text."""
    base_url, api_key, model = _resolve_endpoint()
    if not base_url or not api_key or not model:
        return ""

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Extract from: {text}"},
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
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.debug("Seraph LLM call failed: %s", e)
        return ""
