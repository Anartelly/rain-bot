import asyncio
import base64
import json
import os
import re
import time
import urllib.request
import urllib.parse
import uuid
from playwright.async_api import async_playwright

BOT_TOKEN = "8935522730:AAH2sDOqtf4kCk_-YWaW1Npz0IDtjgRYqOM"

RUN_DURATION = 52 * 60  # 52 минуты работы одной смены
COOLDOWN = 240          # 4 минуты паузы после старта рейна

last_alert = {"bandit": 0, "rustmagic": 0}
widget_state = {"bandit": "Не проверено", "rustmagic": "Не проверено"}
CHAT_ID = None
last_update_id = 0


def log(msg: str):
    print(msg, flush=True)


def send_tg(text: str):
    if not CHAT_ID:
        log(f"[TG] CHAT_ID не найден, сообщение пропущено: {text}")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        log(f"[TG] Отправлено: {text}")
    except Exception as e:
        log(f"[TG] Ошибка отправки текста: {e}")


def send_photo_bytes_tg(image_bytes: bytes, caption: str):
    """Отправляет картинку из памяти напрямую в Telegram."""
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
        log(f"[TG] Скриншот отправлен: {caption}")
    except Exception as e:
        log(f"[TG] Ошибка отправки фото ({caption}): {e}")


async def fast_screenshot(page) -> bytes:
    """Мгновенный скриншот через протокол Chrome (не зависает на шрифтах и анимациях)."""
    try:
        cdp = await page.context.new_cdp_session(page)
        res = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 80})
        await cdp.detach()
        return base64.b64decode(res["data"])
    except Exception as e:
        log(f"Ошибка CDP скриншота: {e}")
        return b""


async def send_status_and_screenshots(pages: dict, header: str):
    """Отправляет текстовый отчет и скриншоты обоих сайтов."""
    send_tg(
        f"{header}\n\n"
        f"📊 RustMagic:\n{widget_state['rustmagic']}\n\n"
        f"📊 Bandit.camp:\n{widget_state['bandit']}"
    )
    for key, page in pages.items():
        img = await fast_screenshot(page)
        if img:
            send_photo_bytes_tg(img, f"Экран: {key}")


def init_chat_id():
    """Находит CHAT_ID из истории сообщений при старте без ложной рассылки."""
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


async def check_telegram_updates(pages: dict):
    """Проверяет новые сообщения от пользователя во время работы цикла."""
    global CHAT_ID, last_update_id
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id + 1}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("result", []):
                last_update_id = item["update_id"]
                msg = item.get("message") or item.get("edited_message")
                if msg and "chat" in msg:
                    CHAT_ID = msg["chat"]["id"]
                    if (msg.get("text") or "").strip():
                        await send_status_and_screenshots(pages, "🟢 Бот работает (опрос каждую 1 сек)!")
    except Exception:
        pass


def extract_amount(text: str) -> str:
    clean = re.sub(r'\b\d+\s*(?:min|sec|m|s|h|hour)[a-z]*\s*ago\b', '', text, flags=re.IGNORECASE)
    clean = re.sub(r'\b\d{1,2}:\d{2}\b', '', clean)
    matches = re.findall(r'\b\d+[.,]\d{2}\b', clean)
    if matches:
        return matches[0].replace(",", ".")
    return "???"


def trigger_rain(site_key: str, amount: str, fmt: str):
    now = time.time()
    if now - last_alert[site_key] < COOLDOWN:
        return
    last_alert[site_key] = now
    msg = fmt.format(amount=amount)
    log(f"🔥 НАЧАЛСЯ РЕЙН [{site_key}]: {msg}")
    send_tg(msg)


async def check_rustmagic(page):
    try:
        info = await page.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('div, section, aside'));
            let widgetText = null;
            for (const el of all) {
                const t = (el.innerText || '').trim();
                if (t.toLowerCase().includes('coin rain') && t.length < 220) {
                    widgetText = t;
                }
            }
            if (!widgetText) return { state: "Виджет Coin Rain не виден на экране", active: false, text: "" };

            const low = widgetText.toLowerCase();
            const isIdle = low.includes('ago');

            const hasJoinBtn = Array.from(document.querySelectorAll('button, [role="button"]')).some(b => {
                const bt = (b.innerText || '').trim().toLowerCase();
                return bt.includes('join rain') || bt.includes('join to') || bt.includes('claim rain') || bt === 'join';
            });

            const hasActiveTimer = !isIdle && (
                low.includes('join') ||
                low.includes('claim') ||
                low.includes('enter') ||
                low.includes('end') ||
                low.includes('left') ||
                /\\b0?[0-3]:[0-5]\\d\\b/.test(low) ||
                /\\b[0-9]+s\\b/.test(low)
            );

            return {
                state: widgetText.replace(/\\n+/g, ' | '),
                active: (!isIdle && hasActiveTimer) || hasJoinBtn,
                text: widgetText
            };
        }""")

        widget_state["rustmagic"] = info["state"]
        if info["active"]:
            amount = extract_amount(info["text"])
            if amount != "???":
                trigger_rain("rustmagic", amount, "Rust magic rain: {amount} coins")
    except Exception as e:
        widget_state["rustmagic"] = f"Ошибка: {e}"


async def check_bandit(page):
    try:
        info = await page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button, [role="button"], a'));
            for (const b of buttons) {
                const bt = (b.innerText || '').trim().toLowerCase();
                if (bt.includes('join rain') || bt.includes('join the rain') || bt.includes('claim rain') || bt.includes('enter rain')) {
                    let parent = b.parentElement;
                    for (let i = 0; i < 4 && parent; i++) {
                        if (/\\d+[.,]\\d{2}/.test(parent.innerText || '')) {
                            return { state: "АКТИВЕН: " + parent.innerText.replace(/\\n+/g, ' | '), active: true, text: parent.innerText };
                        }
                        parent = parent.parentElement;
                    }
                    return { state: "АКТИВЕН: " + b.innerText, active: true, text: b.parentElement ? b.parentElement.innerText : bt };
                }
            }

            const blocks = Array.from(document.querySelectorAll('div, section, aside'));
            for (const el of blocks) {
                const t = (el.innerText || '').trim();
                const low = t.toLowerCase();
                if (t.length > 6 && t.length < 220 && (low.includes('scrap rain') || low.includes('rain pool') || low.includes('rain pot') || low.includes('join rain'))) {
                    if (low.includes('tipped') || low.includes('added to') || low.includes('ago')) continue;
                    const isActive = low.includes('join') || low.includes('claim') || low.includes('end') || /\\b0?[0-3]:[0-5]\\d\\b/.test(low);
                    if (isActive) {
                        return { state: "АКТИВЕН: " + t.replace(/\\n+/g, ' | '), active: true, text: t };
                    } else {
                        return { state: "Ожидание: " + t.replace(/\\n+/g, ' | '), active: false, text: t };
                    }
                }
            }
            return { state: "Виджет Rain не виден (проверь скриншот)", active: false, text: "" };
        }""")

        widget_state["bandit"] = info["state"]
        if info["active"]:
            amount = extract_amount(info["text"])
            trigger_rain("bandit", amount, "bandit camp rain - {amount} scrap")
    except Exception as e:
        widget_state["bandit"] = f"Ошибка: {e}"


async def main():
    init_chat_id()
    log(f"Запуск мониторинга. CHAT_ID: {CHAT_ID}")

    start_time = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--window-size=1920,1080"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )

        pages = {}
        for key, url in [("bandit", "https://bandit.camp"), ("rustmagic", "https://rustmagic.com")]:
            page = await context.new_page()
            try:
                log(f"[{key}] Открываю {url}...")
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(10)
                log(f"[{key}] Заголовок: {await page.title()}")
            except Exception as e:
                log(f"[{key}] Ошибка: {e}")
            pages[key] = page

        # Первая проверка виджетов после загрузки страниц
        await check_rustmagic(pages["rustmagic"])
        await check_bandit(pages["bandit"])

        # Автоматически шлем отчет и скриншоты при старте!
        await send_status_and_screenshots(pages, "✅ Сайты загружены! Начинаю мониторинг (1 сек):")

        loop_count = 0
        while time.time() - start_time < RUN_DURATION:
            await check_rustmagic(pages["rustmagic"])
            await check_bandit(pages["bandit"])

            loop_count += 1
            if loop_count % 2 == 0:
                await check_telegram_updates(pages)

            await asyncio.sleep(1)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
