import asyncio
import logging
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import firebase_admin
from firebase_admin import credentials, firestore
import os
import tempfile
import json
from config import ADMINS

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в файле .env")

# Загрузка Firebase ключа
firebase_key_json = os.getenv("FIREBASE_KEY")
if firebase_key_json:
    # Создаем временный файл из содержимого переменной
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write(firebase_key_json)
        firebase_key_path = f.name
    print(f"Using temporary Firebase key file: {firebase_key_path}")
else:
    firebase_key_path = "fitnesspooh-firebase-key.json"
    print("Using local Firebase key file")

# Инициализация Firebase Admin SDK
cred = credentials.Certificate(firebase_key_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

LEVELS = {
    0: "🍯 Новобранец",
    100: "💪 Боец",
    200: "🚗 Машина",
    300: "🐻 Медведь",
    400: "🔥 Режим зверя",
    500: "👑 Легенда"
}

def get_level(xp):
    for lxp in sorted(LEVELS.keys(), reverse=True):
        if xp >= lxp:
            return LEVELS[lxp]
    return LEVELS[0]

class BroadcastStates(StatesGroup):
    waiting_for_message = State()

class MessageToClientStates(StatesGroup):
    waiting_for_message = State()

async def show_main_menu(target, edit=True):
    if isinstance(target, types.CallbackQuery):
        user_id = target.from_user.id
    else:
        user_id = target.from_user.id
    is_admin = user_id in ADMINS
    keyboard_buttons = [
        [InlineKeyboardButton(text="✅ Отметить привычки", callback_data="habits")],
        [InlineKeyboardButton(text="📊 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🏆 Тренировки", callback_data="workouts")],
    ]
    if is_admin:
        keyboard_buttons.append([InlineKeyboardButton(text="⚙️ Админ панель", callback_data="admin_panel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    text = "🍯 Главное меню\nВыбери действие:"
    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
        await target.answer()
    else:
        if edit:
            try:
                await target.edit_text(text, reply_markup=keyboard)
            except:
                await target.answer(text, reply_markup=keyboard)
        else:
            await target.answer(text, reply_markup=keyboard)

@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    user_id = str(message.from_user.id)
    user_ref = db.collection('users').document(user_id)
    doc = user_ref.get()
    if not doc.exists:
        user_username = message.from_user.username if message.from_user.username is not None else ""
        user_ref.set({
            'name': message.from_user.first_name,
            'username': user_username, # Сохраняем username
            'xp': 0,
            'streak': 0,
            'last_action_date': None,
            'subscription': 'free',
            'habits': {'water': 0, 'workout': 0, 'sleep': 0}
        })
        welcome = ("🍯 Привет, я FitnessPooh!\n\n"
                   "Я твой добродушный тренер. Давай начнём с малого:\n"
                   "Каждый день отмечай привычки и получай XP.\n"
                   "Чем выше уровень, тем круче ты становишься 😉")
        await message.answer(welcome)
        await show_main_menu(message, edit=False)
    else:
        await message.answer("С возвращением, чемпион!")
        await show_main_menu(message, edit=False)

@dp.callback_query(lambda c: c.data == "profile")
async def show_profile(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    doc = db.collection('users').document(user_id).get()
    if doc.exists:
        data = doc.to_dict()
        xp = data.get('xp', 0)
        level = get_level(xp)
        streak = data.get('streak', 0)
        sub = data.get('subscription', 'free')
        text = (f"📊 Твой профиль\n\n"
                f"Имя: {data['name']}\n"
                f"Уровень: {level}\n"
                f"Опыт (XP): {xp}\n"
                f"Серия дней: {streak} 🔥\n"
                f"Подписка: {sub}")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
        ])
        await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "habits")
async def show_habits(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💧 Вода (2л)", callback_data="habit_water")],
        [InlineKeyboardButton(text="🏋️ Тренировка", callback_data="habit_workout")],
        [InlineKeyboardButton(text="😴 Сон 7-8ч", callback_data="habit_sleep")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
    ])
    await callback.message.edit_text("Что ты сделал сегодня? Нажимай на каждую привычку, +10 XP за каждую.", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("habit_"))
async def mark_habit(callback: types.CallbackQuery):
    habit = callback.data.split("_")[1]
    user_id = str(callback.from_user.id)
    today = datetime.now(timezone.utc).date().isoformat()
    user_ref = db.collection('users').document(user_id)
    doc = user_ref.get()
    if not doc.exists:
        await callback.answer("Сначала напиши /start", show_alert=True)
        return
    data = doc.to_dict()
    habits = data.get('habits', {})
    last_date = data.get('last_action_date')
    if last_date != today:
        habits = {'water': 0, 'workout': 0, 'sleep': 0}
        if last_date and (datetime.now(timezone.utc).date() - datetime.fromisoformat(last_date).date()).days == 1:
            streak = data.get('streak', 0) + 1
        else:
            streak = 1
    else:
        streak = data.get('streak', 0)
    if habits.get(habit, 0) >= 1:
        await callback.answer(f"Ты уже отметил {habit} сегодня!", show_alert=True)
        return
    habits[habit] = 1
    new_xp = data.get('xp', 0) + 10
    user_ref.update({
        'xp': new_xp,
        'streak': streak,
        'last_action_date': today,
        'habits': habits
    })
    old_level = get_level(data.get('xp', 0))
    new_level = get_level(new_xp)
    level_up_msg = ""
    if old_level != new_level:
        level_up_msg = f"\n\n🎉 ПОЗДРАВЛЯЮ! Ты достиг уровня {new_level}! 🎉"
    await callback.answer(f"+10 XP! Серия: {streak} дней{level_up_msg}", show_alert=False)
    await show_habits(callback)

@dp.callback_query(lambda c: c.data == "workouts")
async def show_workouts(callback: types.CallbackQuery):
    text = "🏋️ Доступные тренировки:\n\n1. Утренняя зарядка (Free)\n2. Разминка для спины (Free)\n3. Интенсив на пресс (Base)\n\nПодписка Base откроет больше тренировок."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back")
async def back_to_menu(callback: types.CallbackQuery):
    await show_main_menu(callback, edit=True)

@dp.message(Command('admin'))
async def admin_command(message: types.Message):
    if message.from_user.id not in ADMINS:
        await message.answer("⛔ У вас нет прав.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список клиентов", callback_data="admin_clients:1")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🚪 Выход", callback_data="back")]
    ])
    await message.answer("⚙️ **Админ-панель**", reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == "admin_panel")
async def admin_panel_button(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список клиентов", callback_data="admin_clients:1")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🚪 Выход", callback_data="back")]
    ])
    await callback.message.edit_text("⚙️ **Админ-панель**\nВыберите действие:", reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("admin_clients"))
async def admin_list_clients(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    parts = callback.data.split(':')
    page = int(parts[1]) if len(parts) > 1 else 1
    limit = 5
    offset = (page - 1) * limit
    all_users = db.collection('users').get()
    total = len(all_users)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    users_ref = db.collection('users').limit(limit).offset(offset)
    docs = users_ref.stream()
    keyboard_buttons = []
    for doc in docs:
        user_data = doc.to_dict()
        name = user_data.get('name', 'Без имени')
        user_id = doc.id
        username = user_data.get('username', '')
        display_username = f" (@{username})" if username else ""
        # Имя клиента теперь кликабельно
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"{name}{display_username} (ID: {user_id})", callback_data=f"client_details_{user_id}")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="🔗 Открыть профиль", url=f"tg://user?id={user_id}"),
            InlineKeyboardButton(text="✏️ Написать", callback_data=f"msg_client_{user_id}")
        ])
        keyboard_buttons.append([InlineKeyboardButton(text="—"*25, callback_data="ignore")])
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"admin_clients:{page-1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"admin_clients:{page+1}"))
    if nav_buttons:
        keyboard_buttons.append(nav_buttons)
    keyboard_buttons.append([InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_panel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    text = f"📋 **Список клиентов** (стр. {page} из {total_pages})\nВсего: {total}\n\n_Нажмите на имя, чтобы увидеть прогресс_"
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()
    
@dp.callback_query(lambda c: c.data and c.data.startswith("client_details_"))
async def client_details(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    client_id = callback.data.split("_")[2]
    doc = db.collection('users').document(client_id).get()
    if not doc.exists:
        await callback.answer("Клиент не найден.", show_alert=True)
        return
    data = doc.to_dict()
    name = data.get('name', 'Без имени')
    xp = data.get('xp', 0)
    level = get_level(xp)
    streak = data.get('streak', 0)
    sub = data.get('subscription', 'free')
    last_active = data.get('last_action_date', 'никогда')
    
    text = (f"📊 **Прогресс клиента**\n\n"
            f"👤 {name}\n"
            f"🆔 ID: `{client_id}`\n"
            f"🏆 Уровень: {level}\n"
            f"✨ XP: {xp}\n"
            f"🔥 Серия дней: {streak}\n"
            f"💎 Подписка: {sub}\n"
            f"📅 Последняя активность: {last_active}")
    
    # Кнопки для изменения подписки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Free", callback_data=f"change_sub_{client_id}_free"),
         InlineKeyboardButton(text="Base", callback_data=f"change_sub_{client_id}_base"),
         InlineKeyboardButton(text="PRO", callback_data=f"change_sub_{client_id}_pro"),
         InlineKeyboardButton(text="VIP", callback_data=f"change_sub_{client_id}_vip")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data=f"admin_clients:1")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()
    
@dp.callback_query(lambda c: c.data and c.data.startswith("change_sub_"))
async def change_subscription(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    _, client_id, new_sub = callback.data.split("_")
    user_ref = db.collection('users').document(client_id)
    user_ref.update({'subscription': new_sub})
    await callback.answer(f"Подписка изменена на {new_sub.upper()}", show_alert=False)
    # Обновляем отображение карточки
    await client_details(callback)

@dp.callback_query(lambda c: c.data == "ignore")
async def ignore_callback(callback: types.CallbackQuery):
    await callback.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("msg_client_"))
async def start_message_to_client(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    client_id = callback.data.split("_")[2]
    await state.update_data(client_id=client_id)
    await state.set_state(MessageToClientStates.waiting_for_message)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_msg")]
    ])
    await callback.message.edit_text(
        f"✍️ Введите сообщение для клиента (ID: `{client_id}`):\n\n"
        "Можно отправить текст, фото или видео.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(StateFilter(MessageToClientStates.waiting_for_message), lambda c: c.data == "cancel_msg")
async def cancel_message_to_client(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await admin_list_clients(callback)
    await callback.answer("Отправка отменена.")

@dp.message(MessageToClientStates.waiting_for_message)
async def send_message_to_client(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMINS:
        return
    data = await state.get_data()
    client_id = data.get('client_id')
    if not client_id:
        await message.answer("Ошибка. Попробуйте снова.")
        await state.clear()
        return
    try:
        await message.send_copy(chat_id=int(client_id))
        await message.answer(f"✅ Сообщение отправлено клиенту {client_id}.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}\nВозможно, клиент не начал диалог с ботом.")
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список клиентов", callback_data="admin_clients:1")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🚪 Выход", callback_data="back")]
    ])
    await message.answer("⚙️ Админ-панель", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data == "admin_broadcast")
async def admin_start_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await state.set_state(BroadcastStates.waiting_for_message)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back")]
    ])
    await callback.message.edit_text("📢 **Введите текст сообщения для рассылки.**\n\nДля отмены нажмите кнопку ниже.", reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.message(BroadcastStates.waiting_for_message)
async def admin_send_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMINS:
        return
    await bot.send_chat_action(message.chat.id, "typing")
    users_ref = db.collection('users')
    docs = users_ref.stream()
    success = 0
    fail = 0
    status_msg = await message.answer("⏳ Идёт рассылка...")
    for doc in docs:
        try:
            await message.send_copy(chat_id=int(doc.id))
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            fail += 1
    await state.clear()
    await status_msg.edit_text(f"✅ **Рассылка завершена!**\nОтправлено: {success}\nОшибок: {fail}", parse_mode="Markdown")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список клиентов", callback_data="admin_clients:1")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🚪 Выход", callback_data="back")]
    ])
    await message.answer("⚙️ Админ-панель", reply_markup=keyboard)

async def main():
    print("Бот FitnessPooh запущен...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())