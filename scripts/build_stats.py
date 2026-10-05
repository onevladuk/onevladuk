# -*- coding: utf-8 -*-
"""
Build the dashboard-style profile SVG cards from live GitHub data.

Cards (each in a dark and a light variant, picked by README <picture>):
  hero      — "> keep building_" intro + facts
  stats     — contributions / commits / PRs / issues, last 12 months vs. the 12 before
  languages — languages by commit, all owned repos (+ the company repo)
  activity  — recent activity + quote card

Data source: GitHub REST + GraphQL via urllib (no third-party deps).
Auth: token from env GH_TOKEN or GITHUB_TOKEN (needs `repo` scope).
Optional env PROFILE_PRIVATE (JSON, kept as a repo secret, never committed):
  {"company": {"repo": "...", "authors": [...], "language": "...", "fallback": N},
   "org_labels": {"<org>": "<label shown instead of repo names>"}}

Run locally:  GH_TOKEN=$(gh auth token) python scripts/build_stats.py
In CI:        see .github/workflows/profile-stats.yml

Copy (intro, facts, quote) lives in scripts/profile.json.
Private repo NAMES are never written into the SVGs — only public ones.
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(ROOT, "assets")
OWNER = "onevladuk"

TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("ERROR: set GH_TOKEN (or GITHUB_TOKEN) with repo scope.")
HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "onevladuk-profile-stats",
}
PRIVATE = json.loads(os.environ.get("PROFILE_PRIVATE") or "{}")

# ----------------------------------------------------------------------------
# API helpers
# ----------------------------------------------------------------------------
def _get(url, data=None):
    for attempt in range(4):
        try:
            return urlopen(Request(url, data=data, headers=HEADERS), timeout=30)
        except HTTPError as e:
            rate_limited = e.code == 429 or (
                e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0")
            if rate_limited:
                if attempt < 3:
                    time.sleep(2 ** attempt * 3)
                    continue
                return None
            if e.code in (403, 404, 409, 422, 451):   # forbidden/missing/empty/invalid -> skip gracefully
                return None
            raise                                      # 401 (bad token) etc. -> fail loudly
        except URLError:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    return None


def get_json(url):
    r = _get(url)
    return json.load(r) if r else None


def graphql(query, **variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    r = _get("https://api.github.com/graphql", data=body)
    d = json.load(r) if r else {}
    if d.get("errors"):
        raise RuntimeError(d["errors"])
    return d.get("data")


def search_count(kind, q):
    d = get_json(f"https://api.github.com/search/{kind}?q={q}&per_page=1")
    return d["total_count"] if d else 0


def count_commits(repo, author):
    """Commits on the default branch authored by `author` (cheap: read Link header)."""
    url = f"https://api.github.com/repos/{repo}/commits?author={author}&per_page=1"
    r = _get(url)
    if not r:
        return 0
    link = r.headers.get("Link", "") or ""
    m = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link)
    if m:
        return int(m.group(1))
    try:
        return len(json.load(r))
    except Exception:
        return 0


def list_owned_repos():
    repos, page = [], 1
    while True:
        data = get_json(f"https://api.github.com/user/repos?affiliation=owner&per_page=100&page={page}")
        if not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
    return [r for r in repos if r["owner"]["login"].lower() == OWNER.lower()]


def primary_language(repo_full):
    d = get_json(f"https://api.github.com/repos/{repo_full}/languages")
    if not d:
        return None
    return max(d, key=d.get)


# ----------------------------------------------------------------------------
# Gather
# ----------------------------------------------------------------------------
def gather_totals(now):
    """Last 12 months vs. the 12 months before: contributions, commits, PRs, issues."""
    cur0, prev0 = now - timedelta(days=365), now - timedelta(days=730)
    q = """query($login:String!,$a:DateTime!,$b:DateTime!,$c:DateTime!){
      user(login:$login){
        cur: contributionsCollection(from:$b,to:$c){ contributionCalendar{ totalContributions } }
        prev: contributionsCollection(from:$a,to:$b){ contributionCalendar{ totalContributions } }
      }}"""
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    u = graphql(q, login=OWNER, a=iso(prev0), b=iso(cur0), c=iso(now))["user"]
    day = lambda d: d.strftime("%Y-%m-%d")
    cur_rng, prev_rng = f"{day(cur0)}..{day(now)}", f"{day(prev0)}..{day(cur0 - timedelta(days=1))}"
    out = {"contributions": (u["cur"]["contributionCalendar"]["totalContributions"],
                             u["prev"]["contributionCalendar"]["totalContributions"])}
    out["commits"] = tuple(search_count("commits", f"author:{OWNER}+author-date:{r}") for r in (cur_rng, prev_rng))
    out["prs"] = tuple(search_count("issues", f"author:{OWNER}+type:pr+created:{r}") for r in (cur_rng, prev_rng))
    out["issues"] = tuple(search_count("issues", f"author:{OWNER}+type:issue+created:{r}") for r in (cur_rng, prev_rng))
    for k, v in out.items():
        print(f"  {k}: {v[0]} (prev {v[1]})")
    return out


def gather_languages(cfg):
    """Commits per primary language across owned repos + the company repo."""
    exclude = set(cfg["exclude"])
    lang_commits, repo_lang = {}, {}
    for r in list_owned_repos():
        name = r["name"]
        lang = primary_language(f"{OWNER}/{name}") or "Other"
        repo_lang[f"{OWNER}/{name}"] = lang
        if name in exclude:
            continue
        commits = count_commits(f"{OWNER}/{name}", OWNER)
        if commits > 0:
            lang_commits[lang] = lang_commits.get(lang, 0) + commits
    # Actions logs are public once the repo is: print aggregates only, never repo names.
    print(f"  {len(repo_lang)} repos, " + ", ".join(f"{k} {v}" for k, v in sorted(lang_commits.items(), key=lambda kv: -kv[1])))

    comp = PRIVATE.get("company")
    if comp:
        n = sum(count_commits(comp["repo"], a) for a in comp["authors"])
        if n == 0 and comp.get("fallback"):
            n = comp["fallback"]
            print(f"  company repo inaccessible -> fallback {n}")
        else:
            print(f"  company repo: {n}")
        lang_commits[comp["language"]] = lang_commits.get(comp["language"], 0) + n
    return lang_commits, repo_lang


ZERO_SHA = "0" * 40


def gather_activity(cfg, repo_lang, now, limit=5):
    """Latest event of each kind, newest first. Private repos are shown by label only."""
    events = get_json(f"https://api.github.com/users/{OWNER}/events?per_page=100") or []
    events.sort(key=lambda e: e["created_at"], reverse=True)
    exclude = {f"{OWNER}/{n}" for n in cfg["exclude"]}
    org_labels = PRIVATE.get("org_labels", {})

    def label(repo):
        owner = repo.split("/")[0]
        if owner.lower() == OWNER.lower():
            lang = repo_lang.get(repo) or primary_language(repo)
            return f"Private repo · {lang}" if lang else "Private repo"
        return org_labels.get(owner, "Team repository")

    rows, seen = [], set()
    for e in events:
        repo, p, public = e["repo"]["name"], e["payload"], e.get("public", False)
        if repo in exclude:
            continue
        t, action = e["type"], p.get("action")
        detail = None
        if t == "PushEvent":
            kind, ico, col = "push", "repo-push", "green"
            if p.get("before") == ZERO_SHA:
                title = "Pushed a new branch"
            else:
                cmp = get_json(f"https://api.github.com/repos/{repo}/compare/{p['before']}...{p['head']}")
                n = cmp["total_commits"] if cmp else 0
                title = f"Pushed {n} commit{'s' if n != 1 else ''}" if n else "Pushed commits"
                if public and cmp and cmp.get("commits"):
                    detail = cmp["commits"][-1]["commit"]["message"].splitlines()[0]
        elif t == "PullRequestEvent" and (action == "merged" or (action == "closed" and p.get("pull_request", {}).get("merged"))):
            kind, ico, col, title = "pr_merged", "git-merge", "purple", "Merged a pull request"
        elif t == "PullRequestEvent" and action == "opened":
            kind, ico, col, title = "pr_opened", "git-pull-request", "green", "Opened a pull request"
        elif t == "IssuesEvent" and action == "opened":
            kind, ico, col, title = "issue_opened", "issue-opened", "red", "Opened an issue"
        elif t == "IssuesEvent" and action == "closed":
            kind, ico, col, title = "issue_closed", "issue-closed", "purple", "Closed an issue"
        elif t == "IssueCommentEvent":
            on_pr = bool(p.get("issue", {}).get("pull_request"))
            kind, ico, col = "comment", "comment", "blue"
            title = f"Commented on {'a pull request' if on_pr else 'an issue'}"
        elif t == "PullRequestReviewEvent":
            kind, ico, col, title = "review", "code-review", "green", "Reviewed a pull request"
        elif t == "CreateEvent" and p.get("ref_type") == "repository":
            kind, ico, col, title = "repo", "repo", "blue", "Created a repository"
        elif t == "WatchEvent":
            kind, ico, col, title = "star", "star", "yellow", "Starred a repository"
        elif t == "ForkEvent":
            kind, ico, col, title = "fork", "repo-forked", "blue", "Forked a repository"
        elif t == "ReleaseEvent" and action == "published":
            kind, ico, col, title = "release", "tag", "green", "Published a release"
        else:
            continue
        if kind in seen:
            continue
        seen.add(kind)
        if public and detail is None:
            if t in ("PullRequestEvent", "PullRequestReviewEvent"):
                pr = get_json(f"https://api.github.com/repos/{repo}/pulls/{p.get('number') or p['pull_request']['number']}")
                detail = pr and pr.get("title")
            elif t in ("IssuesEvent", "IssueCommentEvent"):
                detail = p.get("issue", {}).get("title")
            else:
                info = get_json(f"https://api.github.com/repos/{repo}")
                detail = info and info.get("description")
        when = datetime.fromisoformat(e["created_at"].replace("Z", "+00:00"))
        rows.append({"icon": ico, "color": col, "title": title,
                     "repo": repo if public else None,
                     "detail": (detail or repo) if public else label(repo),
                     "when": ago(now, when)})
        print(f"  activity: {title} · {repo if public else label(repo)}")
        if len(rows) >= limit:
            break
    return rows


def ago(now, then):
    d = (now.date() - then.date()).days
    if d <= 0:
        return "today"
    if d == 1:
        return "yesterday"
    if d < 7:
        return f"{d} days ago"
    if d < 30:
        return "1 week ago" if d < 14 else f"{d // 7} weeks ago"
    return "1 month ago" if d < 60 else f"{d // 30} months ago"


# ----------------------------------------------------------------------------
# Drawing — GitHub Primer look, one layout, two palettes
# ----------------------------------------------------------------------------
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"
W, PAD, GAP = 880, 24, 16

PAL = {
    "dark": {"bg": "#0d1117", "border": "#30363d", "fg": "#e6edf3", "text": "#c9d1d9",
             "muted": "#8b949e", "track": "#21262d",
             "green": "#3fb950", "blue": "#4493f8", "purple": "#ab7df8", "red": "#f85149", "yellow": "#d29922",
             # quote-card landscape
             "sky0": "#0d1117", "sky1": "#141c26", "glow": "#3b4c60", "far": "#1a2330",
             "mid": "#121922", "near": "#06090d"},
    "light": {"bg": "#ffffff", "border": "#d1d9e0", "fg": "#1f2328", "text": "#31363c",
              "muted": "#59636e", "track": "#eff2f5",
              "green": "#1a7f37", "blue": "#0969da", "purple": "#8250df", "red": "#cf222e", "yellow": "#9a6700",
              "sky0": "#ffffff", "sky1": "#f1f5f9", "glow": "#dfe7ef", "far": "#cbd4dd",
              "mid": "#a7b2be", "near": "#58626d"},
}
LANG_COLOR = {"Python": "#3572A5", "JavaScript": "#f1e05a", "1C/BSL": "#814CCC", "1C Enterprise": "#814CCC",
              "TypeScript": "#3178c6", "C": "#8a94a3", "C++": "#f34b7d", "Astro": "#ff5a03", "HTML": "#e34c26",
              "Rust": "#dea584", "Go": "#00ADD8", "PLpgSQL": "#336790", "CSS": "#663399", "C#": "#178600",
              "Svelte": "#ff3e00", "Vue": "#41b883", "Shell": "#89e051", "PowerShell": "#5391fe",
              "Other": "#8b949e"}

NOT_STACK = {"Other", "HTML", "CSS", "SCSS", "Astro", "Svelte", "Vue", "Dockerfile", "Makefile", "Batchfile"}

with open(os.path.join(HERE, "octicons.json"), encoding="utf-8") as _f:
    OCTICONS = json.load(_f)   # subset of @primer/octicons (MIT), see scripts/octicons.json


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def text_w(s, size, mono=False):
    """Rough rendered width; system fonts differ, so layouts keep ~10% slack."""
    if mono:
        return len(s) * size * 0.6
    em = 0.0
    for ch in s:
        if ch in "iljI.,:;'|!·":
            em += 0.27
        elif ch in "frt()[]-/ ":
            em += 0.34
        elif ch in "mwMW@%":
            em += 0.86
        elif ch.isupper() or ch.isdigit():
            em += 0.62
        else:
            em += 0.53
    return em * size


def fit(s, size, max_w):
    if text_w(s, size) <= max_w:
        return s
    while s and text_w(s + "…", size) > max_w:
        s = s[:-1]
    return s.rstrip() + "…"


def wrap(s, size, max_w):
    lines, cur = [], ""
    for word in s.split():
        cand = f"{cur} {word}".strip()
        if cur and text_w(cand, size) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    return lines + ([cur] if cur else [])


def svg(h, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" viewBox="0 0 {W} {h}" '
            f'fill="none" role="img"><title>{esc(title)}</title>{body}</svg>')


def card(p, x, y, w, h, fill=None):
    return (f'<rect x="{x + .5}" y="{y + .5}" width="{w - 1}" height="{h - 1}" rx="6" '
            f'fill="{fill or p["bg"]}" stroke="{p["border"]}"/>')


def text(x, y, s, size, color, weight=400, anchor="start", family=SANS, extra=""):
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    wt = f' font-weight="{weight}"' if weight != 400 else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}"{wt}{a} '
            f'fill="{color}"{extra}>{s}</text>')


def icon(name, x, y, size, color, src=16):
    ic = OCTICONS[f"{name}/{src}"]
    paths = "".join(f'<path d="{d}"/>' for d in ic["d"])
    return f'<g transform="translate({x:.1f} {y:.1f}) scale({size / src:.4f})" fill="{color}">{paths}</g>'


def card_hero(p, cfg, stack):
    hero = cfg["hero"]
    div_x = 532
    lines = [part for line in hero["lines"] for part in wrap(line, 14.5, div_x - 2 * PAD - 8)]
    h = max(136, 80 + 24 * (len(lines) - 1) + 32)
    b = [card(p, 0, 0, W, h)]
    # left: prompt with a blinking cursor + intro
    cursor = (f'<tspan fill="{p["green"]}">_<animate attributeName="fill-opacity" values="1;0" '
              f'keyTimes="0;0.5" calcMode="discrete" dur="1.1s" repeatCount="indefinite"/></tspan>')
    b.append(text(PAD, 46, f'&gt; {esc(hero["prompt"])}{cursor}', 16, p["green"], family=MONO))
    for i, part in enumerate(lines):
        b.append(text(PAD, 80 + 24 * i, esc(part), 14.5, p["text"]))
    # right: facts
    b.append(f'<line x1="{div_x}" y1="{PAD}" x2="{div_x}" y2="{h - PAD}" stroke="{p["border"]}"/>')
    fx, step = div_x + 24, 28
    max_w = W - PAD - (fx + 24)
    facts = hero["facts"]
    for i, f in enumerate(facts):
        cy = h / 2 + (i - (len(facts) - 1) / 2) * step
        s = f["text"]
        if "{stack}" in s:
            s = fit_stack(s, stack, 13.5, max_w)
        col = p["green"] if f.get("accent") else p["muted"]
        b.append(icon(f["icon"], fx, cy - 8, 16, col))
        b.append(text(fx + 24, cy + 4.5, esc(fit(s, 13.5, max_w)), 13.5, p["text"]))
    return svg(h, "".join(b), "keep building — about me")


def fit_stack(template, stack, size, max_w):
    """Fill {stack} with as many top languages as fit on one line."""
    for n in range(min(7, len(stack)), 0, -1):
        s = template.replace("{stack}", ", ".join(stack[:n]))
        if text_w(s, size) <= max_w:
            return s
    return template.replace("{stack}", stack[0] if stack else "")


def delta(cur, prev, long):
    suffix = " vs. last year" if long else ""
    if prev == 0:
        return ("new this year", "green") if cur else ("—", "muted")
    r = cur / prev
    if r >= 10:
        return f"↑ {r:.0f}×{suffix}", "green"
    if r >= 2:
        return f"↑ {r:.1f}×{suffix}", "green"
    pct = round(abs(r - 1) * 100)
    if cur >= prev:
        return f"↑ {pct}%{suffix}", "green"
    return f"↓ {pct}%{suffix}", "muted"


def card_stats(p, totals):
    h = 108
    tiles = [("person", "Total contributions", totals["contributions"]),
             ("git-commit", "Commits", totals["commits"]),
             ("git-pull-request", "Pull requests", totals["prs"]),
             ("issue-opened", "Issues", totals["issues"])]
    tw = (W - GAP * (len(tiles) - 1)) / len(tiles)
    b = []
    for i, (ico, label, (cur, prev)) in enumerate(tiles):
        x = i * (tw + GAP)
        b.append(card(p, x, 0, tw, h))
        b.append(icon(ico, x + 20, 24, 24, p["green"], src=24))
        b.append(text(x + 58, 45, f"{cur:,}", 24, p["fg"], weight=600))
        b.append(text(x + 58, 67, esc(label), 13.5, p["text"]))
        d, col = delta(cur, prev, long=(i == 0))
        b.append(text(x + 58, 89, esc(d), 12.5, p[col], weight=500))
    return svg(h, "".join(b), "Last 12 months: contributions, commits, pull requests, issues")


def card_languages(p, langs):
    h = 124
    total = sum(c for _, c in langs) or 1
    b = [card(p, 0, 0, W, h)]
    b.append(text(PAD, 36, "Languages", 15, p["fg"], weight=600))
    b.append(text(W - PAD, 36, "by commits · all repositories", 12, p["muted"], anchor="end"))
    bx, by, bw, bh = PAD, 52, W - 2 * PAD, 8
    b.append(f'<clipPath id="bar"><rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="4"/></clipPath>')
    seg, x = [], bx
    for i, (name, c) in enumerate(langs):
        w = bw * c / total
        gap = 2 if i < len(langs) - 1 else 0
        seg.append(f'<rect x="{x:.1f}" y="{by}" width="{max(w - gap, 1.5):.1f}" height="{bh}" '
                   f'fill="{LANG_COLOR.get(name, LANG_COLOR["Other"])}"/>')
        x += w
    b.append(f'<g clip-path="url(#bar)">{"".join(seg)}</g>')
    cols = 3
    cw = (W - 2 * PAD) / cols
    for i, (name, c) in enumerate(langs):
        x = PAD + (i % cols) * cw
        y = 88 + (i // cols) * 22
        b.append(f'<circle cx="{x + 4:.1f}" cy="{y - 4.5}" r="4" fill="{LANG_COLOR.get(name, LANG_COLOR["Other"])}"/>')
        b.append(text(x + 16, y, esc(name), 13.5, p["fg"], weight=600))
        b.append(text(x + 16 + text_w(name, 13.5) + 8, y, f"{100 * c / total:.1f}%", 13, p["muted"]))
    return svg(h, "".join(b), "Languages by commit")


def card_activity(p, rows, quote, now):
    h = 346
    aw = 548
    b = [card(p, 0, 0, aw, h)]
    b.append(text(PAD, 36, "Recent activity", 15, p["fg"], weight=600))
    b.append(text(aw - PAD, 36, f"updated {now.strftime('%b')} {now.day}", 12, p["muted"], anchor="end"))
    top, rh, cx = 78, 54, 40
    if len(rows) > 1:
        b.append(f'<line x1="{cx}" y1="{top}" x2="{cx}" y2="{top + (len(rows) - 1) * rh}" stroke="{p["border"]}"/>')
    tx, rx = 70, aw - PAD
    for i, r in enumerate(rows):
        cy = top + i * rh
        col = p[r["color"]]
        b.append(f'<circle cx="{cx}" cy="{cy}" r="15.5" fill="{p["bg"]}"/>')
        b.append(f'<circle cx="{cx}" cy="{cy}" r="15.5" fill="{col}" fill-opacity="0.12" stroke="{col}" stroke-opacity="0.45"/>')
        b.append(icon(r["icon"], cx - 8, cy - 8, 16, col))
        b.append(text(rx, cy - 3, esc(r["when"]), 12, p["muted"], anchor="end"))
        title_w = rx - tx - text_w(r["when"], 12) - 16
        if r["repo"]:
            head = r["title"] + " to " if r["icon"] == "repo-push" else r["title"] + " · "
            repo = fit(r["repo"], 14, title_w - text_w(head, 14))
            t = f'{esc(head)}<tspan fill="{p["blue"]}">{esc(repo)}</tspan>'
        else:
            t = esc(fit(r["title"], 14, title_w))
        b.append(text(tx, cy - 3, t, 14, p["fg"], weight=500))
        b.append(text(tx, cy + 16, esc(fit(r["detail"], 12.5, rx - tx)), 12.5, p["muted"]))
        if i < len(rows) - 1:
            b.append(f'<line x1="{tx}" y1="{cy + rh / 2}" x2="{rx}" y2="{cy + rh / 2}" stroke="{p["border"]}" stroke-opacity="0.7"/>')
    if not rows:
        b.append(text(PAD, 80, "No recent activity.", 13.5, p["muted"]))
    b.append(card_quote(p, aw + GAP, W - aw - GAP, h, quote))
    return svg(h, "".join(b), "Recent activity")


def card_quote(p, x0, w, h, quote):
    b = [f'<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">'
         f'<stop offset="0" stop-color="{p["sky0"]}"/><stop offset="1" stop-color="{p["sky1"]}"/></linearGradient>'
         f'<radialGradient id="glow" cx="0.78" cy="0.74" r="0.55">'
         f'<stop offset="0" stop-color="{p["glow"]}" stop-opacity="0.9"/><stop offset="1" stop-color="{p["glow"]}" stop-opacity="0"/></radialGradient>'
         f'<linearGradient id="fog" x1="0" y1="0" x2="0" y2="1">'
         f'<stop offset="0" stop-color="{p["sky1"]}" stop-opacity="0"/><stop offset="1" stop-color="{p["sky1"]}" stop-opacity="0.85"/></linearGradient>'
         f'<clipPath id="qc"><rect x="{x0 + 1}" y="1" width="{w - 2}" height="{h - 2}" rx="5.5"/></clipPath></defs>',
         card(p, x0, 0, w, h, fill="url(#sky)"),
         f'<g clip-path="url(#qc)">{landscape(p, x0, w, h)}</g>',
         f'<rect x="{x0 + .5}" y=".5" width="{w - 1}" height="{h - 1}" rx="6" stroke="{p["border"]}"/>']
    x, y = x0 + PAD, 46
    for line in wrap(f"“{quote['text']}”", 17, w - 2 * PAD - 10):
        b.append(text(x, y, esc(line), 17, p["text"], extra=' font-style="italic"'))
        y += 25
    b.append(text(x, y + 2, f"— {esc(quote['author'])}", 12.5, p["muted"]))
    y += 40
    for it in quote["items"]:
        b.append(icon(it["icon"], x, y - 11.5, 14, p["green"]))
        b.append(text(x + 22, y, esc(it["text"]), 13.5, p["text"]))
        y += 27
    return "".join(b)


def pine(x, base, h):
    """Layered conifer silhouette as one path."""
    tiers = [(0.00, 0.36, 0.13), (0.16, 0.58, 0.21), (0.36, 0.80, 0.29), (0.56, 0.96, 0.37)]
    d = []
    for top, bot, half in tiers:
        ty, by_, hw = base - h + top * h, base - h + bot * h, half * h
        d.append(f"M{x:.1f} {ty:.1f}L{x + hw:.1f} {by_:.1f}L{x - hw:.1f} {by_:.1f}Z")
    d.append(f"M{x - .03 * h:.1f} {base - .06 * h:.1f}h{.06 * h:.1f}V{base:.1f}h{-.06 * h:.1f}Z")
    return "".join(d)


def landscape(p, x0, w, h):
    rnd = random.Random(7)
    b = [f'<rect x="{x0}" y="0" width="{w}" height="{h}" fill="url(#glow)"/>']
    # far ridge
    b.append(f'<path d="M{x0} {h - 92} C{x0 + w * .25} {h - 112} {x0 + w * .45} {h - 84} {x0 + w * .62} {h - 104} '
             f'S{x0 + w * .9} {h - 96} {x0 + w} {h - 118} V{h} H{x0} Z" fill="{p["far"]}"/>')
    # far forest line
    far = "".join(pine(x0 + i * 11 + rnd.uniform(-3, 3), h - 58 + rnd.uniform(-4, 4), rnd.uniform(20, 36))
                  for i in range(int(w / 11) + 2))
    b.append(f'<path d="{far}" fill="{p["mid"]}" opacity="0.85"/>')
    b.append(f'<rect x="{x0}" y="{h - 90}" width="{w}" height="60" fill="url(#fog)"/>')
    # cliff on the right with the figure on top
    cx = x0 + w * 0.80
    top = h - 76
    b.append(f'<path d="M{x0 + w * .58} {h} C{x0 + w * .64} {h - 30} {cx - 26} {top + 6} {cx - 12} {top} '
             f'L{cx + 14} {top - 2} C{x0 + w * .92} {top + 4} {x0 + w * .96} {top + 20} {x0 + w} {top + 26} V{h} Z" fill="{p["near"]}"/>')
    b.append(figure(cx, top, p["near"]))
    # near trees on the edges
    near = "".join(pine(x0 + xx, h + 4, hh) for xx, hh in
                   [(-4, 92), (14, 70), (30, 56), (48, 38), (64, 28), (w - 10, 84), (w + 4, 66)])
    b.append(f'<path d="{near}" fill="{p["near"]}"/>')
    return "".join(b)


def figure(x, feet, color):
    """A small standing hiker seen from behind, ~30px tall."""
    t = feet - 30
    return (f'<g fill="{color}">'
            f'<circle cx="{x}" cy="{t + 3.4}" r="3.3"/>'
            f'<path d="M{x - 5.6} {t + 9.5}Q{x} {t + 6.2} {x + 5.6} {t + 9.5}L{x + 5} {t + 19.5}H{x - 5}Z"/>'
            f'<rect x="{x - 4.4}" y="{t + 9}" width="8.8" height="10.5" rx="2.2"/>'
            f'<rect x="{x - 7.2}" y="{t + 10}" width="2" height="9.5" rx="1"/>'
            f'<rect x="{x + 5.2}" y="{t + 10}" width="2" height="9.5" rx="1"/>'
            f'<path d="M{x - 4.6} {t + 19}H{x - .4}L{x - .8} {feet}H{x - 3.8}Z"/>'
            f'<path d="M{x + .4} {t + 19}H{x + 4.6}L{x + 3.8} {feet}H{x + .8}Z"/>'
            f'</g>')


# ----------------------------------------------------------------------------
def main():
    with open(os.path.join(HERE, "profile.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    now = datetime.now(timezone.utc)

    print("Totals ...")
    totals = gather_totals(now)
    print("Languages ...")
    lang_commits, repo_lang = gather_languages(cfg)
    print("Activity ...")
    rows = gather_activity(cfg, repo_lang, now)

    items = sorted(((k, v) for k, v in lang_commits.items() if v > 0), key=lambda kv: kv[1], reverse=True)
    langs = items[:5]
    rest = sum(v for _, v in items[5:])
    if rest > 0:
        langs.append(("Other", rest))
    stack = []
    for k, _ in items:   # JS and TS read as one skill in a one-line stack
        k = "JS/TS" if k in ("JavaScript", "TypeScript") else k
        if k not in NOT_STACK and k not in stack:
            stack.append(k)

    os.makedirs(ASSETS, exist_ok=True)
    for theme, p in PAL.items():
        out = {
            f"hero.{theme}.svg": card_hero(p, cfg, stack),
            f"stats.{theme}.svg": card_stats(p, totals),
            f"languages.{theme}.svg": card_languages(p, langs),
            f"activity.{theme}.svg": card_activity(p, rows, cfg["quote"], now),
        }
        for fname, content in out.items():
            with open(os.path.join(ASSETS, fname), "w", encoding="utf-8") as f:
                f.write(content)
            print(f"  wrote assets/{fname}")
    print("DONE.")


if __name__ == "__main__":
    main()
