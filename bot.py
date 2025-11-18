import requests
import asyncio
import json
import os
import logging
from datetime import datetime, time, timedelta, timezone
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)

# Токен бота
BOT_TOKEN = "8036194666:AAEpr97NxUk9wrgj9tvi5StBgvUSRbwxlhk"

# Состояния для диалогов
SETTING_TIME, CONFIRM_DELETE, SETTING_TIMEZONE = range(3)

# Хранилище данных пользователей в оперативной памяти
user_data = {}

# Кэш для погодных данных
weather_cache = {}

# Флаг для отслеживания отправленных уведомлений
sent_notifications = {}

# DATA file in "data" folder next to this script
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DATA_FILE = os.path.join(DATA_DIR, "user_data.json")


def ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)


def save_user_data():
    """Сохраняет user_data в JSON"""
    try:
        ensure_data_dir()
        data_to_save = {}
        for uid, info in user_data.items():
            # keys in JSON should be strings
            data_to_save[str(uid)] = {
                "lat": info.get("lat"),
                "lon": info.get("lon"),
                "has_location": info.get("has_location", False),
                "schedules": [t.strftime("%H:%M") for t in info.get("schedules", [])],
                "timezone_offset": info.get("timezone_offset", 0),  # Сохраняем смещение часового пояса
            }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        print(f"💾 Данные сохранены ({len(user_data)} пользователей)")
    except Exception as e:
        print(f"❌ Ошибка при сохранении: {e}")


def load_user_data():
    """Загружает user_data из JSON"""
    global user_data
    ensure_data_dir()
    if not os.path.exists(DATA_FILE):
        print("📁 Файл user_data.json отсутствует, будет создан при первом сохранении.")
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        user_data.clear()
        for uid_str, info in data.items():
            try:
                uid = int(uid_str)
            except Exception:
                # skip invalid keys
                continue
            schedules = []
            for t in info.get("schedules", []):
                try:
                    h, m = map(int, t.split(":"))
                    schedules.append(time(h, m))
                except Exception:
                    pass
            user_data[uid] = {
                "lat": info.get("lat"),
                "lon": info.get("lon"),
                "has_location": info.get("has_location", False),
                "schedules": schedules,
                "timezone_offset": info.get("timezone_offset", 0),  # Загружаем смещение часового пояса
            }
        print(f"✅ Загружено {len(user_data)} пользователей из JSON.")
    except Exception as e:
        print(f"❌ Ошибка при загрузке: {e}")


def get_user(user_id):
    if user_id not in user_data:
        user_data[user_id] = {
            "lat": None,
            "lon": None,
            "schedules": [],
            "has_location": False,
            "timezone_offset": 0,  # По умолчанию UTC+0
        }
    return user_data[user_id]


def get_user_local_time(user_id):
    """Получает локальное время пользователя"""
    user = get_user(user_id)
    utc_now = datetime.now(timezone.utc)
    user_timezone = timezone(timedelta(hours=user.get("timezone_offset", 0)))
    return utc_now.astimezone(user_timezone)


def validate_coordinates(lat, lon):
    """Проверка валидности координат"""
    try:
        lat = float(lat)
        lon = float(lon)
        return -90 <= lat <= 90 and -180 <= lon <= 180
    except (ValueError, TypeError):
        return False


def get_weather_icon(weather_code, is_day=True):
    """Получение иконки погоды"""
    icons = {
        0: "☀️" if is_day else "🌙",
        1: "🌤️",
        2: "⛅",
        3: "☁️",
        45: "🌫️",
        48: "🌫️",
        51: "🌦️",
        53: "🌦️",
        55: "🌧️",
        61: "🌧️",
        63: "🌧️",
        65: "⛈️",
        71: "❄️",
        73: "❄️",
        75: "❄️",
        77: "🌨️",
        80: "🌦️",
        81: "🌧️",
        82: "⛈️",
        85: "🌨️",
        86: "🌨️",
        95: "⛈️",
        96: "⛈️",
        99: "⛈️",
    }
    return icons.get(weather_code, "🌤️")


def get_weather_description(weather_code):
    """Получение описания погоды"""
    descriptions = {
        0: "Ясно",
        1: "Преимущественно ясно",
        2: "Переменная облачность",
        3: "Пасмурно",
        45: "Туман",
        48: "Густой туман",
        51: "Легкая морось",
        53: "Умеренная морось",
        55: "Сильная морось",
        61: "Небольшой дождь",
        63: "Умеренный дождь",
        65: "Сильный дождь",
        71: "Небольшой снег",
        73: "Умеренный снег",
        75: "Сильный снег",
        77: "Снежные зерна",
        80: "Небольшие ливни",
        81: "Умеренные ливни",
        82: "Сильные ливни",
        85: "Небольшие снежные ливни",
        86: "Сильные снежные ливни",
        95: "Гроза",
        96: "Гроза с небольшим градом",
        99: "Гроза с сильным градом",
    }
    return descriptions.get(weather_code, "Неизвестно")


async def get_weather_by_coords(lat, lon):
    """Получение погоды по координатам с кэшированием"""
    cache_key = f"{lat:.2f}_{lon:.2f}"

    # Проверяем кэш (данные не старше 10 минут)
    if cache_key in weather_cache:
        cached_time, cached_data = weather_cache[cache_key]
        if datetime.now() - cached_time < timedelta(minutes=10):
            return cached_data

    try:
        if not validate_coordinates(lat, lon):
            return {"success": False, "error": "Неверные координаты"}

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current_weather": "true",
            "daily": "temperature_2m_max,temperature_2m_min,weathercode",
            "timezone": "auto",
            "forecast_days": 3,
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        current = data["current_weather"]
        temperature = round(current["temperature"])
        weather_code = current["weathercode"]
        wind_speed = current["windspeed"]
        is_day = current.get("is_day", 1) == 1

        # Получаем иконку и описание
        icon = get_weather_icon(weather_code, is_day)
        description = get_weather_description(weather_code)

        # Прогноз на несколько дней
        forecast = []
        if "daily" in data:
            daily = data["daily"]
            for i in range(min(3, len(daily["time"]))):
                forecast.append(
                    {
                        "date": daily["time"][i],
                        "max_temp": round(daily["temperature_2m_max"][i]),
                        "min_temp": round(daily["temperature_2m_min"][i]),
                        "weather_code": daily["weathercode"][i],
                    }
                )

        result = {
            "temperature": temperature,
            "condition": f"{icon} {description}",
            "wind_speed": wind_speed,
            "forecast": forecast,
            "success": True,
        }

        # Сохраняем в кэш
        weather_cache[cache_key] = (datetime.now(), result)

        return result

    except requests.exceptions.RequestException as e:
        print(f"Ошибка получения погоды: {e}")
        return {"success": False, "error": "Не удалось получить данные о погоде"}
    except Exception as e:
        print(f"Неожиданная ошибка погоды: {e}")
        return {"success": False, "error": "Произошла непредвиденная ошибка"}


def get_russian_day_name(weekday):
    """Получение русского названия дня недели"""
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return days[weekday]


def get_time_emoji(hour):
    """Получение эмодзи для времени"""
    if 0 <= hour < 4:
        return "🌙"
    elif 4 <= hour < 8:
        return "🌅"
    elif 8 <= hour < 12:
        return "☀️"
    elif 12 <= hour < 16:
        return "🌞"
    elif 16 <= hour < 20:
        return "🌇"
    else:
        return "🌃"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.message.from_user.id
    user = get_user(user_id)

    # Клавиатура с кнопкой геолокации
    location_button = KeyboardButton("📍 Отправить местоположение", request_location=True)
    keyboard = [
        [location_button],
        ["🌤️ Погода здесь", "📅 Прогноз на 3 дня"],
        ["⏰ Настроить уведомления", "📋 Мои уведомления"],
        ["🕐 Настроить часовой пояс", "🔄 Сбросить данные"],
        ["ℹ️ Помощь"],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    welcome_text = "🌤️ **Бот погоды с геолокацией**\n\n📍 **Ваш статус:** "

    if user["has_location"]:
        local_time = get_user_local_time(user_id)
        timezone_offset = user.get("timezone_offset", 0)
        timezone_sign = "+" if timezone_offset >= 0 else ""
        welcome_text += f"Местоположение установлено ✅\nКоординаты: {user['lat']:.4f}, {user['lon']:.4f}\n"
        welcome_text += f"🕐 Часовой пояс: UTC{timezone_sign}{timezone_offset}\n"
        welcome_text += f"⏰ Ваше локальное время: {local_time.strftime('%H:%:%S %d.%m.%Y')}"
    else:
        welcome_text += "Местоположение не установлено ❌\n\nНажмите кнопку ниже чтобы отправить местоположение"

    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик геолокации"""
    user_id = update.message.from_user.id
    location = update.message.location
    lat = location.latitude
    lon = location.longitude

    # Сохраняем координаты пользователя
    user = get_user(user_id)
    user["lat"] = lat
    user["lon"] = lon
    user["has_location"] = True
    save_user_data()
    await update.message.reply_text(
        "✅ **Местоположение сохранено!**\n\nТеперь вы можете получать точный прогноз погоды для вашего местоположения.",
        reply_markup=get_main_keyboard(),
    )


async def weather_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Погода по текущим координатам"""
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if not user["has_location"]:
        await update.message.reply_text(
            "❌ **Сначала установите местоположение!**\n\nНажмите кнопку '📍 Отправить местоположение' и разрешите доступ к геоданным.",
            reply_markup=get_main_keyboard(),
        )
        return

    await update.message.reply_text("🌤️ Получаю актуальный прогноз...")

    weather = await get_weather_by_coords(user["lat"], user["lon"])

    if weather and weather["success"]:
        temp_emoji = "❄️" if weather["temperature"] < 0 else "🌡️"
        local_time = get_user_local_time(user_id)
        message = (
            f"🌤️ **Погода в вашем местоположении:**\n\n"
            f"{temp_emoji} **Температура:** {weather['temperature']}°C\n"
            f"{weather['condition']}\n"
            f"💨 **Ветер:** {weather['wind_speed']} м/с\n"
            f"📍 **Координаты:** {user['lat']:.4f}, {user['lon']:.4f}\n"
            f"🕐 **Локальное время:** {local_time.strftime('%H:%M %d.%m.%Y')}"
        )
    else:
        error_msg = weather.get("error", "Данные временно недоступны") if weather else "Данные временно недоступны"
        message = f"❌ **Не удалось получить данные о погоде**\n\nОшибка: {error_msg}"

    await update.message.reply_text(message, parse_mode="Markdown")


async def three_day_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прогноз на 3 дня"""
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if not user["has_location"]:
        await update.message.reply_text("❌ **Сначала установите местоположение!**", reply_markup=get_main_keyboard())
        return

    await update.message.reply_text("📅 Получаю расширенный прогноз...")

    weather = await get_weather_by_coords(user["lat"], user["lon"])

    if weather and weather["success"] and weather.get("forecast"):
        message = "📅 **Прогноз на 3 дня:**\n\n"

        for day in weather["forecast"]:
            date = datetime.strptime(day["date"], "%Y-%m-%d")
            day_name = get_russian_day_name(date.weekday())
            icon = get_weather_icon(day["weather_code"])

            message += (
                f"**{day_name}** ({date.strftime('%d.%m')})\n"
                f"{icon} {get_weather_description(day['weather_code'])}\n"
                f"⬆️ {day['max_temp']}°C ⬇️ {day['min_temp']}°C\n\n"
            )
    else:
        message = "❌ Не удалось получить расширенный прогноз."

    await update.message.reply_text(message, parse_mode="Markdown")


async def setup_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка часового пояса"""
    user_id = update.message.from_user.id
    
    keyboard = [
        ["UTC-11", "UTC-10", "UTC-9", "UTC-8"],
        ["UTC-7", "UTC-6", "UTC-5", "UTC-4"],
        ["UTC-3", "UTC-2", "UTC-1", "UTC±0"],
        ["UTC+1", "UTC+2", "UTC+3", "UTC+4"],
        ["UTC+5", "UTC+6", "UTC+7", "UTC+8"],
        ["UTC+9", "UTC+10", "UTC+11", "UTC+12"],
        ["🔙 Отмена"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    user = get_user(user_id)
    current_offset = user.get("timezone_offset", 0)
    current_sign = "+" if current_offset >= 0 else ""
    
    await update.message.reply_text(
        f"🕐 **Настройка часового пояса**\n\n"
        f"Текущий часовой пояс: UTC{current_sign}{current_offset}\n"
        f"Выберите ваш часовой пояс:",
        reply_markup=reply_markup
    )
    
    return SETTING_TIMEZONE


async def handle_timezone_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора часового пояса"""
    user_id = update.message.from_user.id
    timezone_text = update.message.text

    if timezone_text == "🔙 Отмена":
        await update.message.reply_text("❌ Настройка часового пояса отменена", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    try:
        # Парсим часовой пояс из текста (например, "UTC+3")
        if timezone_text == "UTC±0":
            offset = 0
        else:
            offset_str = timezone_text.replace("UTC", "").strip()
            offset = int(offset_str)
        
        # Сохраняем часовой пояс пользователя
        user = get_user(user_id)
        user["timezone_offset"] = offset
        save_user_data()
        
        local_time = get_user_local_time(user_id)
        sign = "+" if offset >= 0 else ""
        
        await update.message.reply_text(
            f"✅ **Часовой пояс установлен!**\n\n"
            f"🕐 Ваш часовой пояс: UTC{sign}{offset}\n"
            f"⏰ Ваше локальное время: {local_time.strftime('%H:%M:%S %d.%m.%Y')}",
            reply_markup=get_main_keyboard()
        )
        
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("❌ Неверный формат часового пояса", reply_markup=get_main_keyboard())
        return ConversationHandler.END


async def setup_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка уведомлений - выбор часов"""
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if not user["has_location"]:
        await update.message.reply_text("❌ **Сначала установите местоположение!**", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    # Показываем текущее локальное время пользователя
    local_time = get_user_local_time(user_id)
    timezone_offset = user.get("timezone_offset", 0)
    timezone_sign = "+" if timezone_offset >= 0 else ""
    
    keyboard = [
        ["🕐 00-03 часа", "🕑 04-07 часов", "🕒 08-11 часов"],
        ["🕓 12-15 часов", "🕔 16-19 часов", "🕕 20-23 часа"],
        ["🔙 Отмена"],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        f"⏰ **Настройка напоминаний**\n\n"
        f"🕐 Ваш часовой пояс: UTC{timezone_sign}{timezone_offset}\n"
        f"⏰ Ваше текущее время: {local_time.strftime('%H:%M:%S')}\n\n"
        f"Выберите временной диапазон для настройки (ваше локальное время):",
        reply_markup=reply_markup
    )

    return SETTING_TIME


async def handle_time_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора временного диапазона"""
    time_text = update.message.text

    if time_text == "🔙 Отмена":
        await update.message.reply_text("❌ Настройка напоминаний отменена", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    # Определяем диапазон часов
    if "00-03" in time_text:
        start_hour, end_hour = 0, 3
        time_range = "00-03 часа"
    elif "04-07" in time_text:
        start_hour, end_hour = 4, 7
        time_range = "04-07 часов"
    elif "08-11" in time_text:
        start_hour, end_hour = 8, 11
        time_range = "08-11 часов"
    elif "12-15" in time_text:
        start_hour, end_hour = 12, 15
        time_range = "12-15 часов"
    elif "16-19" in time_text:
        start_hour, end_hour = 16, 19
        time_range = "16-19 часов"
    elif "20-23" in time_text:
        start_hour, end_hour = 20, 23
        time_range = "20-23 часа"
    else:
        await update.message.reply_text("❌ Неверный диапазон", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    # Сохраняем диапазон в контексте
    context.user_data["time_range"] = (start_hour, end_hour, time_range)

    # Создаем клавиатуру с минутами для выбранного диапазона
    keyboard = []
    for hour in range(start_hour, end_hour + 1):
        row = []
        for minute in [0, 15, 30, 45]:
            time_str = f"{hour:02d}:{minute:02d}"
            emoji = get_time_emoji(hour)
            row.append(f"{emoji} {time_str}")
        keyboard.append(row)

    keyboard.append(["🔙 Назад", "🔙 Отмена"])

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(f"⏰ **Выберите время в диапазоне {time_range} (ваше локальное время):**\n\nДоступные минуты: 00, 15, 30, 45", reply_markup=reply_markup)

    return SETTING_TIME


async def save_notification_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение времени уведомления"""
    user_id = update.message.from_user.id
    time_text = update.message.text

    if time_text == "🔙 Отмена":
        await update.message.reply_text("❌ Настройка напоминаний отменена", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    if time_text == "🔙 Назад":
        # Возвращаем к выбору диапазона
        return await setup_notifications(update, context)

    try:
        # Извлекаем время из текста (формат "🌙 02:15")
        time_parts = time_text.split()
        if len(time_parts) < 2:
            raise ValueError("Неверный формат времени")

        time_str = time_parts[1]
        hours, minutes = map(int, time_str.split(":"))

        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            await update.message.reply_text("❌ **Неверное время!**", reply_markup=get_main_keyboard())
            return await setup_notifications(update, context)

        user = get_user(user_id)
        notification_time = time(hours, minutes)

        if notification_time in user["schedules"]:
            await update.message.reply_text(f"❌ Напоминание на {time_str} уже установлено!", reply_markup=get_continue_keyboard())
            return SETTING_TIME

        user["schedules"].append(notification_time)
        user["schedules"].sort()
        save_user_data()
        
        # Показываем пользователю время в UTC для информации
        user_timezone = timezone(timedelta(hours=user.get("timezone_offset", 0)))
        utc_timezone = timezone.utc
        today = datetime.now().date()
        user_datetime = datetime.combine(today, notification_time).replace(tzinfo=user_timezone)
        utc_datetime = user_datetime.astimezone(utc_timezone)
        
        await update.message.reply_text(
            f"✅ **Напоминание установлено на {time_str}!**\n"
            f"🕐 Время по UTC: {utc_datetime.strftime('%H:%M')}\n\n"
            f"Хотите установить еще одно напоминание?",
            reply_markup=get_continue_keyboard()
        )

        return SETTING_TIME

    except (ValueError, IndexError):
        await update.message.reply_text("❌ **Неверный формат времени!**\n\nПожалуйста, выберите время из предложенных вариантов.", reply_markup=get_main_keyboard())
        return await setup_notifications(update, context)


async def handle_continue_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора продолжить или закончить"""
    choice = update.message.text

    if choice == "✅ Да, добавить еще":
        return await setup_notifications(update, context)
    else:
        await update.message.reply_text("✅ Настройка напоминаний завершена", reply_markup=get_main_keyboard())
        return ConversationHandler.END


async def show_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать текущие уведомления"""
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if not user["schedules"]:
        await update.message.reply_text("📋 **У вас нет активных напоминаний**", reply_markup=get_main_keyboard())
        return

    # Сортируем напоминания по времени
    user["schedules"].sort()

    schedules_text = "⏰ **Ваши напоминания (ваше локальное время):**\n\n"
    for i, schedule_time in enumerate(user["schedules"], 1):
        emoji = get_time_emoji(schedule_time.hour)
        schedules_text += f"{i}. {emoji} {schedule_time.strftime('%H:%M')}\n"

    # Показываем соответствующие UTC времена
    user_timezone = timezone(timedelta(hours=user.get("timezone_offset", 0)))
    utc_timezone = timezone.utc
    today = datetime.now().date()
    
    schedules_text += "\n**Соответствующее время UTC:**\n"
    for i, schedule_time in enumerate(user["schedules"], 1):
        user_datetime = datetime.combine(today, schedule_time).replace(tzinfo=user_timezone)
        utc_datetime = user_datetime.astimezone(utc_timezone)
        schedules_text += f"{i}. {utc_datetime.strftime('%H:%M')}\n"

    schedules_text += f"\n📊 **Всего: {len(user['schedules'])} напоминаний**"
    schedules_text += "\n\nℹ️ *Напоминания работают только при активном боте*"

    await update.message.reply_text(schedules_text, parse_mode="Markdown")


async def reset_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сброс всех данных"""
    user_id = update.message.from_user.id
    user = get_user(user_id)

    user["lat"] = None
    user["lon"] = None
    user["has_location"] = False
    user["schedules"] = []
    user["timezone_offset"] = 0
    save_user_data()
    await update.message.reply_text("✅ **Все данные сброшены!**\n\nТеперь вы можете установить новое местоположение.", reply_markup=get_main_keyboard())


async def delete_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список уведомлений для удаления (inline)"""
    user_id = update.message.from_user.id
    user = get_user(user_id)

    if not user["schedules"]:
        await update.message.reply_text("📋 У вас нет напоминаний для удаления.", reply_markup=get_main_keyboard())
        return

    user["schedules"].sort()
    keyboard = []
    for i, t in enumerate(user["schedules"]):
        keyboard.append([InlineKeyboardButton(f"Удалить {t.strftime('%H:%M')}", callback_data=f"del_{i}")])

    keyboard.append([InlineKeyboardButton("🗑 Удалить все", callback_data="del_all"), InlineKeyboardButton("❌ Отмена", callback_data="del_cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🕐 Выберите уведомление для удаления:", reply_markup=reply_markup)


async def handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка удаления уведомлений (callback)"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)

    if query.data == "del_cancel":
        try:
            await query.edit_message_text("❌ Удаление отменено.")
        except Exception:
            await query.message.reply_text("❌ Удаление отменено.")
        return

    if query.data == "del_all":
        count = len(user["schedules"])
        user["schedules"].clear()
        save_user_data()
        try:
            await query.edit_message_text(f"✅ Удалено {count} напоминаний.")
        except Exception:
            await query.message.reply_text(f"✅ Удалено {count} напоминаний.")
        return

    if query.data.startswith("del_"):
        try:
            idx = int(query.data.split("_", 1)[1])
            if 0 <= idx < len(user["schedules"]):
                removed = user["schedules"].pop(idx)
                save_user_data()
                try:
                    await query.edit_message_text(f"✅ Уведомление {removed.strftime('%H:%M')} удалено.")
                except Exception:
                    await query.message.reply_text(f"✅ Уведомление {removed.strftime('%H:%M')} удалено.")
            else:
                try:
                    await query.edit_message_text("❌ Указанное напоминание не найдено.")
                except Exception:
                    await query.message.reply_text("❌ Указанное напоминание не найдено.")
        except Exception as e:
            print("Ошибка при обработке callback удаления:", e)
            try:
                await query.edit_message_text("❌ Ошибка при удалении.")
            except Exception:
                await query.message.reply_text("❌ Ошибка при удалении.")


async def send_test_notification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тестовая отправка уведомления"""
    user_id = update.message.from_user.id
    user = get_user(user_id)

    # Проверяем, установлено ли местоположение
    if not user["has_location"]:
        await update.message.reply_text(
            "❌ Сначала установите местоположение!\n\n"
            "Нажмите 📍 'Отправить местоположение' и разрешите доступ к геоданным."
        )
        return

    await update.message.reply_text("🔔 Отправляю тестовое уведомление...")

    try:
        # Отправляем уведомление прямо сейчас
        await send_weather_notification(context.application, user_id)
        await update.message.reply_text("✅ Тестовое уведомление отправлено!")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при отправке уведомления: {e}")
        print(f"[Ошибка send_test_notification] {e}")


async def send_weather_notification(application, user_id):
    """Отправка уведомления о погоде"""
    try:
        user = get_user(user_id)
        if not user["has_location"]:
            return

        weather = await get_weather_by_coords(user["lat"], user["lon"])

        if weather and weather["success"]:
            local_time = get_user_local_time(user_id)
            message = (
                f"🔔 **Напоминание о погоде** ({local_time.strftime('%H:%M')})\n\n"
                f"🌤️ **Погода в вашем местоположении:**\n"
                f"• 🌡️ Температура: {weather['temperature']}°C\n"
                f"• 📝 {weather['condition']}\n"
                f"• 💨 Ветер: {weather['wind_speed']} м/с\n\n"
                f"Хорошего дня! ☀️"
            )
        else:
            message = "❌ Не удалось получить данные о погоде для напоминания."

        await application.bot.send_message(chat_id=user_id, text=message, parse_mode="Markdown")

    except Exception as e:
        print(f"Ошибка отправки уведомления: {e}")


async def check_and_send_notifications(application):
    """Проверка и отправка уведомлений по расписанию"""
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            current_utc_time = now_utc.time().replace(second=0, microsecond=0)
            current_date = now_utc.date()

            for user_id, user in list(user_data.items()):
                if user.get("has_location") and user.get("schedules"):
                    # Получаем часовой пояс пользователя
                    user_timezone = timezone(timedelta(hours=user.get("timezone_offset", 0)))
                    
                    # Конвертируем UTC время в локальное время пользователя
                    user_datetime = now_utc.astimezone(user_timezone)
                    user_local_time = user_datetime.time().replace(second=0, microsecond=0)
                    
                    # Проверяем, совпадает ли локальное время пользователя с любым из его расписаний
                    if any(user_local_time == t for t in user.get("schedules", [])):
                        # Проверяем, не отправляли ли уже уведомление в это время сегодня
                        notification_key = f"{user_id}_{current_date}_{user_local_time}"
                        if notification_key not in sent_notifications:
                            print(f"🕐 Отправляю уведомление пользователю {user_id} в {user_local_time} (UTC: {current_utc_time})")
                            await send_weather_notification(application, user_id)
                            sent_notifications[notification_key] = True

            # Очищаем старые записи (старше 1 дня)
            current_date_str = str(current_date)
            keys_to_remove = [key for key in sent_notifications.keys() if current_date_str not in key]
            for key in keys_to_remove:
                del sent_notifications[key]

            # Проверяем каждую минуту
            await asyncio.sleep(60)

        except Exception as e:
            print(f"Ошибка в проверке уведомлений: {e}")
            await asyncio.sleep(60)


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать справку"""
    help_text = (
        "ℹ️ **Помощь по боту погоды**\n\n"
        "📍 **Отправить местоположение**\nБот запомнит ваши координаты для показа погоды\n\n"
        "🌤️ **Погода здесь**\nПоказывает актуальный прогноз для вашего местоположения\n\n"
        "📅 **Прогноз на 3 дня**\nРасширенный прогноз с минимумом и максимумом температуры\n\n"
        "⏰ **Настроить уведомления**\nУстановите время для напоминаний о погоде\n• Доступны все 24 часа\n• Шаг 15 минут\n• Можно установить несколько напоминаний\n\n"
        "🕐 **Настроить часовой пояс**\nУстановите ваш часовой пояс для корректной работы уведомлений\n\n"
        "📋 **Мои уведомления**\nПросмотр установленных напоминаний\n\n"
        "🔄 **Сбросить данные**\nОчистка всех сохраненных данных\n\n"
        "🔔 **Тест уведомление**\nПроверить работу напоминаний\n\n"
        "❓ **Для работы бота:**\n1. Нажмите '📍 Отправить местоположение'\n2. Разрешите доступ к геолокации\n3. Настройте часовой пояс\n4. Настройте напоминания\n5. Получайте прогнозы автоматически!"
    )

    await update.message.reply_text(help_text, reply_markup=get_main_keyboard(), parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена диалога"""
    await update.message.reply_text("❌ Действие отменено", reply_markup=get_main_keyboard())
    return ConversationHandler.END


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик неизвестных сообщений"""
    await update.message.reply_text("❌ **Неизвестная команда**\n\nИспользуйте кнопки меню или /help для справки.", reply_markup=get_main_keyboard())


def get_main_keyboard():
    """Основная клавиатура"""
    location_button = KeyboardButton("📍 Отправить местоположение", request_location=True)
    keyboard = [
        [location_button],
        ["🌤️ Погода здесь", "📅 Прогноз на 3 дня"],
        ["⏰ Настроить уведомления", "📋 Мои уведомления"],
        ["🕐 Настроить часовой пояс", "🔄 Сбросить данные"],
        ["🔔 Тест уведомления", "ℹ️ Помощь"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_continue_keyboard():
    """Клавиатура для продолжения настройки"""
    keyboard = [["✅ Да, добавить еще", "❌ Нет, закончить"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def main():
    """Главная функция"""
    try:
        # Загружаем данные перед стартом
        load_user_data()

        # Создаем приложение
        application = Application.builder().token(BOT_TOKEN).build()

        # ConversationHandler для настройки уведомлений
        notification_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⏰ Настроить уведомления$"), setup_notifications)],
            states={
                SETTING_TIME: [
                    MessageHandler(filters.Regex("^(🕐|🕑|🕒|🕓|🕔|🕕)"), handle_time_range),
                    MessageHandler(filters.Regex("^(✅|❌)"), handle_continue_choice),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, save_notification_time),
                ]
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
        
        # ConversationHandler для настройки часового пояса
        timezone_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^🕐 Настроить часовой пояс$"), setup_timezone)],
            states={
                SETTING_TIMEZONE: [
                    MessageHandler(filters.Regex("^UTC"), handle_timezone_selection),
                ]
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        # Добавляем обработчики
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", show_help))
        application.add_handler(CommandHandler("cancel", cancel))

        # Обработчик геолокации
        application.add_handler(MessageHandler(filters.LOCATION, handle_location))

        # Обработчики кнопок
        application.add_handler(MessageHandler(filters.Regex("^🌤️ Погода здесь$"), weather_here))
        application.add_handler(MessageHandler(filters.Regex("^📅 Прогноз на 3 дня$"), three_day_forecast))
        application.add_handler(MessageHandler(filters.Regex("^📋 Мои уведомления$"), show_notifications))
        application.add_handler(MessageHandler(filters.Regex("^🔔 Тест уведомления$"), send_test_notification))
        application.add_handler(MessageHandler(filters.Regex("^🔄 Сбросить данные$"), reset_data))
        application.add_handler(MessageHandler(filters.Regex("^ℹ️ Помощь$"), show_help))
        application.add_handler(MessageHandler(filters.Regex("^❌ Удалить уведомления$"), delete_notifications))

        # Callback handler для inline удаления
        application.add_handler(CallbackQueryHandler(handle_delete_callback, pattern="^(del_|del_all|del_cancel)"))

        # ConversationHandler'ы
        application.add_handler(notification_conv)
        application.add_handler(timezone_conv)

        # Обработчик неизвестных команд
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

        # ✅ Запускаем проверку уведомлений как отдельную задачу после старта
        async def on_startup(app):
            app.create_task(check_and_send_notifications(app))

        application.post_init = on_startup

        print("🌤️ Бот погоды запущен и готов к работе!")
        print("📱 Используйте /start в Telegram для начала работы")
        print("⏰ Доступна настройка напоминаний на все 24 часа с шагом 15 минут")
        print("🕐 Добавлена поддержка часовых поясов")
        print("🔔 Система уведомлений активирована")
        print(f"💾 Данные хранятся в {DATA_FILE}")
        application.add_error_handler(lambda update, context: print("Ошибка:", context.error))
        application.run_polling()

    except Exception as e:
        print(f"❌ Ошибка при запуске бота: {e}")


if __name__ == "__main__":

    main()
