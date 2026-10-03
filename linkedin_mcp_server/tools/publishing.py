"""Tools for publishing or scheduling your own LinkedIn posts, with explicit approval.

``publish_post`` without ``confirm`` returns the exact text, its links and the
schedule exactly as LinkedIn will word it, and changes nothing. With
``confirm=true`` and ``MCP_LINKEDIN_WRITE_ENABLED=true`` it types that text
into LinkedIn's own share composer, reads it back, sets the schedule, and only
then presses Post or Schedule; afterwards it looks for the post on LinkedIn.
A local ledger refuses to submit the same text twice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import logging

from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AccountRestrictedError,
    AuthenticationError,
    RateLimitError,
)
from linkedin_mcp_server.dependencies import get_ready_post_composer, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.posting.plan import (
    PostingError,
    PostingErrorCode,
    PostLedger,
    content_lines,
    plan_post,
)
from linkedin_mcp_server.profile_edit import settings

logger = logging.getLogger(__name__)


def _found(haystack: str, text: str) -> bool:
    flat = " ".join(haystack.split())
    first = content_lines(text)[0]
    return first[:80] in flat


def register_publishing_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register approval-gated post publishing and the scheduled-posts reader."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Publish Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"post", "write"},
    )
    async def publish_post(
        text: str,
        ctx: Context,
        schedule_at: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Publish or schedule a post on your own LinkedIn feed, only with approval.

        Without confirm=true this is a dry run: it returns the exact text,
        character count, links and when it would go out (in LinkedIn's own
        "Posting at Tue, Oct 6, 8:30 AM" wording) and changes nothing. Show
        that preview to the user; call again with confirm=true only after they
        approve it. Writing also needs MCP_LINKEDIN_WRITE_ENABLED=true.

        Only post text the user wrote or approved word for word. The text is
        typed as given (paragraphs separated by a blank line), read back from
        the composer, and refused on any difference. The post uses the
        account's default audience, which the result reports.

        Args:
            text: The post, at most 3000 characters.
            ctx: FastMCP context for progress reporting
            schedule_at: Optional local wall-clock time in the browser's time
                zone, ISO format without an offset, on a quarter hour, 20
                minutes to 90 days ahead, e.g. "2026-10-06T08:30". Omit to
                post immediately.
            confirm: Must be true to publish or schedule.

        Returns status "preview", "scheduled" or "posted", or "error" with a
        code. retry_safe=false means the button may have been pressed: check
        LinkedIn (get_scheduled_posts) before calling again.
        """
        try:
            plan = plan_post(text, schedule_at, now=datetime.now())
            ledger = PostLedger()
            previous = ledger.find(plan.fingerprint)
            if previous and previous.get("status") != "failed":
                raise PostingError(
                    PostingErrorCode.DUPLICATE_POST,
                    f"This text was already submitted ({previous.get('status')} at {previous.get('at')}); "
                    "LinkedIn would show it twice. Change the text to post again.",
                )
            if not confirm:
                return {
                    "status": "preview",
                    "posted": False,
                    **plan.preview(),
                    "next": "Show this to the user; publish only after approval, with confirm=true.",
                }
            if not settings.writes_enabled():
                raise PostingError(
                    PostingErrorCode.WRITES_DISABLED,
                    "Publishing is off. Start the server with MCP_LINKEDIN_WRITE_ENABLED=true.",
                )
            composer = await get_ready_post_composer(ctx, tool_name="publish_post")
            await ctx.report_progress(progress=10, total=100, message="Typing the post")
            try:
                submitted = await composer.submit(plan)
            except PostingError as e:
                ledger.record(plan, "submitted-unconfirmed" if e.submitted else "failed", code=e.code.value)
                raise
            ledger.record(plan, "scheduled" if plan.schedule else "posted")
            await ctx.report_progress(progress=70, total=100, message="Verifying on LinkedIn")
            try:
                seen = (
                    await composer.scheduled_posts_text()
                    if plan.schedule
                    else await composer.recent_activity_text()
                )
                verified = _found(seen, plan.text)
            except Exception:
                logger.warning("Post verification failed", exc_info=True)
                verified = False
            return {
                "status": "scheduled" if plan.schedule else "posted",
                "posted": True,
                "verified": verified,
                "retry_safe": False,
                **plan.preview(),
                **submitted,
            }
        except PostingError as e:
            return e.to_result()
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "publish_post")
        except (RateLimitError, AccountRestrictedError) as e:
            return PostingError(
                PostingErrorCode.AUTHENTICATION_REQUIRED, str(e)[:200]
            ).to_result()
        except Exception as e:
            raise_tool_error(e, "publish_post")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Scheduled Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post"},
    )
    async def get_scheduled_posts(ctx: Context) -> dict[str, Any]:
        """List your own scheduled LinkedIn posts, as LinkedIn's scheduled list shows them.

        Opens the share composer's scheduled-posts view, returns its text,
        and closes the composer without saving anything. Also returns this
        server's local record of posts it submitted.
        """
        try:
            composer = await get_ready_post_composer(ctx, tool_name="get_scheduled_posts")
            text = await composer.scheduled_posts_text()
            return {"text": text, "submittedByThisServer": PostLedger().entries()[-50:]}
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_scheduled_posts")
        except Exception as e:
            raise_tool_error(e, "get_scheduled_posts")  # NoReturn
