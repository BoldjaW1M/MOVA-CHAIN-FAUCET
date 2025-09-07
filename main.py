# main.py — Mars Faucet (headless, no solver), per-address proxy, DEBUG FULL
# Playwright >= 1.46
# Cara pakai ringkas:
#   pip install playwright==1.46.0 && python -m playwright install chromium
#   isi address.txt & proxies.txt, lalu: python main.py

import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, List

from playwright.async_api import async_playwright, BrowserContext, Page, Error as PWError

# ====== Branding / Colors ======
# Pakai colorama kalau ada (Windows friendly), fallback ke ANSI
GREEN = "\033[92m"
RESET = "\033[0m"
try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
    GREEN = Fore.GREEN
    RESET = Style.RESET_ALL
except Exception:
    pass

def pad(n: int = 1):
    """Print n blank lines untuk jarak log."""
    print("\n" * n, end="")

def banner():
    print("=" * 50)
    print(f"   🚀 {GREEN}Follow https://x.com/BoldjaW1M{RESET} 🚀")
    print("=" * 50)

FAUCET_URL = "https://faucet.mars.movachain.com"

# ==== Konfigurasi umum ====
HEADLESS = True
CONCURRENCY = 2
ACTION_DELAY = (2.0, 5.0)         # detik
RETRIES = 2                        # retry ringan
SAVE_CSV = True                    # set False jika tak perlu CSV

# Timeout (ms)
TIMEOUT_GOTO_MS = 30000           # page.goto
TIMEOUT_WAIT_SEL_MS = 8000        # tunggu selector
TASK_WATCHDOG_S = 120             # timeout per-address task (hard cap)

# Output
OUT_DIR = "out"
RESULT_CSV = os.path.join(OUT_DIR, "results.csv")

# ====== Selector heuristik (ubah jika UI berubah) ======
SELECTORS = {
    "address_input": [
        'input[placeholder*="address" i]',
        'input[placeholder*="wallet" i]',
        'input[type="text"]',
        'input',
    ],
    "claim_button": [
        'button:has-text("claim")',
        'button:has-text("get")',
        'button:has-text("request")',
        'button',
        '[role="button"]',
    ],
    "status_banner": [
        '[class*="toast"]',
        '[class*="alert"]',
        '[data-status]',
        'div[role="status"]',
    ],
}

# Heuristik klasifikasi pesan
SUCCESS_HINTS = ["success", "claimed", "ok", "done"]
ALREADY_HINTS = ["already", "once", "claimed", "limit", "duplicate"]
RATE_HINTS    = ["rate", "too many", "wait", "cooldown", "busy"]
CAPTCHA_HINTS = ["captcha", "hcaptcha", "recaptcha", "human"]

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# ===== Utilities =====
def dlog(s: str):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {s}", flush=True)

@dataclass
class ProxyConf:
    server: str
    username: Optional[str] = None
    password: Optional[str] = None

def parse_proxy_url(url: str) -> ProxyConf:
    m = re.match(r"^(http|https)://(?:(?P<user>[^:@]+):(?P<pw>[^@]+)@)?(?P<hostport>[^/]+)$", url.strip())
    if not m:
        raise ValueError(f"Proxy format invalid: {url}")
    server = f"{m.group(1)}://{m.group('hostport')}"
    return ProxyConf(server=server, username=m.group("user"), password=m.group("pw"))

def load_lines(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]

def validate_address(addr: str) -> bool:
    return bool(ADDRESS_RE.match(addr))

def rand_delay() -> float:
    return random.uniform(*ACTION_DELAY)

def ensure_outdir():
    os.makedirs(OUT_DIR, exist_ok=True)
    if SAVE_CSV and not os.path.exists(RESULT_CSV):
        with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["timestamp", "address", "status", "message", "proxy", "ip"])

def classify_message(msg: str) -> Tuple[str, str]:
    mlow = (msg or "").lower()
    if any(k in mlow for k in CAPTCHA_HINTS):
        return ("captcha", msg)
    if any(k in mlow for k in ALREADY_HINTS):
        return ("already", msg)
    if any(k in mlow for k in RATE_HINTS):
        return ("rate_limited", msg)
    if any(k in mlow for k in SUCCESS_HINTS):
        return ("success", msg)
    return ("unknown", msg)

# ===== Playwright helpers =====
async def wait_and_type(page: Page, selector_candidates: List[str], text: str) -> bool:
    for sel in selector_candidates:
        try:
            await page.wait_for_selector(sel, timeout=TIMEOUT_WAIT_SEL_MS)
            dlog(f"  - Typing address into selector: {sel}")
            await page.fill(sel, text)
            return True
        except Exception:
            continue
    return False

async def wait_and_click(page: Page, selector_candidates: List[str]) -> bool:
    for sel in selector_candidates:
        try:
            await page.wait_for_selector(sel, timeout=TIMEOUT_WAIT_SEL_MS)
            dlog(f"  - Clicking button: {sel}")
            await page.click(sel)
            return True
        except Exception:
            continue
    try:
        dlog("  - Fallback: clicking first role=button")
        await page.get_by_role("button").first.click(timeout=2000)
        return True
    except Exception:
        return False

async def sniff_api_message(page: Page) -> Optional[str]:
    """Ambil pesan dari XHR/fetch yang mengandung kata kunci endpoint."""
    dlog("  - Sniffing API responses...")
    found_msg = None

    def pick_message_from_json(js: dict) -> Optional[str]:
        for k in ("message", "msg", "detail", "error", "status"):
            if k in js and isinstance(js[k], (str, int, float)):
                return str(js[k])
        try:
            return json.dumps(js)[:300]
        except Exception:
            return None

    t_end = time.time() + 5.0
    responses = []

    def _resp_handler(resp):
        try:
            url = resp.url.lower()
            if any(s in url for s in ["claim", "faucet", "drip", "/api/"]):
                responses.append(resp)
        except Exception:
            pass

    page.on("response", _resp_handler)
    while time.time() < t_end:
        await asyncio.sleep(0.2)

    for resp in responses:
        try:
            ctype = resp.headers.get("content-type", "").lower()
            if "application/json" in ctype:
                js = await resp.json()
                msg = pick_message_from_json(js)
                if msg:
                    found_msg = msg
                    break
            else:
                txt = (await resp.text())[:500]
                if txt:
                    found_msg = txt
                    break
        except Exception:
            continue
    dlog(f"  - API message: {found_msg!r}")
    return found_msg

async def check_captcha_presence(page: Page) -> bool:
    """Deteksi kasar keberadaan hCaptcha/reCAPTCHA (iframe/src/teks)."""
    try:
        els = await page.query_selector_all("iframe")
        for el in els:
            src = (await el.get_attribute("src") or "").lower()
            if any(k in src for k in ["hcaptcha", "recaptcha"]):
                return True
        body = (await page.content()).lower()
        if any(k in body for k in ["hcaptcha", "recaptcha"]):
            return True
    except Exception:
        pass
    return False

async def get_public_ip_via_context(context: BrowserContext) -> str:
    try:
        resp = await context.request.get("https://api.ipify.org?format=json", timeout=15000)
        if resp.ok:
            j = await resp.json()
            return j.get("ip", "")
    except Exception:
        pass
    return ""

async def claim_once(context: BrowserContext, address: str) -> Tuple[str, str]:
    """Return (status, message). status: success|already|captcha|rate_limited|unknown|error"""
    page = await context.new_page()

    # Forward console logs & page errors agar kelihatan di terminal
    page.on("console", lambda msg: dlog(f"  [page.console] {msg.type().upper()}: {msg.text()}"))
    page.on("pageerror", lambda exc: dlog(f"  [page.error] {exc}"))

    try:
        dlog(f"[{address}] Navigating to {FAUCET_URL} ...")
        await page.goto(FAUCET_URL, timeout=TIMEOUT_GOTO_MS, wait_until="domcontentloaded")
        dlog(f"[{address}] Page loaded (domcontentloaded)")
        await asyncio.sleep(rand_delay())

        # Captcha on landing?
        if await check_captcha_presence(page):
            dlog(f"[{address}] Captcha detected on landing → skip")
            return ("captcha", "Captcha detected on page load")

        # Isi address
        ok = await wait_and_type(page, SELECTORS["address_input"], address)
        if not ok:
            inputs = await page.query_selector_all("input")
            if inputs:
                dlog(f"[{address}] Fallback: typing into first <input>")
                await inputs[0].fill(address)
            else:
                return ("error", "Address input not found")

        await asyncio.sleep(rand_delay())

        # Klik claim
        ok = await wait_and_click(page, SELECTORS["claim_button"])
        if not ok:
            return ("error", "Claim button not found")

        await asyncio.sleep(rand_delay())

        # Captcha after click?
        if await check_captcha_presence(page):
            dlog(f"[{address}] Captcha required after clicking → skip")
            return ("captcha", "Captcha required after clicking claim")

        # Ambil pesan dari API/banner
        msg = await sniff_api_message(page)
        if not msg:
            for sel in SELECTORS["status_banner"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        text = (await el.text_content() or "").strip()
                        if text:
                            msg = text
                            break
                except Exception:
                    continue

        if not msg:
            msg = "No explicit message; UI/response not captured."

        status, message = classify_message(msg)
        dlog(f"[{address}] Classified: {status} — {message}")
        return (status, msg or message)

    except PWError as e:
        dlog(f"[{address}] Playwright error: {e}")
        return ("error", f"PlaywrightError: {e}")
    except Exception as e:
        dlog(f"[{address}] Exception: {type(e).__name__}: {e}")
        return ("error", f"{type(e).__name__}: {e}")
    finally:
        try:
            await page.close()
        except Exception:
            pass

async def make_context(pw, proxy: ProxyConf):
    """Launch browser in per-context proxy mode, then apply real proxy on context."""
    # Penting: aktifkan mode per-context proxy saat LAUNCH
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        proxy={"server": "http://per-context"},   # wajib untuk per-context proxy
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    )
    context = await browser.new_context(
        proxy={
            "server": proxy.server,
            "username": proxy.username,
            "password": proxy.password
        } if proxy else None,
        viewport={"width": 1280, "height": 800},
    )
    return browser, context

async def proxy_sanity_check(context: BrowserContext, address: str, proxy: ProxyConf) -> Tuple[bool, str]:
    """Cek IP via proxy. Kalau gagal, tandai proxy_failed."""
    dlog(f"[{address}] Checking proxy connectivity via {proxy.server} ...")
    ip = await get_public_ip_via_context(context)
    if not ip:
        dlog(f"[{address}] Proxy check FAILED (no IP)")
        return False, ""
    dlog(f"[{address}] Proxy OK, IP={ip}")
    return True, ip

async def process_address(pw, address: str, proxy: ProxyConf, writer: Optional[csv.writer]):
    browser = None
    context = None
    status, message, ip_info = "error", "uninitialized", ""

    try:
        pad()  # jarak antar akun
        dlog(f"[{address}] ===== START (proxy {proxy.server}) =====")
        browser, context = await make_context(pw, proxy)

        # Cek proxy dulu supaya gak nyangkut di goto()
        ok, ip = await proxy_sanity_check(context, address, proxy)
        ip_info = ip
        if not ok:
            status, message = "proxy_failed", "Cannot fetch IP via proxy"
            raise RuntimeError(message)

        # Klaim
        for attempt in range(1, RETRIES + 1):
            dlog(f"[{address}] Attempt {attempt}/{RETRIES}")
            status, message = await claim_once(context, address)
            if status in ("success", "already", "captcha"):
                break
            if status == "rate_limited":
                sleep_s = 8 + attempt * 4
                dlog(f"[{address}] Rate limited, retry in {sleep_s}s")
                await asyncio.sleep(sleep_s)
                continue
            # retry umum
            await asyncio.sleep(2 + attempt)

        pad()
        dlog(f"[{address}] RESULT: {status} — {message}")
        ts = datetime.utcnow().isoformat()
        row = [ts, address, status, message, f"{proxy.server}", ip_info]
        if SAVE_CSV and writer:
            writer.writerow(row)

    except Exception as e:
        ts = datetime.utcnow().isoformat()
        status, message = "error", f"{type(e).__name__}: {e}"
        dlog(f"[{address}] ERROR: {message}")
        if SAVE_CSV and writer:
            writer.writerow([ts, address, status, message, f"{proxy.server}", ip_info])
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        pad()
        dlog(f"[{address}] ===== END =====")

async def main():
    banner()
    time.sleep(0.2)  # jeda visual kecil
    pad()

    ensure_outdir()

    address = load_lines("address.txt")
    proxies_raw = load_lines("proxies.txt")

    if not address:
        dlog("address.txt kosong / tidak ada.")
        sys.exit(1)
    if not proxies_raw:
        dlog("proxies.txt kosong / tidak ada.")
        sys.exit(1)

    bad = [a for a in address if not validate_address(a)]
    if bad:
        dlog("Address invalid:")
        for a in bad:
            dlog(f" - {a}")
        sys.exit(1)

    proxies = [parse_proxy_url(p) for p in proxies_raw]
    def pick_proxy(i: int) -> ProxyConf:
        return proxies[i % len(proxies)]

    sem = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as pw:
        async def worker(i_addr: int, addr: str):
            async with sem:
                # watchdog per address supaya ga hang
                async def _run():
                    f = None
                    writer = None
                    try:
                        if SAVE_CSV:
                            f = open(RESULT_CSV, "a", newline="", encoding="utf-8")
                            writer = csv.writer(f)
                        await process_address(pw, addr, pick_proxy(i_addr), writer)
                    finally:
                        if f:
                            try: f.close()
                            except Exception: pass

                try:
                    await asyncio.wait_for(_run(), timeout=TASK_WATCHDOG_S)
                except asyncio.TimeoutError:
                    ts = datetime.utcnow().isoformat()
                    msg = f"Task exceeded {TASK_WATCHDOG_S}s watchdog"
                    dlog(f"[{addr}] WATCHDOG TIMEOUT — {msg}")
                    if SAVE_CSV:
                        with open(RESULT_CSV, "a", newline="", encoding="utf-8") as f:
                            csv.writer(f).writerow([ts, addr, "timeout", msg, f"{pick_proxy(i_addr).server}", ""])

        tasks = [asyncio.create_task(worker(i, addr)) for i, addr in enumerate(address)]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        dlog("Interrupted by user")
