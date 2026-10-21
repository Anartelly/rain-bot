import asyncio
import json
import os
import re
import time
import urllib.request
import urllib.parse
from playwright.async_api import async_playwright

BOT_TOKEN = "8935522730:AAH2sDOqtf4kCk_-YWaW1Npz0IDtjgRYqOM"

RUN_DURATION = 55 * 60  # 55 минут работы
COOLDOWN = 600          # 10 минут паузы после уведомления, чтобы не слать повторно за один Rain

SITES = {
    "bandit": {
        "url": "https://bandit.camp",
        # Строгие слова кнопки входа (без общих слов вроде 'scrap rain')
        "join_buttons": ["join rain", "join the rain", "claim rain", "enter rain", "click to join"],
        "format": "bandit camp rain - {amount} scrap"
    },
    "rustmagic": {
        "url": "https://rustmagic.com",
        # Убрали 'coin rain', так как этот заголовок висит 24/7
        "join_buttons": ["join rain", "join to claim", "claim rain", "enter rain", "click to join"],
        "format": "Rust magic rain: {amount} coins"
    }
}

last_alert = {"bandit": 0, "rustmagic": 0}
CHAT_ID = None
last_update_id = 0


def log(msg: str):
    print(msg, flush=True)


def send_tg(text: str):
    if not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        log(f"[TG] Отправлено: {text}")
    except Exception as e:
        log(f"[TG] Ошибка отправки: {e}")


def check_telegram_updates():
    global CHAT_ID, last_update_id
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id + 1}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("result", []):
                last_update_id = item["update_id"]
                msg = item.get("message") or item.get("edited_message")
                if msg and "chat" in msg:
                    CHAT_ID = msg["chat"]["id"]
    except Exception:
        pass


def extract_amount_from_text(text: str) -> str:
    # Убираем строки вида "27 min ago", чтобы не перепутать минуты с монетами
    clean_text = re.sub(r'\b\d+\s*(?:min|sec|m|s|h|hour)[a-z]*\s*ago\b', '', text, flags=re.IGNORECASE)

    # 1. Ищем дробное число (например, 10.91 или 38.35)
    dec_matches = re.findall(r'\b\d+[.,]\d{2}\b', clean_text)
    if dec_matches:
        return dec_matches[0].replace(",", ".")

    # 2. Ищем в JSON-полях WebSocket
    ws_match = re.search(
        r'"(?:pot|amount|pool|prize|value|total|reward|coins|scrap)"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        clean_text,
        re.IGNORECASE
    )
    if ws_match:
        val = float(ws_match.group(1))
        return f"{val:.2f}" if val % 1 != 0 else str(int(val))

    return "???"


def trigger_alert(site_key: str, amount: str, reason: str):
    now = time.time()
    if now - last_alert[site_key] < COOLDOWN:
        return
    last_alert[site_key] = now
    msg = SITES[site_key]["format"].format(amount=amount)
    log(f"[{site_key}] СРАБОТАЛ ТРИГГЕР ({reason}): {msg}")
    send_tg(msg)


def attach_ws(page, site_key: str):
    """Слушает WebSocket, но реагирует ТОЛЬКО на переход Rain в активную фазу раздачи."""
    def on_ws(ws):
        def on_frame(payload):
            if not isinstance(payload, str):
                return
            p_low = payload.lower()
            if "rain" not in p_low:
                return
            # Игнорируем пакеты обычного пополнения пула (tip / contribute / update)
            if any(ignore in p_low for ignore in ["tip", "contribute", "donat", "added"]):
                return
            # Реагируем только на явный старт раздачи
            start_signals = [
                '"state":"active"',
                '"status":"active"',
                '"active":true',
                '"phase":"active"',
                '"phase":"claiming"',
                '"rainstarted"',
                '"rain_start"',
                '"canjoin":true'
            ]
            if any(sig in p_low.replace(" ", "") for sig in start_signals):
                amount = extract_amount_from_text(payload)
                if amount != "???":
                    trigger_alert(site_key, amount, "WebSocket Active Signal")
        ws.on("framereceived", on_frame)
    page.on("websocket", on_ws)


async def scan_page_for_active_rain(page, site_key: str):
    """Проверяет виджет на странице: срабатывает только если есть кнопка Join или таймер раздачи."""
    try:
        widget_text = await page.evaluate("""(joinKeywords) => {
            // 1. Проверяем все кнопки на странице: появилась ли кнопка Join Rain / Claim
            const buttons = Array.from(document.querySelectorAll('button, [role="button"], a'));
            for (const btn of buttons) {
                const bTxt = (btn.innerText || '').trim().toLowerCase();
                if (joinKeywords.some(kw => bTxt.includes(kw)) || bTxt === 'join') {
                    // Нашли кнопку входа! Берем текст родительского виджета Rain, чтобы вытащить сумму
                    let container = btn.parentElement;
                    for (let i = 0; i < 4 && container; i++) {
                        const cTxt = container.innerText || '';
                        if (/\\d+[.,]\\d{2}/.test(cTxt)) {
                            return cTxt;
                        }
                        container = container.parentElement;
                    }
                    return btn.parentElement ? btn.parentElement.innerText : bTxt;
                }
            }

            // 2. Проверяем сам виджет Coin Rain / Scrap Rain:
            // Когда раздача НЕ идет, там написано "XX min ago".
            // Когда раздача НАЧАЛАСЬ, "ago" пропадает и появляется "join", "claim" или таймер "01:45" / "ends in"
            const blocks = Array.from(document.querySelectorAll('div, section, aside'));
            for (const el of blocks) {
                const txt = (el.innerText || '').trim();
                const low = txt.toLowerCase();
                if (txt.length > 8 && txt.length < 250 && (low.includes('coin rain') || low.includes('scrap rain'))) {
                    const isIdle = low.includes('ago'); // Висит в режиме ожидания ("27 min ago")
                    const hasActiveSign = low.includes('join') || low.includes('claim') || low.includes('end') || /\\b0?[0-5]:[0-5]\\d\\b/.test(low);
                    if (!isIdle && hasActiveSign) {
                        return txt;
                    }
                }
            }
            return null;
        }""", SITES[site_key]["join_buttons"])

        if widget_text:
            amount = extract_amount_from_text(widget_text)
            trigger_alert(site_key, amount, "DOM Active Widget")
    except Exception:
        pass


async def main():
    check_telegram_updates()
    log(f"Запуск мониторинга. CHAT_ID: {CHAT_ID}")

    start_time = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )

        pages = {}
        for key, site in SITES.items():
            page = await context.new_page()
            attach_ws(page, key)
            try:
                await page.goto(site["url"], wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(6)
                log(f"[{key}] Открыт: {await page.title()}")
            except Exception as e:
                log(f"[{key}] Ошибка: {e}")
            pages[key] = page

        while time.time() - start_time < RUN_DURATION:
            if not CHAT_ID:
                check_telegram_updates()
            for key, page in pages.items():
                await scan_page_for_active_rain(page, key)
            await asyncio.sleep(4)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
