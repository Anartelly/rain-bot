import asyncio
import base64
import json
import os
import re
import subprocess
import time
import urllib.request
import urllib.parse
import uuid
from playwright.async_api import async_playwright

BOT_TOKEN = "8935522730:AAH2sDOqtf4kCk_-YWaW1Npz0IDtjgRYqOM"
SESSION_FILE = "session.json"

RUN_DURATION = 52 * 60  # 52 минуты работы одной смены
COOLDOWN = 210          # 3.5 минуты паузы после уведомления, чтобы не дублировать один и тот же рейн

last_alert = 0
widget_state = "Загрузка..."
CHAT_ID = None
last_update_id = 0
auth_stage = "IDLE"
session_saved_this_run = False


def log(msg: str):
    print(msg, flush=True)


def _send_tg_sync(text: str):
    if not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        log(f"[TG] Отправлено: {text}")
    except Exception as e:
        log(f"[TG] Ошибка отправки текста: {e}")


async def send_tg(text: str):
    await asyncio.to_thread(_send_tg_sync, text)


def _send_photo_sync(image_bytes: bytes, caption: str):
    if not CHAT_ID or not image_bytes:
        return
    boundary = uuid.uuid4().hex
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{CHAT_ID}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="screen.jpg"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode("utf-8") + image_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        log(f"[TG] Ошибка отправки фото: {e}")


async def send_photo_bytes_tg(image_bytes: bytes, caption: str):
    await asyncio.to_thread(_send_photo_sync, image_bytes, caption)


def get_active_page(context):
    pages = [p for p in context.pages if not p.is_closed()]
    return pages[-1] if pages else None


def get_rustmagic_page(context, fallback_page):
    pages = [p for p in context.pages if not p.is_closed() and "rustmagic.com" in p.url]
    if pages:
        return pages[-1]
    active = get_active_page(context)
    return active if active else fallback_page


async def fast_screenshot(page) -> bytes:
    try:
        cdp = await page.context.new_cdp_session(page)
        res = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 80})
        await cdp.detach()
        return base64.b64decode(res["data"])
    except Exception as e:
        log(f"Ошибка скриншота: {e}")
        return b""


async def send_screen(page, caption: str):
    img = await fast_screenshot(page)
    if img:
        await send_photo_bytes_tg(img, caption)


async def save_session_to_github(context, notify=True):
    global session_saved_this_run
    try:
        await context.storage_state(path=SESSION_FILE)
        subprocess.run(["git", "config", "user.name", "rain-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "bot@github.com"], check=False)
        subprocess.run(["git", "add", SESSION_FILE], check=False)
        subprocess.run(["git", "commit", "-m", "Save login session"], check=False)
        subprocess.run(["git", "push"], check=False)
        session_saved_this_run = True
        log("Сессия успешно сохранена в session.json")
        if notify:
            await send_tg("💾 Сессия аккаунта сохранена в GitHub (session.json)!")
    except Exception as e:
        log(f"Ошибка сохранения сессии: {e}")


async def start_login_process(context, main_page):
    global auth_stage
    page = get_rustmagic_page(context, main_page)
    try:
        await send_tg("🔐 Нажимаю кнопку Sign In на сайте...")
        await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, a, [role="button"], div'));
            for (const b of btns) {
                const t = (b.innerText || '').trim().toLowerCase();
                if (t === 'sign in' || t === 'login' || t === 'log in') {
                    b.click();
                    return true;
                }
            }
            return false;
        }""")

        await asyncio.sleep(4)

        await page.evaluate("""() => {
            const checkboxes = Array.from(document.querySelectorAll('input[type="checkbox"], [role="checkbox"]'));
            checkboxes.forEach(cb => { if (!cb.checked) cb.click(); });

            const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            for (const b of btns) {
                const t = (b.innerText || '').trim().toLowerCase();
                if (t.includes('steam') || t.includes('sign in with') || t.includes('continue')) {
                    b.click();
                    break;
                }
            }
        }""")

        await asyncio.sleep(5)
        active_page = get_active_page(context)
        auth_stage = "WAIT_LOGIN"
        await send_screen(
            active_page,
            "🔑 Шаг 1: Отправь логин и пароль:\n"
            "логин ТВОЙ_ЛОГИН ТВОЙ_ПАРОЛЬ\n\n"
            "Или вручную: клик X Y ТЕКСТ"
        )
    except Exception as e:
        await send_tg(f"⚠️ Ошибка при открытии входа: {e}")


async def submit_credentials(context, username: str, password: str):
    global auth_stage
    page = get_active_page(context)
    try:
        await send_tg("⏳ Ввожу логин и пароль...")
        text_inputs = page.locator('input[type="text"]:visible, input[type="email"]:visible')
        pass_inputs = page.locator('input[type="password"]:visible')

        if await text_inputs.count() > 0 and await pass_inputs.count() > 0:
            await text_inputs.first.click(timeout=5000)
            await text_inputs.first.fill(username)
            await asyncio.sleep(0.4)
            await pass_inputs.first.click(timeout=5000)
            await pass_inputs.first.fill(password)
            await asyncio.sleep(0.4)
            await page.keyboard.press("Enter")
        else:
            await page.keyboard.type(username, delay=80)
            await page.keyboard.press("Tab")
            await page.keyboard.type(password, delay=80)
            await page.keyboard.press("Enter")

        await asyncio.sleep(6)
        auth_stage = "WAIT_CODE"
        await send_screen(
            get_active_page(context),
            "📩 Шаг 2: Отправь 5-значный код с почты/Steam:\n"
            "код XXXXX\n\n"
            "(Либо кликни вручную: клик X Y XXXXX)"
        )
    except Exception as e:
        await send_tg(f"⚠️ Ошибка ввода логина/пароля: {e}")
        await send_screen(page, "Экран после ошибки")


async def submit_guard_code(context, main_page, code: str):
    global auth_stage
    page = get_active_page(context)
    try:
        await send_tg(f"⏳ Печатаю код: {code}...")
        await page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type="hidden"]):not([type="password"]):not([type="checkbox"])'))
                .filter(el => el.offsetParent !== null);
            if (inputs.length > 0) inputs[0].focus();
        }""")
        await asyncio.sleep(0.3)

        await page.keyboard.type(code, delay=150)
        await asyncio.sleep(1)
        await page.keyboard.press("Enter")

        await asyncio.sleep(6)

        page = get_active_page(context)
        if "steamcommunity.com" in page.url:
            clicked = await page.evaluate("""() => {
                const btn = document.querySelector('#imageLogin') ||
                    Array.from(document.querySelectorAll('input[type="submit"], button[type="submit"]'))
                        .find(el => el.offsetParent !== null);
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            }""")
            if clicked:
                await asyncio.sleep(6)

        auth_stage = "DONE"
        await save_session_to_github(context, notify=True)
        rm_page = get_rustmagic_page(context, main_page)
        await rm_page.bring_to_front()
        await send_screen(
            rm_page,
            "✅ Вход выполнен и сохранен! Мониторинг Rain работает без задержек."
        )
    except Exception as e:
        await send_tg(f"⚠️ Ошибка при вводе кода: {e}")
        await send_screen(get_active_page(context), "Текущий экран")


def init_chat_id():
    global CHAT_ID, last_update_id
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("result", []):
                last_update_id = item["update_id"]
                msg = item.get("message") or item.get("edited_message")
                if msg and "chat" in msg:
                    CHAT_ID = msg["chat"]["id"]
    except Exception as e:
        log(f"Ошибка init_chat_id: {e}")


def _fetch_updates_sync(offset: int):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8")).get("result", [])


async def check_telegram_updates(context, main_page):
    global CHAT_ID, last_update_id
    try:
        results = await asyncio.to_thread(_fetch_updates_sync, last_update_id + 1)
        for item in results:
            last_update_id = item["update_id"]
            msg = item.get("message") or item.get("edited_message")
            if not msg or "chat" not in msg:
                continue

            CHAT_ID = msg["chat"]["id"]
            raw_text = (msg.get("text") or "").strip()
            if not raw_text:
                continue
            low_text = raw_text.lower()
            page = get_active_page(context)

            if low_text in ["вход", "войти", "/login", "login"]:
                await start_login_process(context, main_page)

            elif low_text.startswith("логин ") or low_text.startswith("login "):
                parts = raw_text.split(maxsplit=2)
                if len(parts) >= 3:
                    await submit_credentials(context, parts[1], parts[2])
                else:
                    await send_tg("⚠️ Формат: логин ТВОЙ_ЛОГИН ТВОЙ_ПАРОЛЬ")

            elif low_text.startswith("код ") or low_text.startswith("code "):
                parts = raw_text.split(maxsplit=1)
                if len(parts) >= 2:
                    await submit_guard_code(context, main_page, parts[1].strip())

            elif auth_stage == "WAIT_CODE" and len(raw_text) == 5 and " " not in raw_text:
                await submit_guard_code(context, main_page, raw_text)

            elif low_text.startswith("ввод ") or low_text.startswith("type "):
                txt = raw_text.split(maxsplit=1)[1]
                await page.keyboard.type(txt, delay=120)
                await asyncio.sleep(0.5)
                await page.keyboard.press("Enter")
                await asyncio.sleep(4)
                await send_screen(get_active_page(context), f"⌨️ Введен текст: {txt}")

            elif low_text in ["энтер", "enter"]:
                await page.keyboard.press("Enter")
                await asyncio.sleep(4)
                await send_screen(get_active_page(context), "⌨️ Нажат Enter")

            elif low_text in ["сохранить", "save"]:
                await save_session_to_github(context, notify=True)
                await send_screen(get_active_page(context), "Экран при сохранении")

            elif low_text.startswith("клик") or low_text.startswith("click"):
                parts = raw_text.split(maxsplit=3)
                if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                    x, y = int(parts[1]), int(parts[2])
                    await page.mouse.click(x, y)
                    await asyncio.sleep(0.5)
                    if len(parts) == 4:
                        await page.keyboard.type(parts[3], delay=120)
                        await asyncio.sleep(0.5)
                        await page.keyboard.press("Enter")
                    await asyncio.sleep(4)
                    await send_screen(get_active_page(context), f"🖱 Клик ({x}, {y})")
                else:
                    await send_tg("⚠️ Формат: клик X Y  или  клик X Y ТЕКСТ")

            else:
                await send_tg(
                    f"🟢 Бот на связи (опрос каждые 0.3 сек)!\n"
                    f"📊 Виджет: {widget_state}\n\n"
                    f"Команды:\n"
                    f"• клик X Y — нажать мышкой\n"
                    f"• клик X Y ТЕКСТ — нажать и вписать текст\n"
                    f"• ввод ТЕКСТ — напечатать по буквам + Enter\n"
                    f"• сохранить — сохранить вход в GitHub"
                )
                await send_screen(page, "Текущий экран")
    except Exception:
        pass


def extract_amount(text: str) -> str:
    clean = re.sub(r'\b\d{1,2}\s*:\s*\d{2}\b', ' ', text)
    clean = re.sub(r'\b\d+\s*(?:min|sec|m|s|h|hour)[a-z]*\s*ago\b', ' ', clean, flags=re.IGNORECASE)
    clean = re.sub(r'(\d+)\s*[.,]\s*(\d{2})\b', r'\1.\2', clean)

    matches = re.findall(r'\b\d+\.\d{2}\b', clean)
    if matches:
        return matches[0]

    int_matches = re.findall(r'\b\d+\b', clean)
    if int_matches:
        return f"{int(int_matches[0]):.2f}"
    return "???"


async def trigger_rain(amount: str):
    global last_alert
    now = time.time()
    if now - last_alert < COOLDOWN:
        return
    last_alert = now
    msg = f"Its raining: {amount}"
    log(f"🔥 НАЧАЛСЯ РЕЙН: {msg}")
    await send_tg(msg)


async def check_rustmagic(page):
    global widget_state
    try:
        info = await page.evaluate(r"""() => {
            const allElements = Array.from(document.querySelectorAll('div, section, aside, button'));

            let rainContainerText = null;
            let isRainActive = false;

            for (const el of allElements) {
                const rect = el.getBoundingClientRect();
                if (rect.left > 360 || rect.top > 300 || rect.width < 50 || rect.height < 20) continue;

                const rawText = (el.innerText || el.textContent || '').trim();
                const low = rawText.toLowerCase().replace(/’/g, "'");

                if (low.includes("it's raining") || low.includes("its raining") || low.includes("join rain")) {
                    isRainActive = true;
                    if (/\d/.test(rawText) && rawText.length < 250) {
                        if (!rainContainerText || rawText.length < rainContainerText.length) {
                            rainContainerText = rawText;
                        }
                    }
                }
            }

            if (isRainActive) {
                if (!rainContainerText) {
                    const topLeftTexts = allElements
                        .filter(el => {
                            const r = el.getBoundingClientRect();
                            return r.left < 320 && r.top >= 50 && r.top < 250 && r.height > 50 && r.height < 200;
                        })
                        .map(el => (el.innerText || el.textContent || '').trim())
                        .filter(t => t.length > 5 && t.length < 300);
                    rainContainerText = topLeftTexts[0] || "It's Raining!";
                }
                return {
                    state: "🔥 ИДЕТ РЕЙН: " + rainContainerText.replace(/\s+/g, ' '),
                    active: true,
                    text: rainContainerText
                };
            }

            for (const el of allElements) {
                const rect = el.getBoundingClientRect();
                if (rect.left > 360 || rect.top > 300) continue;
                const t = (el.innerText || el.textContent || '').trim();
                const low = t.toLowerCase();
                if (t.length > 5 && t.length < 200 && low.includes("coin rain")) {
                    return {
                        state: "Ожидание: " + t.replace(/\s+/g, ' '),
                        active: false,
                        text: t
                    };
                }
            }

            return { state: "Виджет не виден на экране", active: false, text: "" };
        }""")

        widget_state = info["state"]
        if info["active"]:
            amount = extract_amount(info["text"])
            if amount != "???":
                await trigger_rain(amount)
    except Exception as e:
        widget_state = f"Ошибка: {e}"


async def is_logged_in(page) -> bool:
    try:
        return await page.evaluate("""() => {
            const bodyText = (document.body.innerText || '').toLowerCase();
            if (bodyText.includes('log out') || bodyText.includes('welcome back') || bodyText.includes('+ deposit')) {
                return true;
            }
            const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            const hasSignIn = btns.some(b => (b.innerText || '').trim().toLowerCase() === 'sign in');
            return !hasSignIn;
        }""")
    except Exception:
        return False


async def main():
    global session_saved_this_run
    init_chat_id()
    log(f"Запуск мониторинга RustMagic. CHAT_ID: {CHAT_ID}")

    start_time = time.time()
    async with async_playwright() as p:
        # Флаги отключают фоновое засыпание вкладки и задержку таймеров в Chromium
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--window-size=1920,1080",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding"
            ]
        )

        context_kwargs = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "viewport": {"width": 1920, "height": 1080}
        }
        if os.path.exists(SESSION_FILE):
            log("Загружаю сохраненную сессию из session.json...")
            context_kwargs["storage_state"] = SESSION_FILE
            session_saved_this_run = True

        context = await browser.new_context(**context_kwargs)
        main_page = await context.new_page()

        try:
            log("Открываю https://rustmagic.com...")
            await main_page.goto("https://rustmagic.com", wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(10)
        except Exception as e:
            log(f"Ошибка загрузки: {e}")

        rm_page = get_rustmagic_page(context, main_page)
        await rm_page.bring_to_front()
        await check_rustmagic(rm_page)

        if await is_logged_in(rm_page):
            await send_screen(rm_page, f"✅ Сайт открыт (вход уже выполнен)!\n📊 {widget_state}")
        else:
            await send_tg("🔓 Вход еще не выполнен. Начинаю авторизацию...")
            await start_login_process(context, rm_page)

        # Быстрый цикл мониторинга (проверка 3 раза в секунду)
        last_tg_check = 0
        while time.time() - start_time < RUN_DURATION:
            rm_page = get_rustmagic_page(context, main_page)
            await check_rustmagic(rm_page)

            now = time.time()
            if now - last_tg_check >= 2.0:
                last_tg_check = now
                if not session_saved_this_run and "rustmagic.com" in rm_page.url:
                    if await is_logged_in(rm_page):
                        await save_session_to_github(context, notify=True)
                await check_telegram_updates(context, main_page)

            await asyncio.sleep(0.3)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
