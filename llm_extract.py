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
    "Judge each candidate by KNOWLEDGE-GRAPH VALUE, not by rule lists: "
    "extract ONLY if ALL hold: "
    "(1) IDENTIFIABLE — it names one specific thing (a host, a person, a project, "
    "a domain, a service) that another fact could plausibly reference later; "
    "(2) STABLE — it is a lasting name, not a transient detail of this statement; "
    "(3) NOT REDUNDANT — it adds a node worth linking, not a value that belongs "
    "as a property of another entity. "
    "Typical NON-entities: raw IPs/ports/paths that are just contact details "
    "(a host entity is enough; 10.10.10.97, :3389, ~/.ssh/silk are properties, "
    "not graph nodes), generic role labels (system, cluster, subdomain, data dir, "
    "machine, root), transient process/component instances (monitorv3(transfer)), "
    "file names, install packages, vague categories. "
    "EXCEPTION: a raw IP/path IS an entity when it is the subject itself "
    "(e.g. '10.10.10.97 is the new host'), not when it merely appears in a list. "
    "When a short name is ambiguous, prefer the full form (e.g. 'app dashboard' not 'dashboard'). "
    "Classify each entity with ONE of these types: "
    "person, server, device, domain, service, project, repo, tool, resource, company, system. "
    "Return ONLY a JSON array of [name, type] pairs, e.g. "
    "[[\"host-a\", \"server\"], [\"app.example.com\", \"domain\"], [\"my-project\", \"project\"]]. "
    "No explanation, no markdown."
)

_RELATION_PROMPT = (
    "You extract relationships between named entities from a fact statement. "
    "A relationship is a triplet: (source_entity, relation, target_entity) where "
    "the relation is a short semantic verb like runs_on, resolves_to, proxies_to, "
    "deployed_on, stores_in, depends_on, connects_to, part_of, uses, hosts, etc. "
    "Only include relations explicitly implied by the statement. "
    "Include lowercase, Chinese, and domain names as entities. "
    "Apply the same KNOWLEDGE-GRAPH VALUE judgment as entity extraction: "
    "both endpoints must be durable, referenceable named things. "
    "Do NOT create triplets for raw IPs, ports, paths, file names, transient "
    "process instances, or generic role labels — those belong as properties, "
    "not graph nodes. "
    "EXCEPTION: a raw IP/path may be an endpoint only when it is the subject "
    "of the statement itself (e.g. '10.10.10.97 hosts the new service'). "
    "Return ONLY a JSON array of triplets, e.g. "
    "[[\"app.example.com\", \"resolves_to\", \"host-a\"], [\"host-a\", \"hosts\", \"notes-service\"]]. "
    "No explanation, no markdown."
)

_DESCRIPTION_PROMPT = (
    "You write a structured description for ONE entity based on the "
    "facts and relations associated with it. "
    "Output EXACTLY 3 lines, each starting with one of the fixed labels: "
    "[类型] [别名] [说明]. "
    "Line 1 [类型]: the entity's type/role in Chinese (e.g. 服务器, 服务, "
    "工具, 域名, 项目, 仓库, 设备, 系统, 资源, 公司, 人员). "
    "Line 2 [别名]: key identifiers separated by ' · ' (IPs, hostnames, "
    "domains, ports, usernames, URLs). If none are known, write '-'. "
    "Line 3 [说明]: one concise sentence describing what it is and its "
    "important connections. Only state what the facts support — do not "
    "invent details. "
    "Use the entity's own language (Chinese if the facts are Chinese). "
    "No markdown, no JSON, no extra lines."
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


def extract_entities_llm(text: str) -> list[tuple[str, str]]:
    """Extract entities as (name, type) pairs via the configured Hermes model.

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
            out: list[tuple[str, str]] = []
            seen: set[str] = set()
            for item in data:
                if isinstance(item, list) and len(item) >= 1 and isinstance(item[0], str):
                    name = item[0].strip()
                    etype = item[1].strip() if len(item) > 1 and isinstance(item[1], str) else ""
                    if name and name.lower() not in seen:
                        seen.add(name.lower())
                        out.append((name, etype))
                elif isinstance(item, str):
                    name = item.strip()
                    if name and name.lower() not in seen:
                        seen.add(name.lower())
                        out.append((name, ""))
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
    if not facts and not relations:
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


_NOISE_PROMPT = (
    "You decide whether each entity in a knowledge graph is noise (not worth a node). "
    "For EACH entity, judge by MEANING using its name, its current type, AND its "
    "associated facts. Keep it if it names a specific, stable, referenceable thing "
    "(a host, service, domain, storage path that is a real mount, a model, a project). "
    "Flag as noise if it is just a transient detail, a generic role label, a raw "
    "port/number, a file name that is not a durable resource, a package version string, "
    "or a vague category. IMPORTANT: shape alone is not decisive — '/PikPak' is a real "
    "storage mount (keep), '~/.ssh/silk' is a config detail (noise), '8080' alone is "
    "noise, 'deepseek-v4-flash' is a model (keep). Trust the facts when they show real "
    "usage. Return ONLY a JSON object mapping each exact entity name to "
    '{"keep": true} or {"noise": true, "reason": "..."}, e.g. '
    '{"host-a": {"keep": true}, "8080": {"noise": true, "reason": "raw port"}}. '
    "No explanation outside the JSON, no markdown."
)


def classify_noise_llm(
    entities: list[tuple[str, str, int, int]], facts_map: dict[str, list[str]]
) -> dict[str, bool]:
    """Batch-classify whether entities are noise via the configured LLM.

    Args:
        entities: list of (name, entity_type, fe_count, er_count) — fe/er context only.
        facts_map: entity name -> short fact snippets (context for judgment).

    Returns dict {name: is_noise_bool} for names the LLM judged. On failure
    returns {} (caller falls back to keeping everything, never auto-delete
    without LLM confirmation — matches the 'no fixed rules' principle).
    """
    if not entities:
        return {}
    out: dict[str, bool] = {}
    # 分批（每批 25 个）——一次塞太多 LLM 会截断/超长，返回空
    BATCH = 25
    for i in range(0, len(entities), BATCH):
        batch = entities[i : i + BATCH]
        lines = []
        for name, cur, fe, er in batch:
            ctx = ""
            if name in facts_map and facts_map[name]:
                ctx = " | facts: " + " ; ".join(facts_map[name][:3])
            lines.append(
                f"- {name} (type: {cur or 'unknown'}, refs: {fe}){ctx}"
            )
        prompt = "Entities to judge:\n" + "\n".join(lines)
        raw = _chat(prompt, _NOISE_PROMPT)
        if not raw:
            continue
        raw = raw.strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, dict):
                        if v.get("noise"):
                            out[k.strip()] = True
                        elif v.get("keep"):
                            out[k.strip()] = False
        except Exception as e:
            logger.debug("Seraph LLM noise parse failed: %s", e)
    return out


_CLASSIFY_PROMPT = (
    "You classify entities by their semantic role in a knowledge graph. "
    "For EACH entity, use its name AND its associated facts to decide the best "
    "type from: person, server, device, domain, service, project, repo, tool, "
    "resource, company, system, user, package, account, file. "
    "Judge by meaning, not by shape (a name that looks like a hostname may "
    "really be a service; an IP is usually a resource; a model name like "
    "deepseek-v4-flash is a tool/model; a channel like qqbot is a service). "
    "If the facts clearly show what it is, trust the facts. "
    "Return ONLY a JSON object mapping each exact entity name to a type, e.g. "
    '{"host-a": "server", "app.example.com": "domain"}. '
    "No explanation, no markdown."
)


def classify_entity_types_llm(
    entities: list[tuple[str, str]], facts_map: dict[str, list[str]]
) -> dict[str, str]:
    """Batch-classify entity types via the configured LLM.

    Args:
        entities: list of (name, current_type) — current_type is context only.
        facts_map: entity name -> short fact snippets (context for judgment).

    Returns dict {name: suggested_type} — only names the LLM confidently
    re-typed. On failure returns {} (caller keeps current types).
    """
    if not entities:
        return {}
    out: dict[str, str] = {}
    # 分批（每批 25 个）——一次塞太多 LLM 会截断/超长，返回空
    BATCH = 25
    for i in range(0, len(entities), BATCH):
        batch = entities[i : i + BATCH]
        lines = []
        for name, cur in batch:
            ctx = ""
            if name in facts_map and facts_map[name]:
                ctx = " | facts: " + " ; ".join(facts_map[name][:3])
            lines.append(f"- {name} (current: {cur or 'unknown'}){ctx}")
        prompt = "Entities to classify:\n" + "\n".join(lines)
        raw = _chat(prompt, _CLASSIFY_PROMPT)
        if not raw:
            continue
        raw = raw.strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                valid = {
                    "person", "server", "device", "domain", "service", "project",
                    "repo", "tool", "resource", "company", "system", "user",
                    "package", "account", "file", "script", "format",
                }
                for k, v in data.items():
                    if isinstance(v, str) and v.strip().lower() in valid:
                        out[k] = v.strip().lower()
        except Exception as e:
            logger.debug("Seraph LLM classify parse failed: %s", e)
    return out
