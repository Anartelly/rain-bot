import asyncio
import json
import os
import re
import time
import urllib.request
import urllib.parse
from playwright.async_api import async_playwright

# Твой токен бота
BOT_TOKEN = "8935522730:AAH2sDOqtf4kCk_-YWaW1Npz0IDtjgRYqOM"

# Время работы одного цикла (53 минуты) и пауза между одинаковыми уведомлениями (5 минут)
RUN_DURATION = 53 * 60
COOLDOWN = 300

SITES = {
    "bandit": {
        "url": "https://bandit.camp",
        "dom_keywords": ["join rain", "join the rain", "claim rain", "scrap rain"],
        "format": "bandit camp rain - {amount} scrap"
    },
    "rustmagic": {
        "url": "https://rustmagic.com",
        "dom_keywords": ["join rain", "coin rain", "join to claim"],
        "format": "Rust magic rain: {amount} coins"
    }
}

last_alert = {"bandit": 0, "rustmagic": 0}
CHAT_ID = None


def get_chat_id():
    """Автоматически находит твой ID чата по последнему сообщению боту."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("result", [])
            for item in reversed(results):
                msg = item.get("message") or item.get("edited_message")
                if msg and "chat" in msg:
                    return msg["chat"]["id"]
    except Exception as e:
        print(f"Ошибка получения CHAT_ID: {e}")
    return None


def send_tg(text: str):
    """Отправляет сообщение в Telegram."""
    if not CHAT_ID:
        print("CHAT_ID не найден! Напиши боту /start в Telegram.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        print(f"Отправлено в TG: {text}")
    except Exception as e:
        print(f"Ошибка отправки в TG: {e}")


def extract_amount_from_text(text: str) -> str:
    """Ищет сумму призового пула (например, 38.35 или 11.33) в тексте или JSON."""
    # 1. Ищем в JSON-полях WebSocket (pot, amount, pool, prize, total, coins, scrap)
    ws_match = re.search(
        r'"(?:pot|amount|pool|prize|value|total|reward|coins|scrap)"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        text,
        re.IGNORECASE
    )
    if ws_match:
        val = float(ws_match.group(1))
        return f"{val:.2f}" if val % 1 != 0 else str(int(val))

    # 2. Ищем дробное число вида 38.35 или 11,33 в тексте виджета
    dec_matches = re.findall(r'\b\d+[.,]\d{1,2}\b', text)
    if dec_matches:
        return dec_matches[0].replace(",", ".")

    # 3. Если дробного нет, ищем целое число рядом со словами scrap / coins / rain
    int_match = re.search(r'\b(\d{2,6})\b', text)
    if int_match:
        return int_match.group(1)

    return "???"


def trigger_alert(site_key: str, amount: str):
    now = time.time()
    if now - last_alert[site_key] < COOLDOWN:
        return
    last_alert[site_key] = now
    msg = SITES[site_key]["format"].format(amount=amount)
    send_tg(msg)


def attach_ws(page, site_key: str):
    """Слушает WebSocket-пакеты сайта и вытаскивает сумму при старте Rain."""
    def on_ws(ws):
        def on_frame(payload):
            if not isinstance(payload, str):
                return
            p_low = payload.lower()
            if "rain" in p_low and any(k in p_low for k in ['"active":true', '"state":"active"', '"status":"active"', '"rainstarted"', '"join"']):
                amount = extract_amount_from_text(payload)
                trigger_alert(site_key, amount)
        ws.on("framereceived", on_frame)
    page.on("websocket", on_ws)


async def scan_page_for_rain(page, site_key: str):
    """Ищет активный блок Rain на странице и достает из него сумму."""
    try:
        # Ищем блоки в DOM, где упоминается rain и кнопка входа
        rain_data = await page.evaluate("""(keywords) => {
            const elements = Array.from(document.querySelectorAll('div, section, aside, button'));
            for (const el of elements) {
                const txt = (el.innerText || '').toLowerCase();
                if (txt.length > 5 && txt.length < 400) {
                    if (keywords.some(kw => txt.includes(kw))) {
                        return el.innerText;
                    }
                }
            }
            return null;
        }""", SITES[site_key]["dom_keywords"])

        if rain_data:
            amount = extract_amount_from_text(rain_data)
            trigger_alert(site_key, amount)
    except Exception:
        pass


async def main():
    global CHAT_ID
    CHAT_ID = get_chat_id()
    print(f"Определен CHAT_ID: {CHAT_ID}")

    # Если запуск был включен вручную через кнопку Run workflow — шлем тестовый отчет
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        send_tg("✅ Бот мониторинга запущен в облаке! Начинаю отслеживать Bandit.camp и RustMagic.")

    start_time = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )

        pages = {}
        for key, site in SITES.items():
            page = await context.new_page()
            attach_ws(page, key)
            try:
                await page.goto(site["url"], wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(5)
                print(f"[{key}] Заголовок страницы: {await page.title()}")
            except Exception as e:
                print(f"[{key}] Ошибка перехода: {e}")
            pages[key] = page

        while time.time() - start_time < RUN_DURATION:
            for key, page in pages.items():
                await scan_page_for_rain(page, key)
            await asyncio.sleep(5)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
