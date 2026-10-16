import asyncio
import getpass
import os
from pathlib import Path

from playwright.async_api import async_playwright


def load_env(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in Path(path).read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        result[key] = value
    return result


async def first_visible(page, selectors: list[str], timeout: int = 7000):
    last_error = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            return locator
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("no selector configured")


async def click_first(page, selectors: list[str], timeout: int = 4000) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click()
            return True
        except Exception:
            pass
    return False


async def main() -> None:
    email = input("email: ").strip()
    password = getpass.getpass("password: ")
    env = load_env("/etc/zendesk-browser-relay/env")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = env.get(
        "PLAYWRIGHT_BROWSERS_PATH",
        "/opt/zendesk-browser-relay/ms-playwright",
    )
    profile = env.get("ZENDESK_PROFILE_DIR", "/var/lib/zendesk-browser-relay/profile")
    proxy = env.get("ZENDESK_PROXY_SERVER") or None
    subdomain = env.get("ZENDESK_SUBDOMAIN", "langgenius")
    base = f"https://{subdomain}.zendesk.com"

    async with async_playwright() as playwright:
        launch_options = {
            "user_data_dir": profile,
            "headless": True,
            "args": ["--no-sandbox"],
        }
        if proxy:
            launch_options["proxy"] = {"server": proxy}
        context = await playwright.chromium.launch_persistent_context(**launch_options)
        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(20_000)
        await page.goto(f"{base}/agent/dashboard", wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        email_input = await first_visible(
            page,
            ["input[type=email]", "input[name=email]", "input[name=login]", "input[name=username]", "input[type=text]"],
        )
        await email_input.fill(email)
        await click_first(page, ['button:has-text("Sign in")', "button[type=submit]"])
        await page.wait_for_timeout(1500)

        password_input = await first_visible(page, ["input[type=password]", "input[name=password]"])
        await password_input.fill(password)
        await click_first(page, ['button:has-text("Sign in")', "button[type=submit]"])
        await page.wait_for_timeout(5000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        print("url=", page.url)
        print("title=", await page.title())
        inputs = await page.locator("input").evaluate_all(
            """els => els.map((el, i) => ({
                i,
                type: el.getAttribute('type'),
                name: el.getAttribute('name'),
                id: el.getAttribute('id'),
                className: el.getAttribute('class'),
                autocomplete: el.getAttribute('autocomplete'),
                inputmode: el.getAttribute('inputmode'),
                placeholder: el.getAttribute('placeholder'),
                aria: el.getAttribute('aria-label'),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            }))"""
        )
        print("inputs=", inputs)
        buttons = await page.locator("button").evaluate_all(
            """els => els.map((el, i) => ({
                i,
                type: el.getAttribute('type'),
                text: el.innerText,
                aria: el.getAttribute('aria-label'),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            }))"""
        )
        print("buttons=", buttons)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

