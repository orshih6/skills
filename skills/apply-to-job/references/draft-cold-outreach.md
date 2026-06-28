# draft-cold-outreach

Draft a personalised cold outreach email using structured context from the pipeline.

---

## Steps

### 1. Match CV to job requirements

Compare the candidate's skills and experience against the job requirements:
- List requirements the candidate clearly meets → **covered**
- List requirements missing or unclear → **gaps**

Only write about covered requirements in the email. Never fabricate or overstate.

---

### 2. Pick the hook

From the company research, select the single most specific and recent signal to open with:
- A product launch or new feature
- A funding round and what it means for growth
- A specific technical challenge mentioned in the job post
- A mission statement that genuinely resonates

Avoid generic openers. The hook must show the candidate read past the job title.

---

### 3. Write the email

Follow all rules in `references/humanizer-rules.md`.

Structure:
1. **Hook** (1 sentence) — specific to the company/role, not the candidate
2. **Relevance** (2–3 sentences) — match 2 of the strongest covered requirements to the role, with concrete details
3. **Ask** (1 sentence) — one low-friction request: 15-min call, coffee chat, or consideration for the role
4. **Sign-off** — name + portfolio/GitHub link if relevant

Target: 80–120 words. Hard max: 150 words.

---

### 4. Quality check

Before outputting, verify:
- [ ] Under 150 words
- [ ] Hook is specific to this company/role
- [ ] No banned phrases (see `references/humanizer-rules.md`)
- [ ] Single clear ask
- [ ] Subject line under 8 words

Rewrite any section that fails before showing the result.

---

## Output

```
To: <name, title> — <email>
Subject: <subject line>

<email body>
```
