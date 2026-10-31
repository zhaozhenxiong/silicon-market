"""Observe the normal Xianyu web UI using a locally authenticated browser.

This adapter never signs private APIs, solves challenges, rotates identities, or
infers a sale from disappearance. Platform changes must fail visibly.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("XIANYU_DATA_DIR", str(ROOT / "data")))
PROFILE = DATA / "browser_profile"
SESSION = DATA / "session_meta.json"
_lock = threading.Lock()
_busy = False
_login_running = False
_last_message = "本地登录态已保存；有效性待平台验证。" if SESSION.exists() else "首次使用请打开登录窗口，在闲鱼官网完成登录。"
SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
DETAIL_API = "mtop.taobao.idle.pc.detail"


class CollectorError(RuntimeError):
    pass


def _browser_installed() -> bool:
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not cache:
        cache = str(Path.home() / ("Library/Caches/ms-playwright" if sys.platform == "darwin" else "AppData/Local/ms-playwright" if sys.platform == "win32" else ".cache/ms-playwright"))
    return any(Path(cache).glob("chromium-*/**/chrome")) or any(Path(cache).glob("chromium-*/**/Google Chrome for Testing")) or any(Path(cache).glob("chromium-*/**/chrome.exe"))


def collector_status() -> dict:
    browser = _browser_installed()
    return {
        "available": browser and SESSION.exists() and not _busy,
        "browser_installed": browser,
        "session_exists": SESSION.exists(),
        "login_running": _login_running,
        "busy": _busy,
        "message": _last_message if browser else "浏览器尚未安装，请在项目目录运行 uv run playwright install chromium。",
    }


def _claim(login: bool = False) -> None:
    global _busy, _login_running
    with _lock:
        if _busy:
            raise CollectorError("浏览器正忙，请等待当前采集或登录结束。")
        _busy = True
        _login_running = login


def _release() -> None:
    global _busy, _login_running
    with _lock:
        _busy = False
        _login_running = False


def start_login() -> dict:
    if not _browser_installed():
        raise CollectorError("请先运行 uv run playwright install chromium 安装浏览器。")
    _claim(login=True)
    try:
        threading.Thread(target=_login_thread, daemon=True, name="xianyu-local-login").start()
    except Exception:
        _release()
        raise CollectorError("无法启动登录窗口，请重试。") from None
    return {"ok": True, "message": "已打开本机闲鱼登录窗口。请扫码并完成平台验证，检测到登录后窗口会自动关闭（最多等待 5 分钟）。"}


async def _context(playwright, headless: bool):
    DATA.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    PROFILE.chmod(0o700)
    return await playwright.chromium.launch_persistent_context(
        str(PROFILE), headless=headless, viewport={"width": 1440, "height": 1000},
        locale="zh-CN", timezone_id="Asia/Shanghai", accept_downloads=False,
        # Keep the browser defaults; do not hide automation or bypass challenges.
    )


def _login_thread() -> None:
    global _last_message
    try:
        asyncio.run(_login())
    except CollectorError as exc:
        _last_message = str(exc)
    except Exception:
        _last_message = "登录窗口已关闭或启动失败。请重试并在窗口中完成登录；浏览器运行环境也可检查终端。"
    finally:
        _release()


async def _login() -> None:
    global _last_message
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        context = await _context(p, headless=False)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=60000)
            _last_message = "等待你在本机闲鱼窗口完成登录和平台验证。"
            for _ in range(150):
                if await _platform_blocked(page):
                    raise CollectorError("闲鱼提示“非法访问，请使用正常浏览器访问闲鱼”。本次登录已停止，请先在普通浏览器确认官网可正常访问。")
                cookies = await context.cookies("https://www.goofish.com/")
                names = {c["name"] for c in cookies if c.get("value")}
                if ("unb" in names and ("cookie2" in names or "sgcookie" in names)
                        and not await _gate(page) and not await _login_visible(page)):
                    # Metadata only. All credentials remain in the private Chromium profile.
                    SESSION.write_text(json.dumps({"saved_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
                    SESSION.chmod(0o600)
                    _last_message = "本地登录态已保存；有效性将在下次采集时由平台验证。"
                    await asyncio.sleep(2)
                    return
                await asyncio.sleep(2)
            _last_message = "登录等待超时，请重新打开登录窗口。"
        finally:
            await context.close()


def _success(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise CollectorError("闲鱼响应不是 JSON 对象，请检查登录或更新采集适配器。")
    result = payload.get("ret")
    if "ret" in payload and (not isinstance(result, list) or not result
                             or not all(isinstance(r, str) and r.startswith("SUCCESS") for r in result)):
        raise CollectorError("闲鱼未返回成功结果，可能需要重新登录或在官网完成验证。本次采集已停止。")


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _price(value) -> float | None:
    if isinstance(value, list):
        value = "".join(str(part.get("text", "")) for part in value if isinstance(part, dict))
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace("当前价", "").replace(",", "").replace("¥", "").replace("￥", "").removesuffix("元").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(万)?", text)
    if not match:
        return None  # price ranges, negotiable prices, and deposits are not numeric quotes
    number = float(match[1]) * (10000 if match[2] else 1)
    return round(number, 2) if math.isfinite(number) and number <= 100_000_000 else None


def parse_search_payload(payload: dict) -> list[dict]:
    """Map observed, version-sensitive search fields into our own record schema."""
    _success(payload)
    results = _mapping(payload.get("data")).get("resultList")
    if not isinstance(results, list):
        raise CollectorError("闲鱼搜索响应结构发生变化（缺少 resultList），请更新采集适配器。")
    records = []
    for result in results:
        data = _mapping(_mapping(result).get("data"))
        main = _mapping(_mapping(data.get("item")).get("main"))
        content = _mapping(main.get("exContent"))
        item_id, title = content.get("itemId"), content.get("title")
        if (isinstance(item_id, bool) or not isinstance(item_id, (str, int))
                or not re.fullmatch(r"\d{1,40}", str(item_id))
                or not isinstance(title, str) or not title.strip()):
            continue  # recommendation/ad cards are not listing observations
        price = _price(content.get("price"))
        if price is None:
            continue
        record = {
            "item_id": str(item_id), "title": title.strip(), "price": price,
            "url": "https://www.goofish.com/item?" + urlencode({"id": str(item_id)}),
            "status": "on_sale", "evidence": "闲鱼搜索结果直接观测到在售挂牌；不包含成交价。",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        published = _mapping(_mapping(main.get("clickParam")).get("args")).get("publishTime")
        if str(published).isdigit():
            try:
                stamp = float(published)
                if stamp > 1e12:
                    stamp /= 1000
                dt = datetime.fromtimestamp(stamp, timezone.utc)
                if datetime(2010, 1, 1, tzinfo=timezone.utc) < dt <= datetime.now(timezone.utc):
                    record["published_at"] = dt.isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        records.append(record)
    if results and not records:
        raise CollectorError("搜索返回了内容，但无法解析商品ID、标题和价格。已停止，避免把解析失败当成零结果。")
    return records


async def _gate(page) -> bool:
    # Only visible challenge frames / modal UI, never arbitrary words in listing titles.
    frames = page.locator('iframe[src*="punish"], iframe[src*="captcha"], #nocaptcha, #baxia-dialog-content, .baxia-dialog-mask, .J_MIDDLEWARE_FRAME_WIDGET')
    return (any([await frames.nth(i).is_visible() for i in range(min(await frames.count(), 30))])
            or await _platform_blocked(page))


async def _platform_blocked(page) -> bool:
    # Observed first-party denial screen. Require both exact, visible notices;
    # a listing title or hidden template mentioning access restrictions is not a gate.
    for notice in ("非法访问", "为了保障您的体验，请使用正常浏览器访问闲鱼~"):
        elements = page.get_by_text(notice, exact=True)
        if not any([await elements.nth(i).is_visible() for i in range(min(await elements.count(), 10))]):
            return False
    return True


async def _login_visible(page) -> bool:
    parsed = urlparse(page.url)
    if parsed.hostname not in {"www.goofish.com", "goofish.com"} or "login" in parsed.path.lower():
        return True
    if await _platform_blocked(page):
        return True
    frames = page.locator('iframe[src*="login"], [class*="login-dialog"], [class*="loginDialog"]')
    if any([await frames.nth(i).is_visible() for i in range(min(await frames.count(), 30))]):
        return True
    # Search headers/recommendations can contain persistent “登录” text even
    # when the local session works. Only a visible dialog or redirect is a gate.
    return False


async def _wait_ready(page, headless: bool, timeout_seconds: float = 120) -> bool:
    """Wait only for a person to clear visible login/verification UI; never solve it.

    True means the window needed manual intervention. No navigation or retry is
    performed here, so response listeners remain active throughout the wait.
    """
    global _last_message
    if not (await _gate(page) or await _login_visible(page)):
        return False
    if headless:
        raise CollectorError("闲鱼要求登录或平台验证；无界面采集已停止。请使用默认可见采集窗口手动完成验证。")
    _last_message = "采集窗口需要你手动完成闲鱼登录或平台验证；正在等待，最多120秒。"

    async def poll():
        for _ in range(max(1, math.ceil(timeout_seconds / 2))):
            await page.wait_for_timeout(min(2, timeout_seconds) * 1000)
            if not (await _gate(page) or await _login_visible(page)):
                return
        raise TimeoutError

    try:
        await asyncio.wait_for(poll(), timeout=timeout_seconds)
    except TimeoutError:
        _last_message = "手动登录或平台验证等待超时（120秒）；本次采集已停止，请完成官网验证后再手动运行。"
        raise CollectorError(_last_message) from None
    _last_message = "平台验证提示已消失，继续等待网页自身的搜索响应。"
    return True


def _successful_search_payload(payload) -> bool:
    try:
        _success(payload)
    except CollectorError:
        return False
    return isinstance(_mapping(payload.get("data")).get("resultList"), list)


def _item_url_matches(url: str, item_id: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname in {"www.goofish.com", "goofish.com"}
            and parsed.path.rstrip("/") == "/item"
            and parse_qs(parsed.query).get("id") == [item_id])


def _is_search_response(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname == "h5api.m.goofish.com"
            and parsed.path.lower().startswith(f"/h5/{SEARCH_API}/"))


async def _detail_observation(page, old: dict, headless: bool = True) -> dict | None:
    """Revisit known items. Only an item's own explicit UI status counts as sold.

    No numeric private status code is guessed. Unknown DOM layouts stay unknown.
    Top status components only: recommendations and arbitrary body text are ignored.
    """
    item_id = str(old["item_id"])
    await page.goto("https://www.goofish.com/item?" + urlencode({"id": item_id}), wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)
    await _wait_ready(page, headless)
    if not _item_url_matches(page.url, item_id):
        raise CollectorError("详情页跳转离开了目标商品，无法绑定状态证据；请检查登录或页面变化。")
    candidates = page.locator('[class*="itemStatus"], [class*="item-status"], [class*="detailStatus"], [class*="detail-status"], [class*="tradeStatus"], [class*="reason-text"]')
    sold_pattern = re.compile(r"^(?:该)?(?:宝贝|商品)?已(?:经)?(?:售出|卖出)[！!。]?$" )
    down_pattern = re.compile(r"^(?:该)?(?:宝贝|商品)已下架[！!。]?$" )
    for index in range(min(await candidates.count(), 80)):
        element = candidates.nth(index)
        if not await element.is_visible():
            continue
        text = (await element.inner_text(timeout=1500)).strip()
        if not (sold_pattern.fullmatch(text) or down_pattern.fullmatch(text)):
            continue
        # Reject status badges within another item's link or recommendation cards.
        eligible = await element.evaluate("""(el) => {
          if (el.closest('[class*="recommend" i], [class*="guess" i], [class*="feeds" i]')) return false;
          const a = el.closest('a');
          if (a) return false;
          const r = el.getBoundingClientRect();
          return r.top >= 0 && r.top < 650;
        }""")
        if eligible:
            return {**{k: old[k] for k in ("item_id", "title", "price", "category", "model") if k in old},
                    "url": "https://www.goofish.com/item?" + urlencode({"id": item_id}),
                    "status": "sold" if sold_pattern.fullmatch(text) else "delisted",
                    "evidence": f"该商品详情页顶部状态组件显示：{text}；item_id={item_id}。价格沿用末次挂牌价，实际成交价未知。",
                    "observed_at": datetime.now(timezone.utc).isoformat()}
    # Do not fabricate a fresh price/state if the detail layout is unrecognized.
    return {**{k: old[k] for k in ("item_id", "title", "price", "category", "model") if k in old},
            "status": "unknown", "evidence": "复查了详情，但未识别到该商品明确状态组件；不视为成交或下架，价格沿用末次挂牌。",
            "observed_at": datetime.now(timezone.utc).isoformat()}


async def collect(task: dict) -> list[dict]:
    global _last_message
    if not SESSION.exists():
        raise CollectorError("尚未保存闲鱼登录态，请先在采集中心打开本机登录窗口。")
    _claim()
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            headless = os.environ.get("XIANYU_HEADLESS", "0") == "1"
            context = await _context(p, headless=headless)
            pending = set()
            try:
                page = await context.new_page()
                payloads = []

                async def capture(response):
                    try:
                        payloads.append(await asyncio.wait_for(response.json(), timeout=10))
                    except Exception:
                        payloads.append({"parse_error": True})

                def on_response(response):
                    if not _is_search_response(response.url):
                        return
                    job = asyncio.create_task(capture(response))
                    pending.add(job)
                    job.add_done_callback(pending.discard)

                page.on("response", on_response)
                await page.goto("https://www.goofish.com/search?" + urlencode({"q": task["keyword"]}), wait_until="domcontentloaded", timeout=60000)
                manually_verified = await _wait_ready(page, headless)
                attempts = 0
                while attempts < 10:
                    if await _gate(page) or await _login_visible(page):
                        if manually_verified:
                            raise CollectorError("平台验证提示再次出现，本次采集已停止。请在官网确认可正常搜索后再手动运行。")
                        manually_verified = await _wait_ready(page, headless)
                        attempts = 0  # allow ten seconds for the page's own response after verification
                    if any(_successful_search_payload(payload) for payload in payloads):
                        break
                    await page.wait_for_timeout(1000)
                    attempts += 1
                if pending:
                    await asyncio.wait_for(asyncio.gather(*pending), timeout=12)
                page.remove_listener("response", on_response)
                if await _gate(page) or await _login_visible(page):
                    raise CollectorError("闲鱼要求登录或平台验证，本次采集已停止。请重新打开登录窗口完成验证。")
                if not payloads:
                    raise CollectorError("未观测到闲鱼搜索响应。可能登录失效、页面受限或接口已更改；请在官网验证后重试。")
                if manually_verified and any(_successful_search_payload(payload) for payload in payloads):
                    # A challenge failure seen before human verification cannot poison
                    # the successful response the page generated after it was cleared.
                    payloads = [payload for payload in payloads if _successful_search_payload(payload)]
                records = []
                for payload in payloads:
                    records.extend(parse_search_payload(payload))
                # One normal search page per run; never scroll indefinitely.
                records = list({r["item_id"]: r for r in records}.values())[:40]
                fresh_ids = {r["item_id"] for r in records}
                for old in task.get("tracked_items", [])[:10]:
                    if str(old["item_id"]) in fresh_ids:
                        continue
                    await page.wait_for_timeout(2500)
                    record = await asyncio.wait_for(_detail_observation(page, old, headless=headless), timeout=55 if headless else 185)
                    if record:
                        records.append(record)
                _last_message = f"本次观测到 {len(records)} 条记录；只覆盖搜索样本和有限详情复查。"
                return records
            finally:
                for job in list(pending):
                    job.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await context.close()
    except CollectorError as exc:
        _last_message = str(exc)
        raise
    except Exception:
        raise CollectorError("浏览器采集失败，可能是网络、浏览器占用或页面发生变化。请检查登录和终端环境后重试。") from None
    finally:
        _release()
