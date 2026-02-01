"""
Инструмент для пробуждения спящего бота на Render.
Отправляет запрос каждые 14 минут.

Использование:
    python keep_alive.py
"""

import os
import time
import requests
from datetime import datetime

WEBHOOK_URL = os.getenv("WEBHOOK_URL")

def keep_alive():
    """Отправляет пинги боту"""
    if not WEBHOOK_URL:
        print("❌ WEBHOOK_URL не установлен в .env")
        return
    
    print(f"🔄 Запускаю keep_alive для: {WEBHOOK_URL}")
    print("Отправляю пинг каждые 14 минут...")
    
    while True:
        try:
            response = requests.get(WEBHOOK_URL, timeout=30)
            status = "✅" if response.status_code == 200 else "⚠️"
            print(f"{status} [{datetime.now().strftime('%H:%M:%S')}] Пинг отправлен")
        except Exception as e:
            print(f"❌ [{datetime.now().strftime('%H:%M:%S')}] Ошибка: {e}")
        
        # Ждём 14 минут
        time.sleep(14 * 60)

if __name__ == "__main__":
    keep_alive()
