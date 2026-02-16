import requests
import logging
import os
import datetime

# --- НАСТРОЙКИ ---
# Теперь мы берем ключи из секретов GitHub, а не пишем их в коде
WOG_API_KEY = os.environ.get('WOG_API_KEY')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# Порог можно оставить здесь или тоже вынести в секреты
BALANCE_THRESHOLD = 1000.0
# --- КОНЕЦ НАСТРОЕК ---

# Настройка логирования для вывода информации в консоль
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def send_telegram_message(api_url, message, chat_id):
    """Отправляет сообщение в Telegram и проверяет результат."""
    payload = {'chat_id': chat_id, 'text': message, 'parse_mode': 'Markdown'}
    try:
        response = requests.post(api_url, data=payload)
        # Проверяем, что Telegram API вернул успешный статус
        if response.status_code == 200:
            logging.info("Уведомление в Telegram успешно отправлено.")
        else:
            # Логируем ошибку от Telegram
            logging.error(f"Ошибка отправки в Telegram: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Сетевая ошибка при отправке в Telegram: {e}")

def main():
    """Основная функция, которая выполняет всю логику."""
    if not all([WOG_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.error("Одна или несколько переменных окружения (WOG_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) не установлены.")
        return

    WOG_API_URL = f"https://api-fuelcards.wog.ua/{WOG_API_KEY}"
    TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    logging.info("Проверка баланса WOG...")
    headers = {'Content-Type': 'application/json'}
    data = {
        "date": datetime.datetime.now().strftime("%Y%m%d"),
        "version": "1.0"
    }

    try:
        response = requests.post(WOG_API_URL, headers=headers, json=data, params={'Action': 'WalletsRemains'})
        response.raise_for_status() # Проверка на HTTP-ошибки

        response_data = response.json()
        if response_data.get("status") == 0 and "remains" in response_data:
            # Находим ВСЕ гривневые кошельки, а не только первый
            uah_wallets = [wallet for wallet in response_data["remains"] if wallet.get("GoodsName") == "Грн"]

            if uah_wallets:
                # Суммируем балансы всех найденных кошельков
                current_balance = sum(float(wallet.get("Value", 0.0)) for wallet in uah_wallets)
                logging.info(f"Общий баланс: {current_balance:.2f} грн.")

                if current_balance < BALANCE_THRESHOLD:
                    message = (
                        f"🚨 *Внимание!* 🚨\n\n"
                        f"Баланс на счету WOG упал ниже порога.\n\n"
                        f"Текущий баланс: *{current_balance:.2f} грн.*\n"
                        f"Установленный порог: *{BALANCE_THRESHOLD:.2f} грн.*\n\n"
                        f"Пора пополнить счет!"
                    )
                    send_telegram_message(TELEGRAM_API_URL, message, TELEGRAM_CHAT_ID)
                else:
                    logging.info(f"Баланс в норме (больше или равен {BALANCE_THRESHOLD:.2f} грн).")
            else:
                logging.warning("Гривневый кошелек не найден в ответе API.")
        else:
            logging.error(f"API WOG вернуло ошибку: {response_data.get('error', 'Неизвестная ошибка')}")

    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка сети при запросе к WOG: {e}")
    except Exception as e:
        logging.error(f"Произошла непредвиденная ошибка: {e}")

if __name__ == "__main__":
    main()
