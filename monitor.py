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
COOLDOWN = 240          # 4 минуты паузы после уведомления о рейне

last_alert = 0
widget_state = "Загрузка..."
CHAT_ID = None
last_update_id = 0
auth_stage = "IDLE"  # IDLE, WAIT_LOGIN, WAIT_CODE, DONE


def log(msg: str):
    print(msg, flush=True)


def send_tg(text: str):
    if not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        log(f"[TG] Отправлено: {text}")
    except Exception as e:
        log(f"[TG] Ошибка отправки текста: {e}")


def send_photo_bytes_tg(image_bytes: bytes, caption: str):
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


def get_active_page(context):
    """Возвращает последнюю открытую вкладку (если открылось окно входа Steam)."""
    pages = context.pages
    return pages[-1] if pages else None


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
        send_photo_bytes_tg(img, caption)


async def save_session_to_github(context):
    """Сохраняет куки входа в репозиторий, чтобы не вводить код каждый час."""
    try:
        await context.storage_state(path=SESSION_FILE)
        subprocess.run(["git", "config", "user.name", "rain-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "bot@github.com"], check=False)
        subprocess.run(["git", "add", SESSION_FILE], check=False)
        subprocess.run(["git", "commit", "-m", "Save login session"], check=False)
        subprocess.run(["git", "push"], check=False)
        log("Сессия успешно сохранена в session.json")
    except Exception as e:
        log(f"Ошибка сохранения сессии: {e}")


async def start_login_process(context, main_page):
    """Нажимает Sign In на RustMagic и открывает форму входа."""
    global auth_stage
    try:
        send_tg("🔐 Нажимаю кнопку Sign In на сайте...")
        # Ищем кнопку Sign In
        clicked = await main_page.evaluate("""() => {
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

        # Если появилось окно с галочками (18+ / Terms) и кнопкой Steam — ставим галочки и жмем
        await main_page.evaluate("""() => {
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
            "🔑 Шаг 1: Отправь боту логин и пароль через пробел:\n"
            "логин ТВОЙ_ЛОГИН ТВОЙ_ПАРОЛЬ\n\n"
            "(Если на экране нужно куда-то нажать вручную, напиши: клик X Y)"
        )
    except Exception as e:
        send_tg(f"⚠️ Ошибка при открытии входа: {e}")


async def submit_credentials(context, username: str, password: str):
    """Вводит логин и пароль на странице авторизации."""
    global auth_stage
    page = get_active_page(context)
    try:
        send_tg("⏳ Ввожу логин и пароль...")
        # Заполняем текстовое поле (логин) и поле пароля
        text_inputs = page.locator('input[type="text"]:visible, input[type="email"]:visible')
        pass_inputs = page.locator('input[type="password"]:visible')

        if await text_inputs.count() > 0 and await pass_inputs.count() > 0:
            await text_inputs.first.click()
            await text_inputs.first.fill(username)
            await asyncio.sleep(0.5)
            await pass_inputs.first.click()
            await pass_inputs.first.fill(password)
            await asyncio.sleep(0.5)

            submit_btn = page.locator('button[type="submit"]:visible, input[type="submit"]:visible')
            if await submit_btn.count() > 0:
                await submit_btn.first.click()
            else:
                await page.keyboard.press("Enter")
        else:
            # Резервный ввод через клавиатуру
            await page.keyboard.type(username, delay=80)
            await page.keyboard.press("Tab")
            await page.keyboard.type(password, delay=80)
            await page.keyboard.press("Enter")

        await asyncio.sleep(6)
        auth_stage = "WAIT_CODE"
        await send_screen(
            get_active_page(context),
            "📩 Шаг 2: Проверь почту (или Steam Guard) и отправь код в формате:\n"
            "код XXXXX"
        )
    except Exception as e:
        send_tg(f"⚠️ Ошибка ввода логина/пароля: {e}")


async def submit_guard_code(context, main_page, code: str):
    """Вводит 5-значный код с почты / Steam Guard и завершает вход."""
    global auth_stage
    page = get_active_page(context)
    try:
        send_tg(f"⏳ Ввожу код подтверждения: {code}...")
        # Фокусируемся на первом поле ввода кода (в новом интерфейсе Steam 5 отдельных ячеек)
        code_inputs = page.locator('input[type="text"]:visible, input[maxlength="1"]:visible, input:not([type="hidden"]):visible')
        if await code_inputs.count() > 0:
            await code_inputs.first.click()
            await asyncio.sleep(0.3)

        await page.keyboard.type(code, delay=150)
        await asyncio.sleep(1)
        await page.keyboard.press("Enter")

        await asyncio.sleep(6)

        # Если открылась страница подтверждения OpenID (кнопка "Войти" / "Sign In" на сайте Steam)
        page = get_active_page(context)
        if "steamcommunity.com" in page.url:
            openid_btn = page.locator('#imageLogin, input[type="submit"], button[type="submit"]')
            if await openid_btn.count() > 0:
                await openid_btn.first.click()
                await asyncio.sleep(6)

        auth_stage = "DONE"
        await save_session_to_github(context)
        await send_screen(
            main_page,
            "✅ Код введен, сессия сохранена! Теперь бот следит за Rain из твоего аккаунта."
        )
    except Exception as e:
        send_tg(f"⚠️ Ошибка при вводе кода: {e}")


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


async def check_telegram_updates(context, main_page):
    global CHAT_ID, last_update_id
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id + 1}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("result", []):
                last_update_id = item["update_id"]
                msg = item.get("message") or item.get("edited_message")
                if not msg or "chat" not in msg:
                    continue

                CHAT_ID = msg["chat"]["id"]
                raw_text = (msg.get("text") or "").strip()
                if not raw_text:
                    continue
                low_text = raw_text.lower()

                # 1. Команда старта входа
                if low_text in ["вход", "войти", "/login", "login"]:
                    await start_login_process(context, main_page)

                # 2. Ввод логина и пароля: "логин mylogin mypass"
                elif low_text.startswith("логин ") or low_text.startswith("login "):
                    parts = raw_text.split(maxsplit=2)
                    if len(parts) >= 3:
                        await submit_credentials(context, parts[1], parts[2])
                    else:
                        send_tg("⚠️ Напиши через пробел: логин ТВОЙ_ЛОГИН ТВОЙ_ПАРОЛЬ")

                # 3. Ввод кода с почты: "код A1B2C"
                elif low_text.startswith("код ") or low_text.startswith("code "):
                    parts = raw_text.split(maxsplit=1)
                    if len(parts) >= 2:
                        await submit_guard_code(context, main_page, parts[1].strip())
                    else:
                        send_tg("⚠️ Напиши через пробел: код XXXXX")

                # 4. Если бот уже ждет код и прислали ровно 5 символов без слова "код"
                elif auth_stage == "WAIT_CODE" and len(raw_text) == 5 and " " not in raw_text:
                    await submit_guard_code(context, main_page, raw_text)

                # 5. Ручной клик по координатам: "клик X Y"
                elif low_text.startswith("клик") or low_text.startswith("click"):
                    nums = re.findall(r'\d+', raw_text)
                    page = get_active_page(context)
                    if len(nums) >= 2:
                        x, y = int(nums[0]), int(nums[1])
                        await page.mouse.click(x, y)
                        await asyncio.sleep(4)
                        await send_screen(page, f"🖱 Клик в точке ({x}, {y})")
                    else:
                        send_tg("⚠️ Укажи координаты, например: клик 1850 60")

                # 6. Любое другое сообщение — показать текущий статус и скриншот
                else:
                    page = get_active_page(context)
                    send_tg(
                        f"🟢 Бот на связи!\n"
                        f"📊 Виджет: {widget_state}\n\n"
                        f"Команды:\n"
                        f"• вход — начать вход в аккаунт\n"
                        f"• логин ЛОГИН ПАРОЛЬ — ввести данные\n"
                        f"• код XXXXX — ввести код с почты\n"
                        f"• клик X Y — нажать мышкой в точку (X, Y)"
                    )
                    await send_screen(page, "Текущий экран")
    except Exception:
        pass


def extract_amount(text: str) -> str:
    clean = re.sub(r'\b\d{1,2}:\d{2}\b', '', text)
    clean = re.sub(r'\b\d+\s*(?:min|sec|m|s|h|hour)[a-z]*\s*ago\b', '', clean, flags=re.IGNORECASE)
    matches = re.findall(r'\b\d+[.,]\d{2}\b', clean)
    if matches:
        return matches[0].replace(",", ".")
    return "???"


def trigger_rain(amount: str):
    global last_alert
    now = time.time()
    if now - last_alert < COOLDOWN:
        return
    last_alert = now
    msg = f"Rust magic rain: {amount} coins"
    log(f"🔥 НАЧАЛСЯ РЕЙН: {msg}")
    send_tg(msg)


async def check_rustmagic(page):
    global widget_state
    try:
        info = await page.evaluate("""() => {
            const blocks = Array.from(document.querySelectorAll('div, section, aside'));

            // 1. Ищем активный рейн ("It's Raining!" или кнопку "Join Rain" / "Joined")
            let activeMatches = [];
            for (const el of blocks) {
                const t = (el.innerText || '').trim();
                const low = t.toLowerCase();
                if (t.length > 5 && t.length < 180) {
                    const isRainActive = low.includes("it's raining") || low.includes("its raining") || low.includes("join rain");
                    if (isRainActive && /\\d+[.,]\\d{2}/.test(t)) {
                        activeMatches.push(t);
                    }
                }
            }

            if (activeMatches.length > 0) {
                activeMatches.sort((a, b) => a.length - b.length);
                const best = activeMatches[0];
                return {
                    state: "🔥 ИДЕТ РЕЙН: " + best.replace(/\\n+/g, ' | '),
                    active: true,
                    text: best
                };
            }

            // 2. Ищем режим ожидания ("Coin Rain")
            let idleMatches = [];
            for (const el of blocks) {
                const t = (el.innerText || '').trim();
                const low = t.toLowerCase();
                if (t.length > 5 && t.length < 180 && low.includes("coin rain")) {
                    idleMatches.push(t);
                }
            }

            if (idleMatches.length > 0) {
                idleMatches.sort((a, b) => a.length - b.length);
                return {
                    state: "Ожидание: " + idleMatches[0].replace(/\\n+/g, ' | '),
                    active: false,
                    text: idleMatches[0]
                };
            }

            return { state: "Виджет не виден на экране", active: false, text: "" };
        }""")

        widget_state = info["state"]
        if info["active"]:
            amount = extract_amount(info["text"])
            trigger_rain(amount)
    except Exception as e:
        widget_state = f"Ошибка: {e}"


async def is_logged_in(page) -> bool:
    """Проверяет, есть ли на странице кнопка Sign In (если нет — значит уже вошли)."""
    try:
        has_sign_in = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            return btns.some(b => (b.innerText || '').trim().toLowerCase() === 'sign in');
        }""")
        return not has_sign_in
    except Exception:
        return False


async def main():
    init_chat_id()
    log(f"Запуск мониторинга RustMagic. CHAT_ID: {CHAT_ID}")

    start_time = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--window-size=1920,1080"]
        )

        # Если уже есть сохраненная сессия — подгружаем её
        context_kwargs = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "viewport": {"width": 1920, "height": 1080}
        }
        if os.path.exists(SESSION_FILE):
            log("Загружаю сохраненную сессию из session.json...")
            context_kwargs["storage_state"] = SESSION_FILE

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        try:
            log("Открываю https://rustmagic.com...")
            await page.goto("https://rustmagic.com", wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(10)
        except Exception as e:
            log(f"Ошибка загрузки: {e}")

        await check_rustmagic(page)

        # Проверяем, вошли ли мы в аккаунт
        if await is_logged_in(page):
            await send_screen(page, f"✅ Сайт открыт (вход в аккаунт уже выполнен)!\n📊 {widget_state}")
        else:
            send_tg("🔓 Ты еще не вошел в аккаунт на RustMagic. Запускаю процедуру входа...")
            await start_login_process(context, page)

        loop_count = 0
        while time.time() - start_time < RUN_DURATION:
            await check_rustmagic(page)

            loop_count += 1
            if loop_count % 2 == 0:
                await check_telegram_updates(context, page)

            await asyncio.sleep(1)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
