# Publishing and scheduling posts

`publish_post` puts a post on your own LinkedIn feed, now or at a scheduled
time, through LinkedIn's own share composer. `get_scheduled_posts` reads back
LinkedIn's list of scheduled posts. Nothing is published without your approval.

## Setup

Writing is off by default. Start the server with:

```bash
MCP_LINKEDIN_WRITE_ENABLED=true uv run -m linkedin_mcp_server
```

Every publish also needs `confirm: true` on the call. The two switches are
independent; neither implies the other.

## Flow

1. **Preview.** Call `publish_post` with `text` and, optionally,
   `schedule_at`. Without `confirm` it returns `status: "preview"`: the exact
   text, its character count, its links, and when it would go out, worded as
   LinkedIn words it (`Scheduled: Posting at Tue, Oct 6, 8:30 AM`). Nothing on
   LinkedIn changes.
2. **Approve.** Show the preview to the user. Only text the user wrote or
   approved word for word should be posted.
3. **Publish.** Call again with `confirm: true`. The server opens the share
   composer from the feed, types the text (a blank line separates
   paragraphs), and reads it back. For a scheduled post it sets the date and
   time and checks LinkedIn's "Posting at ..." line before pressing Confirm.
   Only then does it press **Post** or **Schedule**.
4. **Verify.** A scheduled post is looked up in LinkedIn's scheduled list; an
   immediate one in your recent activity. The result carries `verified`.

## Arguments

| Argument | Meaning |
|---|---|
| `text` | The post, at most 3,000 characters. Never truncated. |
| `schedule_at` | Local wall-clock time in the browser's time zone, ISO format without an offset, on a quarter hour, 20 minutes to 90 days ahead, e.g. `2026-10-06T08:30`. Omit to post immediately. |
| `confirm` | Must be `true` to publish or schedule. |

The post uses the account's default audience; the result reports it (e.g.
`Post to Anyone`).

## Results

| `status` / `code` | Meaning |
|---|---|
| `preview` | Dry run. Nothing changed. |
| `scheduled`, `posted` | Submitted. `verified` says whether it was found on LinkedIn afterwards. |
| `VALIDATION_ERROR` | Empty or over-long text, or an unusable `schedule_at`. |
| `WRITES_DISABLED` | The server was started without `MCP_LINKEDIN_WRITE_ENABLED=true`. |
| `DUPLICATE_POST` | This text was already submitted by this server. Change the text to post again. |
| `COMPOSER_CHANGED` | The share box held a draft, or a control was missing. |
| `TEXT_MISMATCH` | The composer's text differed from the approved text. The draft was discarded. |
| `SCHEDULE_MISMATCH` | LinkedIn's schedule did not read as planned. The draft was discarded. |

Every error carries `retry_safe`. It is `true` when nothing was submitted. It
is `false` only when the Post or Schedule button was pressed and the outcome
is unclear: check `get_scheduled_posts` or your feed before calling again.

## Local records

`~/.linkedin-mcp/posts/ledger.jsonl` (or `LINKEDIN_POSTS_DIR`) records each
submitted post: a fingerprint of its text, its status, schedule and first
line. It holds no cookies or tokens. It is what refuses duplicates.

## When LinkedIn changes the composer

The selectors live at the top of `linkedin_mcp_server/linkedin/post_composer.py`:
the "Start a post" entry, the `<dialog>` and its ProseMirror editor, the
clock link (matched by an accessible name containing "schedul"), the
`mm/dd/yyyy` date input, the time input and its menu, and the Confirm,
Schedule and Post buttons. Calibrated in October 2026.

> [!WARNING]
> LinkedIn's terms don't permit automated access, even to your own account.
> Keep posting occasional, and every post human-approved.
