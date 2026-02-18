import os
import json
import logging
import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import requests


# --- ENV ---
WOG_API_KEY = os.environ.get("WOG_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BALANCE_THRESHOLD = Decimal(os.environ.get("BALANCE_THRESHOLD", "40000.00"))
WOG_TIMEZONE = os.environ.get("WOG_TIMEZONE", "Europe/Kyiv")

# Для WalletsRemains нужен именно WalletCode (UUID)
WOG_WALLET_CODE = os.environ.get("WOG_WALLET_CODE")

# Ключевые слова для входящих операций, если нет явного направления
WOG_CREDIT_KEYWORDS = [
    x.strip().lower()
    for x in os.environ.get(
        "WOG_CREDIT_KEYWORDS",
        "попов,зарах,возврат,повернен,коригув"
    ).split(",")
    if x.strip()
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


def norm(v: str) -> str:
    return " ".join(str(v or "").strip().lower().split())


def now_in_tz() -> dt.datetime:
    try:
        return dt.datetime.now(ZoneInfo(WOG_TIMEZONE))
    except Exception:
        logging.warning("Не удалось применить таймзону %s, используем локальную.", WOG_TIMEZONE)
        return dt.datetime.now()


def send_telegram_message(api_url: str, message: str, chat_id: str) -> None:
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
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


def pick_uah_wallets(remains: list) -> list:
    wallets = []
    for w in remains:
        goods_name = norm(w.get("GoodsName", ""))
        goods_code = str(w.get("GoodsCode", "")).strip()
        currency_code = str(w.get("CurrencyCode", "")).strip().upper()
        if goods_name in {"грн", "uah"} or goods_code == "980" or currency_code in {"UAH", "980"}:
            wallets.append(w)
    return wallets


def select_wallet(uah_wallets: list) -> dict:
    if WOG_WALLET_CODE:
        selected = [w for w in uah_wallets if str(w.get("WalletCode", "")).strip() == WOG_WALLET_CODE.strip()]
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


def get_tx_amount(tx: dict) -> Optional[Decimal]:
    # 1) если итоговая сумма готова - берем ее
    summ_with_discount = parse_decimal(tx.get("summwithdiscount", -1))
    if summ_with_discount != Decimal("-1"):
        return summ_with_discount

    # 2) если итоговая еще не готова, берем сырой sum
    raw_sum = parse_decimal(tx.get("sum", 0))
    if raw_sum != Decimal("0"):
        return raw_sum

    # 3) суммы нет
    return None


def transaction_signed_amount(tx: dict) -> Optional[Decimal]:
    amount = get_tx_amount(tx)
    if amount is None:
        return None

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

    text = norm(f"{tx.get('goodsName', '')} {tx.get('walletname', '')} {tx.get('cardinfo', '')}")
    if any(k in text for k in WOG_CREDIT_KEYWORDS):
        return abs(amount)

    # По умолчанию считаем расходом
    return -abs(amount)


def calc_today_delta(transactions: list, wallet_name: str) -> Tuple[Decimal, int, int, int]:
    delta = Decimal("0")
    matched = 0
    used = 0
    no_amount = 0
    wallet_name_n = norm(wallet_name)

    for tx in transactions:
        tx_wallet_name = norm(tx.get("walletname", ""))
        if wallet_name_n and tx_wallet_name and tx_wallet_name != wallet_name_n:
            continue

        matched += 1
        signed = transaction_signed_amount(tx)
        if signed is None:
            no_amount += 1
            continue

        delta += signed
        used += 1

    return delta, matched, used, no_amount


def main() -> None:
    if not all([WOG_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.error("Не заданы WOG_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return

    wog_api_url = f"https://api-fuelcards.wog.ua/{WOG_API_KEY}"
    tg_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    now_local = now_in_tz()
    request_date = now_local.strftime("%Y%m%d")
    body = {"date": request_date, "version": "1.0"}

    logging.info("Проверка баланса WOG. date=%s tz=%s", request_date, WOG_TIMEZONE)

    try:
        # 1) Остаток на начало дня
        wr = wog_post(wog_api_url, "WalletsRemains", body)
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

        # 2) Дельта транзакций за день
        tr = wog_post(wog_api_url, "Transaction", body)
        transactions = tr.get("transactions", [])
        if not isinstance(transactions, list):
            logging.error("Transaction не вернул список transactions. Не отправляю уведомление.")
            return

        if DEBUG_WOG:
            logging.info("RAW transactions: %s", json.dumps(transactions, ensure_ascii=False))

        tx_delta, tx_matched, tx_used, tx_no_amount = calc_today_delta(
            transactions,
            str(wallet.get("WalletName", ""))
        )

        # Критичная защита: если ни одной транзакции не удалось учесть,
        # не отправляем, чтобы не показывать баланс на 00:00
        if tx_used == 0:
            logging.error(
                "Не удалось учесть транзакции (matched=%s, no_amount=%s). "
                "Чтобы не показывать баланс на 00:00, уведомление не отправляется.",
                tx_matched, tx_no_amount
            )
            return

        balance_for_check = opening_balance + tx_delta

        logging.info(
            "WalletCode=%s WalletName=%s Opening=%s DeltaTx=%s MatchedTx=%s UsedTx=%s NoAmountTx=%s BalanceForCheck=%s",
            wallet.get("WalletCode"),
            wallet.get("WalletName"),
            fmt_money(opening_balance),
            fmt_money(tx_delta),
            tx_matched,
            tx_used,
            tx_no_amount,
            fmt_money(balance_for_check)
        )

        # Сравнение с порогом
        if balance_for_check < BALANCE_THRESHOLD:
            message = (
                "🚨 *Внимание!* 🚨\n\n"
                f"Баланс для проверки: *{fmt_money(balance_for_check)} грн.*"
            )
            send_telegram_message(tg_api_url, message, TELEGRAM_CHAT_ID)
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

