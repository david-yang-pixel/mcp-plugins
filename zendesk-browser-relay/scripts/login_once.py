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
        print("url_after_open=", page.url)

        try:
            email_input = await first_visible(
                page,
                [
                    "input[type=email]",
                    "input[name=email]",
                    "input[name=login]",
                    "input[name=username]",
                    "input[type=text]",
                ],
            )
            await email_input.fill(email)
            await click_first(
                page,
                [
                    'button:has-text("Continue")',
                    'button:has-text("Next")',
                    'button:has-text("下一步")',
                    'button:has-text("继续")',
                    'button:has-text("登录")',
                    "button[type=submit]",
                ],
            )
            await page.wait_for_timeout(2500)
        except Exception as exc:
            print("email_step=skipped_or_failed", type(exc).__name__)

        try:
            password_input = await first_visible(
                page,
                ["input[type=password]", "input[name=password]"],
            )
            await password_input.fill(password)
            await click_first(
                page,
                [
                    'button:has-text("Log in")',
                    'button:has-text("Sign in")',
                    'button:has-text("登录")',
                    'button:has-text("继续")',
                    "button[type=submit]",
                ],
            )
            await page.wait_for_timeout(5000)
        except Exception as exc:
            print("password_step=skipped_or_failed", type(exc).__name__)

        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        print("final_url=", page.url)
        print("title=", await page.title())
        if "/agent/" in page.url and "auth" not in page.url:
            print("status=logged_in")
        elif "mfa" in page.url.lower() or "verify" in page.url.lower():
            print("status=additional_verification_required")
        else:
            print("status=login_not_finished")
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())

