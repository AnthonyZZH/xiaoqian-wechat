"""Named characters in a drawing request: their Danbooru tag, series and the look that keeps them recognisable.

Danbooru's tag index is the vocabulary the Illustrious checkpoint was trained on, so a character drawn
from its own tag plus the tags most often seen with it (hair, eyes, signature accessories) comes out as
that character. Every lookup is best effort: offline or unknown just means no reference.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

API = "https://danbooru.donmai.us"
CHARACTER, COPYRIGHT = 4, 3
_cache: dict = {}
# Tags about how a character looks, as opposed to what they wear or do.
LOOKS = re.compile(r"hair|eyes|ears|ahoge|bangs|twintails|ponytail|braid|bun|sidelocks|horn|tail|wings|elf|skin|"
                   r"freckles|mole|fang|halo|breasts")


def _get(route, params, timeout=8):
    url = f"{API}{route}?{urlencode(params)}"
    if url not in _cache:
        if len(_cache) > 500:
            _cache.clear()
        with urlopen(Request(url, headers={"User-Agent": "xiaoqian-wechat/1.0"}), timeout=timeout) as response:
            _cache[url] = json.loads(response.read(2 * 1024 * 1024))
    return _cache[url]


def normalize(tag):
    return re.sub(r"\s+", "_", str(tag).strip().lower())


def known(tags):
    """{tag: post_count} for those of `tags` that are Danbooru character tags."""
    tags = [normalize(t) for t in tags if t and normalize(t)]
    if not tags:
        return {}
    found = _get("/tags.json", {"search[name_comma]": ",".join(tags), "only": "name,post_count,category"})
    return {t["name"]: t["post_count"] for t in found if t.get("category") == CHARACTER and t.get("post_count")}


def from_web(name, search):
    """Character tags a web search for `name` turns up on Danbooru (posts or wiki pages), best ranked first."""
    found = []
    for result in search(f"{name} danbooru", 5):
        parts = urlsplit(str(result.get("url", "")))
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not (host == "donmai.us" or host.endswith(".donmai.us")):
            continue  # only Danbooru itself, not a page whose path merely mentions it
        tags = parse_qs(parts.query).get("tags", [""])[0] if parts.path == "/posts" else \
            parts.path.removeprefix("/wiki_pages/") if parts.path.startswith("/wiki_pages/") else ""
        tag = normalize(unquote(tags.split()[0])) if tags.strip() else ""
        if tag and "/" not in tag and tag not in found:
            found.append(tag)
    hits = known(found)
    return [tag for tag in found if tag in hits]


def resolve(name, guess, search=None):
    """Danbooru tag for a character asked for as `name`, given the model's `guess`; None if unsure.

    A guess that exists can still be someone else (a sister, a namesake), so the web has the last word
    when it names other characters for `name`; the guess stands when the web agrees or says nothing."""
    guess = normalize(guess or "")
    valid = bool(guess) and guess in known([guess])
    if search:
        try:
            web = from_web(name, search)
        except Exception:
            web = []
        if web and guess not in web:
            return web[0]
    if valid:
        return guess
    # The model often has the romanised name right but the qualifier wrong: "yukino_(xxx)".
    stem = normalize(guess or "").split("_(")[0]
    if len(stem) >= 4:
        found = _get("/tags.json", {"search[name_matches]": f"{stem}*", "search[category]": CHARACTER,
                                    "search[order]": "count", "limit": 1, "only": "name,post_count"})
        if found and found[0].get("post_count", 0) >= 50:
            return found[0]["name"]
    return None


def profile(tag, limit=30):
    """{tag, series, traits: [(general tag, how often it comes with this character)]}."""
    general = _get("/related_tag.json", {"query": tag, "category": "general", "limit": limit})
    series = _get("/related_tag.json", {"query": tag, "category": "copyright", "limit": 1})
    traits = [(t["tag"]["name"], round(float(t.get("frequency") or 0), 2))
              for t in general.get("related_tags", []) if float(t.get("frequency") or 0) >= 0.2]
    copyright = [t["tag"]["name"] for t in series.get("related_tags", [])]
    return {"tag": tag, "series": copyright[0] if copyright else "", "traits": traits}


def as_prompt_tag(tag):
    """Danbooru's underscores to the spaced form the checkpoint saw; brackets escaped for A1111."""
    return tag.replace("_", " ").replace("(", r"\(").replace(")", r"\)")
