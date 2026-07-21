#!/usr/bin/env python3
"""
Fast Job Notifier Bot
----------------------
Polls multiple ATS platforms (Greenhouse, Lever, Ashby, SmartRecruiters),
scores each posting against your resume profile, and pushes high-match
jobs to a Discord webhook in near real-time.

Usage:
    python job_notifier.py            # loops forever, polling every N seconds
    python job_notifier.py --once     # runs a single pass (good for cron)

Config:
    config.json (copy config.example.json and edit it first)

State:
    seen_jobs.json - tracks job IDs already notified, so you don't get
    duplicate pings. Keep this file around between runs.
"""

import json
import re
import time
import os
import sys
import concurrent.futures
import requests
from datetime import datetime, timezone

CONFIG_PATH = os.environ.get("JOB_BOT_CONFIG", "config.json")
STATE_PATH = os.environ.get("JOB_BOT_STATE", "seen_jobs.json")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------- ATS Fetchers ----------------
# Each fetcher returns a list of jobs normalized to:
# {id, title, company, location, url, description, ats}

def fetch_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": f"gh_{slug}_{j['id']}",
            "title": j.get("title", ""),
            "company": slug,
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "description": strip_html(j.get("content", "")),
            "ats": "greenhouse",
        })
    return jobs


def fetch_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data:
        jobs.append({
            "id": f"lever_{slug}_{j['id']}",
            "title": j.get("text", ""),
            "company": slug,
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl", ""),
            "description": strip_html(j.get("descriptionPlain") or j.get("description") or ""),
            "ats": "lever",
        })
    return jobs


def fetch_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": f"ashby_{slug}_{j['id']}",
            "title": j.get("title", ""),
            "company": slug,
            "location": j.get("location", ""),
            "url": j.get("jobUrl", ""),
            "description": strip_html(j.get("descriptionPlain") or ""),
            "ats": "ashby",
        })
    return jobs


def fetch_smartrecruiters(slug):
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("content", []):
        jobs.append({
            "id": f"sr_{slug}_{j['id']}",
            "title": j.get("name", ""),
            "company": slug,
            "location": (j.get("location") or {}).get("city", ""),
            "url": j.get("applyUrl") or f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
            "description": "",  # full description needs a 2nd request; skipped for speed
            "ats": "smartrecruiters",
        })
    return jobs


def fetch_workday(tenant, host, site, limit=20, max_jobs=100):
    """Workday CXS API. URL pattern: https://{tenant}.{host}.myworkdayjobs.com/{site}
    e.g. https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
      -> tenant='nvidia', host='wd5', site='NVIDIAExternalCareerSite'
    Listing calls don't return full descriptions -- only title, location, and
    short 'bullet' fields -- so scoring here leans more on title text."""
    jobs = []
    offset = 0
    while offset < max_jobs:
        url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        body = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
        r = requests.post(url, json=body, timeout=20)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for p in postings:
            bullets = " ".join(p.get("bulletFields", []) or [])
            path = p.get("externalPath", "")
            jobs.append({
                "id": f"workday_{tenant}_{path}",
                "title": p.get("title", ""),
                "company": tenant,
                "location": p.get("locationsText", ""),
                "url": f"https://{tenant}.{host}.myworkdayjobs.com/{site}{path}",
                "description": bullets,
                "ats": "workday",
            })
        offset += limit
        if offset >= data.get("total", 0):
            break
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    # workday is handled separately in run_once() since it needs
    # (tenant, host, site) instead of a single slug
}


# ---------------- Aggregator APIs ----------------
# These sit on top of thousands of employers each, instead of us tracking
# companies one by one. All are official public endpoints called directly
# via requests - no third-party scraper code, no scraped datasets.

def fetch_remotive(query):
    url = "https://remotive.com/api/remote-jobs"
    r = requests.get(url, params={"search": query}, timeout=20)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": f"remotive_{j['id']}",
            "title": j.get("title", ""),
            "company": j.get("company_name", ""),
            "location": j.get("candidate_required_location", ""),
            "url": j.get("url", ""),
            "description": strip_html(j.get("description", "")),
            "ats": "remotive",
        })
    return jobs


def fetch_remoteok():
    url = "https://remoteok.com/api"
    headers = {"User-Agent": "Mozilla/5.0 (job-notifier-bot; personal use)"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data:
        if not isinstance(j, dict) or "id" not in j or "position" not in j:
            continue  # first element is a legal notice, not a job
        jobs.append({
            "id": f"remoteok_{j['id']}",
            "title": j.get("position", ""),
            "company": j.get("company", ""),
            "location": j.get("location", ""),
            "url": j.get("url", ""),
            "description": strip_html(j.get("description", "")),
            "ats": "remoteok",
        })
    return jobs


def fetch_arbeitnow(pages=2):
    jobs = []
    for page in range(1, pages + 1):
        url = "https://www.arbeitnow.com/api/job-board-api"
        r = requests.get(url, params={"page": page}, timeout=20)
        r.raise_for_status()
        data = r.json()
        for j in data.get("data", []):
            jobs.append({
                "id": f"arbeitnow_{j.get('slug', j.get('title',''))}",
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("location", ""),
                "url": j.get("url", ""),
                "description": strip_html(j.get("description", "")),
                "ats": "arbeitnow",
            })
        if not data.get("links", {}).get("next"):
            break
    return jobs


def fetch_themuse(category, pages=2):
    jobs = []
    for page in range(pages):
        url = "https://www.themuse.com/api/public/jobs"
        r = requests.get(url, params={"page": page, "category": category}, timeout=20)
        r.raise_for_status()
        data = r.json()
        for j in data.get("results", []):
            company = (j.get("company") or {}).get("name", "")
            locations = ", ".join(l.get("name", "") for l in j.get("locations", []))
            jobs.append({
                "id": f"themuse_{j.get('id')}",
                "title": j.get("name", ""),
                "company": company,
                "location": locations,
                "url": (j.get("refs") or {}).get("landing_page", ""),
                "description": strip_html(j.get("contents", "")),
                "ats": "themuse",
            })
    return jobs


def fetch_adzuna(query, app_id, app_key, location="us", results_per_page=50):
    url = f"https://api.adzuna.com/v1/api/jobs/{location}/search/1"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": results_per_page,
        "what": query,
        "content-type": "application/json",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("results", []):
        jobs.append({
            "id": f"adzuna_{j.get('id')}",
            "title": j.get("title", ""),
            "company": (j.get("company") or {}).get("display_name", ""),
            "location": (j.get("location") or {}).get("display_name", ""),
            "url": j.get("redirect_url", ""),
            "description": strip_html(j.get("description", "")),
            "ats": "adzuna",
        })
    return jobs


def fetch_jooble(query, api_key, location="United States", page=1):
    url = f"https://jooble.org/api/{api_key}"
    body = {"keywords": query, "location": location, "page": str(page)}
    r = requests.post(url, json=body, timeout=20)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": f"jooble_{j.get('id')}",
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "location": j.get("location", ""),
            "url": j.get("link", ""),
            "description": strip_html(j.get("snippet", "")),
            "ats": "jooble",
        })
    return jobs


# ---------------- Scoring ----------------

SENIORITY_HIGH = [
    "staff", "principal", "director", " vp ", "vice president",
    "head of", "10+ years", "8+ years", "chief",
]
JUNIOR_MARKERS = ["intern", "internship", "new grad"]

# Broad vocabulary used only to spot GAPS -- skills a posting mentions that
# aren't in your profile. This is deliberately wider than resume_profile
# skills so it can actually surface things worth addressing in your
# application, not just re-confirm what you already listed.
GAP_SKILL_VOCAB = [
    "kubernetes", "terraform", "ansible", "helm", "prometheus", "grafana",
    "spark", "hadoop", "airflow", "kafka", "dbt", "snowflake", "databricks",
    "ray", "graphql", "grpc", "go", "golang", "rust", "scala", "java", "c++",
    "typescript", "react", "node.js", "postgres", "mysql", "redis",
    "elasticsearch", "feature store", "data pipeline", "etl", "distributed systems",
    "computer vision", "speech recognition", "reinforcement learning",
    "time series", "statistics", "a/b testing", "tableau", "looker", "power bi",
    "gcp", "azure", "prompt caching", "multi-agent",
]

# Patterns for "requires N years" style phrasing. The old SENIORITY_HIGH
# list only caught title words like "staff"/"principal" -- it missed a
# posting that says e.g. "7+ years" in a "Senior ML Engineer" title with no
# other seniority keyword. This closes that gap.
YEARS_REQUIRED_PATTERNS = [
    re.compile(r"(\d{1,2})\s*\+\s*years"),
    re.compile(r"(\d{1,2})\s*-\s*\d{1,2}\s*years"),
    re.compile(r"(?:minimum(?: of)?|at least)\s*(\d{1,2})\s*years"),
]


def extract_required_years(text):
    """Returns the lowest 'N years' figure mentioned, or None."""
    candidates = []
    for pattern in YEARS_REQUIRED_PATTERNS:
        for m in pattern.finditer(text):
            candidates.append(int(m.group(1)))
    return min(candidates) if candidates else None


def parse_profile_years(experience_level):
    """'2-4' -> (2, 4). Falls back to (None, None) if unparseable."""
    m = re.match(r"(\d+)\s*-\s*(\d+)", experience_level or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def score_job(job, profile):
    text = f"{job['title']} {job['description']}".lower()
    score = 0
    reasons = []

    matched_role = None
    for role in profile.get("target_roles", []):
        if role.lower() in text:
            score += 30
            matched_role = role
            reasons.append(f"Role match: {role}")
            break

    skill_hits = [s for s in profile.get("skills", []) if s.lower() in text]
    if skill_hits:
        score += min(20, 5 * len(skill_hits))
        reasons.append(f"Skills: {', '.join(skill_hits[:5])}")

    kw_hits = [k for k in profile.get("keywords", []) if k.lower() in text]
    if kw_hits:
        score += min(15, 5 * len(kw_hits))
        reasons.append(f"Keywords: {', '.join(kw_hits[:5])}")

    if any(s in text for s in SENIORITY_HIGH):
        score -= 30
        reasons.append("Seniority looks too high for your profile")

    if profile.get("experience_level") not in ("0-2", "intern") and any(s in text for s in JUNIOR_MARKERS):
        score -= 15
        reasons.append("Looks like an intern / new-grad role")

    required_years = extract_required_years(text)
    _, profile_max_years = parse_profile_years(profile.get("experience_level", ""))
    if required_years is not None and profile_max_years is not None and required_years > profile_max_years + 1:
        score -= 20
        reasons.append(f"Posting asks for {required_years}+ years — above your {profile.get('experience_level')} profile")

    # -- gap analysis: what the posting wants that isn't in your skill list --
    profile_skills_lower = {s.lower() for s in profile.get("skills", [])}
    gap_skills = list(dict.fromkeys(
        g for g in GAP_SKILL_VOCAB
        if g in text and g not in profile_skills_lower
    ))[:5]

    # -- best matching resume highlight to emphasize in your application --
    all_matched_terms = {s.lower() for s in skill_hits} | {k.lower() for k in kw_hits}
    best_highlight = None
    best_overlap = 0
    for h in profile.get("experience_highlights", []):
        h_tags = {t.lower() for t in h.get("skills", [])}
        overlap = len(h_tags & all_matched_terms)
        if overlap > best_overlap:
            best_overlap = overlap
            best_highlight = h.get("bullet")

    # -- match tier label --
    if score >= 80:
        tier = "🔥 Strong Match"
    elif score >= 65:
        tier = "✅ Good Match"
    else:
        tier = "👀 Worth a Look"

    return {
        "score": score,
        "tier": tier,
        "matched_role": matched_role,
        "matched_skills": skill_hits,
        "matched_keywords": kw_hits,
        "gap_skills": gap_skills,
        "highlight": best_highlight,
        "reasons": reasons,
    }


# ---------------- Discord ----------------

def notify_discord(webhook_url, job, result, priority=False):
    score = result["score"]
    title_prefix = "🔥 PRIORITY — " if priority else ""
    lines = [f"**{result['tier']} — {score} pts**"]

    strong_points = []
    if result["matched_role"]:
        strong_points.append(f"Role: {result['matched_role']}")
    if result["matched_skills"]:
        strong_points.append(f"Skills: {', '.join(result['matched_skills'][:6])}")
    if result["matched_keywords"]:
        strong_points.append(f"Keywords: {', '.join(result['matched_keywords'][:6])}")

    fields = [
        {"name": "Location", "value": job.get("location") or "N/A", "inline": True},
        {"name": "Source", "value": job["ats"], "inline": True},
    ]
    if strong_points:
        fields.append({
            "name": "✅ Why you're a strong fit",
            "value": "\n".join(f"• {p}" for p in strong_points),
            "inline": False,
        })
    if result["highlight"]:
        fields.append({
            "name": "📌 Emphasize in your application",
            "value": result["highlight"],
            "inline": False,
        })
    if result["gap_skills"]:
        fields.append({
            "name": "🧩 This posting also wants (consider addressing)",
            "value": ", ".join(result["gap_skills"]),
            "inline": False,
        })
    penalty_notes = [r for r in result["reasons"] if "too high" in r or "new-grad" in r or "above your" in r]
    if penalty_notes:
        fields.append({
            "name": "⚠️ Worth double-checking",
            "value": "\n".join(f"• {p}" for p in penalty_notes),
            "inline": False,
        })

    embed = {
        "title": f"{title_prefix}{job['title']} — {job['company']}",
        "url": job["url"],
        "description": "\n".join(lines),
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = {"embeds": [embed]}
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code >= 300:
            print(f"[discord] failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[discord] error: {e}")


# ---------------- Main loop ----------------


def run_once(config, state):
    profile = config["resume_profile"]
    threshold = config.get("score_threshold", 60)
    webhook_url = config["discord_webhook_url"]
    priority_webhook = config.get("discord_webhook_url_priority")
    priority_threshold = config.get("priority_score_threshold", 80)
    companies = config.get("companies", [])
    workday_companies = config.get("workday_companies", [])
    agg = config.get("aggregators", {})

    all_jobs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as ex:
        futures = {}  # future -> label, for error reporting

        # -- per-company ATS boards (slug-based) --
        for c in companies:
            fetcher = FETCHERS.get(c["ats"])
            if not fetcher:
                print(f"[skip] unknown ats '{c['ats']}' for {c.get('slug')}")
                continue
            futures[ex.submit(fetcher, c["slug"])] = f"{c['ats']}/{c['slug']}"

        # -- Workday companies (tenant/host/site-based) --
        for c in workday_companies:
            futures[ex.submit(fetch_workday, c["tenant"], c["host"], c["site"])] = f"workday/{c['tenant']}"

        # -- aggregator APIs (each covers thousands of employers) --
        remotive_cfg = agg.get("remotive", {})
        if remotive_cfg.get("enabled"):
            for q in remotive_cfg.get("queries", []):
                futures[ex.submit(fetch_remotive, q)] = f"remotive/{q}"

        remoteok_cfg = agg.get("remoteok", {})
        if remoteok_cfg.get("enabled"):
            futures[ex.submit(fetch_remoteok)] = "remoteok"

        arbeitnow_cfg = agg.get("arbeitnow", {})
        if arbeitnow_cfg.get("enabled"):
            futures[ex.submit(fetch_arbeitnow, arbeitnow_cfg.get("pages", 2))] = "arbeitnow"

        themuse_cfg = agg.get("themuse", {})
        if themuse_cfg.get("enabled"):
            for cat in themuse_cfg.get("categories", []):
                futures[ex.submit(fetch_themuse, cat, themuse_cfg.get("pages", 2))] = f"themuse/{cat}"

        adzuna_cfg = agg.get("adzuna", {})
        if adzuna_cfg.get("enabled") and adzuna_cfg.get("app_id") and adzuna_cfg.get("app_key"):
            for q in adzuna_cfg.get("queries", []):
                futures[ex.submit(
                    fetch_adzuna, q, adzuna_cfg["app_id"], adzuna_cfg["app_key"],
                    adzuna_cfg.get("location", "us"), adzuna_cfg.get("results_per_page", 50),
                )] = f"adzuna/{q}"

        jooble_cfg = agg.get("jooble", {})
        if jooble_cfg.get("enabled") and jooble_cfg.get("api_key"):
            for q in jooble_cfg.get("queries", []):
                futures[ex.submit(
                    fetch_jooble, q, jooble_cfg["api_key"], jooble_cfg.get("location", "United States"),
                )] = f"jooble/{q}"

        for fut, label in futures.items():
            try:
                all_jobs.extend(fut.result())
            except Exception as e:
                print(f"[{label}] fetch error: {e}")

    new_count = 0
    notified = 0
    cross_source_dupes = 0
    for job in all_jobs:
        if job["id"] in state["ids"]:
            continue
        state["ids"].add(job["id"])
        new_count += 1

        # Cross-source dedup: the same real job often appears via both a
        # direct ATS board AND an aggregator (different IDs, same posting).
        # This catches that so you don't get double-pinged for one job.
        fingerprint = "{}::{}".format(
            job.get("company", "").strip().lower(),
            re.sub(r"[^a-z0-9]+", "", job.get("title", "").lower()),
        )
        if fingerprint in state["fingerprints"]:
            cross_source_dupes += 1
            continue
        state["fingerprints"].add(fingerprint)

        result = score_job(job, profile)
        if result["score"] >= threshold:
            notify_discord(webhook_url, job, result)
            notified += 1
            print(f"[NOTIFY] score={result['score']} {job['title']} @ {job['company']}")
            if priority_webhook and result["score"] >= priority_threshold:
                notify_discord(priority_webhook, job, result, priority=True)

    print(f"{datetime.now().isoformat()} — checked {len(all_jobs)} jobs, "
          f"{new_count} new, {cross_source_dupes} cross-source dupes skipped, "
          f"{notified} notified")


def main():
    config = load_json(CONFIG_PATH, None)
    if config is None:
        print(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and edit it first.")
        sys.exit(1)

    # Allow overriding the webhook via env var so the real URL never has to
    # be committed to a (possibly public) repo. Set DISCORD_WEBHOOK_URL as
    # a GitHub Actions secret.
    env_webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if env_webhook:
        config["discord_webhook_url"] = env_webhook

    if not config.get("discord_webhook_url") or "PASTE_YOUR" in config["discord_webhook_url"]:
        print("No Discord webhook URL set (config.json or DISCORD_WEBHOOK_URL env var).")
        sys.exit(1)

    # Optional second webhook for a curated "priority" channel -- only
    # scores >= priority_score_threshold get cross-posted here, so you can
    # set different Discord notification settings per channel (e.g. mobile
    # push only for priority, muted for the main firehose channel).
    env_priority_webhook = os.environ.get("DISCORD_WEBHOOK_URL_PRIORITY")
    if env_priority_webhook:
        config["discord_webhook_url_priority"] = env_priority_webhook

    # Same pattern for the aggregator API keys -- set as GitHub Actions
    # secrets (ADZUNA_APP_ID, ADZUNA_APP_KEY, JOOBLE_API_KEY) so they never
    # need to sit in plaintext in config.json. Presence of the env vars
    # auto-enables that source, no need to also flip "enabled" in config.
    config.setdefault("aggregators", {})

    adzuna_id = os.environ.get("ADZUNA_APP_ID")
    adzuna_key = os.environ.get("ADZUNA_APP_KEY")
    if adzuna_id and adzuna_key:
        config["aggregators"].setdefault("adzuna", {})
        config["aggregators"]["adzuna"]["app_id"] = adzuna_id
        config["aggregators"]["adzuna"]["app_key"] = adzuna_key
        config["aggregators"]["adzuna"]["enabled"] = True

    jooble_key = os.environ.get("JOOBLE_API_KEY")
    if jooble_key:
        config["aggregators"].setdefault("jooble", {})
        config["aggregators"]["jooble"]["api_key"] = jooble_key
        config["aggregators"]["jooble"]["enabled"] = True

    # State tracks both raw job IDs (exact re-fetch dedup) and cross-source
    # fingerprints (company+title, catches the same real job appearing via
    # both a direct ATS and an aggregator, which would otherwise double-alert).
    raw_state = load_json(STATE_PATH, {"ids": [], "fingerprints": []})
    if isinstance(raw_state, list):  # backward compat with the old list-only format
        raw_state = {"ids": raw_state, "fingerprints": []}
    state = {
        "ids": set(raw_state.get("ids", [])),
        "fingerprints": set(raw_state.get("fingerprints", [])),
    }

    interval = config.get("poll_interval_seconds", 300)
    loop_forever = "--once" not in sys.argv

    while True:
        run_once(config, state)
        save_json(STATE_PATH, {
            "ids": sorted(state["ids"]),
            "fingerprints": sorted(state["fingerprints"]),
        })
        if not loop_forever:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
