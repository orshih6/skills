# Personal Skills

A collection of personal Claude Code skills — reusable slash commands that automate multi-step workflows directly inside your Claude Code session.

## What are skills?

Skills are custom slash commands for [Claude Code](https://claude.ai/code). Each skill lives in its own directory under `skills/` and contains a `SKILL.md` file that tells Claude how to execute a specific workflow when you type `/skill-name`.

Skills can include reference documents, scripts, and any other supporting files the workflow needs.

## Skills catalog

| Skill | Command | Description |
|-------|---------|-------------|
| [apply-to-job](skills/apply-to-job/SKILL.md) | `/apply-to-job <job_url> <cv_url>` | Full pipeline: research company, verify legitimacy, find contact email, draft cold outreach |

## Installation

### Prerequisites

- [Claude Code](https://claude.ai/code) installed and authenticated

### Install all skills

Clone this repo into your Claude Code user skills directory:

```bash
git clone <this-repo-url> ~/.claude/skills/personal
```

Claude Code will automatically discover skills placed under `~/.claude/skills/`.

### Install a single skill

Copy the skill directory into your user skills folder:

```bash
cp -r skills/apply-to-job ~/.claude/skills/
```

### Project-scoped install

To make skills available only within a specific project, copy the `skills/` directory into that project's `.claude/` folder:

```bash
cp -r skills/apply-to-job /path/to/project/.claude/skills/
```

## Usage

After installation, type the command in any Claude Code session:

```
/apply-to-job https://jobs.lever.co/stripe/1234 https://myportfolio.com/cv
```

Run `/help` inside Claude Code to see all available skills.
