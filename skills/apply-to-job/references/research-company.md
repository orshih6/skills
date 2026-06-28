# research-company

Research a company, verify it is legitimate, and produce a structured summary used for personalising job outreach.

---

## Steps

### 1. Legitimacy check (run first — stop pipeline if company is fake)

Before researching anything else, score the company against the legitimacy signals below. If the verdict is **Suspicious** or **Fake**, stop immediately and warn the user — do not draft an email.

**Check all of the following in parallel:**

**Website quality:**
- Does it have a real Team or About page with named people and photos?
- Is the content specific to this company, or does it read like a template?
- Are there real case studies, blog posts, or product screenshots?
- WebFetch the homepage and `/about` or `/team` page

**Domain age:**
- WebSearch `whois <domain>` or search `"<domain>" site:whois.domaintools.com`
- Domain registered less than 6 months ago + no team page = red flag

**LinkedIn presence:**
- WebSearch `site:linkedin.com/company "<company name>"`
- Does the company page exist? Does it have followers and listed employees?
- WebSearch `site:linkedin.com/in "<company name>"` — are there real employees?
- Zero employees on LinkedIn = strong red flag

**Social media:**
- WebSearch `"<company>" site:twitter.com OR site:x.com`
- WebSearch `"<company>" site:linkedin.com`
- No social presence at all = red flag

**Third-party coverage:**
- WebSearch `"<company name>" crunchbase OR angellist OR glassdoor`
- WebSearch `"<company name>" review OR scam OR fraud OR legit`
- Any scam reports = immediate Fake verdict

**Email domain:**
- Is the company using a free email provider (Gmail, Yahoo, Outlook personal) for business contact? = red flag
- Run MX check: `bash .claude/skills/apply-to-job/scripts/mx-check.sh <domain>`
- No MX records on a company that claims to be operational = suspicious

---

**Verdict scoring:**

| Signal | Weight |
|---|---|
| No team/about page with real names | High |
| Domain < 6 months old | High |
| Zero LinkedIn employees found | High |
| No social media presence | Medium |
| No Crunchbase / Glassdoor listing | Medium |
| Business uses personal Gmail/Yahoo | Medium |
| Scam reports found in search | Immediate Fake |
| No MX records | Medium |

- **Legitimate** — passes most checks, no red flags
- **Suspicious** — 2–3 medium flags or 1 high flag → warn the user, ask if they want to continue
- **Fake** — any scam reports, OR 2+ high flags → stop pipeline, warn user strongly

Output the verdict before continuing:
```
Legitimacy: <Legitimate / Suspicious / Fake>
Flags: <list any red flags found>
```

If **Suspicious**, ask the user: "This company has legitimacy concerns. Do you want to continue anyway?"
If **Fake**, stop and do not proceed.

---

### 2. Basic profile

Fetch the company website and search for:
- What the company does and who it serves (one sentence)
- Company size (headcount range)
- Funding stage and latest round (Crunchbase, TechCrunch)
- HQ location and remote policy
- Founded year

Sources to check in parallel:
- Company homepage
- WebSearch `"<company>" crunchbase funding`
- WebSearch `"<company>" employees size`

---

### 3. Recent news and milestones

Search for activity in the last 6 months:
- WebSearch `"<company>" 2025 OR 2026 launch OR raised OR announced OR released`
- Look for: new product launches, funding rounds, partnerships, engineering blog posts, hiring surges

Pick the 1–2 most concrete and specific items — these are hook material for the email.

---

### 4. Tech stack signals

Gather what technologies the company actually uses:
- WebSearch `"<company>" site:stackshare.io`
- WebFetch `https://stackshare.io/<company>`
- Scan other open job posts for repeated tech mentions
- Check GitHub: WebSearch `"<company>" site:github.com`
- Engineering blog: WebSearch `"<company>" engineering blog`

---

### 5. Culture signals

- Fetch the About or Team page
- WebSearch `"<company>" glassdoor reviews`
- Note: mission statement tone, how they describe their team, any stated values

---

### 6. Key people

Identify people relevant to the role being applied for:
- WebSearch `site:linkedin.com/in "<company>" "engineering manager" OR "VP Engineering" OR "CTO" OR "recruiter" OR "talent"`
- Note full name, title, LinkedIn URL

---

## Output (passed to next pipeline step)

```
Legitimacy: <Legitimate / Suspicious / Fake>
Flags: <any red flags, or "none">

Company: <name>
Domain: <domain>
Size: <range>
Stage: <seed / series A / public / etc>
What they do: <one sentence>
Recent hook: <specific news item — used in email opening>
Tech stack: <comma-separated list>
Key contact: <name, title, LinkedIn URL>
```
