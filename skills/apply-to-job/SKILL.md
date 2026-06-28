---
name: apply-to-job
description: Full pipeline for applying to a job. Given a job post URL and a CV URL, researches the company, verifies its legitimacy, finds the right contact email, and drafts a personalised cold outreach email ready to send. Use when the user wants to apply to a job, reach out about a job posting, or prepare a job application email.
argument-hint: <job_post_url> <cv_url>
---

# apply-to-job

Full pipeline: given a job post and a CV, research the company, find the right contact, and draft a ready-to-send cold outreach email.

## Usage

```
/apply-to-job <job_post_url> <cv_url>
```

- `job_post_url` — **required.** Direct URL to the job posting.
- `cv_url` — **required.** URL to the candidate's CV or portfolio (PDF or web page).

Example:
- `/apply-to-job https://jobs.lever.co/stripe/1234 https://myportfolio.com/cv`

---

## Pipeline

Run the following steps in order. Each step feeds into the next.

---

### Step 1 — Parse the job post

Fetch `job_post_url` and extract:
- **Company name** and **website domain**
- **Role title**
- **Key requirements** (must-have skills, years of experience, tools/stack)
- **Nice-to-haves**
- **Team / department**
- **Any named hiring manager or recruiter**
- **Location / remote policy**

If the page returns 403 or requires auth, search for the job title + company name to find a cached version or ask the user to paste the description.

---

### Step 2 — Research the company

Follow the steps in `references/research-company.md` using the company name and domain from Step 1.

Collect:
- What the company does and who it serves
- Company size, stage, funding
- Recent news, launches, or milestones (last 6 months)
- Tech stack signals (job posts, engineering blog, GitHub)
- Culture signals (mission statement, Glassdoor, team page tone)
- Key people relevant to this role (hiring manager, team lead, VP/CTO)

---

### Step 3 — Find the contact email

Follow the steps in `references/find-company-email.md` using the company domain and the key people identified in Step 2.

Target priority:
1. Named hiring manager or recruiter from the job post
2. Engineering manager / team lead for this department
3. VP Engineering or CTO
4. Generic fallback: `jobs@`, `careers@`, `hello@`

---

### Step 4 — Parse the candidate CV

Fetch `cv_url` and extract:
- Full name
- Tech stack and skills
- Work history (company, role, dates, key achievements)
- Notable projects
- Education and awards

---

### Step 5 — Draft the cold outreach email

Follow the steps in `references/draft-cold-outreach.md` using all context gathered above.

Use the company research from Step 2 for the hook.
Match the candidate's skills from Step 4 against the job requirements from Step 1.
Address the email to the contact found in Step 3.

---

### Step 6 — Final output

Present everything in this order:

#### Company snapshot
- Name, domain, size, stage
- What they do in one sentence
- Relevant recent news used in the hook

#### Contact found
| Name | Title | Email | Confidence | MX Provider |
|------|-------|-------|------------|-------------|

#### Email draft
```
To: <name> — <email>
Subject: <subject line>

<email body>
```

#### Match summary
- **Requirements covered:** list
- **Requirements not covered:** list (flag to candidate)
- **Suggested apply link:** direct application URL if available alongside the email
