#!/usr/bin/env python3
"""
Telegram Weather Bot with Location and Notifications
Enhanced version with proper daily forecasts and professional structure
"""

import asyncio
import json
import logging
import os
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from enum import Enum

import requests
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)

# ========== CONFIGURATION ==========
BOT_TOKEN = "8036194666:AAEpr97NxUk9wrgj9tvi5StBgvUSRbwxlhk"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Conversation states
SETTING_TIME, CONFIRM_DELETE = range(2)

# Data paths
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "user_data.json")

# Cache settings
WEATHER_CACHE_TTL = timedelta(minutes=10)

# ========== DATA MODELS ==========
class WeatherCondition(Enum):
    """Weather conditions with codes from Open-Meteo API"""
    CLEAR_SKY = 0
    MAINLY_CLEAR = 1
    PARTLY_CLOUDY = 2
    OVERCAST = 3
    FOG = 45
    DEPOSITING_RIME_FOG = 48
    LIGHT_DRIZZLE = 51
    MODERATE_DRIZZLE = 53
    DENSE_DRIZZLE = 55
    LIGHT_RAIN = 61
    MODERATE_RAIN = 63
    HEAVY_RAIN = 65
    LIGHT_SNOW = 71
    MODERATE_SNOW = 73
    HEAVY_SNOW = 75
    SNOW_GRAINS = 77
    LIGHT_RAIN_SHOWERS = 80
    MODERATE_RAIN_SHOWERS = 81
    HEAVY_RAIN_SHOWERS = 82
    LIGHT_SNOW_SHOWERS = 85
    HEAVY_SNOW_SHOWERS = 86
    THUNDERSTORM = 95
    THUNDERSTORM_LIGHT_HAIL = 96
    THUNDERSTORM_HEAVY_HAIL = 99


@dataclass
class WeatherData:
    """Data class for weather information"""
    temperature: float
    condition_code: int
    condition_text: str
    icon: str
    wind_speed: float
    is_day: bool
    timestamp: datetime
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)


@dataclass
class DailyForecast:
    """Data class for daily forecast"""
    date: str
    max_temp: float
    min_temp: float
    condition_code: int
    condition_text: str
    icon: str
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)


@dataclass
class UserData:
    """Data class for user information"""
    user_id: int
    lat: Optional[float] = None
    lon: Optional[float] = None
    has_location: bool = False
    schedules: List[time] = None
    
    def __post_init__(self):
        if self.schedules is None:
            self.schedules = []
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "user_id": self.user_id,
            "lat": self.lat,
            "lon": self.lon,
            "has_location": self.has_location,
            "schedules": [t.strftime("%H:%M") for t in self.schedules]
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserData':
        """Create from dictionary"""
        schedules = []
        for t in data.get("schedules", []):
            try:
                h, m = map(int, t.split(":"))
                schedules.append(time(h, m))
            except (ValueError, AttributeError):
                continue
        
        return cls(
            user_id=data["user_id"],
            lat=data.get("lat"),
            lon=data.get("lon"),
            has_location=data.get("has_location", False),
            schedules=schedules
        )


# ========== GLOBAL STORAGE ==========
class DataStorage:
    """Centralized data storage with persistence"""
    
    def __init__(self):
        self.users: Dict[int, UserData] = {}
        self.weather_cache: Dict[str, Tuple[datetime, Dict]] = {}
        self.sent_notifications: Dict[str, bool] = {}
        self._load_data()
    
    def _load_data(self) -> None:
        """Load user data from JSON file"""
        try:
            if not os.path.exists(DATA_FILE):
                logging.info("No existing data file found. Starting fresh.")
                return
            
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            for user_id_str, user_dict in data.items():
                try:
                    user_dict["user_id"] = int(user_id_str)
                    user = UserData.from_dict(user_dict)
                    self.users[user.user_id] = user
                except (ValueError, KeyError) as e:
                    logging.warning(f"Failed to load user {user_id_str}: {e}")
            
            logging.info(f"Loaded {len(self.users)} users from storage.")
        except Exception as e:
            logging.error(f"Error loading data: {e}")
    
    def save_data(self) -> None:
        """Save user data to JSON file"""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            
            data_to_save = {}
            for user_id, user in self.users.items():
                data_to_save[str(user_id)] = user.to_dict()
            
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=2)
            
            logging.info(f"Saved data for {len(self.users)} users.")
        except Exception as e:
            logging.error(f"Error saving data: {e}")
    
    def get_user(self, user_id: int) -> UserData:
        """Get or create user data"""
        if user_id not in self.users:
            self.users[user_id] = UserData(user_id=user_id)
            self.save_data()
        return self.users[user_id]
    
    def update_user_location(self, user_id: int, lat: float, lon: float) -> None:
        """Update user location"""
        user = self.get_user(user_id)
        user.lat = lat
        user.lon = lon
        user.has_location = True
        self.save_data()
    
    def add_schedule(self, user_id: int, schedule_time: time) -> bool:
        """Add a schedule time for user"""
        user = self.get_user(user_id)
        if schedule_time in user.schedules:
            return False
        user.schedules.append(schedule_time)
        user.schedules.sort()
        self.save_data()
        return True
    
    def remove_schedule(self, user_id: int, schedule_time: time) -> bool:
        """Remove a schedule time for user"""
        user = self.get_user(user_id)
        if schedule_time in user.schedules:
            user.schedules.remove(schedule_time)
            self.save_data()
            return True
        return False
    
    def clear_schedules(self, user_id: int) -> int:
        """Clear all schedules for user"""
        user = self.get_user(user_id)
        count = len(user.schedules)
        user.schedules.clear()
        self.save_data()
        return count
    
    def reset_user(self, user_id: int) -> None:
        """Reset all user data"""
        self.users[user_id] = UserData(user_id=user_id)
        self.save_data()


# ========== WEATHER SERVICE ==========
class WeatherService:
    """Service for weather data retrieval and processing"""
    
    @staticmethod
    def validate_coordinates(lat: float, lon: float) -> bool:
        """Validate latitude and longitude"""
        return -90 <= lat <= 90 and -180 <= lon <= 180
    
    @staticmethod
    def get_weather_icon(condition_code: int, is_day: bool = True) -> str:
        """Get emoji icon for weather condition"""
        icon_map = {
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
        return icon_map.get(condition_code, "🌤️")
    
    @staticmethod
    def get_weather_description(condition_code: int) -> str:
        """Get text description for weather condition"""
        description_map = {
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
        return description_map.get(condition_code, "Неизвестно")
    
    @staticmethod
    def get_time_emoji(hour: int) -> str:
        """Get emoji for time of day"""
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
    
    @staticmethod
    def get_russian_day_name(date: datetime) -> str:
        """Get Russian name for day of week"""
        days = [
            "Понедельник", "Вторник", "Среда", 
            "Четверг", "Пятница", "Суббота", "Воскресенье"
        ]
        return days[date.weekday()]
    
    @classmethod
    async def get_weather_forecast(
        cls, 
        lat: float, 
        lon: float, 
        forecast_days: int = 3
    ) -> Tuple[Optional[WeatherData], Optional[List[DailyForecast]]]:
        """
        Get current weather and daily forecast from Open-Meteo API
        
        Returns:
            Tuple of (current_weather, daily_forecasts)
        """
        if not cls.validate_coordinates(lat, lon):
            return None, None
        
        try:
            params = {
                "latitude": lat,
                "longitude": lon,
                "current_weather": "true",
                "daily": "temperature_2m_max,temperature_2m_min,weathercode",
                "timezone": "auto",
                "forecast_days": forecast_days,
            }
            
            response = requests.get(OPEN_METEO_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Parse current weather
            current = data.get("current_weather", {})
            if not current:
                return None, None
            
            condition_code = current.get("weathercode", 0)
            is_day = current.get("is_day", 1) == 1
            
            current_weather = WeatherData(
                temperature=round(current.get("temperature", 0)),
                condition_code=condition_code,
                condition_text=cls.get_weather_description(condition_code),
                icon=cls.get_weather_icon(condition_code, is_day),
                wind_speed=current.get("windspeed", 0),
                is_day=is_day,
                timestamp=datetime.now()
            )
            
            # Parse daily forecasts
            daily_forecasts = []
            daily_data = data.get("daily", {})
            if daily_data:
                times = daily_data.get("time", [])
                max_temps = daily_data.get("temperature_2m_max", [])
                min_temps = daily_data.get("temperature_2m_min", [])
                weather_codes = daily_data.get("weathercode", [])
                
                for i in range(min(forecast_days, len(times))):
                    condition_code = weather_codes[i] if i < len(weather_codes) else 0
                    
                    forecast = DailyForecast(
                        date=times[i],
                        max_temp=round(max_temps[i]) if i < len(max_temps) else 0,
                        min_temp=round(min_temps[i]) if i < len(min_temps) else 0,
                        condition_code=condition_code,
                        condition_text=cls.get_weather_description(condition_code),
                        icon=cls.get_weather_icon(condition_code, is_day=True)
                    )
                    daily_forecasts.append(forecast)
            
            return current_weather, daily_forecasts
            
        except requests.exceptions.RequestException as e:
            logging.error(f"API request failed: {e}")
            return None, None
        except Exception as e:
            logging.error(f"Unexpected error in weather forecast: {e}")
            return None, None


# ========== BOT HANDLERS ==========
class BotHandlers:
    """Collection of bot handler methods"""
    
    def __init__(self, storage: DataStorage, weather_service: WeatherService):
        self.storage = storage
        self.weather_service = weather_service
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command"""
        user_id = update.effective_user.id
        user = self.storage.get_user(user_id)
        
        welcome_text = "🌤️ **Бот погоды с геолокацией**\n\n📍 **Ваш статус:** "
        
        if user.has_location:
            welcome_text += (
                f"Местоположение установлено ✅\n"
                f"Координаты: {user.lat:.4f}, {user.lon:.4f}\n\n"
                f"🔔 **Активных напоминаний:** {len(user.schedules)}"
            )
        else:
            welcome_text += (
                "Местоположение не установлено ❌\n\n"
                "Нажмите кнопку ниже чтобы отправить местоположение"
            )
        
        await update.message.reply_text(
            welcome_text,
            reply_markup=self._get_main_keyboard(),
            parse_mode="Markdown"
        )
    
    async def handle_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle location sharing"""
        user_id = update.effective_user.id
        location = update.message.location
        
        self.storage.update_user_location(
            user_id=user_id,
            lat=location.latitude,
            lon=location.longitude
        )
        
        await update.message.reply_text(
            "✅ **Местоположение сохранено!**\n\n"
            "Теперь вы можете получать точный прогноз погоды для вашего местоположения.",
            reply_markup=self._get_main_keyboard()
        )
    
    async def weather_current(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show current weather"""
        user_id = update.effective_user.id
        user = self.storage.get_user(user_id)
        
        if not user.has_location:
            await update.message.reply_text(
                "❌ **Сначала установите местоположение!**\n\n"
                "Нажмите кнопку '📍 Отправить местоположение' и разрешите доступ к геоданным.",
                reply_markup=self._get_main_keyboard()
            )
            return
        
        await update.message.reply_text("🌤️ Получаю актуальный прогноз...")
        
        current_weather, daily_forecasts = await self.weather_service.get_weather_forecast(
            lat=user.lat,
            lon=user.lon
        )
        
        if not current_weather:
            await update.message.reply_text(
                "❌ **Не удалось получить данные о погоде**\n\n"
                "Пожалуйста, попробуйте позже."
            )
            return
        
        # Current weather message
        temp_emoji = "❄️" if current_weather.temperature < 0 else "🌡️"
        message = (
            f"🌤️ **Погода в вашем местоположении:**\n\n"
            f"{temp_emoji} **Температура:** {current_weather.temperature}°C\n"
            f"{current_weather.icon} **Состояние:** {current_weather.condition_text}\n"
            f"💨 **Ветер:** {current_weather.wind_speed} м/с\n"
            f"📍 **Координаты:** {user.lat:.4f}, {user.lon:.4f}"
        )
        
        await update.message.reply_text(message, parse_mode="Markdown")
    
    async def weather_forecast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show 3-day forecast"""
        user_id = update.effective_user.id
        user = self.storage.get_user(user_id)
        
        if not user.has_location:
            await update.message.reply_text(
                "❌ **Сначала установите местоположение!**",
                reply_markup=self._get_main_keyboard()
            )
            return
        
        await update.message.reply_text("📅 Получаю расширенный прогноз...")
        
        current_weather, daily_forecasts = await self.weather_service.get_weather_forecast(
            lat=user.lat,
            lon=user.lon,
            forecast_days=3
        )
        
        if not daily_forecasts:
            await update.message.reply_text(
                "❌ **Не удалось получить прогноз на 3 дня**\n\n"
                "Пожалуйста, попробуйте позже."
            )
            return
        
        # Build forecast message
        message = "📅 **Прогноз на 3 дня:**\n\n"
        
        for forecast in daily_forecasts:
            date_obj = datetime.strptime(forecast.date, "%Y-%m-%d")
            day_name = self.weather_service.get_russian_day_name(date_obj)
            
            message += (
                f"**{day_name}** ({date_obj.strftime('%d.%m')})\n"
                f"{forecast.icon} **Погода:** {forecast.condition_text}\n"
                f"⬆️ **Макс:** {forecast.max_temp}°C\n"
                f"⬇️ **Мин:** {forecast.min_temp}°C\n\n"
            )
        
        await update.message.reply_text(message, parse_mode="Markdown")
    
    async def weather_tomorrow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show tomorrow's forecast"""
        user_id = update.effective_user.id
        user = self.storage.get_user(user_id)
        
        if not user.has_location:
            await update.message.reply_text(
                "❌ **Сначала установите местоположение!**",
                reply_markup=self._get_main_keyboard()
            )
            return
        
        await update.message.reply_text("📅 Получаю прогноз на завтра...")
        
        current_weather, daily_forecasts = await self.weather_service.get_weather_forecast(
            lat=user.lat,
            lon=user.lon,
            forecast_days=2  # Get today and tomorrow
        )
        
        if not daily_forecasts or len(daily_forecasts) < 2:
            await update.message.reply_text(
                "❌ **Не удалось получить прогноз на завтра**\n\n"
                "Пожалуйста, попробуйте позже."
            )
            return
        
        # Tomorrow is the second forecast (index 1)
        tomorrow = daily_forecasts[1]
        date_obj = datetime.strptime(tomorrow.date, "%Y-%m-%d")
        day_name = self.weather_service.get_russian_day_name(date_obj)
        
        # Get emoji for temperature
        temp_emoji = "❄️" if tomorrow.max_temp < 0 else "🌡️"
        
        message = (
            f"📅 **Прогноз на завтра ({date_obj.strftime('%d.%m')}):**\n\n"
            f"📌 **День недели:** {day_name}\n"
            f"{tomorrow.icon} **Погода:** {tomorrow.condition_text}\n"
            f"{temp_emoji} **Температура:** {tomorrow.min_temp}°C ... {tomorrow.max_temp}°C\n\n"
            f"📍 **Координаты:** {user.lat:.4f}, {user.lon:.4f}"
        )
        
        await update.message.reply_text(message, parse_mode="Markdown")
    
    async def setup_notifications(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Start notification setup"""
        user_id = update.effective_user.id
        user = self.storage.get_user(user_id)
        
        if not user.has_location:
            await update.message.reply_text(
                "❌ **Сначала установите местоположение!**",
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END
        
        # Store current hour ranges for keyboard
        context.user_data["hour_ranges"] = [
            ("🕐 00-03 часа", 0, 3),
            ("🕑 04-07 часов", 4, 7),
            ("🕒 08-11 часов", 8, 11),
            ("🕓 12-15 часов", 12, 15),
            ("🕔 16-19 часов", 16, 19),
            ("🕕 20-23 часа", 20, 23),
        ]
        
        keyboard = [[text] for text, _, _ in context.user_data["hour_ranges"]]
        keyboard.append(["🔙 Отмена"])
        
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            "⏰ **Настройка напоминаний**\n\n"
            "Выберите временной диапазон для настройки:",
            reply_markup=reply_markup
        )
        
        return SETTING_TIME
    
    async def handle_time_range(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle time range selection"""
        time_text = update.message.text
        
        if time_text == "🔙 Отмена":
            await update.message.reply_text(
                "❌ Настройка напоминаний отменена",
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END
        
        # Find selected hour range
        hour_ranges = context.user_data.get("hour_ranges", [])
        selected_range = None
        
        for text, start, end in hour_ranges:
            if time_text == text:
                selected_range = (start, end, text)
                break
        
        if not selected_range:
            await update.message.reply_text(
                "❌ Неверный диапазон",
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END
        
        start_hour, end_hour, range_text = selected_range
        context.user_data["selected_range"] = selected_range
        
        # Create keyboard with times for this range
        keyboard = []
        for hour in range(start_hour, end_hour + 1):
            row = []
            for minute in [0, 15, 30, 45]:
                time_str = f"{hour:02d}:{minute:02d}"
                emoji = self.weather_service.get_time_emoji(hour)
                row.append(f"{emoji} {time_str}")
            keyboard.append(row)
        
        keyboard.append(["🔙 Назад", "🔙 Отмена"])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            f"⏰ **Выберите время в диапазоне {range_text}:**\n\n"
            f"Доступные минуты: 00, 15, 30, 45",
            reply_markup=reply_markup
        )
        
        return SETTING_TIME
    
    async def save_notification_time(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Save selected notification time"""
        user_id = update.effective_user.id
        time_text = update.message.text
        
        if time_text == "🔙 Отмена":
            await update.message.reply_text(
                "❌ Настройка напоминаний отменена",
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END
        
        if time_text == "🔙 Назад":
            # Go back to hour range selection
            return await self.setup_notifications(update, context)
        
        try:
            # Extract time from text (format "🌙 02:15")
            parts = time_text.split()
            if len(parts) < 2:
                raise ValueError("Invalid time format")
            
            time_str = parts[1]
            hours, minutes = map(int, time_str.split(":"))
            
            if not (0 <= hours <= 23 and 0 <= minutes <= 59):
                raise ValueError("Invalid time")
            
            # Save the schedule
            schedule_time = time(hours, minutes)
            success = self.storage.add_schedule(user_id, schedule_time)
            
            if success:
                await update.message.reply_text(
                    f"✅ **Напоминание установлено на {time_str}!**\n\n"
                    f"Хотите установить еще одно напоминание?",
                    reply_markup=self._get_continue_keyboard()
                )
            else:
                await update.message.reply_text(
                    f"❌ Напоминание на {time_str} уже установлено!",
                    reply_markup=self._get_continue_keyboard()
                )
            
            return SETTING_TIME
            
        except (ValueError, IndexError) as e:
            await update.message.reply_text(
                "❌ **Неверный формат времени!**\n\n"
                "Пожалуйста, выберите время из предложенных вариантов.",
                reply_markup=self._get_main_keyboard()
            )
            return await self.setup_notifications(update, context)
    
    async def handle_continue_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle continue choice after saving time"""
        choice = update.message.text
        
        if choice == "✅ Да, добавить еще":
            return await self.setup_notifications(update, context)
        else:
            await update.message.reply_text(
                "✅ Настройка напоминаний завершена",
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END
    
    async def show_notifications(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show user's notifications"""
        user_id = update.effective_user.id
        user = self.storage.get_user(user_id)
        
        if not user.schedules:
            await update.message.reply_text(
                "📋 **У вас нет активных напоминаний**",
                reply_markup=self._get_main_keyboard()
            )
            return
        
        # Sort schedules by time
        user.schedules.sort()
        
        schedules_text = "⏰ **Ваши напоминания:**\n\n"
        for i, schedule_time in enumerate(user.schedules, 1):
            emoji = self.weather_service.get_time_emoji(schedule_time.hour)
            schedules_text += f"{i}. {emoji} {schedule_time.strftime('%H:%M')}\n"
        
        schedules_text += f"\n📊 **Всего: {len(user.schedules)} напоминаний**"
        schedules_text += "\n\nℹ️ *Для удаления напоминания используйте команду /delete*"
        
        await update.message.reply_text(schedules_text, parse_mode="Markdown")
    
    async def delete_notifications(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show delete notification interface"""
        user_id = update.effective_user.id
        user = self.storage.get_user(user_id)
        
        if not user.schedules:
            await update.message.reply_text(
                "📋 У вас нет напоминаний для удаления.",
                reply_markup=self._get_main_keyboard()
            )
            return
        
        user.schedules.sort()
        keyboard = []
        for i, t in enumerate(user.schedules):
            keyboard.append([
                InlineKeyboardButton(
                    f"Удалить {t.strftime('%H:%M')}",
                    callback_data=f"del_{i}"
                )
            ])
        
        keyboard.append([
            InlineKeyboardButton("🗑 Удалить все", callback_data="del_all"),
            InlineKeyboardButton("❌ Отмена", callback_data="del_cancel")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🕐 Выберите уведомление для удаления:",
            reply_markup=reply_markup
        )
    
    async def handle_delete_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle delete notification callbacks"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        user = self.storage.get_user(user_id)
        
        if query.data == "del_cancel":
            await query.edit_message_text("❌ Удаление отменено.")
            return
        
        if query.data == "del_all":
            count = self.storage.clear_schedules(user_id)
            await query.edit_message_text(f"✅ Удалено {count} напоминаний.")
            return
        
        if query.data.startswith("del_"):
            try:
                idx = int(query.data.split("_", 1)[1])
                if 0 <= idx < len(user.schedules):
                    removed_time = user.schedules.pop(idx)
                    self.storage.save_data()
                    await query.edit_message_text(
                        f"✅ Уведомление {removed_time.strftime('%H:%M')} удалено."
                    )
                else:
                    await query.edit_message_text("❌ Указанное напоминание не найдено.")
            except Exception as e:
                logging.error(f"Error handling delete callback: {e}")
                await query.edit_message_text("❌ Ошибка при удалении.")
    
    async def send_test_notification(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send test notification"""
        user_id = update.effective_user.id
        user = self.storage.get_user(user_id)
        
        if not user.has_location:
            await update.message.reply_text(
                "❌ Сначала установите местоположение!\n\n"
                "Нажмите 📍 'Отправить местоположение' и разрешите доступ к геоданным."
            )
            return
        
        await update.message.reply_text("🔔 Отправляю тестовое уведомление...")
        
        try:
            await self._send_weather_notification(context.application, user_id)
            await update.message.reply_text("✅ Тестовое уведомление отправлено!")
        except Exception as e:
            logging.error(f"Error sending test notification: {e}")
            await update.message.reply_text(f"❌ Ошибка при отправке уведомления")
    
    async def _send_weather_notification(self, application, user_id: int) -> None:
        """Send weather notification to user"""
        try:
            user = self.storage.get_user(user_id)
            if not user.has_location:
                return
            
            current_weather, daily_forecasts = await self.weather_service.get_weather_forecast(
                lat=user.lat,
                lon=user.lon
            )
            
            if not current_weather:
                return
            
            current_time = datetime.now().strftime("%H:%M")
            message = (
                f"🔔 **Напоминание о погоде** ({current_time})\n\n"
                f"🌤️ **Текущая погода:**\n"
                f"• 🌡️ Температура: {current_weather.temperature}°C\n"
                f"• 📝 {current_weather.condition_text} {current_weather.icon}\n"
                f"• 💨 Ветер: {current_weather.wind_speed} м/с\n\n"
            )
            
            # Add tomorrow's forecast if available
            if daily_forecasts and len(daily_forecasts) > 1:
                tomorrow = daily_forecasts[1]
                date_obj = datetime.strptime(tomorrow.date, "%Y-%m-%d")
                
                message += (
                    f"📅 **Прогноз на завтра ({date_obj.strftime('%d.%m')}):**\n"
                    f"• {tomorrow.icon} {tomorrow.condition_text}\n"
                    f"• ⬆️ {tomorrow.max_temp}°C ⬇️ {tomorrow.min_temp}°C\n\n"
                )
            
            message += "Хорошего дня! ☀️"
            
            await application.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logging.error(f"Error sending weather notification: {e}")
    
    async def reset_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reset user data"""
        user_id = update.effective_user.id
        self.storage.reset_user(user_id)
        
        await update.message.reply_text(
            "✅ **Все данные сброшены!**\n\n"
            "Теперь вы можете установить новое местоположение.",
            reply_markup=self._get_main_keyboard()
        )
    
    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show help information"""
        help_text = (
            "ℹ️ **Помощь по боту погоды**\n\n"
            "📍 **Отправить местоположение**\n"
            "Бот запомнит ваши координаты для показа погоды\n\n"
            "🌤️ **Погода здесь**\n"
            "Показывает актуальный прогноз для вашего местоположения\n\n"
            "📅 **Прогноз на 3 дня**\n"
            "Расширенный прогноз с минимумом и максимумом температуры\n\n"
            "📆 **Погода на завтра**\n"
            "Детальный прогноз на следующий день\n\n"
            "⏰ **Настроить уведомления**\n"
            "Установите время для напоминаний о погоде\n"
            "• Доступны все 24 часа\n"
            "• Шаг 15 минут\n"
            "• Можно установить несколько напоминаний\n\n"
            "📋 **Мои уведомления**\n"
            "Просмотр установленных напоминаний\n\n"
            "🗑️ **Удалить уведомления**\n"
            "Удаление выбранных или всех напоминаний\n\n"
            "🔔 **Тест уведомление**\n"
            "Проверить работу напоминаний\n\n"
            "🔄 **Сбросить данные**\n"
            "Очистка всех сохраненных данных\n\n"
            "❓ **Для работы бота:**\n"
            "1. Нажмите '📍 Отправить местоположение'\n"
            "2. Разрешите доступ к геолокации\n"
            "3. Настройте напоминания\n"
            "4. Получайте прогнозы автоматически!"
        )
        
        await update.message.reply_text(
            help_text,
            reply_markup=self._get_main_keyboard(),
            parse_mode="Markdown"
        )
    
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel conversation"""
        await update.message.reply_text(
            "❌ Действие отменено",
            reply_markup=self._get_main_keyboard()
        )
        return ConversationHandler.END
    
    async def handle_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle unknown messages"""
        await update.message.reply_text(
            "❌ **Неизвестная команда**\n\n"
            "Используйте кнопки меню или /help для справки.",
            reply_markup=self._get_main_keyboard()
        )
    
    # ========== KEYBOARD HELPERS ==========
    
    def _get_main_keyboard(self) -> ReplyKeyboardMarkup:
        """Get main keyboard"""
        location_button = KeyboardButton(
            "📍 Отправить местоположение",
            request_location=True
        )
        
        keyboard = [
            [location_button],
            ["🌤️ Погода здесь", "📅 Прогноз на 3 дня", "📆 Погода на завтра"],
            ["⏰ Настроить уведомления", "📋 Мои уведомления", "🗑️ Удалить уведомления"],
            ["🔔 Тест уведомления", "🔄 Сбросить данные", "ℹ️ Помощь"],
        ]
        
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def _get_continue_keyboard(self) -> ReplyKeyboardMarkup:
        """Get continue choice keyboard"""
        keyboard = [
            ["✅ Да, добавить еще", "❌ Нет, закончить"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ========== NOTIFICATION SERVICE ==========
class NotificationService:
    """Service for managing weather notifications"""
    
    def __init__(self, storage: DataStorage, weather_service: WeatherService):
        self.storage = storage
        self.weather_service = weather_service
    
    async def check_and_send_notifications(self, application) -> None:
        """Check and send scheduled notifications"""
        while True:
            try:
                now = datetime.utcnow()
                current_time = now.time().replace(second=0, microsecond=0)
                current_date = now.date()
                
                for user_id, user in list(self.storage.users.items()):
                    if not user.has_location:
                        continue
                    
                    # Check if any schedule matches current time
                    for schedule_time in user.schedules:
                        if current_time == schedule_time:
                            # Create notification key to prevent duplicates
                            notification_key = f"{user_id}_{current_date}_{schedule_time}"
                            
                            if notification_key not in self.storage.sent_notifications:
                                logging.info(
                                    f"Sending notification to user {user_id} "
                                    f"at {schedule_time}"
                                )
                                
                                # Send notification
                                await self._send_notification(application, user_id)
                                
                                # Mark as sent
                                self.storage.sent_notifications[notification_key] = True
                
                # Cleanup old sent notifications (older than 1 day)
                self._cleanup_old_notifications()
                
                # Wait for next minute
                await asyncio.sleep(60)
                
            except Exception as e:
                logging.error(f"Error in notification service: {e}")
                await asyncio.sleep(60)
    
    async def _send_notification(self, application, user_id: int) -> None:
        """Send notification to user"""
        try:
            user = self.storage.get_user(user_id)
            
            current_weather, daily_forecasts = await self.weather_service.get_weather_forecast(
                lat=user.lat,
                lon=user.lon
            )
            
            if not current_weather:
                return
            
            # Build notification message
            message = self._build_notification_message(
                current_weather,
                daily_forecasts
            )
            
            await application.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logging.error(f"Error sending notification: {e}")
    
    def _build_notification_message(self, 
                                  current_weather: WeatherData,
                                  daily_forecasts: List[DailyForecast]) -> str:
        """Build notification message"""
        current_time = datetime.now().strftime("%H:%M")
        
        message = (
            f"🔔 **Напоминание о погоде** ({current_time})\n\n"
            f"🌤️ **Текущая погода:**\n"
            f"• 🌡️ Температура: {current_weather.temperature}°C\n"
            f"• 📝 {current_weather.condition_text} {current_weather.icon}\n"
            f"• 💨 Ветер: {current_weather.wind_speed} м/с\n\n"
        )
        
        # Add tomorrow's forecast if available
        if daily_forecasts and len(daily_forecasts) > 1:
            tomorrow = daily_forecasts[1]
            date_obj = datetime.strptime(tomorrow.date, "%Y-%m-%d")
            
            message += (
                f"📅 **Прогноз на завтра ({date_obj.strftime('%d.%m')}):**\n"
                f"• {tomorrow.icon} {tomorrow.condition_text}\n"
                f"• ⬆️ Макс: {tomorrow.max_temp}°C\n"
                f"• ⬇️ Мин: {tomorrow.min_temp}°C\n\n"
            )
        
        message += "Хорошего дня! ☀️"
        
        return message
    
    def _cleanup_old_notifications(self) -> None:
        """Cleanup old sent notifications"""
        current_date = datetime.now().date()
        current_date_str = str(current_date)
        
        keys_to_remove = [
            key for key in self.storage.sent_notifications.keys()
            if current_date_str not in key
        ]
        
        for key in keys_to_remove:
            del self.storage.sent_notifications[key]


# ========== MAIN APPLICATION ==========
def main() -> None:
    """Main application entry point"""
    # Configure logging
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    logger = logging.getLogger(__name__)
    
    try:
        # Initialize services
        storage = DataStorage()
        weather_service = WeatherService()
        bot_handlers = BotHandlers(storage, weather_service)
        
        # Create application
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Create notification service
        notification_service = NotificationService(storage, weather_service)
        
        # ========== CONVERSATION HANDLERS ==========
        
        # Notification setup conversation
        notification_conv = ConversationHandler(
            entry_points=[
                MessageHandler(
                    filters.Regex("^⏰ Настроить уведомления$"),
                    bot_handlers.setup_notifications
                )
            ],
            states={
                SETTING_TIME: [
                    MessageHandler(
                        filters.Regex("^(🕐|🕑|🕒|🕓|🕔|🕕)"),
                        bot_handlers.handle_time_range
                    ),
                    MessageHandler(
                        filters.Regex("^(✅|❌)"),
                        bot_handlers.handle_continue_choice
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        bot_handlers.save_notification_time
                    ),
                ]
            },
            fallbacks=[CommandHandler("cancel", bot_handlers.cancel)],
        )
        
        # ========== COMMAND HANDLERS ==========
        
        # Basic commands
        application.add_handler(CommandHandler("start", bot_handlers.start))
        application.add_handler(CommandHandler("help", bot_handlers.show_help))
        application.add_handler(CommandHandler("cancel", bot_handlers.cancel))
        
        # Location handler
        application.add_handler(
            MessageHandler(filters.LOCATION, bot_handlers.handle_location)
        )
        
        # Weather handlers
        application.add_handler(
            MessageHandler(
                filters.Regex("^🌤️ Погода здесь$"),
                bot_handlers.weather_current
            )
        )
        application.add_handler(
            MessageHandler(
                filters.Regex("^📅 Прогноз на 3 дня$"),
                bot_handlers.weather_forecast
            )
        )
        application.add_handler(
            MessageHandler(
                filters.Regex("^📆 Погода на завтра$"),
                bot_handlers.weather_tomorrow
            )
        )
        
        # Notification handlers
        application.add_handler(
            MessageHandler(
                filters.Regex("^📋 Мои уведомления$"),
                bot_handlers.show_notifications
            )
        )
        application.add_handler(
            MessageHandler(
                filters.Regex("^🔔 Тест уведомления$"),
                bot_handlers.send_test_notification
            )
        )
        application.add_handler(
            MessageHandler(
                filters.Regex("^🗑️ Удалить уведомления$"),
                bot_handlers.delete_notifications
            )
        )
        
        # Data management
        application.add_handler(
            MessageHandler(
                filters.Regex("^🔄 Сбросить данные$"),
                bot_handlers.reset_data
            )
        )
        
        # Callback handlers
        application.add_handler(
            CallbackQueryHandler(
                bot_handlers.handle_delete_callback,
                pattern="^(del_|del_all|del_cancel)"
            )
        )
        
        # Conversation handlers
        application.add_handler(notification_conv)
        
        # Unknown message handler
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                bot_handlers.handle_unknown
            )
        )
        
        # ========== STARTUP ==========
        
        async def on_startup(app) -> None:
            """Startup tasks"""
            logger.info("Starting notification service...")
            app.create_task(notification_service.check_and_send_notifications(app))
        
        application.post_init = on_startup
        
        # ========== START POLLING ==========
        
        logger.info("🌤️ Weather Bot is starting...")
        logger.info(f"📁 Data storage: {DATA_FILE}")
        logger.info("✅ Bot is ready and listening for messages")
        
        application.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        raise


if __name__ == "__main__":
    main()
