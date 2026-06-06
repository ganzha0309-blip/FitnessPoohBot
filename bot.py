import asyncio
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

import firebase_admin
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
from firebase_admin import credentials, firestore

from config import ADMINS


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в файле .env")

firebase_key_json = os.getenv("FIREBASE_KEY")
if firebase_key_json:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(firebase_key_json)
        firebase_key_path = f.name
    print(f"Using temporary Firebase key file: {firebase_key_path}")
else:
    firebase_key_path = "fitnesspooh-firebase-key.json"
    print("Using local Firebase key file")

cred = credentials.Certificate(firebase_key_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

MSK = timezone(timedelta(hours=3))
DEFAULT_HABITS = [
    {"code": "water", "title": "Вода", "icon": "💧", "caption": "2л", "is_default": True},
    {"code": "workout", "title": "Тренировка", "icon": "🏋️", "caption": "активность", "is_default": True},
    {"code": "sleep", "title": "Сон", "icon": "😴", "caption": "7-8ч", "is_default": True},
]
CUSTOM_HABIT_LIMITS = {"free": 0, "base": 1, "pro": 2, "vip": 3}
SUBSCRIPTION_ORDER = {"free": 0, "base": 1, "pro": 2, "vip": 3}
SUBSCRIPTION_LABELS = {
    "free": "Free",
    "base": "Base",
    "pro": "PRO",
    "vip": "VIP",
}
LEVELS = {
    0: "🍯 Новобранец",
    100: "💪 Боец",
    200: "🚗 Машина",
    300: "🐻 Медведь",
    400: "🔥 Режим зверя",
    500: "👑 Легенда",
}


class BroadcastStates(StatesGroup):
    waiting_for_message = State()


class MessageToClientStates(StatesGroup):
    waiting_for_message = State()


class TrainerNoteStates(StatesGroup):
    waiting_for_note = State()


class HabitEditStates(StatesGroup):
    waiting_for_title = State()


class HabitAddStates(StatesGroup):
    waiting_for_title = State()


def today_iso() -> str:
    return datetime.now(MSK).date().isoformat()


def get_level(xp: int) -> str:
    for level_xp in sorted(LEVELS.keys(), reverse=True):
        if xp >= level_xp:
            return LEVELS[level_xp]
    return LEVELS[0]


def get_subscription(data: dict) -> tuple[str, str | None, bool]:
    subscription = data.get("subscription", "free")
    if subscription not in SUBSCRIPTION_ORDER:
        subscription = "free"

    until = data.get("subscription_until")
    if subscription == "free" or not until:
        return subscription, until, False

    try:
        is_expired = datetime.fromisoformat(until).date() < datetime.now(MSK).date()
    except ValueError:
        is_expired = True

    if is_expired:
        return "free", until, True
    return subscription, until, False


def format_subscription(data: dict) -> str:
    subscription, until, is_expired = get_subscription(data)
    label = SUBSCRIPTION_LABELS.get(subscription, "Free")
    if subscription == "free":
        if is_expired and until:
            return f"Free (прошлая подписка истекла {until})"
        return "Free"
    if not until:
        return f"{label} (без срока)"
    return f"{label} до {until}"


def can_access(user_sub: str, required_sub: str) -> bool:
    return SUBSCRIPTION_ORDER.get(user_sub, 0) >= SUBSCRIPTION_ORDER.get(required_sub, 0)


def parse_iso_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def get_habit_items(data: dict) -> list[dict]:
    settings = data.get("habit_settings", {}) or {}
    items = []
    for habit in DEFAULT_HABITS:
        custom = settings.get(habit["code"], {})
        items.append({**habit, "title": custom.get("title") or habit["title"]})

    subscription, _, _ = get_subscription(data)
    limit = CUSTOM_HABIT_LIMITS.get(subscription, 0)
    for habit in (data.get("custom_habits") or [])[:limit]:
        code = habit.get("code")
        title = (habit.get("title") or "").strip()
        if code and title:
            items.append(
                {
                    "code": code,
                    "title": title[:32],
                    "icon": habit.get("icon") or "✅",
                    "caption": "кастом",
                    "is_default": False,
                }
            )
    return items


def empty_habits_for(data: dict) -> dict[str, int]:
    return {habit["code"]: 0 for habit in get_habit_items(data)}


def custom_habit_limit(data: dict) -> int:
    subscription, _, _ = get_subscription(data)
    return CUSTOM_HABIT_LIMITS.get(subscription, 0)


def find_habit_title(data: dict, code: str) -> str:
    for habit in get_habit_items(data):
        if habit["code"] == code:
            return f"{habit['icon']} {habit['title']}"
    return code


async def ensure_user(message: types.Message) -> bool:
    user_id = str(message.from_user.id)
    user_ref = db.collection("users").document(user_id)
    if user_ref.get().exists:
        return False

    user_ref.set(
        {
            "name": message.from_user.first_name,
            "username": message.from_user.username or "",
            "xp": 0,
            "streak": 0,
            "last_action_date": None,
            "subscription": "free",
            "subscription_until": None,
            "habits": {"water": 0, "workout": 0, "sleep": 0},
            "habit_settings": {},
            "custom_habits": [],
            "created_at": today_iso(),
        }
    )
    return True


async def show_main_menu(target: types.CallbackQuery | types.Message, edit: bool = True):
    user_id = target.from_user.id
    keyboard_buttons = [
        [InlineKeyboardButton(text="✅ Отметить привычки", callback_data="habits")],
        [InlineKeyboardButton(text="📊 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🏆 Тренировки", callback_data="workouts")],
    ]
    if user_id in ADMINS:
        keyboard_buttons.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    text = "🍯 Главное меню\nВыбери действие:"

    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
        await target.answer()
    elif edit:
        try:
            await target.edit_text(text, reply_markup=keyboard)
        except Exception:
            await target.answer(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Список клиентов", callback_data="admin_clients:1")],
            [InlineKeyboardButton(text="📈 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="⏳ Подписки заканчиваются", callback_data="admin_expiring_subs")],
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🚪 Выход", callback_data="back")],
        ]
    )


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    is_new_user = await ensure_user(message)
    if is_new_user:
        welcome = (
            "🍯 Привет, я FitnessPooh!\n\n"
            "Я твой добродушный тренер. Давай начнем с малого: "
            "каждый день отмечай привычки и получай XP.\n"
            "Чем выше уровень, тем крепче режим 😉"
        )
        await message.answer(welcome)
    else:
        await message.answer("С возвращением, чемпион!")

    await show_main_menu(message, edit=False)


@dp.callback_query(lambda c: c.data == "profile")
async def show_profile(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    doc = db.collection("users").document(user_id).get()
    if not doc.exists:
        await callback.answer("Сначала напиши /start", show_alert=True)
        return

    data = doc.to_dict()
    xp = data.get("xp", 0)
    text = (
        "📊 Твой профиль\n\n"
        f"Имя: {data.get('name', 'Без имени')}\n"
        f"Уровень: {get_level(xp)}\n"
        f"Опыт (XP): {xp}\n"
        f"Серия дней: {data.get('streak', 0)} 🔥\n"
        f"Подписка: {format_subscription(data)}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back")]]
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "habits")
async def show_habits(callback: types.CallbackQuery):
    doc = db.collection("users").document(str(callback.from_user.id)).get()
    data = doc.to_dict() if doc.exists else {}
    buttons = []
    for habit in get_habit_items(data):
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{habit['icon']} {habit['title']} ({habit['caption']})",
                    callback_data=f"habit:{habit['code']}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="⚙️ Настроить привычки", callback_data="habit_settings")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back")])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=buttons
    )
    await callback.message.edit_text(
        "Что ты сделал сегодня? Нажимай на каждую привычку, +10 XP за каждую.",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("habit:") or c.data.startswith("habit_"))
async def mark_habit(callback: types.CallbackQuery):
    habit = callback.data.split(":")[1] if ":" in callback.data else callback.data.split("_")[1]
    user_id = str(callback.from_user.id)
    today = today_iso()
    user_ref = db.collection("users").document(user_id)
    doc = user_ref.get()
    if not doc.exists:
        await callback.answer("Сначала напиши /start", show_alert=True)
        return

    data = doc.to_dict()
    habit_codes = {item["code"] for item in get_habit_items(data)}
    if habit not in habit_codes:
        await callback.answer("Эта привычка недоступна для твоей подписки.", show_alert=True)
        return

    habits = data.get("habits", {})
    last_date = data.get("last_action_date")
    if last_date != today:
        habits = empty_habits_for(data)
        try:
            previous_date = datetime.fromisoformat(last_date).date() if last_date else None
        except ValueError:
            previous_date = None

        if previous_date and (datetime.now(MSK).date() - previous_date).days == 1:
            streak = data.get("streak", 0) + 1
        else:
            streak = 1
    else:
        streak = data.get("streak", 0)

    if habits.get(habit, 0) >= 1:
        await callback.answer(f"Ты уже отметил: {find_habit_title(data, habit)}", show_alert=True)
        return

    habits[habit] = 1
    old_xp = data.get("xp", 0)
    new_xp = old_xp + 10
    user_ref.update(
        {
            "xp": new_xp,
            "streak": streak,
            "last_action_date": today,
            "habits": habits,
        }
    )

    level_up_msg = ""
    if get_level(old_xp) != get_level(new_xp):
        level_up_msg = f"\n\n🎉 Поздравляю! Ты достиг уровня {get_level(new_xp)}!"

    await callback.answer(f"+10 XP! Серия: {streak} дней{level_up_msg}", show_alert=False)
    await show_habits(callback)


@dp.callback_query(lambda c: c.data == "habit_settings")
async def show_habit_settings(callback: types.CallbackQuery, state: FSMContext | None = None):
    if state:
        await state.clear()

    user_id = str(callback.from_user.id)
    doc = db.collection("users").document(user_id).get()
    if not doc.exists:
        await callback.answer("Сначала напиши /start", show_alert=True)
        return

    data = doc.to_dict()
    habit_items = get_habit_items(data)
    custom_habits = data.get("custom_habits") or []
    limit = custom_habit_limit(data)
    buttons = []
    for habit in habit_items:
        buttons.append(
            [InlineKeyboardButton(text=f"✏️ {habit['icon']} {habit['title']}", callback_data=f"edit_habit:{habit['code']}")]
        )
        if not habit.get("is_default"):
            buttons.append(
                [InlineKeyboardButton(text=f"🗑 Удалить {habit['title']}", callback_data=f"delete_habit:{habit['code']}")]
            )

    if len(custom_habits) < limit:
        buttons.append([InlineKeyboardButton(text="➕ Добавить привычку", callback_data="add_habit")])
    else:
        buttons.append([InlineKeyboardButton(text="🔒 Лимит привычек", callback_data="habit_limit_info")])

    buttons.append([InlineKeyboardButton(text="🔙 К привычкам", callback_data="habits")])
    subscription, _, _ = get_subscription(data)
    text = (
        "⚙️ Настройка привычек\n\n"
        f"Подписка: {SUBSCRIPTION_LABELS.get(subscription, 'Free')}\n"
        f"Дополнительные привычки: {len(custom_habits)}/{limit}\n\n"
        "Базовые привычки можно переименовывать на любой подписке. "
        "Добавление доступно: Base +1, PRO +2, VIP +3."
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(lambda c: c.data == "habit_limit_info")
async def habit_limit_info(callback: types.CallbackQuery):
    await callback.answer("Лимит зависит от подписки: Free 0, Base 1, PRO 2, VIP 3.", show_alert=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("edit_habit:"))
async def start_edit_habit(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":")[1]
    user_id = str(callback.from_user.id)
    doc = db.collection("users").document(user_id).get()
    if not doc.exists:
        await callback.answer("Сначала напиши /start", show_alert=True)
        return

    data = doc.to_dict()
    if code not in {habit["code"] for habit in get_habit_items(data)}:
        await callback.answer("Привычка не найдена.", show_alert=True)
        return

    await state.update_data(habit_code=code)
    await state.set_state(HabitEditStates.waiting_for_title)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="habit_settings")]]
    )
    await callback.message.edit_text(
        f"Напиши новое название для привычки:\n{find_habit_title(data, code)}",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.message(HabitEditStates.waiting_for_title)
async def save_habit_title(message: types.Message, state: FSMContext):
    title = (message.text or "").strip()[:32]
    if len(title) < 2:
        await message.answer("Название должно быть минимум 2 символа.")
        return

    state_data = await state.get_data()
    code = state_data.get("habit_code")
    user_id = str(message.from_user.id)
    user_ref = db.collection("users").document(user_id)
    doc = user_ref.get()
    if not doc.exists or not code:
        await message.answer("Не получилось сохранить. Открой настройки заново.")
        await state.clear()
        return

    data = doc.to_dict()
    default_codes = {habit["code"] for habit in DEFAULT_HABITS}
    if code in default_codes:
        user_ref.update({f"habit_settings.{code}.title": title})
    else:
        custom_habits = data.get("custom_habits") or []
        for habit in custom_habits:
            if habit.get("code") == code:
                habit["title"] = title
                break
        user_ref.update({"custom_habits": custom_habits})

    await state.clear()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки привычек", callback_data="habit_settings")]]
    )
    await message.answer("✅ Привычка обновлена.", reply_markup=keyboard)


@dp.callback_query(lambda c: c.data == "add_habit")
async def start_add_habit(callback: types.CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)
    doc = db.collection("users").document(user_id).get()
    if not doc.exists:
        await callback.answer("Сначала напиши /start", show_alert=True)
        return

    data = doc.to_dict()
    if len(data.get("custom_habits") or []) >= custom_habit_limit(data):
        await callback.answer("Лимит привычек для подписки достигнут.", show_alert=True)
        return

    await state.set_state(HabitAddStates.waiting_for_title)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="habit_settings")]]
    )
    await callback.message.edit_text("Напиши название новой привычки:", reply_markup=keyboard)
    await callback.answer()


@dp.message(HabitAddStates.waiting_for_title)
async def save_new_habit(message: types.Message, state: FSMContext):
    title = (message.text or "").strip()[:32]
    if len(title) < 2:
        await message.answer("Название должно быть минимум 2 символа.")
        return

    user_id = str(message.from_user.id)
    user_ref = db.collection("users").document(user_id)
    doc = user_ref.get()
    if not doc.exists:
        await message.answer("Сначала напиши /start")
        await state.clear()
        return

    data = doc.to_dict()
    custom_habits = data.get("custom_habits") or []
    if len(custom_habits) >= custom_habit_limit(data):
        await message.answer("Лимит привычек для подписки достигнут.")
        await state.clear()
        return

    custom_habits.append(
        {
            "code": f"custom_{int(datetime.now(MSK).timestamp())}_{len(custom_habits) + 1}",
            "title": title,
            "icon": "✅",
        }
    )
    user_ref.update({"custom_habits": custom_habits})
    await state.clear()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки привычек", callback_data="habit_settings")]]
    )
    await message.answer("✅ Привычка добавлена.", reply_markup=keyboard)


@dp.callback_query(lambda c: c.data and c.data.startswith("delete_habit:"))
async def delete_custom_habit(callback: types.CallbackQuery):
    code = callback.data.split(":")[1]
    user_id = str(callback.from_user.id)
    user_ref = db.collection("users").document(user_id)
    doc = user_ref.get()
    if not doc.exists:
        await callback.answer("Сначала напиши /start", show_alert=True)
        return

    data = doc.to_dict()
    if code in {habit["code"] for habit in DEFAULT_HABITS}:
        await callback.answer("Базовую привычку можно только переименовать.", show_alert=True)
        return

    custom_habits = [habit for habit in (data.get("custom_habits") or []) if habit.get("code") != code]
    user_ref.update({"custom_habits": custom_habits})
    await callback.answer("Привычка удалена.", show_alert=False)
    await show_habit_settings(callback)


@dp.callback_query(lambda c: c.data == "workouts")
async def show_workouts(callback: types.CallbackQuery):
    doc = db.collection("users").document(str(callback.from_user.id)).get()
    data = doc.to_dict() if doc.exists else {}
    user_sub, _, _ = get_subscription(data)

    workouts = [
        ("Утренняя зарядка", "free"),
        ("Разминка для спины", "free"),
        ("Интенсив на пресс", "base"),
        ("Массонабор: тренировка А", "pro"),
        ("Личная схема от Пуха", "vip"),
    ]
    lines = ["🏋️ Доступные тренировки:\n"]
    for index, (title, required_sub) in enumerate(workouts, start=1):
        label = SUBSCRIPTION_LABELS[required_sub]
        status = "✅" if can_access(user_sub, required_sub) else "🔒"
        lines.append(f"{index}. {status} {title} ({label})")

    lines.append("\nBase/PRO/VIP открывают больше тренировок.")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back")]]
    )
    await callback.message.edit_text("\n".join(lines), reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "back")
async def back_to_menu(callback: types.CallbackQuery):
    await show_main_menu(callback, edit=True)


@dp.message(Command("admin"))
async def admin_command(message: types.Message):
    if message.from_user.id not in ADMINS:
        await message.answer("⛔ У вас нет прав.")
        return
    await message.answer("⚙️ Админ-панель\nВыберите действие:", reply_markup=admin_panel_keyboard())


@dp.callback_query(lambda c: c.data == "admin_panel")
async def admin_panel_button(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    await callback.message.edit_text(
        "⚙️ Админ-панель\nВыберите действие:",
        reply_markup=admin_panel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    today = datetime.now(MSK).date()
    week_start = today - timedelta(days=6)
    docs = db.collection("users").stream()

    total = 0
    active_today = 0
    active_week = 0
    total_streak = 0
    total_xp = 0
    subscription_counts = {key: 0 for key in SUBSCRIPTION_ORDER}
    top_users = []
    inactive_users = []

    for doc in docs:
        data = doc.to_dict()
        total += 1
        xp = data.get("xp", 0)
        streak = data.get("streak", 0)
        total_xp += xp
        total_streak += streak

        subscription, _, _ = get_subscription(data)
        subscription_counts[subscription] = subscription_counts.get(subscription, 0) + 1

        last_action_date = parse_iso_date(data.get("last_action_date"))
        if last_action_date == today:
            active_today += 1
        if last_action_date and last_action_date >= week_start:
            active_week += 1
        if not last_action_date or (today - last_action_date).days >= 7:
            inactive_users.append((data.get("name", "Без имени"), doc.id, last_action_date))

        top_users.append((xp, data.get("name", "Без имени"), doc.id))

    average_streak = round(total_streak / total, 1) if total else 0
    average_xp = round(total_xp / total, 1) if total else 0
    today_rate = round(active_today / total * 100, 1) if total else 0
    week_rate = round(active_week / total * 100, 1) if total else 0
    top_users.sort(reverse=True, key=lambda item: item[0])
    inactive_users.sort(key=lambda item: item[2] or datetime.min.date())

    top_lines = []
    for place, (xp, name, user_id) in enumerate(top_users[:5], start=1):
        top_lines.append(f"{place}. {name} — {xp} XP")
    if not top_lines:
        top_lines.append("Пока нет клиентов")

    inactive_lines = []
    for name, user_id, last_action_date in inactive_users[:5]:
        last_seen = last_action_date.isoformat() if last_action_date else "никогда"
        inactive_lines.append(f"• {name} — {last_seen}")
    if not inactive_lines:
        inactive_lines.append("Нет клиентов без активности 7+ дней")

    sub_lines = [
        f"{SUBSCRIPTION_LABELS[sub]}: {subscription_counts.get(sub, 0)}"
        for sub in ("free", "base", "pro", "vip")
    ]

    text = (
        "📈 Статистика Fitness Pooh\n\n"
        f"Обновлено: {datetime.now(MSK).strftime('%H:%M:%S')}\n\n"
        f"👥 Клиентов всего: {total}\n"
        f"🟢 Активны сегодня: {active_today} ({today_rate}%)\n"
        f"📅 Активны за 7 дней: {active_week} ({week_rate}%)\n"
        f"🔥 Средний streak: {average_streak}\n"
        f"✨ Средний XP: {average_xp}\n\n"
        "💎 Подписки:\n"
        + "\n".join(sub_lines)
        + "\n\n🏆 Топ по XP:\n"
        + "\n".join(top_lines)
        + "\n\n😴 Без активности 7+ дней:\n"
        + "\n".join(inactive_lines)
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_panel")],
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
        await callback.answer("Данные уже актуальны.", show_alert=False)
        return
    await callback.answer()


@dp.callback_query(lambda c: c.data == "admin_expiring_subs")
async def admin_expiring_subscriptions(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    today = datetime.now(MSK).date()
    soon_limit = today + timedelta(days=7)
    expiring = []
    expired = []

    for doc in db.collection("users").stream():
        data = doc.to_dict()
        raw_sub = data.get("subscription", "free")
        if raw_sub == "free":
            continue

        until = parse_iso_date(data.get("subscription_until"))
        if not until:
            continue

        row = {
            "id": doc.id,
            "name": data.get("name", "Без имени"),
            "subscription": SUBSCRIPTION_LABELS.get(raw_sub, raw_sub),
            "until": until,
        }
        if until < today:
            expired.append(row)
        elif until <= soon_limit:
            expiring.append(row)

    expiring.sort(key=lambda item: item["until"])
    expired.sort(key=lambda item: item["until"])

    lines = ["⏳ Подписки заканчиваются\n"]
    if expiring:
        lines.append("В ближайшие 7 дней:")
        for item in expiring[:10]:
            days_left = (item["until"] - today).days
            lines.append(
                f"• {item['name']} — {item['subscription']} до {item['until'].isoformat()} ({days_left} дн.)"
            )
    else:
        lines.append("В ближайшие 7 дней никто не заканчивается.")

    if expired:
        lines.append("\nУже истекли:")
        for item in expired[:10]:
            days_ago = (today - item["until"]).days
            lines.append(
                f"• {item['name']} — {item['subscription']} истекла {item['until'].isoformat()} ({days_ago} дн. назад)"
            )

    keyboard_buttons = []
    for item in (expiring + expired)[:5]:
        keyboard_buttons.append(
            [InlineKeyboardButton(text=f"Открыть: {item['name']}", callback_data=f"client_details:{item['id']}")]
        )
    keyboard_buttons.extend(
        [
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_expiring_subs")],
            [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_panel")],
        ]
    )

    try:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
        await callback.answer("Данные уже актуальны.", show_alert=False)
        return
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("admin_clients"))
async def admin_list_clients(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    parts = callback.data.split(":")
    page = int(parts[1]) if len(parts) > 1 else 1
    limit = 5
    offset = (page - 1) * limit
    all_users = db.collection("users").get()
    total = len(all_users)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    docs = db.collection("users").limit(limit).offset(offset).stream()

    keyboard_buttons = []
    for doc in docs:
        user_data = doc.to_dict()
        name = user_data.get("name", "Без имени")
        user_id = doc.id
        username = user_data.get("username", "")
        display_username = f" (@{username})" if username else ""
        keyboard_buttons.append(
            [InlineKeyboardButton(text=f"{name}{display_username}", callback_data=f"client_details:{user_id}")]
        )
        keyboard_buttons.append(
            [
                InlineKeyboardButton(text="🔗 Открыть профиль", url=f"tg://user?id={user_id}"),
                InlineKeyboardButton(text="✏️ Написать", callback_data=f"msg_client:{user_id}"),
            ]
        )

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"admin_clients:{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"admin_clients:{page + 1}"))
    if nav_buttons:
        keyboard_buttons.append(nav_buttons)

    keyboard_buttons.append([InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_panel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    text = (
        f"📋 Список клиентов (стр. {page} из {total_pages})\n"
        f"Всего: {total}\n\n"
        "Нажмите на имя, чтобы увидеть прогресс и подписку."
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


async def render_client_details(callback: types.CallbackQuery, client_id: str):
    doc = db.collection("users").document(client_id).get()
    if not doc.exists:
        await callback.answer("Клиент не найден.", show_alert=True)
        return False

    data = doc.to_dict()
    xp = data.get("xp", 0)
    habits = data.get("habits", {}) if data.get("last_action_date") == today_iso() else {}
    habit_status = ", ".join(
        f"{habit['icon']} {'+' if habits.get(habit['code']) else '-'}" for habit in get_habit_items(data)
    )
    text = (
        "📊 Прогресс клиента\n\n"
        f"👤 {data.get('name', 'Без имени')}\n"
        f"ID: {client_id}\n"
        f"🏆 Уровень: {get_level(xp)}\n"
        f"✨ XP: {xp}\n"
        f"🔥 Серия дней: {data.get('streak', 0)}\n"
        f"💎 Подписка: {format_subscription(data)}\n"
        f"✅ Сегодня: {habit_status}\n"
        f"📅 Последняя активность: {data.get('last_action_date', 'никогда')}\n"
        f"📝 Заметка тренера: {data.get('trainer_note') or 'нет'}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Base 30д", callback_data=f"set_sub:{client_id}:base:30"),
                InlineKeyboardButton(text="PRO 30д", callback_data=f"set_sub:{client_id}:pro:30"),
                InlineKeyboardButton(text="VIP 30д", callback_data=f"set_sub:{client_id}:vip:30"),
            ],
            [
                InlineKeyboardButton(text="+7д", callback_data=f"extend_sub:{client_id}:7"),
                InlineKeyboardButton(text="+30д", callback_data=f"extend_sub:{client_id}:30"),
                InlineKeyboardButton(text="+90д", callback_data=f"extend_sub:{client_id}:90"),
            ],
            [
                InlineKeyboardButton(text="+10 XP", callback_data=f"adjust_xp:{client_id}:10"),
                InlineKeyboardButton(text="+50 XP", callback_data=f"adjust_xp:{client_id}:50"),
                InlineKeyboardButton(text="-10 XP", callback_data=f"adjust_xp:{client_id}:-10"),
            ],
            [
                InlineKeyboardButton(text="📝 Заметка", callback_data=f"trainer_note:{client_id}"),
                InlineKeyboardButton(text="🧹 Очистить заметку", callback_data=f"clear_note:{client_id}"),
            ],
            [
                InlineKeyboardButton(text="Снять подписку", callback_data=f"set_sub:{client_id}:free:0"),
                InlineKeyboardButton(text="✏️ Написать", callback_data=f"msg_client:{client_id}"),
            ],
            [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="admin_clients:1")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    return True


@dp.callback_query(lambda c: c.data and (c.data.startswith("client_details:") or c.data.startswith("client_details_")))
async def client_details(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    client_id = callback.data.split(":")[1] if ":" in callback.data else callback.data.split("_")[2]
    if await render_client_details(callback, client_id):
        await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("adjust_xp:"))
async def adjust_client_xp(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    _, client_id, delta_text = callback.data.split(":")
    delta = int(delta_text)
    user_ref = db.collection("users").document(client_id)
    doc = user_ref.get()
    if not doc.exists:
        await callback.answer("Клиент не найден.", show_alert=True)
        return

    data = doc.to_dict()
    old_xp = data.get("xp", 0)
    new_xp = max(0, old_xp + delta)
    user_ref.update({"xp": new_xp})

    level_message = ""
    if get_level(old_xp) != get_level(new_xp):
        level_message = f" Новый уровень: {get_level(new_xp)}"

    sign = "+" if delta > 0 else ""
    await callback.answer(f"{sign}{delta} XP. Теперь: {new_xp}.{level_message}", show_alert=False)
    await render_client_details(callback, client_id)


@dp.callback_query(lambda c: c.data and c.data.startswith("trainer_note:"))
async def start_trainer_note(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    _, client_id = callback.data.split(":")
    await state.update_data(client_id=client_id)
    await state.set_state(TrainerNoteStates.waiting_for_note)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_note:{client_id}")]]
    )
    await callback.message.edit_text(
        "📝 Напишите заметку о клиенте.\n\n"
        "Она будет видна только в админской карточке.",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(StateFilter(TrainerNoteStates.waiting_for_note), lambda c: c.data and c.data.startswith("cancel_note:"))
async def cancel_trainer_note(callback: types.CallbackQuery, state: FSMContext):
    _, client_id = callback.data.split(":")
    await state.clear()
    if await render_client_details(callback, client_id):
        await callback.answer("Заметка отменена.")


@dp.callback_query(lambda c: c.data and c.data.startswith("clear_note:"))
async def clear_trainer_note(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    _, client_id = callback.data.split(":")
    user_ref = db.collection("users").document(client_id)
    if not user_ref.get().exists:
        await callback.answer("Клиент не найден.", show_alert=True)
        return

    user_ref.update({"trainer_note": ""})
    await callback.answer("Заметка очищена.", show_alert=False)
    await render_client_details(callback, client_id)


@dp.message(TrainerNoteStates.waiting_for_note)
async def save_trainer_note(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMINS:
        return

    note = (message.text or message.caption or "").strip()
    if not note:
        await message.answer("Пришлите текст заметки.")
        return

    data = await state.get_data()
    client_id = data.get("client_id")
    if not client_id:
        await message.answer("Ошибка. Откройте карточку клиента заново.")
        await state.clear()
        return

    db.collection("users").document(client_id).update(
        {
            "trainer_note": note[:500],
            "trainer_note_updated_at": datetime.now(MSK).isoformat(timespec="seconds"),
        }
    )
    await state.clear()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 К клиенту", callback_data=f"client_details:{client_id}")]]
    )
    await message.answer("✅ Заметка сохранена.", reply_markup=keyboard)


@dp.callback_query(lambda c: c.data and (c.data.startswith("set_sub:") or c.data.startswith("change_sub_")))
async def change_subscription(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    if callback.data.startswith("set_sub:"):
        _, client_id, new_sub, days_text = callback.data.split(":")
        days = int(days_text)
    else:
        parts = callback.data.split("_")
        client_id = parts[2]
        new_sub = parts[3]
        days = 30 if new_sub != "free" else 0

    if new_sub not in SUBSCRIPTION_ORDER:
        await callback.answer("Неизвестная подписка.", show_alert=True)
        return

    update_data = {"subscription": new_sub}
    if new_sub == "free":
        update_data["subscription_until"] = None
    else:
        update_data["subscription_until"] = (datetime.now(MSK).date() + timedelta(days=days)).isoformat()

    db.collection("users").document(client_id).update(update_data)
    await callback.answer(f"Подписка изменена: {SUBSCRIPTION_LABELS[new_sub]}", show_alert=False)
    await render_client_details(callback, client_id)


@dp.callback_query(lambda c: c.data and c.data.startswith("extend_sub:"))
async def extend_subscription(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    _, client_id, days_text = callback.data.split(":")
    days = int(days_text)
    user_ref = db.collection("users").document(client_id)
    doc = user_ref.get()
    if not doc.exists:
        await callback.answer("Клиент не найден.", show_alert=True)
        return

    data = doc.to_dict()
    subscription, until, _ = get_subscription(data)
    if subscription == "free":
        await callback.answer("Сначала выдайте Base/PRO/VIP.", show_alert=True)
        return

    start_date = datetime.now(MSK).date()
    if until:
        try:
            start_date = max(start_date, datetime.fromisoformat(until).date())
        except ValueError:
            pass

    new_until = (start_date + timedelta(days=days)).isoformat()
    user_ref.update({"subscription": subscription, "subscription_until": new_until})
    await callback.answer(f"Продлено до {new_until}", show_alert=False)
    await render_client_details(callback, client_id)


@dp.callback_query(lambda c: c.data == "ignore")
async def ignore_callback(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(lambda c: c.data and (c.data.startswith("msg_client:") or c.data.startswith("msg_client_")))
async def start_message_to_client(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    client_id = callback.data.split(":")[1] if ":" in callback.data else callback.data.split("_")[2]
    await state.update_data(client_id=client_id)
    await state.set_state(MessageToClientStates.waiting_for_message)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_msg")]]
    )
    await callback.message.edit_text(
        f"✍️ Введите сообщение для клиента ID {client_id}.\n\n"
        "Можно отправить текст, фото или видео.",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(StateFilter(MessageToClientStates.waiting_for_message), lambda c: c.data == "cancel_msg")
async def cancel_message_to_client(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚙️ Админ-панель", reply_markup=admin_panel_keyboard())
    await callback.answer("Отправка отменена.")


@dp.message(MessageToClientStates.waiting_for_message)
async def send_message_to_client(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMINS:
        return

    data = await state.get_data()
    client_id = data.get("client_id")
    if not client_id:
        await message.answer("Ошибка. Попробуйте снова.")
        await state.clear()
        return

    try:
        await message.send_copy(chat_id=int(client_id))
        await message.answer(f"✅ Сообщение отправлено клиенту {client_id}.")
    except Exception as exc:
        await message.answer(
            f"❌ Ошибка: {exc}\n"
            "Возможно, клиент не начал диалог с ботом."
        )

    await state.clear()
    await message.answer("⚙️ Админ-панель", reply_markup=admin_panel_keyboard())


@dp.callback_query(lambda c: c.data == "admin_broadcast")
async def admin_start_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    await state.set_state(BroadcastStates.waiting_for_message)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast")]]
    )
    await callback.message.edit_text(
        "📢 Введите сообщение для рассылки.\n\n"
        "Можно отправить текст, фото или видео.",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(StateFilter(BroadcastStates.waiting_for_message), lambda c: c.data == "cancel_broadcast")
async def cancel_broadcast(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚙️ Админ-панель", reply_markup=admin_panel_keyboard())
    await callback.answer("Рассылка отменена.")


@dp.message(BroadcastStates.waiting_for_message)
async def admin_send_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMINS:
        return

    await bot.send_chat_action(message.chat.id, "typing")
    success = 0
    fail = 0
    status_msg = await message.answer("⏳ Идет рассылка...")

    for doc in db.collection("users").stream():
        try:
            await message.send_copy(chat_id=int(doc.id))
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1

    await state.clear()
    await status_msg.edit_text(
        f"✅ Рассылка завершена!\nОтправлено: {success}\nОшибок: {fail}"
    )
    await message.answer("⚙️ Админ-панель", reply_markup=admin_panel_keyboard())


async def main():
    print("Бот FitnessPooh запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
