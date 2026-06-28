# SMTP Verification Reference

## MX Provider Behaviour

| MX Pattern | Provider | SMTP Probing | Notes |
|---|---|---|---|
| `aspmx.l.google.com` | Google Workspace | Blocked | Returns 550 or hangs |
| `mail.protection.outlook.com` | Microsoft 365 | Blocked | Returns 550 or 452 |
| `amazonses.com` | Amazon SES | Sometimes | Depends on config |
| `mimecast.com` | Mimecast | Blocked | Security gateway |
| `pphosted.com` | Proofpoint | Blocked | Security gateway |
| `mailgun.org` | Mailgun | Sometimes | Transactional only |
| Custom domain MX | Self-hosted | Possible | Postfix/Exim installs vary |
| None | Dead domain | N/A | Discard all candidates |

## Email Format Frequency (industry average)

| Pattern | Example | Avg. Usage |
|---|---|---|
| firstname.lastname | john.doe@co.com | ~55% |
| firstname | john@co.com | ~20% |
| f.lastname | j.doe@co.com | ~10% |
| flastname | jdoe@co.com | ~8% |
| firstname_lastname | john_doe@co.com | ~4% |
| firstnamelastname | johndoe@co.com | ~3% |

## Public Aggregators to Check

- **Hunter.io** — `https://hunter.io/search/<domain>` — best free coverage
- **LeadIQ** — `https://leadiq.com/c/<company>/<id>/email-format`
- **RocketReach** — `https://rocketreach.co/<company>-email-format_*`
- **SignalHire** — `https://www.signalhire.com/companies/<company>/email-format`
- **NeverBounce** — `https://www.neverbounce.com/company/<company>/<id>` — verification only

## Confidence Rules

- **Confirmed** — email found verbatim on a public page or aggregator, MX valid
- **Inferred** — constructed from verified format + real person's name, MX valid
- **Possible** — format guessed, no cross-check available
- **Discard** — MX missing, regex invalid, or domain is a known disposable provider
