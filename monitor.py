"""
Job monitor: checks each careers page in orgs.csv, compares against what it
saw last time (seen.json), and emails you any new roles.

Alerts are posted as an issue in your GitHub repository, and GitHub emails
you about it automatically, so no email passwords are needed.

Runs automatically on GitHub Actions (see README). You can also run it on
your laptop with:  python3 monitor.py   (it prints the alert instead).
"""
import csv, json, os, re, time
from datetime import date
from urllib.parse import urljoin, urldefrag
import requests
from bs4 import BeautifulSoup

# ---------- Settings you might want to change ----------
# Roles containing these words are left out of alerts entirely.
IGNORE_WORDS = ["volunteer", "trustee", "board member"]
# Roles containing these words are listed separately, lower down the email.
SENIOR_WORDS = ["director", "head of", "chief", "senior", "principal"]
# -------------------------------------------------------

STATE_FILE = "seen.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job search; checks twice a day)"}

TITLE_WORDS = re.compile(
    r"\b(officer|manager|co-?ordinator|director|assistant|lead|head of|analyst|"
    r"researcher|associate|fellow(ship)?|intern(ship)?|advis[eo]r|campaigner|executive|"
    r"specialist|producer|editor|consultant|organi[sz]er|administrator|engineer|"
    r"designer|developer|strategist|programme|program|partnerships?|operations)\b", re.I)
JOB_URL = re.compile(r"job|career|vacanc|recruit|position|opportunit|apply|\.pdf", re.I)
SKIP = re.compile(r"privacy|cookie|log ?in|sign ?in|newsletter|donate|subscribe|"
                  r"facebook|twitter|linkedin|instagram|youtube|mailto:|tel:", re.I)
GENERIC = re.compile(r"^(jobs?|careers?|vacancies|work with us|join us|apply( now)?|"
                     r"read more|more|view all.*|see all.*|current vacancies|opportunities)$", re.I)

ATS_PATTERNS = {
    "workable": re.compile(r"apply\.workable\.com/([a-z0-9-]+)", re.I),
    "greenhouse": re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I),
    "lever": re.compile(r"jobs(?:\.eu)?\.lever\.co/([a-z0-9-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([a-z0-9._-]+)", re.I),
}


def fetch(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=20, **kw)
    r.raise_for_status()
    return r


def from_ats(kind, slug):
    """Read jobs from a hiring platform's public feed. Returns None if it fails."""
    try:
        if kind == "workable":
            data = fetch(f"https://apply.workable.com/api/v1/widget/accounts/{slug}").json()
            return [(j["title"], j.get("url") or j.get("shortlink", "")) for j in data.get("jobs", [])]
        if kind == "greenhouse":
            data = fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json()
            return [(j["title"], j["absolute_url"]) for j in data.get("jobs", [])]
        if kind == "lever":
            data = fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json").json()
            return [(j["text"], j["hostedUrl"]) for j in data]
        if kind == "ashby":
            data = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
            return [(j["title"], j["jobUrl"]) for j in data.get("jobs", [])]
    except Exception:
        return None


def from_page(url, html):
    """Pick out things on an ordinary careers page that look like job adverts."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["nav", "footer", "header", "script", "style"]):
        tag.decompose()
    found = []
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        href = urldefrag(urljoin(url, a["href"]))[0]
        if not (5 <= len(text) <= 150) or SKIP.search(text + " " + href) or GENERIC.match(text):
            continue
        if href.rstrip("/") == url.rstrip("/"):
            continue
        if TITLE_WORDS.search(text) or (JOB_URL.search(href) and len(text.split()) >= 2):
            found.append((text, href))
    for h in soup.find_all(["h2", "h3", "h4"]):
        text = " ".join(h.get_text(" ", strip=True).split())
        if 5 <= len(text) <= 120 and TITLE_WORDS.search(text) and not h.find_parent("a"):
            found.append((text, url))
    return found


def check(url):
    page = fetch(url)
    for kind, pattern in ATS_PATTERNS.items():
        m = pattern.search(page.url) or pattern.search(page.text)
        if m:
            jobs = from_ats(kind, m.group(1))
            if jobs is not None:
                return jobs
    return from_page(page.url, page.text)


def post_alert(title, body):
    """Open an issue in the repository. GitHub then emails you about it."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if not (token and repo):
        print(f"\n(Not running on GitHub, so printing instead)\n\n# {title}\n\n{body}")
        return
    # Issues have a size limit, so split very long alerts into parts
    chunks, current = [], ""
    for line in body.split("\n"):
        if len(current) + len(line) > 60000:
            chunks.append(current)
            current = ""
        current += line + "\n"
    chunks.append(current)
    for i, chunk in enumerate(chunks, 1):
        part = f" (part {i} of {len(chunks)})" if len(chunks) > 1 else ""
        r = requests.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"title": title + part, "body": f"@{owner}\n\n{chunk}"},
            timeout=30)
        r.raise_for_status()


def main():
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    orgs = list(csv.DictReader(open("orgs.csv", encoding="utf-8")))
    new, baseline, failing = [], [], []

    for row in orgs:
        org, url = row["Organisation"], row["Careers page"].strip()
        prev = state.get(org, {})
        try:
            jobs = check(url)
        except Exception:
            fails = prev.get("fails", 0) + 1
            state[org] = {**prev, "fails": fails}
            if fails >= 3:
                failing.append(f"{org}: {url}")
            continue
        jobs = [(t, u) for t, u in jobs if not any(w in t.lower() for w in IGNORE_WORDS)]
        keys = {f"{t}|{u}" for t, u in jobs}
        if "seen" not in prev:
            baseline += [(org, row["Priority"], t, u) for t, u in jobs]
        else:
            new += [(org, row["Priority"], t, u) for t, u in jobs if f"{t}|{u}" not in prev["seen"]]
        # Remember everything ever seen, so a role that briefly disappears isn't re-sent
        state[org] = {"seen": sorted(set(prev.get("seen", [])) | keys), "fails": 0}
        time.sleep(1)

    json.dump(state, open(STATE_FILE, "w"), indent=1, ensure_ascii=False)

    def fmt(items):
        items = sorted(items, key=lambda x: (x[1], x[0]))
        main_ = [i for i in items if not any(w in i[2].lower() for w in SENIOR_WORDS)]
        senior = [i for i in items if i not in main_]
        out = [f"- **{o}** (P{p}): [{t}]({u})" for o, p, t, u in main_]
        if senior:
            out.append("\n**Probably too senior**\n")
            out += [f"- {o} (P{p}): [{t}]({u})" for o, p, t, u in senior]
        return "\n".join(out)

    sections = []
    if new:
        sections.append(f"## New roles ({len(new)})\n\n" + fmt(new))
    if baseline:
        sections.append("## First check: roles currently listed\n\n"
                        "_From now on you'll only be alerted to new ones._\n\n" + fmt(baseline))
    if failing:
        sections.append("## Pages that have failed 3+ times in a row\n\n"
                        "The address may have changed; update it in orgs.csv.\n\n"
                        + "\n".join(f"- {f}" for f in failing))
    if sections:
        n = len(new)
        title = (f"{n} new role{'s' if n != 1 else ''} ({date.today():%d %b})" if n
                 else f"First check complete ({date.today():%d %b})")
        post_alert(title, "\n\n".join(sections))
    else:
        print("Nothing new today.")


if __name__ == "__main__":
    main()
