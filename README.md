# Fast Job Notifier Bot — Complete Setup Guide

All 5 major ATS platforms + 6 aggregator APIs, resume-aware scoring with
detailed per-job insights, a priority channel for your best matches, and
free 24/7 hosting.

---

## How effective is this, honestly?

**What's genuinely strong:**
- **Speed.** Once running, alerts land within ~10 minutes of a posting
  appearing on a tracked source. For popular employers (AI labs especially)
  that get hundreds of applicants in the first day, being in the first
  hour instead of the first day is a real, meaningful edge. This is the
  single biggest advantage this bot gives you over manually checking boards.
- **Breadth.** Once Adzuna + Jooble keys are in, you're drawing from
  genuinely thousands of US employers, not a hand-picked few dozen.
- **Actionable insight, not just noise.** Each alert tells you what
  matched, what to emphasize, and what's missing — closer to a first-pass
  recruiter read than a plain keyword ping.

**What's real but limited — rule-based matching isn't semantic understanding:**
- It matches on substrings. "GenAI Engineer" won't match a posting titled
  "Gen AI Engineer" (with a space) or "AI/ML Engineer" in some phrasings.
  This means occasional **false negatives** — real matches it'll miss due
  to wording, not because you're not a fit.
- It can over-score a posting that mentions a skill once in a long
  "nice to have" list the same as one where it's a hard requirement — it
  can't yet tell criticality apart.
- Workday listings only expose title + short bullet points, not full
  descriptions, so Workday scoring leans more on title text than the ATS
  sources with full descriptions (Greenhouse/Lever/Ashby).
- No salary or hard location filtering yet — you'll see onsite roles in
  cities you may not want, alongside remote ones.

**What I just fixed, since you asked "what's the best we can do":**
1. **Experience-range mismatches** — previously only caught title words
   like "staff"/"principal". Now also parses explicit "7+ years" /
   "5-8 years" phrasing and flags it against your profile, even when the
   title itself looks junior/mid-level.
2. **Cross-source duplicate alerts** — the same real job often appears via
   both a direct ATS board *and* an aggregator (different IDs, same
   posting). The bot now fingerprints on company+title and skips the
   second alert for a job you've already seen.
3. **Priority channel** — a second webhook that only fires for your best
   matches (score >= 80 by default), so you can set your phone to buzz for
   that channel specifically. See setup below.
4. **The GitHub Actions minutes issue** — see next section. This was a
   real risk of the bot silently going quiet mid-month; now addressed.

**Further improvements worth doing, ranked by leverage:**
1. *(High, needs your input)* Add explicit `location_preferences` (e.g.
   "remote", "NYC", "willing to relocate") and score against it — cuts
   down on alerts for onsite roles you can't take.
2. *(Medium)* Add salary floor filtering via Adzuna's `salary_min` param,
   if you have a number in mind.
3. *(Medium)* Loosen title matching with light fuzzy/regex normalization
   (strip spaces/hyphens before comparing) to catch the "Gen AI" vs
   "GenAI" style misses.
4. *(Lower, bigger lift)* An optional LLM-based second-pass scorer for
   only the borderline 50-70 range, to catch nuance the rule-based system
   can't -- this would need an API key and adds latency/cost, so it's
   worth doing only if you find the rule-based filter too noisy.

---

## Important: GitHub Actions minutes

At a 10-minute polling interval, this workflow uses about 4,320 minutes/
month. GitHub's free tier gives 2,000 minutes/month for **private** repos
-- so on a private repo, this would run fine for roughly the first 2 weeks
of the month, then silently stop until the next billing cycle. **Public**
repos get unlimited free Actions minutes.

**Recommended: make the repo public.** Nothing in the tracked files is
personal -- `config.json` only has role/skill/experience data, no name,
email, or phone. Your webhook URLs and API keys stay fully protected as
GitHub Secrets regardless of repo visibility; they're never written into
any file or exposed in logs.

If you'd rather keep it private, change the cron in
`.github/workflows/job-notifier.yml` to every 25-30 minutes instead of 10
(`*/25 * * * *`) to stay under the free quota -- still far faster than
checking manually, just not quite as fast as public+10min.

---

## Part 0 -- What each alert looks like

- **Match tier** -- Strong Match / Good Match / Worth a Look
- **Why you're a strong fit** -- matched role/skills/keywords
- **Emphasize in your application** -- the single most relevant bullet
  from your actual Klarna/Alloy/OLX experience for *this* posting
- **This posting also wants** -- skills mentioned that aren't in your
  profile, worth addressing or upskilling on
- **Worth double-checking** -- seniority/experience-range mismatches

---

## Part 1 -- GitHub setup (10 min)

1. Free account at https://github.com/signup if you don't have one.
2. **+** (top right) -> **New repository** -> name it (e.g. `job-notifier`)
   -> set **Public** (see minutes note above) -> **Create repository**.
3. **Add file -> Upload files**, drag in everything from this folder,
   keeping the `.github/workflows/job-notifier.yml` path intact.
4. Commit.

Or with git:
```bash
git init
git add .
git commit -m "Initial job notifier setup"
git branch -M main
git remote add origin https://github.com/<you>/job-notifier.git
git push -u origin main
```

## Part 2 -- Discord webhooks (5 min)

**Main channel:**
1. Discord -> your server (or **+** -> **Create My Own** for a free new one).
2. Create/pick a channel (e.g. `#job-alerts`).
3. Channel settings (gear) -> **Integrations** -> **Webhooks** ->
   **New Webhook** -> **Copy Webhook URL**.

**Priority channel (optional but recommended):**
4. Create a second channel (e.g. `#job-alerts-priority`).
5. Same steps -> **New Webhook** -> **Copy Webhook URL**.
6. Right-click that channel -> **Notification Settings** -> **All Messages**
   (so only your best matches actually buzz your phone). Set the main
   channel to **Mentions Only** or mute it if you don't want the full
   firehose pinging you.

## Part 3 -- Adzuna key (2 min)
1. https://developer.adzuna.com/ -> **Register** (no card).
2. Copy your **Application ID** and **Application Key**.

## Part 4 -- Jooble key (1 min)
1. https://jooble.org/api/about -> fill the short form.
2. Copy your **API key**.

## Part 5 -- Add secrets to GitHub (5 min)

**Settings -> Secrets and variables -> Actions -> New repository secret**,
one at a time:

| Secret name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | main channel webhook (Part 2) |
| `DISCORD_WEBHOOK_URL_PRIORITY` | priority channel webhook (Part 2) -- optional |
| `ADZUNA_APP_ID` | from Part 3 |
| `ADZUNA_APP_KEY` | from Part 3 |
| `JOOBLE_API_KEY` | from Part 4 |

## Part 6 -- Write permissions (1 min)
**Settings -> Actions -> General -> Workflow permissions** -> **"Read and
write permissions"** -> **Save**.

## Part 7 -- Run it (1 min)
**Actions** tab -> **job-notifier** -> **Run workflow** -> **Run workflow**.
Check the run log, check Discord. After this, it repeats automatically.

## Part 8 -- Tuning
- Too many alerts -> raise `score_threshold` (currently 55).
- Too few -> lower it, or add more companies/queries.
- Priority channel threshold -> `priority_score_threshold` (currently 80).

---

## Adding more companies

**Greenhouse/Lever/Ashby/SmartRecruiters** -- slug = usually lowercase
company name. Test directly:
```
https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
https://api.lever.co/v0/postings/{slug}?mode=json
https://api.ashbyhq.com/posting-api/job-board/{slug}
https://api.smartrecruiters.com/v1/companies/{slug}/postings
```

**Workday** -- no predictable slug. From a careers URL like
`https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`, read off
`tenant=nvidia`, `host=wd5`, `site=NVIDIAExternalCareerSite` and add to
`workday_companies`.

## Run it locally instead of GitHub Actions (optional)
```bash
pip install requests
python job_notifier.py            # loops forever
python job_notifier.py --once     # single pass
```

## What I didn't do, on purpose
While researching wider coverage, I found open-source "10,000+ company"
scrapers and paid HR-contact databases. Neither is in this bot -- running
unaudited third-party code against your webhook is a real risk, and
contact-list products are sales tools, not job-search tools. Everything
here is a direct call to a documented, official public API.
