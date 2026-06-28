# find-company-email

Find real email addresses of people at a company.

---

## Steps

### 1. Discover email format

**Resolve the domain:**
- If a website URL was provided — extract the domain (e.g. `https://www.stripe.com` → `stripe.com`)
- If only a company name was provided — search `"<company_name>" official website` to find the domain, then confirm it with a WebFetch before proceeding

Fetch the website and common subpages in parallel, looking for exposed emails:
- `https://<domain>/contact`
- `https://<domain>/about`
- `https://<domain>/team`
- `https://<domain>/careers`

Extract any email addresses found. Note the domain used (e.g. `@stripe.com`).

### 2. Search for emails via web search

Run targeted web searches to surface emails:
- `"@<domain>" site:<domain>` — finds emails indexed on the company site
- `"<domain>" email contact` — general email discovery
- `"<role_or_name>" "<domain>" email` — if a name or role was provided
- `"<company_name>" filetype:pdf OR filetype:xlsx email` — leaked contact lists

Also check aggregators:
- WebFetch `https://hunter.io/search/<domain>`
- WebFetch `https://rocketreach.co/<company-name>-inc-email-format_*` (search for the slug first)
- WebSearch `"<domain>" site:leadiq.com OR site:rocketreach.co OR site:signalhire.com`

### 3. Infer email format

Based on emails found, determine the company's pattern. Common patterns (see `references/smtp-verification.md`):
- `firstname@domain.com`
- `firstname.lastname@domain.com`
- `f.lastname@domain.com`
- `flastname@domain.com`
- `firstname_lastname@domain.com`

### 4. LinkedIn people search

Search for people at the company to get names, then construct emails:
- Search: `site:linkedin.com/in "<company_name>"` to find employee profiles
- If a role was given: `site:linkedin.com/in "<company_name>" "<role>"`
- Apply the inferred format from step 3 to each name found

### 5. Validate with MX check

Run the MX check script:
```bash
bash .claude/skills/apply-to-job/scripts/mx-check.sh <domain>
```

Interpret results using `references/smtp-verification.md`:
- No MX → domain dead, discard all candidates
- Google/M365 MX → domain live, SMTP probing blocked — mark as unverifiable but valid
- Custom MX → domain live

Cross-check specific addresses:
- WebSearch `"<firstname>.<lastname>@<domain>"` — confirms if indexed publicly
- WebSearch `"@<domain>" filetype:pdf OR filetype:csv` — finds leaked lists

### 6. Rank results

- **Confirmed** — found directly on site or public database, MX valid
- **Inferred** — built from pattern + name, MX valid
- **Possible** — guessed format, not cross-checked

### 7. Output

| Name | Title | Email | Confidence | MX Provider | Source |
|------|-------|-------|------------|-------------|--------|
| Jane Doe | CTO | jane.doe@company.com | Confirmed | Google Workspace | Company website |
| John Smith | Eng Manager | john.smith@company.com | Inferred | Google Workspace | LinkedIn + pattern |

Also report:
- **Email format detected:** e.g. `firstname.lastname@domain.com`
- **Domain:** e.g. `company.com`
- **Sources checked:** list of URLs/searches used

## Notes

- Only surface publicly discoverable emails — do not fabricate without an identified format pattern.
- If no email pattern found, say so clearly and suggest Hunter.io, Apollo.io, RocketReach.
- One fetch per URL — do not hammer pages.
- Intended for legitimate outreach (job applications, sales, partnerships).
