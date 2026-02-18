import os
import json
import logging
import datetime as dt
from decimal import Decimal, InvalidOperation

import requests
from zoneinfo import ZoneInfo


# --- ENV ---
WOG_API_KEY = os.environ.get("WOG_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BALANCE_THRESHOLD = Decimal(os.environ.get("BALANCE_THRESHOLD", "110000.00"))
WOG_TIMEZONE = os.environ.get("WOG_TIMEZONE", "Europe/Kyiv")

# WalletsRemains использует WalletCode (UUID), а не WalletId
WOG_WALLET_CODE = os.environ.get("WOG_WALLET_CODE") or os.environ.get("WOG_WALLET_ID")

# OPENING - только остаток на начало дня
# OPENING_PLUS_TX - остаток на начало дня + транзакции за день (оценка)
WOG_BALANCE_MODE = os.environ.get("WOG_BALANCE_MODE", "OPENING_PLUS_TX").upper()

# Ключевые слова для входящих операций (если в транзакции нет явного направления)
WOG_CREDIT_KEYWORDS = [
    w.strip().lower()
    for w in os.environ.get("WOG_CREDIT_KEYWORDS", "попов,зарах,возврат,повернен,коригув").split(",")
    if w.strip()
]

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


def norm(s: str) -> str:
    return " ".join(str(s or "").strip().lower().split())


def now_in_tz() -> dt.datetime:
    try:
        return dt.datetime.now(ZoneInfo(WOG_TIMEZONE))
    except Exception:
        logging.warning("Не удалось применить таймзону %s, используем локальную.", WOG_TIMEZONE)
        return dt.datetime.now()


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


def wog_post(wog_api_url: str, action: str, body: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    r = requests.post(
        wog_api_url,
        headers=headers,
        json=body,
        params={"Action": action},
        timeout=REQUEST_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    if str(data.get("status")) != "0":
        raise RuntimeError(f"WOG API error (Action={action}): {data}")
    return data


def pick_uah_wallets(remains: list[dict]) -> list[dict]:
    wallets = []
    for w in remains:
        goods_name = norm(w.get("GoodsName", ""))
        goods_code = str(w.get("GoodsCode", "")).strip()
        currency_code = str(w.get("CurrencyCode", "")).strip().upper()
        if goods_name in {"грн", "uah"} or goods_code == "980" or currency_code in {"UAH", "980"}:
            wallets.append(w)
    return wallets


def select_wallet(uah_wallets: list[dict]) -> dict:
    if WOG_WALLET_CODE:
        selected = [
            w for w in uah_wallets
            if str(w.get("WalletCode", "")).strip() == WOG_WALLET_CODE.strip()
        ]
        if not selected:
            raise RuntimeError(
                f"WOG_WALLET_CODE={WOG_WALLET_CODE} не найден. "
                f"Доступные WalletCode: {[w.get('WalletCode') for w in uah_wallets]}"
            )
        return selected[0]

    if len(uah_wallets) != 1:
        raise RuntimeError(
            f"Найдено UAH кошельков: {len(uah_wallets)}. "
            f"Укажите WOG_WALLET_CODE. Доступные: {[w.get('WalletCode') for w in uah_wallets]}"
        )
    return uah_wallets[0]


def transaction_signed_amount(tx: dict) -> Decimal:
    amount = parse_decimal(tx.get("summwithdiscount", tx.get("sum", 0)))
    if amount == Decimal("-1"):
        amount = parse_decimal(tx.get("sum", 0))

    if amount == Decimal("0"):
        return Decimal("0")
    if amount < 0:
        return amount

    direction_fields = ("Direction", "direction", "OperationType", "operationType", "Type", "type")
    direction_value = norm(" ".join(str(tx.get(f, "")) for f in direction_fields))
    if any(x in direction_value for x in ("credit", "in", "incoming", "plus", "попов", "зарах", "возврат", "повернен")):
        return abs(amount)
    if any(x in direction_value for x in ("debit", "out", "outgoing", "minus", "спис", "покуп", "оплат")):
        return -abs(amount)

    text = norm(
        f"{tx.get('goodsName', '')} "
        f"{tx.get('walletname', '')} "
        f"{tx.get('cardinfo', '')}"
    )
    if any(k in text for k in WOG_CREDIT_KEYWORDS):
        return abs(amount)

    return -abs(amount)


def calc_today_delta(transactions: list[dict], wallet_name: str) -> tuple[Decimal, int]:
    delta = Decimal("0")
    used = 0
    wallet_name_n = norm(wallet_name)

    for tx in transactions:
        tx_wallet_name = norm(tx.get("walletname", ""))
        if wallet_name_n and tx_wallet_name and tx_wallet_name != wallet_name_n:
            continue
        delta += transaction_signed_amount(tx)
        used += 1

    return delta, used


def main() -> None:
    if not all([WOG_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.error("Не заданы WOG_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return

    wog_api_url = f"https://api-fuelcards.wog.ua/{WOG_API_KEY}"
    tg_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    now_local = now_in_tz()
    request_date = now_local.strftime("%Y%m%d")
    base_body = {"date": request_date, "version": "1.0"}

    logging.info("Проверка баланса WOG. date=%s tz=%s mode=%s", request_date, WOG_TIMEZONE, WOG_BALANCE_MODE)

    try:
        wr = wog_post(wog_api_url, "WalletsRemains", base_body)
        remains = wr.get("remains", [])
        if not isinstance(remains, list) or not remains:
            raise RuntimeError("WOG API: пустой remains")

        if DEBUG_WOG:
            logging.info("RAW remains: %s", json.dumps(remains, ensure_ascii=False))

        uah_wallets = pick_uah_wallets(remains)
        if not uah_wallets:
            raise RuntimeError(f"UAH кошельки не найдены. remains={remains}")

        wallet = select_wallet(uah_wallets)

        opening_balance = parse_decimal(wallet.get("Value", 0))
        balance_for_alert = opening_balance
        tx_delta = Decimal("0")
        tx_used = 0
        method = "OPENING"

        if WOG_BALANCE_MODE == "OPENING_PLUS_TX":
            try:
                tr = wog_post(wog_api_url, "Transaction", base_body)
                transactions = tr.get("transactions", [])
                if isinstance(transactions, list):
                    tx_delta, tx_used = calc_today_delta(transactions, str(wallet.get("WalletName", "")))
                    balance_for_alert = opening_balance + tx_delta
                    method = "OPENING_PLUS_TX"
                    if DEBUG_WOG:
                        logging.info("RAW transactions: %s", json.dumps(transactions, ensure_ascii=False))
                else:
                    logging.warning("Transaction: нет списка transactions, используем OPENING.")
            except Exception as tx_err:
                logging.warning("Не удалось учесть Transaction (%s). Используем OPENING.", tx_err)

        logging.info(
            "Кошелек: WalletCode=%s WalletName=%s Goods=%s Opening=%s DeltaTx=%s UsedTx=%s Balance=%s Method=%s",
            wallet.get("WalletCode"),
            wallet.get("WalletName"),
            wallet.get("GoodsName"),
            fmt_money(opening_balance),
            fmt_money(tx_delta),
            tx_used,
            fmt_money(balance_for_alert),
            method
        )

        if balance_for_alert < BALANCE_THRESHOLD:
            lines = [
                "🚨 *Внимание!* 🚨",
                "",
                "Баланс на счету WOG упал ниже порога.",
                "",
                f"Дата запроса ({WOG_TIMEZONE}): *{now_local.strftime('%Y-%m-%d %H:%M:%S')}*",
                f"Баланс для проверки: *{fmt_money(balance_for_alert)} грн.*",
                f"Остаток на начало дня: *{fmt_money(opening_balance)} грн.*",
            ]
            if method == "OPENING_PLUS_TX":
                lines.append(f"Дельта транзакций за день: *{fmt_money(tx_delta)} грн.*")
            lines.extend([
                f"Установленный порог: *{fmt_money(BALANCE_THRESHOLD)} грн.*",
                "",
                "Пора пополнить счет!"
            ])
            send_telegram_message(tg_api_url, "\n".join(lines), TELEGRAM_CHAT_ID)
        else:
            logging.info("Баланс в норме (>= %s грн).", fmt_money(BALANCE_THRESHOLD))

    except requests.exceptions.RequestException as e:
        logging.error("Ошибка сети WOG: %s", e)
    except ValueError as e:
        logging.error("Ошибка JSON WOG: %s", e)
    except Exception as e:
        logging.error("Непредвиденная ошибка: %s", e)


if __name__ == "__main__":
    main()
