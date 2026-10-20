# Daily Briefing

**Your calendar, the emails that need you, and the news that matters, twice a day. Your email never leaves your computer.**

Every morning and late afternoon, Daily Briefing reads your calendar and your new email, works out which emails
actually need you, turns them into to-dos, adds a few news items on topics you choose, and sends it all to your phone.

```
Good morning, Sam. Wed 21 Oct

TODAY
09:00  Stand-up
14:30  Call with Northwind Climate

NEEDS YOU (3)
• Example Bank: Please confirm your new address by 30 Oct
  → Confirm your new address in online banking (by 30 Oct)
• Northwind Climate: Interview invitation
  → Reply with times for the interview
• Landlord: Rent increase from December
  → Sign and return the rent form (by 15 Nov)
Also: 2 worth reading, 14 routine, 23 promotions skipped.

TO-DO: 5 open, 2 due this week (3 new from email)

NEWS
• EU climate policy: EU adopts new reporting rule for logistics
  https://...
```

## What makes it different

- **Private by design.** Every email is read only by a **local model** on your own computer (Ollama or LM Studio).
  The code refuses to send email to a cloud model, and the email role never has a cloud backup. Only your news topics
  go to a web-search model.
- **Read-only everywhere.** Gmail and Google Calendar use read-only scopes, so it *cannot* send, delete, move or
  mark mail. IMAP folders are opened read-only and messages fetched with `BODY.PEEK`, so nothing is even marked as
  read. Attachments are never opened.
- **Only what needs you.** Promotions and social mail are counted, not read. Newsletters and automatic notices are
  recognised by their headers and skipped, unless they come from an organisation on your `watch` list (your bank,
  your landlord, a company you applied to). Each remaining email is sorted into: action needed, reply needed,
  deadline, application update, worth reading, or routine.
- **Emails become to-dos**, with the due date the email states, and never twice for the same email.
- **News you can trust.** One web search per topic; a link is kept only if it really appeared in the search results,
  home pages are dropped, and nothing you have already seen is shown again.
- **Two editions.** The morning brief covers today; the afternoon update shows what is still ahead, tomorrow's
  calendar, and only what arrived since the morning.
- **Delivered free:** Telegram, ntfy or WhatsApp (CallMeBot), and always saved as a web page that works in light and
  dark mode.

## Quick start

Requires Python 3.11+, a local model, and an API key for one search model (Gemini by default).

```bash
pip install git+https://github.com/abhishekach/daily-briefing.git

# a local model for email, e.g. with Ollama (https://ollama.com)
ollama pull qwen3:8b

mkdir my-briefing && cd my-briefing
briefing init            # creates settings.yaml, .env and a .gitignore that keeps them private
```

1. Edit `settings.yaml`: your name, one paragraph about you, the organisations to watch, your news topics, and any
   IMAP mailbox.
2. Put your search key in `.env` (`GEMINI_API_KEY`, or `ANTHROPIC_API_KEY` with `BRIEFING_MODELS=anthropic`).
3. Link your mail and phone:
   ```bash
   briefing google-link      # Gmail + Google Calendar, read-only (repeat for each account)
   briefing telegram-link    # optional: after putting your bot token in .env
   ```
4. Check, run, schedule:
   ```bash
   briefing check            # what is set up; spends nothing
   briefing run              # make and send the brief now
   briefing schedule --install            # macOS: every day at 07:30 and 17:30
   briefing schedule --times 07:00,18:00  # other systems: prints the cron line to add
   ```
5. Day to day: `briefing todos`, `briefing done 3`, `briefing costs`.

## Mailboxes

**Gmail and Google Calendar.** Create a Google Cloud project, turn on the Gmail API and the Google Calendar API,
create an OAuth client of type *Desktop app*, and save its JSON as `google/client_secret.json` in your workspace.
Then `briefing google-link` opens your browser; sign in and allow read-only access. Repeat for each account. While
your Google app is in *Testing*, Google expires the sign-in after 7 days; publishing it ("In production") avoids that.

**Any other mailbox (IMAP).** Outlook.com, GMX, web.de, iCloud, Fastmail or a company server. Add it to
`settings.yaml` and put an app password in `.env` under the name you chose:

```yaml
mail:
  imap:
    - name: work
      host: outlook.office365.com
      user: you@example.com
      password_env: WORK_MAIL_PASSWORD
      folders: [INBOX]
```

## Models and cost

Two roles, set in `models.yaml` (pick a profile with `BRIEFING_MODELS`):

| Role | Default | What it sees |
|---|---|---|
| `mail` | `ollama_chat/qwen3:8b` on your computer | your email: **local only** |
| `search` | Gemini 3.8 Flash with Google Search (backup: Claude Haiku) | your news topics and the paragraph about you |

Email sorting is free. News costs about **$0.03–0.05 per topic** with Gemini (measured on the author's daily
setup) and about $0.05 with Claude's web search; so roughly **$6–11 a month** for two briefs a day with 3–4 topics.
Fewer topics, fewer editions or `--no-news` cost less. Every call is logged to `.briefing/ledger.sqlite`, and
`BRIEFING_MONTHLY_BUDGET_USD` (default $10) stops paid calls once the month's spend reaches it.

A 7–14B local model sorts email well; expect a few seconds per email. With LM Studio, use the `lmstudio` profile;
the briefing starts LM Studio's server itself if it is installed but not running.

## Privacy and safety

- Email bodies are read by the local model and then dropped: only the sender, subject, a one-line summary and the
  to-do are kept.
- Emails are untrusted input: the sorting prompt tells the model to ignore instructions inside them, and nothing in
  the briefing can act on email anyway (read-only access).
- `allow_cloud_for_email: true` in `settings.yaml` lets a cloud model read your email. It is off by default, and
  `briefing check` warns you when it is on.
- Your settings, sign-in tokens, briefs and to-dos stay in your workspace folder; `briefing init` writes a
  `.gitignore` so they never end up in a repository.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The tests use a fake Gmail, a fake IMAP server and fake models: no keys, no network, no cost. They check, among
other things, that email never reaches a cloud model, that IMAP is read-only and never sets the Seen flag, that
attachments are never read, that unverified news links are dropped, and that nothing is shown twice.

## Background

Daily Briefing is extracted from a personal multi-agent system I run every day. The rest of that system stays private;
this is the part that is useful to anyone.

## Licence

MIT. See [LICENSE](LICENSE).
