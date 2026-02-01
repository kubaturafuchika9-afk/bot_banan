import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
from typing import Optional
import re

from dotenv import load_dotenv
import google.generativeai as genai
from telegram import Update, File
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)
import requests
from PIL import Image
from io import BytesIO

# === КОНФИГУРАЦИЯ ===
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
NANOBANA_API_KEY = os.getenv("NANOBANA_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
TELEGRAM_USER_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))  # Для отчётов

# Создаём папки
Path("dialogs").mkdir(exist_ok=True)
Path("reports").mkdir(exist_ok=True)

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === ИНИЦИАЛИЗАЦИЯ GEMINI ===
genai.configure(api_key=GEMINI_API_KEY)

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
user_contexts = defaultdict(lambda: [])  # История по user_id
report_requests = defaultdict(int)  # Счётчик /ok запросов
last_daily_report = None
last_hourly_reports = {}


# === ФУНКЦИИ ЛОГИРОВАНИЯ ===

def log_message(user_id: int, user_name: str, message_text: str):
    """Сохраняет сообщение в dialogs_YYYY-MM-DD.json"""
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"dialogs/dialogs_{today}.json"
    
    entry = {
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id,
        "user_name": user_name,
        "message_text": message_text[:500]  # Первые 500 символов
    }
    
    try:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = []
        
        data.append(entry)
        
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка логирования: {e}")


def get_today_dialogs() -> list:
    """Читает все диалоги за сегодня"""
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"dialogs/dialogs_{today}.json"
    
    if not os.path.exists(filename):
        return []
    
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


# === ФУНКЦИИ ОТЧЁТОВ ===

def analyze_messages_locally(messages: list) -> dict:
    """Анализирует сообщения локально (без API)"""
    if not messages:
        return {
            "total": 0,
            "users": 0,
            "themes": [],
            "interesting": []
        }
    
    user_count = len(set(m["user_id"] for m in messages))
    
    # Простой анализ тем по ключевым словам
    theme_keywords = {
        "программирование": ["код", "python", "javascript", "debug"],
        "вопросы": ["?", "как", "почему", "что"],
        "новости": ["новость", "произошло", "случилось"],
        "личное": ["я", "мне", "мой", "моя"],
    }
    
    themes = defaultdict(int)
    interesting = []
    
    for msg in messages:
        text = msg["message_text"].lower()
        
        # Определяем тему
        for theme, keywords in theme_keywords.items():
            if any(kw in text for kw in keywords):
                themes[theme] += 1
        
        # Ищем интересные вопросы (с вопросительным знаком)
        if "?" in text and len(text) > 20:
            interesting.append({
                "user": msg["user_name"],
                "text": text[:100]
            })
    
    return {
        "total": len(messages),
        "users": user_count,
        "themes": dict(sorted(themes.items(), key=lambda x: x[1], reverse=True)[:5]),
        "interesting": interesting[:3]
    }


def create_hourly_report():
    """Создаёт отчёт за час"""
    hour = datetime.now().strftime("%H")
    filename = f"reports/hourly_report_{hour}.txt"
    
    # Получаем сообщения за текущий час
    messages = get_today_dialogs()
    now = datetime.now()
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    
    hourly_messages = [
        m for m in messages
        if hour_start.isoformat() <= m["timestamp"] < hour_end.isoformat()
    ]
    
    analysis = analyze_messages_locally(hourly_messages)
    
    report = f"""=== ПОЧАСОВОЙ ОТЧЁТ ({hour}:00) ===
Время: {datetime.now().strftime("%Y-%m-%d %H:%M")}

📊 Статистика:
- Сообщений: {analysis['total']}
- Уникальных пользователей: {analysis['users']}

🏷️ Основные темы:
"""
    
    for theme, count in analysis['themes'].items():
        report += f"  • {theme}: {count}\n"
    
    if analysis['interesting']:
        report += f"\n❓ Интересные вопросы:\n"
        for item in analysis['interesting']:
            report += f"  • {item['user']}: {item['text']}...\n"
    
    report += f"\n✅ Отчёт создан автоматически\n"
    
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"Почасовой отчёт создан: {filename}")
    except Exception as e:
        logger.error(f"Ошибка создания отчёта: {e}")


async def create_daily_report_with_api():
    """Создаёт итоговый отчёт за день через API"""
    filename = f"reports/daily_report.txt"
    
    messages = get_today_dialogs()
    analysis = analyze_messages_locally(messages)
    
    # Формируем краткое резюме для API
    summary_text = f"""
    За день было {analysis['total']} сообщений от {analysis['users']} пользователей.
    Основные темы обсуждения: {', '.join(analysis['themes'].keys())}.
    Интересные вопросы: {json.dumps(analysis['interesting'][:2])}.
    Создай краткий понятный отчёт (2-3 предложения) что обсуждалось и какие тренды.
    """
    
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(summary_text)
        api_summary = response.text
    except Exception as e:
        logger.error(f"Ошибка API для отчёта: {e}")
        api_summary = "Ошибка при генерации отчёта"
    
    report = f"""=== ДНЕВНОЙ ОТЧЁТ ===
Дата: {datetime.now().strftime("%Y-%m-%d")}

📊 Статистика:
- Всего сообщений: {analysis['total']}
- Уникальных пользователей: {analysis['users']}

🏷️ Основные темы:
"""
    
    for theme, count in analysis['themes'].items():
        report += f"  • {theme}: {count}\n"
    
    if analysis['interesting']:
        report += f"\n❓ Интересные вопросы:\n"
        for item in analysis['interesting']:
            report += f"  • {item['user']}: {item['text']}...\n"
    
    report += f"\n📝 Анализ API:\n{api_summary}\n"
    report += f"\n✅ Отчёт создан: {datetime.now().strftime('%H:%M:%S')}\n"
    
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info("Дневной отчёт создан")
        return api_summary
    except Exception as e:
        logger.error(f"Ошибка создания дневного отчёта: {e}")
        return None


# === ФУНКЦИИ GEMINI ===

def is_image_generation_request(text: str) -> bool:
    """Проверяет, это ли запрос на генерацию картинки"""
    keywords = [
        "нарисуй", "создай", "генери", "сделай картину",
        "картинку", "изображение", "draw", "create image",
        "generate image", "make a picture"
    ]
    return any(kw in text.lower() for kw in keywords)


async def generate_image_via_nanobana(prompt: str) -> Optional[bytes]:
    """Генерирует картинку через Nanobana API"""
    try:
        url = "https://api.nanobana.pro/v1/images/generations"
        
        headers = {
            "Authorization": f"Bearer {NANOBANA_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "prompt": prompt,
            "model": "stable-diffusion-xl",
            "size": "1024x1024",
            "quality": "hd",  # 4K качество
            "n": 1
        }
        
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        
        if response.status_code == 200:
            data = response.json()
            image_url = data.get("data", [{}])[0].get("url")
            
            if image_url:
                img_response = requests.get(image_url, timeout=30)
                return img_response.content
        else:
            logger.error(f"Nanobana API error: {response.status_code}")
    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}")
    
    return None


async def get_gemini_response(
    user_id: int,
    message_text: str,
    image_data: Optional[bytes] = None,
    audio_file: Optional[File] = None
) -> str:
    """Получает ответ от Gemini с контекстом"""
    
    # Добавляем в контекст
    user_contexts[user_id].append({
        "role": "user",
        "parts": [message_text]
    })
    
    # Оставляем последние 5 сообщений
    if len(user_contexts[user_id]) > 10:  # 5 пар user-assistant
        user_contexts[user_id] = user_contexts[user_id][-10:]
    
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        
        # Формируем содержимое сообщения
        content_parts = [message_text]
        
        # Если есть картинка
        if image_data:
            try:
                img = Image.open(BytesIO(image_data))
                content_parts = [
                    genai.types.Part.from_data(
                        data=image_data,
                        mime_type="image/jpeg"
                    ),
                    message_text
                ]
            except Exception as e:
                logger.error(f"Ошибка обработки изображения: {e}")
        
        # Формируем историю
        history = []
        for msg in user_contexts[user_id][:-1]:
            history.append({"role": msg["role"], "parts": msg["parts"]})
        
        # Отправляем запрос
        response = model.generate_content(
            content_parts,
            stream=False
        )
        
        answer = response.text[:500]  # Максимум 500 символов
        
        # Сохраняем в контекст
        user_contexts[user_id].append({
            "role": "assistant",
            "parts": [answer]
        })
        
        return answer
        
    except Exception as e:
        logger.error(f"Ошибка Gemini API: {e}")
        return "❌ Ошибка при обработке запроса. Попробуй позже."


# === ОБРАБОТЧИКИ КОМАНД ===

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    welcome = """👋 Привет! Я чат-бот на Gemini Flash.
    
Я умею:
✅ Отвечать на текстовые вопросы
🎤 Обрабатывать голосовые сообщения
🖼️ Анализировать картинки
🎨 Генерировать изображения (нарисуй, создай...)
📝 Помнить контекст (последние 5 сообщений)

Команды:
/clear - очистить историю
/ok - получить отчёт (макс 5 раз в день)
/help - справка"""
    
    await update.message.reply_text(welcome)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    help_text = """📚 СПРАВКА:

🔹 Текстовые вопросы - просто пиши!
🔹 Голосовые - отправь голосовое сообщение
🔹 Картинки - отправь фото и вопрос
🔹 Генерация - "нарисуй...", "создай картинку..."

💬 Я отвечаю коротко (до 500 символов)
🌍 Язык: Русский, Азербайджанский
😏 Могу матом и насмехаться (если заслужил)

/clear - новый разговор
/ok - отчёт за день"""
    
    await update.message.reply_text(help_text)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /clear"""
    user_id = update.effective_user.id
    user_contexts[user_id] = []
    await update.message.reply_text("🧹 История очищена. Начнём с нуля!")


async def ok_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /ok - получить отчёт"""
    user_id = update.effective_user.id
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Счётчик по дням
    key = f"{user_id}_{today}"
    report_requests[key] = report_requests.get(key, 0) + 1
    
    if report_requests[key] > 5:
        await update.message.reply_text("❌ Ты исчерпал лимит отчётов на сегодня (максимум 5)")
        return
    
    # Читаем отчёт если существует
    report_file = "reports/daily_report.txt"
    
    if os.path.exists(report_file):
        try:
            with open(report_file, "r", encoding="utf-8") as f:
                report = f.read()
            await update.message.reply_text(f"📊 Вот отчёт:\n\n{report}")
        except Exception as e:
            logger.error(f"Ошибка чтения отчёта: {e}")
            await update.message.reply_text("❌ Ошибка при чтении отчёта")
    else:
        await update.message.reply_text("📭 Отчёт ещё не создан. Вернись позже.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = update.effective_user.id
    user_name = update.effective_user.username or update.effective_user.first_name
    message_text = update.message.text or ""
    
    # Логируем
    log_message(user_id, user_name, message_text)
    
    # Показываем "печает..."
    await update.message.chat.send_action("typing")
    
    # Проверяем запрос на генерацию картинки
    if is_image_generation_request(message_text):
        # Сначала генерируем картинку
        await update.message.chat.send_action("upload_photo")
        image_bytes = await generate_image_via_nanobana(message_text)
        
        if image_bytes:
            await update.message.reply_photo(
                photo=image_bytes,
                caption="🎨 Вот что я для тебя создал!"
            )
        else:
            await update.message.reply_text("❌ Не смог сгенерировать картинку. Попробуй позже.")
        return
    
    # Обычный ответ
    response = await get_gemini_response(user_id, message_text)
    await update.message.reply_text(response)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик фотографий"""
    user_id = update.effective_user.id
    user_name = update.effective_user.username or update.effective_user.first_name
    caption = update.message.caption or "Вот картинка"
    
    # Логируем
    log_message(user_id, user_name, f"[ФОТО] {caption}")
    
    await update.message.chat.send_action("typing")
    
    try:
        # Скачиваем фото
        photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
        photo_bytes = await photo_file.download_as_bytearray()
        
        # Отправляем в Gemini
        response = await get_gemini_response(
            user_id,
            caption,
            image_data=bytes(photo_bytes)
        )
        await update.message.reply_text(response)
        
    except Exception as e:
        logger.error(f"Ошибка обработки фото: {e}")
        await update.message.reply_text("❌ Не смог обработать картинку")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик голосовых сообщений"""
    user_id = update.effective_user.id
    user_name = update.effective_user.username or update.effective_user.first_name
    
    await update.message.chat.send_action("typing")
    
    try:
        # Скачиваем голос
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        voice_bytes = await voice_file.download_as_bytearray()
        
        # Отправляем в Gemini для транскрибирования
        model = genai.GenerativeModel(GEMINI_MODEL)
        
        audio_part = genai.types.Part.from_data(
            data=bytes(voice_bytes),
            mime_type="audio/mpeg"
        )
        
        response = model.generate_content(
            [audio_part, "Транскрибируй это голосовое сообщение на русский и дай короткий ответ"]
        )
        
        transcribed = response.text[:500]
        
        # Логируем
        log_message(user_id, user_name, f"[ГОЛОС] {transcribed}")
        
        await update.message.reply_text(f"🎤 Я услышал:\n\n{transcribed}")
        
    except Exception as e:
        logger.error(f"Ошибка обработки голоса: {e}")
        await update.message.reply_text("❌ Не смог обработать голос. Попробуй ещё раз.")


async def scheduled_hourly_report(context: ContextTypes.DEFAULT_TYPE):
    """Создание отчёта каждый час"""
    try:
        create_hourly_report()
    except Exception as e:
        logger.error(f"Ошибка при создании почасового отчёта: {e}")


async def scheduled_daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Отправка дневного отчёта в 22:00"""
    try:
        summary = await create_daily_report_with_api()
        
        # Отправляем админу
        if TELEGRAM_USER_ID and summary:
            try:
                await context.bot.send_message(
                    chat_id=TELEGRAM_USER_ID,
                    text=f"📊 ДНЕВНОЙ ОТЧЁТ\n\n{summary}"
                )
            except Exception as e:
                logger.error(f"Ошибка отправки отчёта админу: {e}")
    except Exception as e:
        logger.error(f"Ошибка при создании дневного отчёта: {e}")


async def post_init(application: Application):
    """Инициализация планировщика"""
    # Отчёт каждый час
    application.job_queue.run_repeating(
        scheduled_hourly_report,
        interval=3600,  # 1 час
        first=10  # Первый запуск через 10 секунд
    )
    
    # Дневной отчёт в 22:00 (по UTC)
    application.job_queue.run_daily(
        scheduled_daily_report,
        time=datetime.now().replace(hour=22, minute=0, second=0)
    )


async def main():
    """Главная функция"""
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("ok", ok_command))
    
    # Обработчики сообщений
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("🤖 Бот запущен!")
    
    # Для Render используем webhook вместо polling
    await application.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 8080)),
        url_path=TELEGRAM_TOKEN,
        webhook_url=f"{os.getenv('WEBHOOK_URL')}/{TELEGRAM_TOKEN}"
    )


if __name__ == "__main__":
    asyncio.run(main())
