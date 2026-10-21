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
COOLDOWN = 240          # 4 минуты паузы после уведомления, чтобы не спамить за один и тот же Rain

last_alert = 0
widget_state = "Загрузка..."
CHAT_ID = None
last_update_id = 0


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


async def fast_screenshot(page) -> bytes:
    try:
        cdp = await page.context.new_cdp_session(page)
        res = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 80})
        await cdp.detach()
        return base64.b64decode(res["data"])
    except Exception as e:
        log(f"Ошибка скриншота: {e}")
        return b""


async def send_status(page, header: str):
    send_tg(f"{header}\n\n📊 Статус RustMagic:\n{widget_state}")
    img = await fast_screenshot(page)
    if img:
        send_photo_bytes_tg(img, "Экран RustMagic")


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


async def check_telegram_updates(page):
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
                        await send_status(page, "🟢 Бот работает (опрос каждую 1 сек)!")
    except Exception:
        pass


def extract_amount(text: str) -> str:
    # Убираем таймер вида "01:21" и строки "27 min ago", чтобы достать только сумму монет
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

            // 1. Ищем АКТИВНЫЙ рейн ("It's Raining!" или кнопку "Join Rain")
            let activeMatches = [];
            for (const el of blocks) {
                const t = (el.innerText || '').trim();
                const low = t.toLowerCase();
                if (t.length > 5 && t.length < 160) {
                    if ((low.includes("it's raining") || low.includes("its raining") || low.includes("join rain")) && /\\d+[.,]\\d{2}/.test(t)) {
                        activeMatches.push(t);
                    }
                }
            }

            if (activeMatches.length > 0) {
                // Берем самый компактный блок (сам виджет рейна вверху чата)
                activeMatches.sort((a, b) => a.length - b.length);
                const best = activeMatches[0];
                return {
                    state: "🔥 ИДЕТ РЕЙН: " + best.replace(/\\n+/g, ' | '),
                    active: true,
                    text: best
                };
            }

            // 2. Если активного нет — ищем обычный режим ожидания ("Coin Rain")
            let idleMatches = [];
            for (const el of blocks) {
                const t = (el.innerText || '').trim();
                const low = t.toLowerCase();
                if (t.length > 5 && t.length < 160 && low.includes("coin rain")) {
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

            return { state: "Виджет не найден на экране", active: false, text: "" };
        }""")

        widget_state = info["state"]
        if info["active"]:
            amount = extract_amount(info["text"])
            trigger_rain(amount)
    except Exception as e:
        widget_state = f"Ошибка: {e}"


async def main():
    init_chat_id()
    log(f"Запуск мониторинга RustMagic. CHAT_ID: {CHAT_ID}")

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

        page = await context.new_page()
        try:
            log("Открываю https://rustmagic.com...")
            await page.goto("https://rustmagic.com", wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(10)
        except Exception as e:
            log(f"Ошибка загрузки: {e}")

        await check_rustmagic(page)
        await send_status(page, "✅ RustMagic загружен! Мониторинг активен (каждую 1 сек):")

        loop_count = 0
        while time.time() - start_time < RUN_DURATION:
            await check_rustmagic(page)

            loop_count += 1
            if loop_count % 2 == 0:
                await check_telegram_updates(page)

            await asyncio.sleep(1)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
