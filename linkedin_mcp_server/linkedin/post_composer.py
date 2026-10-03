"""Publish or schedule one post through LinkedIn's own share composer.

The composer is opened from the feed, the planned text is typed and read back,
and only an exact read-back reaches the Post (or Schedule) button. For a
scheduled post the schedule form's own "Posting at ..." line must match the
plan before Confirm is pressed. Any mismatch discards the draft without
publishing. Calibrated against the share dialog of October 2026: a native
``<dialog>`` holding a ProseMirror editor, a "Scheduled" clock link, and a
schedule form with mm/dd/yyyy and h:mm AM/PM inputs.
"""

from __future__ import annotations

import logging
import re

from patchright.async_api import Locator, Page

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.posting.plan import (
    PostingError,
    PostingErrorCode,
    PostPlan,
    content_lines,
    date_field,
    time_field,
)

logger = logging.getLogger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"
ACTIVITY_URL = "https://www.linkedin.com/in/me/recent-activity/all/"
START_POST = "Start a post"
DIALOG = "dialog[open]"
EDITOR = "[contenteditable=true]"
# The clock link is labelled "Scheduled" while nothing is scheduled; once a
# post is, it shows a count and takes its name from aria-labelledby instead.
SCHEDULE_LINK_NAME = re.compile(r"schedul", re.IGNORECASE)
DATE_INPUT = "input[placeholder='mm/dd/yyyy']"
TIME_INPUT = "input:not([placeholder='mm/dd/yyyy'])"
TIME_MENU = "[data-testid=time-picker-menu]"
_UI_TIMEOUT_MS = 15_000
_SETTLE_MS = 1_500
_CLOSE_TIMEOUT_MS = 30_000


class PostComposer:
    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    @property
    def _page(self) -> Page:
        return self._session.page

    def _dialog(self) -> Locator:
        return self._page.locator(DIALOG).last

    def _schedule_link(self) -> Locator:
        return self._dialog().get_by_role("link", name=SCHEDULE_LINK_NAME).first

    async def _open(self) -> Locator:
        await self._navigator._navigate_to_page(FEED_URL)
        await self._session.check_rate_limit()
        await self._page.get_by_text(START_POST, exact=True).first.click(timeout=_UI_TIMEOUT_MS)
        editor = self._dialog().locator(EDITOR).first
        await editor.wait_for(timeout=_UI_TIMEOUT_MS)
        return editor

    async def _discard(self) -> None:
        """Close the composer without publishing; never saves a draft."""
        try:
            await self._dialog().locator('button[aria-label="Dismiss"]').first.click(timeout=5_000)
            discard = self._page.get_by_role("button", name="Discard", exact=True)
            if await discard.count():
                await discard.first.click(timeout=5_000)
        except Exception:  # best effort: the page is left on the feed either way
            logger.warning("Could not close the share composer cleanly", exc_info=True)

    async def _type(self, editor: Locator, text: str) -> None:
        # Paragraphs are separated by an empty paragraph, as LinkedIn's own
        # composer does; a single line break is Shift+Enter. insert_text sends
        # no key events, so it cannot pick a hashtag or mention suggestion.
        keyboard = self._page.keyboard
        await editor.click()
        for i, paragraph in enumerate(text.split("\n\n")):
            if i:
                await keyboard.press("Enter")
                await keyboard.press("Enter")
            for j, line in enumerate(paragraph.split("\n")):
                if j:
                    await keyboard.press("Shift+Enter")
                if line:
                    await keyboard.insert_text(line)
        await self._page.wait_for_timeout(_SETTLE_MS)

    async def _set_schedule(self, plan: PostPlan) -> None:
        assert plan.schedule is not None
        dialog = self._dialog()
        await self._schedule_link().click(timeout=_UI_TIMEOUT_MS)
        date = dialog.locator(DATE_INPUT)
        try:
            await date.wait_for(timeout=_UI_TIMEOUT_MS)
        except Exception:
            raise PostingError(
                PostingErrorCode.COMPOSER_CHANGED,
                "The schedule form did not show its mm/dd/yyyy date field; nothing was posted.",
            ) from None
        await date.fill(date_field(plan.schedule))
        await date.press("Tab")
        hour = time_field(plan.schedule)
        clock = dialog.locator(TIME_INPUT).first
        await clock.click()
        await clock.fill(hour)
        await self._page.wait_for_timeout(800)
        option = self._page.locator(TIME_MENU).get_by_text(hour, exact=True)
        if await option.count():
            await option.first.click()
        await self._page.wait_for_timeout(_SETTLE_MS)
        expected = f"Posting at {plan.posting_at}"
        shown = await dialog.inner_text()
        if expected not in shown:
            raise PostingError(
                PostingErrorCode.SCHEDULE_MISMATCH,
                f"LinkedIn's schedule form did not read '{expected}'; nothing was posted.",
            )
        await dialog.get_by_role("button", name="Confirm", exact=True).click(timeout=_UI_TIMEOUT_MS)
        await self._page.wait_for_timeout(_SETTLE_MS)
        if expected not in await self._dialog().inner_text():
            raise PostingError(
                PostingErrorCode.SCHEDULE_MISMATCH,
                f"The composer did not keep '{expected}' after Confirm; nothing was posted.",
            )

    async def submit(self, plan: PostPlan) -> dict[str, str | None]:
        """Type, check and submit. Raises before submitting on any mismatch."""
        editor = await self._open()
        try:
            if content_lines(await editor.inner_text()):
                raise PostingError(
                    PostingErrorCode.COMPOSER_CHANGED,
                    "The share box already holds a draft. Clear it on linkedin.com first; nothing was posted.",
                )
            await self._type(editor, plan.text)
            typed = content_lines(await editor.inner_text())
            if typed != content_lines(plan.text):
                raise PostingError(
                    PostingErrorCode.TEXT_MISMATCH,
                    "The text in LinkedIn's composer did not match the approved text; nothing was posted.",
                )
            audience = await self._dialog().inner_text()
            visibility = next(
                (line.strip() for line in audience.splitlines() if line.strip().startswith("Post to ")),
                None,
            )
            if plan.schedule:
                await self._set_schedule(plan)
            label = "Schedule" if plan.schedule else "Post"
            button = self._dialog().get_by_role("button", name=label, exact=True)
            if not await button.count() or not await button.first.is_enabled():
                raise PostingError(
                    PostingErrorCode.COMPOSER_CHANGED,
                    f"The composer's {label} button was not available; nothing was posted.",
                )
        except BaseException:
            await self._discard()
            raise
        await button.first.click()
        try:
            await editor.wait_for(state="detached", timeout=_CLOSE_TIMEOUT_MS)
        except Exception:
            raise PostingError(
                PostingErrorCode.COMPOSER_CHANGED,
                f"{label} was pressed but the composer stayed open. Check linkedin.com before retrying.",
                submitted=True,
            ) from None
        return {"visibility": visibility}

    async def scheduled_posts_text(self) -> str:
        """The text of LinkedIn's scheduled-posts list, opened from the composer."""
        await self._open()
        try:
            # An empty composer shows the clock only while something is scheduled.
            if not await self._schedule_link().count():
                return "Scheduled (0)"
            await self._schedule_link().click(timeout=_UI_TIMEOUT_MS)
            await self._page.get_by_text("Scheduled (", exact=False).first.click(timeout=_UI_TIMEOUT_MS)
            await self._page.wait_for_timeout(2 * _SETTLE_MS)
            dialog = self._dialog()
            # The list pages with a "Load more" control; expand it fully.
            for _ in range(20):
                more = dialog.get_by_text("Load more", exact=True)
                if not await more.count():
                    break
                await more.first.click()
                await self._page.wait_for_timeout(_SETTLE_MS)
            # It also scrolls inside the dialog; scroll it so every entry renders.
            for _ in range(10):
                moved = await dialog.evaluate(
                    """d => { const s = [...d.querySelectorAll('*')].find(e => e.scrollHeight > e.clientHeight + 4 && getComputedStyle(e).overflowY !== 'visible');
                              if (!s) return false; const before = s.scrollTop; s.scrollTop = s.scrollHeight; return s.scrollTop !== before; }"""
                )
                if not moved:
                    break
                await self._page.wait_for_timeout(800)
            return await dialog.inner_text()
        finally:
            await self._discard()

    async def recent_activity_text(self) -> str:
        await self._navigator._navigate_to_page(ACTIVITY_URL)
        await self._session.check_rate_limit()
        await self._page.wait_for_timeout(3 * _SETTLE_MS)
        return await self._page.locator("main").inner_text()
