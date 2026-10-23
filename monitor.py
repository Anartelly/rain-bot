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

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID_FILE = "chat_id.txt"

RUN_DURATION = 52 * 60   # 52 минуты работы одной смены
RELOAD_INTERVAL = 20 * 60 # Обновление страницы каждые 20 минут, чтобы чат не зависал
COOLDOWN = 210           # 3.5 минуты паузы после уведомления о рейне

last_alert = 0
widget_state = "Загрузка..."
CHAT_ID = None
last_update_id = 0


def log(msg: str):
    print(msg, flush=True)


def save_chat_id_permanently(chat_id: int):
    """Сохраняет CHAT_ID в файл и коммитит в GitHub, чтобы помнить его вечно."""
    try:
        if os.path.exists(CHAT_ID_FILE):
            with open(CHAT_ID_FILE, "r", encoding="utf-8") as f:
                if f.read().strip() == str(chat_id):
                    return
        with open(CHAT_ID_FILE, "w", encoding="utf-8") as f:
            f.write(str(chat_id))
        subprocess.run(["git", "config", "user.name", "rain-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "bot@github.com"], check=False)
        subprocess.run(["git", "add", CHAT_ID_FILE], check=False)
        subprocess.run(["git", "commit", "-m", "Save CHAT_ID permanently"], check=False)
        subprocess.run(["git", "push"], check=False)
        log(f"CHAT_ID {chat_id} навсегда сохранен в {CHAT_ID_FILE}")
    except Exception as e:
        log(f"Ошибка сохранения CHAT_ID: {e}")


def init_chat_id():
    """Загружает CHAT_ID из файла или из истории Telegram, не стирая последнее сообщение."""
    global CHAT_ID, last_update_id
    if os.path.exists(CHAT_ID_FILE):
        try:
            with open(CHAT_ID_FILE, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val.lstrip("-").isdigit():
                    CHAT_ID = int(val)
                    log(f"Загружен сохраненный CHAT_ID из файла: {CHAT_ID}")
        except Exception:
            pass

    if not BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("result", []):
                last_update_id = item["update_id"]
                msg = item.get("message") or item.get("edited_message")
                if msg and "chat" in msg:
                    CHAT_ID = msg["chat"]["id"]
            if CHAT_ID:
                save_chat_id_permanently(CHAT_ID)
    except Exception as e:
        log(f"Ошибка init_chat_id: {e}")


def _send_tg_sync(text: str):
    if not CHAT_ID or not BOT_TOKEN:
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
    if not CHAT_ID or not BOT_TOKEN or not image_bytes:
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


async def send_screen(page, caption: str):
    img = await fast_screenshot(page)
    if img:
        await asyncio.to_thread(_send_photo_sync, img, caption)


def _fetch_updates_sync(offset: int):
    # Используем offset без +1 для последнего элемента, чтобы очередь в TG никогда не обнулялась
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8")).get("result", [])


async def check_telegram_updates(page):
    global CHAT_ID, last_update_id
    try:
        results = await asyncio.to_thread(_fetch_updates_sync, last_update_id)
        for item in results:
            upd_id = item["update_id"]
            if upd_id <= last_update_id:
                continue
            last_update_id = upd_id

            msg = item.get("message") or item.get("edited_message")
            if not msg or "chat" not in msg:
                continue

            new_chat_id = msg["chat"]["id"]
            if CHAT_ID != new_chat_id:
                CHAT_ID = new_chat_id
                await asyncio.to_thread(save_chat_id_permanently, CHAT_ID)

            raw_text = (msg.get("text") or "").strip()
            if raw_text:
                await send_tg(f"🟢 Бот на связи (CHAT_ID {CHAT_ID} сохранен навсегда)!\n📊 Виджет: {widget_state}")
                await send_screen(page, "Текущий экран RustMagic")
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


async def main():
    init_chat_id()
    log(f"Запуск мониторинга RustMagic. CHAT_ID: {CHAT_ID}")

    start_time = time.time()
    last_reload = time.time()

    async with async_playwright() as p:
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

        last_tg_check = 0
        while time.time() - start_time < RUN_DURATION:
            await check_rustmagic(page)

            now = time.time()
            if now - last_tg_check >= 2.0:
                last_tg_check = now
                await check_telegram_updates(page)

            # Каждые 20 минут перезагружаем страницу, чтобы соединение с чатом не засыпало
            if now - last_reload >= RELOAD_INTERVAL:
                last_reload = now
                try:
                    log("Плановое обновление страницы...")
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(6)
                except Exception as e:
                    log(f"Ошибка перезагрузки страницы: {e}")

            await asyncio.sleep(0.3)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
