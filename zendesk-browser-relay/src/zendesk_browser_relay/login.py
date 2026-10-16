from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from .settings import get_settings


async def run_login() -> None:
    settings = get_settings()
    settings.zendesk_profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        launch_options = {
            "user_data_dir": str(settings.zendesk_profile_dir),
            "headless": False,
            "args": ["--no-sandbox"],
        }
        if settings.zendesk_proxy_server:
            launch_options["proxy"] = {"server": settings.zendesk_proxy_server}
        context = await playwright.chromium.launch_persistent_context(
            **launch_options,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(f"{settings.zendesk_base_url}/agent/dashboard")
        print("Browser opened. Finish Zendesk login and 2FA manually, then press Enter here.")
        await asyncio.to_thread(input)
        await context.close()


def main() -> None:
    asyncio.run(run_login())


if __name__ == "__main__":
    main()
