import asyncio
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
COOLDOWN = 240          # 4 минуты паузы после старта рейна, чтобы не дублировать

last_alert = {"bandit": 0, "rustmagic": 0}
widget_state = {"bandit": "Загрузка...", "rustmagic": "Загрузка..."}
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
        log(f"[TG] Ошибка отправки: {e}")


def send_photo_tg(file_path: str, caption: str):
    """Отправляет скриншот браузера в Telegram."""
    if not CHAT_ID or not os.path.exists(file_path):
        return
    boundary = uuid.uuid4().hex
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(file_path, "rb") as f:
        photo_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{CHAT_ID}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="screen.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode("utf-8") + photo_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        log(f"[TG] Ошибка отправки фото: {e}")


async def check_telegram_updates(pages: dict):
    """Каждые 2 секунды проверяет сообщения от тебя в Telegram и шлет отчет + скриншоты."""
    global CHAT_ID, last_update_id
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id + 1}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("result", []):
                last_update_id = item["update_id"]
                msg = item.get("message") or item.get("edited_message")
                if msg and "chat" in msg:
                    CHAT_ID = msg["chat"]["id"]
                    text = (msg.get("text") or "").strip()
                    if text:
                        send_tg(
                            f"🟢 Бот работает (опрос каждую 1 сек)!\n\n"
                            f"📊 RustMagic виджет:\n{widget_state['rustmagic']}\n\n"
                            f"📊 Bandit.camp виджет:\n{widget_state['bandit']}\n\n"
                            f"📸 Сейчас пришлю скриншоты обоих сайтов..."
                        )
                        for key, page in pages.items():
                            try:
                                path = f"/tmp/{key}.png"
                                await page.screenshot(path=path)
                                send_photo_tg(path, f"Экран {key}")
                            except Exception as e:
                                log(f"Ошибка скриншота {key}: {e}")
    except Exception:
        pass


def extract_amount(text: str) -> str:
    # Убираем упоминания времени ("27 min ago", "01:30"), чтобы не перепутать с монетами
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
    """Проверяет RustMagic каждую секунду. Игнорирует режим ожидания ('min ago')."""
    try:
        info = await page.evaluate("""() => {
            // Ищем плашку Coin Rain
            const all = Array.from(document.querySelectorAll('div, section, aside'));
            let widgetText = null;
            for (const el of all) {
                const t = (el.innerText || '').trim();
                if (t.toLowerCase().includes('coin rain') && t.length < 200) {
                    widgetText = t;
                }
            }
            if (!widgetText) return { state: "Виджет Coin Rain не найден на экране", active: false, text: "" };

            const low = widgetText.toLowerCase();
            // Пока написано 'ago' (например '27 min ago' или 'few seconds ago') — рейн НЕ идет, в него только кидают типы
            const isIdle = low.includes('ago');

            // Проверяем, появилась ли кнопка Join или таймер активной раздачи
            const hasJoinBtn = Array.from(document.querySelectorAll('button, [role="button"]')).some(b => {
                const bt = (b.innerText || '').toLowerCase();
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
            # Ждем 1 секунду, чтобы сумма прогрузилась полностью
            amount = extract_amount(info["text"])
            if amount != "???":
                trigger_rain("rustmagic", amount, "Rust magic rain: {amount} coins")
    except Exception as e:
        widget_state["rustmagic"] = f"Ошибка чтения: {e}"


async def check_bandit(page):
    """Проверяет Bandit.camp каждую секунду. Игнорирует сообщения в чате о типах."""
    try:
        info = await page.evaluate("""() => {
            // 1. Ищем активную кнопку входа в рейн (Join Rain / Claim / Join)
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

            // 2. Ищем виджет рейна в боковой панели чата (исключая обычные сообщения чата "tipped")
            const blocks = Array.from(document.querySelectorAll('div, section, aside'));
            for (const el of blocks) {
                const t = (el.innerText || '').trim();
                const low = t.toLowerCase();
                if (t.length > 6 && t.length < 220 && (low.includes('scrap rain') || low.includes('rain pool') || low.includes('rain pot') || low.includes('join rain'))) {
                    // Игнорируем строчки чата про то, что кто-то типнул
                    if (low.includes('tipped') || low.includes('added to') || low.includes('ago')) continue;
                    const isActive = low.includes('join') || low.includes('claim') || low.includes('end') || /\\b0?[0-3]:[0-5]\\d\\b/.test(low);
                    if (isActive) {
                        return { state: "АКТИВЕН: " + t.replace(/\\n+/g, ' | '), active: true, text: t };
                    } else {
                        return { state: "Ожидание: " + t.replace(/\\n+/g, ' | '), active: false, text: t };
                    }
                }
            }
            return { state: "Ожидание рейна (активной кнопки Join нет)", active: false, text: "" };
        }""")

        widget_state["bandit"] = info["state"]
        if info["active"]:
            amount = extract_amount(info["text"])
            trigger_rain("bandit", amount, "bandit camp rain - {amount} scrap")
    except Exception as e:
        widget_state["bandit"] = f"Ошибка чтения: {e}"


async def main():
    await check_telegram_updates({})
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
                await asyncio.sleep(8)
                log(f"[{key}] Заголовок: {await page.title()}")
            except Exception as e:
                log(f"[{key}] Ошибка: {e}")
            pages[key] = page

        loop_count = 0
        # Главный цикл с опросом КАЖДУЮ 1 СЕКУНДУ
        while time.time() - start_time < RUN_DURATION:
            await check_rustmagic(pages["rustmagic"])
            await check_bandit(pages["bandit"])

            # Каждые 2 секунды проверяем, не написал ли ты боту в Telegram
            loop_count += 1
            if loop_count % 2 == 0:
                await check_telegram_updates(pages)

            await asyncio.sleep(1)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
