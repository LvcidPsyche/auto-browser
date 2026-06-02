import asyncio
import sys
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "controller"
if str(CONTROLLER) not in sys.path:
    sys.path.insert(0, str(CONTROLLER))

# Set env settings to avoid config errors
os.environ["ARTIFACT_ROOT"] = "."
os.environ["AUTH_ROOT"] = "."
os.environ["UPLOAD_ROOT"] = "."
os.environ["APPROVAL_ROOT"] = "."
os.environ["AUDIT_ROOT"] = "."

from app.browser_manager import BrowserSession
from playwright.async_api import async_playwright
from serve_fixtures import PORT, run_server

async def test_live_recovery():
    print("Starting background fixture server...")
    server_thread = threading.Thread(target=run_server, args=(PORT,), daemon=True)
    server_thread.start()
    await asyncio.sleep(1.0) # Wait for server to bind and start
    
    print("Launching Playwright...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        
        # Open primary page
        page = await context.new_page()
        print("Navigating to closed_tab_recovery.html...")
        await page.goto(f"http://localhost:{PORT}/closed_tab_recovery.html")
        
        # Instantiate BrowserSession
        session = BrowserSession(
            id="live-session-1",
            name="live-session-1",
            created_at=None,
            context=context,
            page=page,
            artifact_dir=Path("."),
            auth_dir=Path("."),
            upload_dir=Path("."),
            takeover_url="",
            trace_path=Path("trace.zip")
        )
        
        # Verify initial page is active
        assert session.page is page
        print("Initial page verified active.")
        
        # Click link to open popup target
        print("Clicking 'Open target' to open popup...")
        async def click_and_get_popup():
            async with context.expect_event("page") as event_info:
                await page.click("#open-btn")
            return await event_info.value
            
        popup_page = await click_and_get_popup()
        await popup_page.wait_for_load_state()
        print(f"Popup opened: {popup_page.url}")
        
        # Verify we still have the primary page active in session.page
        assert session.page is page
        
        # Close primary page directly (since browsers restrict window.close() on non-script-opened tabs)
        print("Closing active page...")
        await page.close()
        await asyncio.sleep(0.5)
        
        # Now access session.page. It should have recovered to the popup page!
        print("Checking session.page auto-recovery...")
        current_active = session.page
        print(f"Current active page URL: {current_active.url}")
        assert current_active is popup_page
        print("Auto-recovery verified successfully!")
        
        await browser.close()
    print("Test passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_live_recovery())
