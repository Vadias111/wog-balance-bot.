import os
import logging
import datetime as dt
from decimal import Decimal, InvalidOperation

import requests

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # pip install backports.zoneinfo


# --- НАСТРОЙКИ ---
WOG_API_KEY = os.environ.get("WOG_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Можно задать через env, иначе значение по умолчанию
BALANCE_THRESHOLD = Decimal(os.environ.get("BALANCE_THRESHOLD", "110000.00"))

# Таймзона для правильной даты запроса в WOG API
WOG_TIMEZONE = os.environ.get("WOG_TIMEZONE", "Europe/Kyiv")

# Опционально: ID конкретного кошелька, чтобы НЕ суммировать все гривневые
# (если не задан, будет сумма всех UAH-кошельков)
WOG_WALLET_ID = os.environ.get("WOG_WALLET_ID")

REQUEST_TIMEOUT = 30
# --- КОНЕЦ НАСТРОЕК ---

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


def parse_decimal(value) -> Decimal:
    """Безопасно преобразует строку/число в Decimal (учитывает ',' и пробелы)."""
    if value is None:
        return Decimal("0")
    s = str(value).strip().replace(" ", "").replace("\u00A0", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def fmt_money(amount: Decimal) -> str:
    """Форматирует сумму с 2 знаками и пробелами-разделителями тысяч."""
    return f"{amount:,.2f}".replace(",", " ")


def send_telegram_message(api_url: str, message: str, chat_id: str) -> None:
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        resp = requests.post(api_url, data=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            logging.info("Уведомление в Telegram отправлено.")
        else:
            logging.error("Ошибка Telegram API: %s - %s", resp.status_code, resp.text)
    except requests.exceptions.RequestException as e:
        logging.error("Сетевая ошибка при отправке в Telegram: %s", e)


def main() -> None:
    if not all([WOG_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.error(
            "Не заданы переменные окружения: WOG_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
        )
        return

    wog_api_url = f"https://api-fuelcards.wog.ua/{WOG_API_KEY}"
    telegram_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    now_local = dt.datetime.now(ZoneInfo(WOG_TIMEZONE))
    request_date = now_local.strftime("%Y%m%d")

    logging.info("Проверка баланса WOG...")
    logging.info("Дата запроса в WOG: %s (%s)", request_date, WOG_TIMEZONE)

    headers = {"Content-Type": "application/json"}
    data = {
        "date": request_date,
        "version": "1.0"
    }

    try:
        resp = requests.post(
            wog_api_url,
            headers=headers,
            json=data,
            params={"Action": "WalletsRemains"},
            timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        response_data = resp.json()

        if str(response_data.get("status")) != "0":
            logging.error("WOG API вернуло ошибку: %s", response_data)
            return

        remains = response_data.get("remains", [])
        if not isinstance(remains, list) or not remains:
            logging.warning("В ответе WOG нет списка 'remains' или он пуст.")
            return

        # Фильтр гривневых кошельков
        uah_wallets = []
        for w in remains:
            goods_name = str(w.get("GoodsName", "")).strip().lower()
            currency_code = str(w.get("CurrencyCode", "")).strip().upper()
            if goods_name in {"грн", "uah"} or currency_code in {"UAH", "980"}:
                uah_wallets.append(w)

        if not uah_wallets:
            logging.warning("UAH/Грн кошельки не найдены. Доступные кошельки: %s", [
                {
                    "WalletId": w.get("WalletId"),
                    "GoodsName": w.get("GoodsName"),
                    "CurrencyCode": w.get("CurrencyCode"),
                    "Value": w.get("Value")
                }
                for w in remains
            ])
            return

        # Если указан конкретный кошелек - берем только его
        selected_wallets = uah_wallets
        if WOG_WALLET_ID:
            selected_wallets = [
                w for w in uah_wallets
                if str(w.get("WalletId", "")).strip() == WOG_WALLET_ID.strip()
            ]
            if not selected_wallets:
                logging.error(
                    "Кошелек WOG_WALLET_ID=%s не найден среди UAH кошельков. Найдены: %s",
                    WOG_WALLET_ID,
                    [w.get("WalletId") for w in uah_wallets]
                )
                return

        current_balance = sum(
            (parse_decimal(w.get("Value", 0)) for w in selected_wallets),
            Decimal("0")
        )

        logging.info("Кошельки в расчете: %s", [
            {
                "WalletId": w.get("WalletId"),
                "GoodsName": w.get("GoodsName"),
                "Value": w.get("Value")
            }
            for w in selected_wallets
        ])
        logging.info("Текущий баланс: %s грн", fmt_money(current_balance))

        if current_balance < BALANCE_THRESHOLD:
            message = (
                "🚨 *Внимание!* 🚨\n\n"
                "Баланс на счету WOG упал ниже порога.\n\n"
                f"Дата запроса ({WOG_TIMEZONE}): *{now_local.strftime('%Y-%m-%d %H:%M:%S')}*\n"
                f"Текущий баланс: *{fmt_money(current_balance)} грн.*\n"
                f"Установленный порог: *{fmt_money(BALANCE_THRESHOLD)} грн.*\n\n"
                "Пора пополнить счет!"
            )
            send_telegram_message(telegram_api_url, message, TELEGRAM_CHAT_ID)
        else:
            logging.info("Баланс в норме (>= %s грн).", fmt_money(BALANCE_THRESHOLD))

    except requests.exceptions.RequestException as e:
        logging.error("Ошибка сети при запросе к WOG: %s", e)
    except ValueError as e:
        logging.error("Ошибка разбора JSON ответа WOG: %s", e)
    except Exception as e:
        logging.error("Непредвиденная ошибка: %s", e)


if __name__ == "__main__":
    main()
