# Skill Name: Web Testing

## Description
How to verify a web page actually renders and works using the Playwright browser
tools, then fix what is broken.

## Trigger Keywords
test, verify, check, browser, playwright, screenshot, render, works, does it
work, open the page, preview, broken page, console error, not showing, blank page

## Instructions
When asked to test, check, preview, or verify a web page (requires the playwright
MCP server):
1. Open it with browser_navigate. For a local file use a file:// URL, e.g.
   file:///C:/Moontashir/shamsu/myproject/index.html.
2. Capture the state with browser_snapshot (accessibility/DOM tree) and
   browser_take_screenshot.
3. Check browser_console_messages for JavaScript errors and
   browser_network_requests for assets that failed to load (a missing
   css/js/image shows up as a failed request).
4. Exercise any interaction the page claims to support with browser_click /
   browser_fill_form / browser_type, then re-snapshot to confirm the result.
5. Report concrete findings — what rendered, what errored, which asset 404'd —
   then fix the underlying file and re-test until it is clean.
