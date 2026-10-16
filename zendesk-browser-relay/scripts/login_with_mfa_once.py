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


async def fill_email_password(page, email: str, password: str) -> None:
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

    password_input = await first_visible(page, ["input[type=password]", "input[name=password]"])
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


async def fill_mfa_code(page, code: str) -> bool:
    multi_inputs = page.locator(
        'input[inputmode="numeric"], input[autocomplete="one-time-code"], input[name*="code"], input[type="tel"]'
    )
    count = await multi_inputs.count()
    if count >= len(code) and count > 1:
        for index, char in enumerate(code):
            await multi_inputs.nth(index).fill(char)
        return True

    for selector in [
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[name*="code"]',
        'input[type="tel"]',
        'input[type="password"]',
        'input[type="text"]',
        "input",
    ]:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=5000)
            await locator.fill(code)
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
        if "auth" in page.url.lower() or "signin" in page.url.lower():
            await fill_email_password(page, email, password)

        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        print("url_after_password=", page.url)
        print("title_after_password=", await page.title())

        if "mfa" in page.url.lower() or "双重身份验证" in await page.title():
            code = getpass.getpass("2fa code: ").strip()
            filled = await fill_mfa_code(page, code)
            print("mfa_code_filled=", filled)
            if filled:
                await click_first(
                    page,
                    [
                        'button:has-text("Verify")',
                        'button:has-text("Continue")',
                        'button:has-text("Submit")',
                        'button:has-text("验证")',
                        'button:has-text("继续")',
                        'button:has-text("提交")',
                        "button[type=submit]",
                    ],
                )
                await page.wait_for_timeout(7000)

        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
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
