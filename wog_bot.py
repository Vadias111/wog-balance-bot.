import os
import json
import logging
import datetime as dt
from decimal import Decimal, InvalidOperation

import requests

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


# --- ENV ---
WOG_API_KEY = os.environ.get("WOG_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BALANCE_THRESHOLD = Decimal(os.environ.get("BALANCE_THRESHOLD", "110000.00"))
WOG_TIMEZONE = os.environ.get("WOG_TIMEZONE", "Europe/Kyiv")
WOG_WALLET_ID = os.environ.get("WOG_WALLET_ID")  # РЕКОМЕНДУЕТСЯ задать обязательно
DEBUG_WOG = os.environ.get("DEBUG_WOG", "0") == "1"

REQUEST_TIMEOUT = 30
# --- /ENV ---


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


def parse_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    s = str(value).strip().replace(" ", "").replace("\u00A0", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def fmt_money(amount: Decimal) -> str:
    return f"{amount:,.2f}".replace(",", " ")


def send_telegram_message(api_url: str, message: str, chat_id: str) -> None:
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(api_url, data=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            logging.info("Уведомление в Telegram отправлено.")
        else:
            logging.error("Ошибка Telegram API: %s - %s", r.status_code, r.text)
    except requests.exceptions.RequestException as e:
        logging.error("Сетевая ошибка Telegram: %s", e)


def pick_uah_wallets(remains: list[dict]) -> list[dict]:
    wallets = []
    for w in remains:
        goods = str(w.get("GoodsName", "")).strip().lower()
        code = str(w.get("CurrencyCode", "")).strip().upper()
        if goods in {"грн", "uah"} or code in {"UAH", "980"}:
            wallets.append(w)
    return wallets


def calc_available_balance(wallet: dict) -> tuple[Decimal, str]:
    # 1) Приоритет: явные поля "доступно"
    direct_available_keys = [
        "Available",
        "AvailableValue",
        "AvailableSum",
        "SumAvailable",
        "RestAvailable",
        "ValueAvailable",
        "FreeValue",
        "BalanceAvailable",
        "SaldoAvailable",
    ]
    for key in direct_available_keys:
        if key in wallet and str(wallet.get(key)).strip() not in {"", "None"}:
            return parse_decimal(wallet.get(key)), f"direct:{key}"

    # 2) Если явного "доступно" нет: Value - блокировки/резервы
    total = parse_decimal(wallet.get("Value", 0))
    blocked_keys = [
        "Blocked",
        "BlockedValue",
        "BlockedSum",
        "Reserve",
        "Reserved",
        "ReservedValue",
        "Hold",
        "OnHold",
        "Frozen",
        "NotAvailable",
    ]
    blocked = Decimal("0")
    used = []
    for key in blocked_keys:
        if key in wallet and str(wallet.get(key)).strip() not in {"", "None"}:
            v = parse_decimal(wallet.get(key))
            blocked += v
            used.append(f"{key}={v}")

    if blocked > 0:
        return total - blocked, f"value-minus-blocked:{';'.join(used)}"

    # 3) Фолбэк
    return total, "fallback:Value"


def now_in_tz() -> dt.datetime:
    if ZoneInfo is None:
        # На старом Python без zoneinfo
        return dt.datetime.now()
    try:
        return dt.datetime.now(ZoneInfo(WOG_TIMEZONE))
    except Exception:
        logging.warning("Не удалось применить таймзону %s, используем локальную.", WOG_TIMEZONE)
        return dt.datetime.now()


def main() -> None:
    if not all([WOG_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.error("Не заданы WOG_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return

    wog_api_url = f"https://api-fuelcards.wog.ua/{WOG_API_KEY}"
    tg_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    now_local = now_in_tz()
    request_date = now_local.strftime("%Y%m%d")

    payload = {
        "date": request_date,
        "version": "1.0"
    }
    headers = {"Content-Type": "application/json"}

    logging.info("Проверка баланса WOG. date=%s tz=%s", request_date, WOG_TIMEZONE)

    try:
        r = requests.post(
            wog_api_url,
            headers=headers,
            json=payload,
            params={"Action": "WalletsRemains"},
            timeout=REQUEST_TIMEOUT
        )
        r.raise_for_status()
        data = r.json()

        if str(data.get("status")) != "0":
            logging.error("WOG API error: %s", data)
            return

        remains = data.get("remains", [])
        if not isinstance(remains, list) or not remains:
            logging.error("WOG API: пустой remains")
            return

        if DEBUG_WOG:
            logging.info("RAW remains: %s", json.dumps(remains, ensure_ascii=False))

        uah_wallets = pick_uah_wallets(remains)
        if not uah_wallets:
            logging.error("UAH кошельки не найдены. remains=%s", remains)
            return

        # Не суммируем молча все кошельки: это частая причина неверной суммы.
        if WOG_WALLET_ID:
            selected = [w for w in uah_wallets if str(w.get("WalletId", "")).strip() == WOG_WALLET_ID.strip()]
            if not selected:
                logging.error(
                    "WOG_WALLET_ID=%s не найден. Доступные UAH WalletId: %s",
                    WOG_WALLET_ID,
                    [w.get("WalletId") for w in uah_wallets]
                )
                return
        else:
            if len(uah_wallets) > 1:
                logging.error(
                    "Найдено несколько UAH кошельков (%s). Укажите WOG_WALLET_ID, чтобы не получить неверный баланс.",
                    len(uah_wallets)
                )
                for w in uah_wallets:
                    bal, method = calc_available_balance(w)
                    logging.info(
                        "UAH wallet: WalletId=%s Name=%s GoodsName=%s Value=%s AvailableCalc=%s Method=%s Keys=%s",
                        w.get("WalletId"),
                        w.get("WalletName") or w.get("Name"),
                        w.get("GoodsName"),
                        w.get("Value"),
                        bal,
                        method,
                        list(w.keys())
                    )
                return
            selected = [uah_wallets[0]]

        total_available = Decimal("0")
        details = []
        for w in selected:
            available, method = calc_available_balance(w)
            total_available += available
            details.append({
                "WalletId": w.get("WalletId"),
                "WalletName": w.get("WalletName") or w.get("Name"),
                "Value": str(w.get("Value")),
                "AvailableCalc": str(available),
                "Method": method
            })

        logging.info("Кошельки в расчете: %s", details)
        logging.info("Доступный баланс: %s грн", fmt_money(total_available))

        if total_available < BALANCE_THRESHOLD:
            message = (
                "🚨 *Внимание!* 🚨\n\n"
                "Баланс на счету WOG упал ниже порога.\n\n"
                f"Дата запроса ({WOG_TIMEZONE}): *{now_local.strftime('%Y-%m-%d %H:%M:%S')}*\n"
                f"Текущий баланс: *{fmt_money(total_available)} грн.*\n"
                f"Установленный порог: *{fmt_money(BALANCE_THRESHOLD)} грн.*\n\n"
                "Пора пополнить счет!"
            )
            send_telegram_message(tg_api_url, message, TELEGRAM_CHAT_ID)
        else:
            logging.info("Баланс в норме (>= %s грн)", fmt_money(BALANCE_THRESHOLD))

    except requests.exceptions.RequestException as e:
        logging.error("Ошибка сети WOG: %s", e)
    except ValueError as e:
        logging.error("Ошибка JSON WOG: %s", e)
    except Exception as e:
        logging.error("Непредвиденная ошибка: %s", e)


if __name__ == "__main__":
    main()
