#!/usr/bin/env python3
"""AI Visibility Tracker.

Asks a fixed list of prompts to AI answer engines (OpenAI, Perplexity, Gemini)
through their official APIs and measures how visible your brand is:

  * mentioned  - is your brand / handle / domain named in the answer?
  * rank       - position of your brand among all tracked brands in the answer
  * cited      - is one of your domains linked or used as a source?
  * competitors mentioned in the same answer (for share of voice)

Writes data/latest.json and appends a daily row to data/history.json.
Pure Python standard library. Providers without an API key are skipped.

Usage:
  python track.py            # real run (needs at least one API key)
  python track.py --demo     # write clearly labeled DEMO data for the dashboard
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(ROOT, "config.json")
UA = "ai-visibility-tracker/1.0 (+https://github.com/plutunieer/ai-visibility)"

ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
LABELS = {"openai": "ChatGPT (OpenAI)", "perplexity": "Perplexity", "gemini": "Gemini"}


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def post_json(url, payload, headers, timeout, retries=2):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": UA}
    h.update(headers)
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            last = ApiError(e.code, body)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(4 * (attempt + 1))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = ApiError(0, str(e))
            if attempt < retries:
                time.sleep(4 * (attempt + 1))
                continue
            raise last
    raise last


# --------------------------------------------------------------------------- #
# Providers: each returns (answer_text, [ {url, title} ], model_used)
# --------------------------------------------------------------------------- #
def ask_openai(prompt, cfg, key, timeout):
    model = cfg.get("model", "gpt-6-luna")
    payload = {"model": model, "input": prompt}
    if cfg.get("web_search", True):
        payload["tools"] = [{"type": "web_search"}]
    headers = {"Authorization": f"Bearer {key}"}
    url = "https://api.openai.com/v1/responses"
    try:
        data = post_json(url, payload, headers, timeout)
    except ApiError as e:
        if "tools" in payload and e.status == 400:
            log(f"    openai: web_search not accepted for {model}, retrying as plain answer")
            payload.pop("tools")
            data = post_json(url, payload, headers, timeout)
        else:
            raise
    texts, sources = [], []
    for item in data.get("output", []) or []:
        t = item.get("type")
        if t == "message":
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    texts.append(c.get("text", ""))
                    for a in c.get("annotations", []) or []:
                        if a.get("url"):
                            sources.append({"url": a["url"], "title": a.get("title", "")})
        elif t == "web_search_call":
            for s in ((item.get("action") or {}).get("sources") or []):
                if isinstance(s, dict) and s.get("url"):
                    sources.append({"url": s["url"], "title": s.get("title", "")})
    text = "\n".join(texts) or data.get("output_text", "") or ""
    return text, sources, data.get("model", model)


def ask_perplexity(prompt, cfg, key, timeout):
    headers = {"Authorization": f"Bearer {key}"}
    preset = cfg.get("preset", "fast")
    try:
        data = post_json("https://api.perplexity.ai/v1/agent",
                         {"preset": preset, "input": prompt}, headers, timeout)
        texts, sources = [], []
        for item in data.get("output", []) or []:
            t = item.get("type")
            if t == "message":
                for c in item.get("content", []) or []:
                    if c.get("type") in ("output_text", "text"):
                        texts.append(c.get("text", ""))
                        for a in c.get("annotations", []) or []:
                            if a.get("url"):
                                sources.append({"url": a["url"], "title": a.get("title", "")})
            elif t == "search_results":
                for r in item.get("results", []) or []:
                    if r.get("url"):
                        sources.append({"url": r["url"], "title": r.get("title", "")})
        text = "\n".join(texts) or data.get("output_text", "") or ""
        return text, sources, f"agent:{preset}"
    except ApiError as e:
        if e.status in (401, 403):
            raise
        log(f"    perplexity agent API failed ({e.status}), falling back to sonar")
    model = cfg.get("fallback_model", "sonar")
    data = post_json("https://api.perplexity.ai/chat/completions",
                     {"model": model, "messages": [{"role": "user", "content": prompt}]},
                     headers, timeout)
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""
    sources = [{"url": u, "title": ""} for u in (data.get("citations") or []) if isinstance(u, str)]
    for r in data.get("search_results") or []:
        if r.get("url"):
            sources.append({"url": r["url"], "title": r.get("title", "")})
    return text, sources, model


def ask_gemini(prompt, cfg, key, timeout):
    model = cfg.get("model", "gemini-3.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if cfg.get("google_search", True):
        payload["tools"] = [{"google_search": {}}]
    data = post_json(url, payload, {"x-goog-api-key": key}, timeout)
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))
    sources = []
    gm = cand.get("groundingMetadata") or {}
    for ch in gm.get("groundingChunks") or []:
        web = ch.get("web") or {}
        if web.get("uri"):
            # Gemini returns redirect URLs; the title is usually the real domain.
            sources.append({"url": web["uri"], "title": web.get("title", "")})
    return text, sources, model


PROVIDERS = {"openai": ask_openai, "perplexity": ask_perplexity, "gemini": ask_gemini}


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
URL_RE = re.compile(r"https?://[^\s)\]>\"'<]+", re.I)


def term_regex(term):
    t = re.escape(term.strip()).replace(r"\ ", r"\s+")
    return re.compile(r"(?<![A-Za-z0-9_])" + t + r"(?![A-Za-z0-9_])", re.I)


def norm_domain(d):
    d = d.lower().strip()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    return d.rstrip("/")


def entity_terms(ent):
    terms = [ent["name"]] + list(ent.get("aliases", [])) + list(ent.get("domains", []))
    seen, out = set(), []
    for t in terms:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def first_pos(text, terms):
    best = None
    for t in terms:
        m = term_regex(t).search(text)
        if m and (best is None or m.start() < best):
            best = m.start()
    return best


def analyze(text, sources, brand, competitors):
    text = text or ""
    urls = [s["url"] for s in sources] + URL_RE.findall(text)
    titles = [s.get("title", "") or "" for s in sources]
    domains = [norm_domain(d) for d in brand.get("domains", [])]

    def hits_domain(s):
        s2 = norm_domain(s)
        return any(d and d in s2 for d in domains)

    cited_urls = []
    for u in urls:
        if hits_domain(u) and u not in cited_urls:
            cited_urls.append(u)
    for t in titles:  # Gemini redirect links: domain lives in the title
        if t and hits_domain(t) and t not in cited_urls:
            cited_urls.append(t)

    positions = {}
    bp = first_pos(text, entity_terms(brand))
    if bp is not None:
        positions[brand["name"]] = bp
    comp_hits = []
    for c in competitors:
        p = first_pos(text, entity_terms(c))
        if p is not None:
            positions[c["name"]] = p
            comp_hits.append(c["name"])

    mentioned = brand["name"] in positions
    rank = None
    if mentioned:
        order = sorted(positions, key=lambda k: positions[k])
        rank = order.index(brand["name"]) + 1
    return {
        "mentioned": mentioned,
        "rank": rank,
        "brands_in_answer": len(positions),
        "cited": bool(cited_urls),
        "cited_urls": cited_urls[:10],
        "competitors": comp_hits,
        "sources_total": len(sources),
    }


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def summarize(results, brand, competitors):
    ok = [r for r in results if not r.get("error")]
    n = len(ok)
    ment = sum(1 for r in ok if r["mentioned"])
    cited = sum(1 for r in ok if r["cited"])
    ranks = [r["rank"] for r in ok if r["rank"]]
    counts = {brand["name"]: ment}
    for c in competitors:
        counts[c["name"]] = sum(1 for r in ok if c["name"] in r["competitors"])
    total = sum(counts.values())
    sov = [{"name": k, "mentions": v, "share": pct(v, total), "is_brand": k == brand["name"]}
           for k, v in counts.items()]
    sov.sort(key=lambda x: (-x["mentions"], not x["is_brand"]))
    return {
        "answers": n,
        "errors": len(results) - n,
        "mentioned": ment,
        "cited": cited,
        "visibility": pct(ment, n),
        "citation_rate": pct(cited, n),
        "avg_rank": round(sum(ranks) / len(ranks), 2) if ranks else None,
        "top1": sum(1 for x in ranks if x == 1),
        "share_of_voice": sov,
    }


def by_provider(results, brand, competitors):
    out = []
    for p in dict.fromkeys(r["provider"] for r in results):
        rs = [r for r in results if r["provider"] == p]
        s = summarize(rs, brand, competitors)
        s.pop("share_of_voice")
        s["provider"] = p
        s["label"] = LABELS.get(p, p)
        s["model"] = next((r["model"] for r in rs if r.get("model")), "")
        brand_sov = summarize(rs, brand, competitors)["share_of_voice"]
        s["share_of_voice"] = next((x["share"] for x in brand_sov if x["is_brand"]), 0.0)
        out.append(s)
    return out


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def history_row(date, latest):
    s = latest["summary"]
    return {
        "date": date,
        "demo": latest.get("demo", False),
        "answers": s["answers"],
        "visibility": s["visibility"],
        "citation_rate": s["citation_rate"],
        "avg_rank": s["avg_rank"],
        "share_of_voice": next((x["share"] for x in s["share_of_voice"] if x["is_brand"]), 0.0),
        "by_provider": {p["provider"]: p["visibility"] for p in latest["by_provider"]},
    }


def write_outputs(latest, cfg, replace_history=None):
    hist_path = os.path.join(DATA, "history.json")
    hist = load_json(hist_path, {"days": []})
    days = hist.get("days", []) if replace_history is None else replace_history
    if not latest.get("demo"):
        # first real run wipes the demo rows
        days = [d for d in days if not d.get("demo")]
    days = [d for d in days if d["date"] != latest["date"]]
    days.append(history_row(latest["date"], latest))
    days.sort(key=lambda d: d["date"])
    keep = int(cfg.get("settings", {}).get("history_days", 365))
    days = days[-keep:]
    save_json(hist_path, {"demo": any(d.get("demo") for d in days), "brand": latest["brand"]["name"],
                          "days": days})
    save_json(os.path.join(DATA, "latest.json"), latest)


# --------------------------------------------------------------------------- #
# Main run
# --------------------------------------------------------------------------- #
def run(cfg):
    brand, competitors = cfg["brand"], cfg.get("competitors", [])
    prompts = cfg.get("prompts", [])
    st = cfg.get("settings", {})
    timeout = int(st.get("timeout_seconds", 90))
    max_chars = int(st.get("max_answer_chars", 6000))

    active, skipped = [], []
    for name, pcfg in (cfg.get("providers") or {}).items():
        if name not in PROVIDERS:
            skipped.append({"provider": name, "reason": "unknown provider"})
            continue
        if not (pcfg or {}).get("enabled", True):
            skipped.append({"provider": name, "reason": "disabled in config.json"})
            continue
        key = os.environ.get(ENV_KEYS[name], "").strip()
        if not key:
            skipped.append({"provider": name, "reason": f"{ENV_KEYS[name]} not set"})
            continue
        active.append((name, pcfg or {}, key))

    for s in skipped:
        log(f"skip  {s['provider']:<11} ({s['reason']})")
    if not active:
        log("No API keys found. Nothing to track, data left unchanged. "
            "Add OPENAI_API_KEY, PERPLEXITY_API_KEY and/or GEMINI_API_KEY.")
        return 0
    if not prompts:
        log("config.json has no prompts. Nothing to do.")
        return 0

    results = []
    for name, pcfg, key in active:
        log(f"run   {name}: {len(prompts)} prompts")
        for i, prompt in enumerate(prompts, 1):
            row = {"prompt": prompt, "provider": name, "model": pcfg.get("model") or pcfg.get("preset", "")}
            try:
                text, sources, model = PROVIDERS[name](prompt, pcfg, key, timeout)
                row["model"] = model
                row.update(analyze(text, sources, brand, competitors))
                row["answer"] = text[:max_chars]
                row["sources"] = [s["url"] for s in sources][:15]
                row["error"] = None
                flag = "MENTIONED" if row["mentioned"] else "-"
                log(f"      [{i}/{len(prompts)}] {flag:<9} cited={row['cited']}  {prompt[:60]}")
            except Exception as e:  # never crash the whole run
                msg = str(e)
                for _, _, k in active:
                    msg = msg.replace(k, "***")
                row.update({"mentioned": False, "rank": None, "cited": False, "cited_urls": [],
                            "competitors": [], "answer": "", "sources": [], "error": msg[:400]})
                log(f"      [{i}/{len(prompts)}] ERROR {msg[:160]}")
            results.append(row)

    now = datetime.now(timezone.utc)
    latest = {
        "demo": False,
        "generated_at": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "brand": {"name": brand["name"], "aliases": brand.get("aliases", []),
                  "domains": brand.get("domains", [])},
        "competitors": [c["name"] for c in competitors],
        "providers_run": [n for n, _, _ in active],
        "providers_skipped": skipped,
        "summary": summarize(results, brand, competitors),
        "by_provider": by_provider(results, brand, competitors),
        "results": results,
    }
    if latest["summary"]["answers"] == 0:
        log("Every call failed. Data left unchanged so the dashboard keeps the last good run.")
        for r in results[:3]:
            log(f"  {r['provider']}: {r['error']}")
        return 0
    write_outputs(latest, cfg)
    s = latest["summary"]
    log(f"\nVisibility {s['visibility']}%  |  cited {s['citation_rate']}%  |  "
        f"answers {s['answers']}  |  errors {s['errors']}")
    return 0


# --------------------------------------------------------------------------- #
# Demo data (clearly labeled, not real results)
# --------------------------------------------------------------------------- #
def demo(cfg):
    rnd = random.Random(42)
    brand, competitors = cfg["brand"], cfg.get("competitors", [])
    prompts = cfg.get("prompts", [])
    provs = [p for p in (cfg.get("providers") or {}) if p in PROVIDERS]
    today = datetime.now(timezone.utc).date()
    days, latest = [], None
    n_days = 21
    for d in range(n_days):
        date = (today - timedelta(days=n_days - 1 - d)).isoformat()
        base = 0.08 + 0.30 * d / (n_days - 1)
        results = []
        for p in provs:
            bias = {"perplexity": 0.08, "openai": 0.0, "gemini": -0.05}.get(p, 0)
            for prompt in prompts:
                m = rnd.random() < base + bias
                comps = [c["name"] for c in competitors if rnd.random() < 0.45]
                cited = m and rnd.random() < 0.55
                rank = None
                if m:
                    rank = rnd.randint(1, len(comps) + 1)
                results.append({
                    "prompt": prompt, "provider": p,
                    "model": (cfg["providers"][p].get("model") or cfg["providers"][p].get("preset", "")),
                    "mentioned": m, "rank": rank, "brands_in_answer": len(comps) + (1 if m else 0),
                    "cited": cited,
                    "cited_urls": ["https://plutunieer.github.io/x-virality-board/"] if cited else [],
                    "competitors": comps, "sources_total": 0,
                    "answer": "DEMO placeholder. This is not a real AI answer. Real answers "
                              "appear here after the first tracked run with an API key.",
                    "sources": [], "error": None,
                })
        latest = {
            "demo": True,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "date": date,
            "brand": {"name": brand["name"], "aliases": brand.get("aliases", []),
                      "domains": brand.get("domains", [])},
            "competitors": [c["name"] for c in competitors],
            "providers_run": provs,
            "providers_skipped": [],
            "summary": summarize(results, brand, competitors),
            "by_provider": by_provider(results, brand, competitors),
            "results": results,
        }
        days.append(history_row(date, latest))
    save_json(os.path.join(DATA, "history.json"),
              {"demo": True, "brand": brand["name"], "days": days})
    save_json(os.path.join(DATA, "latest.json"), latest)
    log(f"Wrote DEMO data for {n_days} days to data/ (marked \"demo\": true).")
    return 0


def main(argv):
    cfg = load_json(CONFIG_PATH, None)
    if not cfg or "brand" not in cfg:
        log("config.json missing or invalid (needs at least a \"brand\").")
        return 0 if "--soft" in argv else 1
    if "--demo" in argv:
        return demo(cfg)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
