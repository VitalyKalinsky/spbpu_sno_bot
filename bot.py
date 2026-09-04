import asyncio
import logging
import re
import csv
import io
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, 
                           InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
                           BufferedInputFile)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import os
from dotenv import load_dotenv

# ================= Настройки =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANYA_ID = int(os.getenv("ANYA_ID", 0))  
MY_ID = int(os.getenv("MY_ID", 0))      

GITHUB_LINK = "https://github.com/your_repository"
CONTACTS_TEXT = "📞 Контакты:\nМаша - @MuwiWay\nАня - @smert_mestnikam"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "sno_bot.db")

# ================= База Данных =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY, username TEXT, role TEXT, 
            name TEXT, surname TEXT, group_num TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS lectures (
            id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, place TEXT, 
            time TIMESTAMP, lecturer_username TEXT, access_code TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS attendance (
            user_id INTEGER, lecture_id INTEGER, attended INTEGER,
            PRIMARY KEY (user_id, lecture_id))''')
        await db.execute('''CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, lecture_id INTEGER, text TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS answers (
            question_id INTEGER, user_id INTEGER, answer_text TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER, 
            lecture_id INTEGER, text TEXT, is_answered INTEGER DEFAULT 0)''')
        
        await db.execute("INSERT OR IGNORE INTO users (tg_id, role) VALUES (?, 'admin')", (MY_ID,))
        await db.execute("INSERT OR IGNORE INTO users (tg_id, role) VALUES (?, 'admin')", (ANYA_ID,))
        await db.commit()

# ================= FSM Состояния =================
class RegState(StatesGroup):
    name = State()
    surname = State()
    group = State()

class ProfileEdit(StatesGroup):
    waiting_for_name = State()
    waiting_for_group = State()

class LectureState(StatesGroup):
    topic = State()
    place = State()
    time = State()

class LectureEditState(StatesGroup):
    wait_value = State()

class LetuchkaManage(StatesGroup):
    lecture_id = State()
    wait_code = State()
    wait_add_q = State()
    wait_editall_q = State()

class TestState(StatesGroup):
    code = State()
    answering = State()

class MessageState(StatesGroup):
    text = State()
    text_specific = State()
    ask_lec_id = State()

class InboxState(StatesGroup):
    wait_reply = State()
    msg_id = State()

class AdminManage(StatesGroup):
    wait_for_add = State()
    wait_for_remove = State()

# ================= Клавиатуры =================
def get_main_menu(role: str) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="ℹ️ Информация")],
        [KeyboardButton(text="📝 Пройти летучку"), KeyboardButton(text="💬 Задать вопрос")]
    ]
    if role in ['admin', 'lecturer']:
        kb.append([KeyboardButton(text="📚 Управление лекциями"), KeyboardButton(text="⚙️ Управление летучками")])
        kb.append([KeyboardButton(text="📨 Входящие"), KeyboardButton(text="📊 Посещаемость")])
    if role == 'admin':
        kb.append([KeyboardButton(text="👑 Управление админами")])
    
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_back_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Возврат в главное меню")]],
        resize_keyboard=True
    )

def manage_lectures_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить лекцию", callback_data="mlec_add")],
        [InlineKeyboardButton(text="📋 Список лекций", callback_data="mlec_list")]
    ])

def specific_lecture_kb(lecture_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"mlece_{lecture_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"mlecd_{lecture_id}")
        ],
        [InlineKeyboardButton(text="🔙 К списку лекций", callback_data="mlec_list")]
    ])

def letuchka_menu_kb(lecture_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Установить код", callback_data=f"lm_code_{lecture_id}")],
        [InlineKeyboardButton(text="➕ Добавить вопрос(ы)", callback_data=f"lm_add_{lecture_id}")],
        [InlineKeyboardButton(text="📋 Список вопросов", callback_data=f"lm_list_{lecture_id}")],
        [InlineKeyboardButton(text="✏️ Редактировать вопросы", callback_data=f"lm_editall_{lecture_id}")],
        [InlineKeyboardButton(text="📄 Выгрузить ответы", callback_data=f"lm_export_{lecture_id}")],
        [InlineKeyboardButton(text="🔙 К списку летучек", callback_data="lmbck_main")]
    ])

# ================= Фильтры ролей =================
async def get_user_role(tg_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT role FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
            res = await cursor.fetchone()
            return res[0] if res else None

# ================= Helper: Отрисовка меню =================
async def send_letuchka_menu(message: Message, lec_id: int, success_text: str):
    role = await get_user_role(message.from_user.id)
    await message.answer(success_text, reply_markup=get_main_menu(role)) 
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT topic, access_code FROM lectures WHERE id = ?", (lec_id,)) as cur:
            lec = await cur.fetchone()
        async with db.execute("SELECT COUNT(id) FROM questions WHERE lecture_id = ?", (lec_id,)) as cur:
            q_count = (await cur.fetchone())[0]

    code_text = f"<code>{lec[1]}</code>" if lec[1] else "<i>Не установлен</i>"
    text = (f"⚙️ <b>Лекция:</b> {lec[0]}\n"
            f"🔑 <b>Код доступа:</b> {code_text}\n"
            f"📋 <b>Вопросов в базе:</b> {q_count}")
    
    await message.answer(text, reply_markup=letuchka_menu_kb(lec_id))

async def send_manage_lecture_menu(message: Message, lec_id: int, success_text: str):
    role = await get_user_role(message.from_user.id)
    await message.answer(success_text, reply_markup=get_main_menu(role)) 
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('''
            SELECT l.topic, l.place, l.time, l.lecturer_username, u.name, u.surname 
            FROM lectures l
            LEFT JOIN users u ON l.lecturer_username = u.username
            WHERE l.id = ?
        ''', (lec_id,)) as cur:
            lec = await cur.fetchone()
            
    if not lec:
        return
        
    lecturer_name = f"{lec[4]} {lec[5]} (@{lec[3]})" if lec[4] else f"@{lec[3]}"
    time_display = lec[2][:16] if lec[2] else "Не указано"
    text = (f"⚙️ <b>Управление лекцией:</b>\n"
            f"📌 {lec[0]}\n"
            f"📍 Место: {lec[1]}\n"
            f"⏰ Время: {time_display}\n"
            f"🎓 Лектор: {lecturer_name}")
            
    await message.answer(text, reply_markup=specific_lecture_kb(lec_id))

# ================= Возврат в меню =================
@router.message(F.text == "🔙 Возврат в главное меню")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    role = await get_user_role(message.from_user.id)
    await message.answer("Действие отменено. Выберите действие:", reply_markup=get_main_menu(role))

# ================= Регистрация =================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    role = await get_user_role(message.from_user.id)
    
    if not role and message.from_user.username and message.from_user.username.lower() == "foncream":
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO users (tg_id, username, role, name, surname, group_num) VALUES (?, ?, 'admin', 'FonCream', 'Admin', '0000000/00000')",
                (message.from_user.id, message.from_user.username)
            )
            await db.commit()
        role = 'admin'

    if role in ['admin', 'lecturer']:
        await message.answer(f"Добро пожаловать! Ваша роль: <b>{role}</b>.\nВыберите действие:", 
                             reply_markup=get_main_menu(role))
    elif role == 'student':
        await message.answer("С возвращением! Выберите действие:", 
                             reply_markup=get_main_menu(role))
    else:
        await message.answer("Привет! Ты новенький. Введи свое <b>Имя</b> текстом:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(RegState.name)

@router.message(RegState.name)
async def reg_name(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Пожалуйста, введите Имя текстом:")
    await state.update_data(name=message.text.strip())
    await message.answer("Введи свою <b>Фамилию</b>:")
    await state.set_state(RegState.surname)

@router.message(RegState.surname)
async def reg_surname(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Пожалуйста, введите Фамилию текстом:")
    await state.update_data(surname=message.text.strip())
    await message.answer("Введи номер группы (формат ХХХХХХХ/ХХХХХ):")
    await state.set_state(RegState.group)

@router.message(RegState.group)
async def reg_group(message: Message, state: FSMContext):
    if not message.text or not re.match(r"^\d{7}/\d{5}$", message.text):
        return await message.answer("Неверный формат! Пример: 1234567/12345. Попробуй еще раз текстом:")
    
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO users (tg_id, username, role, name, surname, group_num) VALUES (?, ?, 'student', ?, ?, ?)",
            (message.from_user.id, message.from_user.username, data['name'], data['surname'], message.text)
        )
        await db.commit()
    await message.answer("Регистрация завершена! Выберите действие ниже:", reply_markup=get_main_menu('student'))
    await state.clear()

# ================= Управление админами =================
@router.message(F.text == "👑 Управление админами")
async def manage_admins(message: Message):
    if await get_user_role(message.from_user.id) != 'admin':
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить админа", callback_data="admin_add")],
        [InlineKeyboardButton(text="Удалить админа", callback_data="admin_remove")]
    ])
    await message.answer("Управление администраторами:", reply_markup=kb)

@router.callback_query(F.data.startswith("admin_"))
async def admin_action(call: CallbackQuery, state: FSMContext):
    if await get_user_role(call.from_user.id) != 'admin':
        return
    await call.message.delete_reply_markup()
    if call.data == "admin_add":
        await call.message.answer("Введи @username пользователя для назначения админом:\n<i>(Пользователь должен быть зарегистрирован)</i>", reply_markup=get_back_menu())
        await state.set_state(AdminManage.wait_for_add)
    elif call.data == "admin_remove":
        await call.message.answer("Введи @username админа для разжалования в студенты:", reply_markup=get_back_menu())
        await state.set_state(AdminManage.wait_for_remove)
    await call.answer()

@router.message(AdminManage.wait_for_add)
async def process_add_admin(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Отправьте username текстом.")
    username = message.text.replace("@", "").strip()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("UPDATE users SET role = 'admin' WHERE username = ?", (username,))
        await db.commit()
        if cursor.rowcount > 0:
            await message.answer(f"Пользователь @{username} назначен админом!", reply_markup=get_main_menu('admin'))
        else:
            await message.answer(f"Пользователь @{username} не найден. Пусть сначала запустит бота.", reply_markup=get_main_menu('admin'))
    await state.clear()

@router.message(AdminManage.wait_for_remove)
async def process_remove_admin(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Отправьте username текстом.")
    username = message.text.replace("@", "").strip()
    if message.from_user.username and username.lower() == message.from_user.username.lower():
        return await message.answer("Вы не можете удалить самого себя!", reply_markup=get_main_menu('admin'))
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("UPDATE users SET role = 'student' WHERE username = ? AND role = 'admin'", (username,))
        await db.commit()
        if cursor.rowcount > 0:
            await message.answer(f"Администратор @{username} разжалован в студенты.", reply_markup=get_main_menu('admin'))
        else:
            await message.answer(f"Админ @{username} не найден в базе.", reply_markup=get_main_menu('admin'))
    await state.clear()

# ================= 0. Профиль =================
@router.message(F.text == "👤 Профиль")
async def cmd_me(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name, surname, group_num, role FROM users WHERE tg_id = ?", (message.from_user.id,)) as cur:
            user = await cur.fetchone()
    
    if not user:
        return await message.answer("Пользователь не найден. Нажмите /start")
    
    role_names = {
        'admin': 'Администратор 👑',
        'lecturer': 'Лектор 🎓',
        'student': 'Студент 📚'
    }
    display_role = role_names.get(user[3], user[3])
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить ФИО", callback_data="edit_name")],
        [InlineKeyboardButton(text="Изменить группу", callback_data="edit_group")]
    ])
    
    profile_text = (
        f"<b>Твой профиль:</b>\n"
        f"👤 Имя: {user[0]} {user[1]}\n"
        f"👥 Группа: {user[2]}\n"
        f"🏷 Роль: {display_role}"
    )
    
    await message.answer(profile_text, reply_markup=kb)

@router.callback_query(F.data.startswith("edit_"))
async def edit_profile(call: CallbackQuery, state: FSMContext):
    await call.message.delete_reply_markup()
    if call.data == "edit_name":
        await call.message.answer("Введи новые <b>Имя и Фамилию</b> через пробел:", reply_markup=get_back_menu())
        await state.set_state(ProfileEdit.waiting_for_name)
    elif call.data == "edit_group":
        await call.message.answer("Введи новый номер группы (ХХХХХХХ/ХХХХХ):", reply_markup=get_back_menu())
        await state.set_state(ProfileEdit.waiting_for_group)
    await call.answer()

@router.message(ProfileEdit.waiting_for_name)
async def update_name(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Пожалуйста, введи ФИО текстом.")
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("Пожалуйста, введи и имя, и фамилию через пробел.")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET name = ?, surname = ? WHERE tg_id = ?", (parts[0], parts[1], message.from_user.id))
        await db.commit()
    role = await get_user_role(message.from_user.id)
    await message.answer("ФИО успешно обновлено!", reply_markup=get_main_menu(role))
    await state.clear()

@router.message(ProfileEdit.waiting_for_group)
async def update_group(message: Message, state: FSMContext):
    if not message.text or not re.match(r"^\d{7}/\d{5}$", message.text):
        return await message.answer("Неверный формат. Пример: 1234567/12345")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET group_num = ? WHERE tg_id = ?", (message.text, message.from_user.id))
        await db.commit()
    role = await get_user_role(message.from_user.id)
    await message.answer("Группа успешно обновлена!", reply_markup=get_main_menu(role))
    await state.clear()

# ================= 1. Инфо =================
@router.message(F.text == "ℹ️ Информация")
async def cmd_info(message: Message):
    text = f"📂 Материалы: <a href='{GITHUB_LINK}'>GitHub</a>\n\n{CONTACTS_TEXT}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Список лекций", callback_data="info_upcoming")]
    ])
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)

@router.callback_query(F.data == "info_upcoming")
async def cq_info_upcoming(call: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, topic FROM lectures ORDER BY time ASC") as cur:
            lectures = await cur.fetchall()
            
    if not lectures:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="info_back")]])
        return await call.message.edit_text("Пока нет запланированных лекций.", reply_markup=kb)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=l[1], callback_data=f"info_lec_{l[0]}")] for l in lectures
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="info_back")]])
    
    await call.message.edit_text("🗓 <b>Выберите лекцию для подробностей:</b>", reply_markup=kb)

@router.callback_query(F.data == "info_back")
async def cq_info_back(call: CallbackQuery):
    text = f"📂 Материалы: <a href='{GITHUB_LINK}'>GitHub</a>\n\n{CONTACTS_TEXT}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Список лекций", callback_data="info_upcoming")]
    ])
    await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)

@router.callback_query(F.data.startswith("info_lec_"))
async def cq_info_lec(call: CallbackQuery):
    lec_id = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('''
            SELECT l.topic, l.place, l.time, l.lecturer_username, u.name, u.surname 
            FROM lectures l
            LEFT JOIN users u ON l.lecturer_username = u.username
            WHERE l.id = ?
        ''', (lec_id,)) as cur:
            lec = await cur.fetchone()
            
    if not lec:
        return await call.answer("Лекция не найдена.", show_alert=True)
    
    lecturer_name = f"{lec[4]} {lec[5]} (@{lec[3]})" if lec[4] else f"@{lec[3]}"
    time_display = lec[2][:16] if lec[2] else "Не указано"
    
    text = f"📌 <b>{lec[0]}</b>\n📍 Место: {lec[1]}\n⏰ Время: {time_display}\n🎓 Лектор: {lecturer_name}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Задать вопрос лектору", callback_data=f"info_ask_{lec_id}")],
        [InlineKeyboardButton(text="🔙 К списку лекций", callback_data="info_upcoming")]
    ])
    await call.message.edit_text(text, reply_markup=kb)

# ================= 2. Управление лекциями (Админ/Лектор) =================
@router.message(F.text == "📚 Управление лекциями")
async def cmd_manage_lectures(message: Message):
    if await get_user_role(message.from_user.id) not in ['admin', 'lecturer']:
        return
    await message.answer("Добавьте лекцию или выберите лекцию для редактирования из списка:", reply_markup=manage_lectures_kb())

@router.callback_query(F.data.startswith("mlec_"))
async def cq_manage_lectures(call: CallbackQuery, state: FSMContext):
    role = await get_user_role(call.from_user.id)
    if role not in ['admin', 'lecturer']:
        return
    action = call.data.replace("mlec_", "")
    
    if action == "add":
        await call.message.delete_reply_markup()
        await call.message.answer("Введите <b>тему лекции</b> текстом:", reply_markup=get_back_menu())
        await state.set_state(LectureState.topic)
        
    elif action == "list":
        async with aiosqlite.connect(DB_NAME) as db:
            if role == 'admin':
                cur = await db.execute("SELECT id, topic FROM lectures ORDER BY time ASC")
            else:
                cur = await db.execute("SELECT id, topic FROM lectures WHERE lecturer_username = ? ORDER BY time ASC", (call.from_user.username,))
            lectures = await cur.fetchall()
            
        if not lectures:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="mlec_back")]])
            return await call.message.edit_text("У вас нет доступных лекций.", reply_markup=kb)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=l[1], callback_data=f"mlecsel_{l[0]}")] for l in lectures
        ] + [[InlineKeyboardButton(text="🔙 Назад в меню", callback_data="mlec_back")]])
        
        await call.message.edit_text("📋 <b>Выберите лекцию для управления:</b>", reply_markup=kb)
        
    elif action == "back":
        await call.message.edit_text("Выберите действие с лекциями:", reply_markup=manage_lectures_kb())
    await call.answer()

@router.callback_query(F.data.startswith("mlecsel_"))
async def cq_select_lecture(call: CallbackQuery):
    lec_id = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('''
            SELECT l.topic, l.place, l.time, l.lecturer_username, u.name, u.surname 
            FROM lectures l
            LEFT JOIN users u ON l.lecturer_username = u.username
            WHERE l.id = ?
        ''', (lec_id,)) as cur:
            lec = await cur.fetchone()
    
    if not lec:
        return await call.answer("Лекция не найдена.", show_alert=True)
        
    lecturer_name = f"{lec[4]} {lec[5]} (@{lec[3]})" if lec[4] else f"@{lec[3]}"
    time_display = lec[2][:16] if lec[2] else "Не указано"
    
    text = (f"⚙️ <b>Управление лекцией:</b>\n"
            f"📌 {lec[0]}\n"
            f"📍 Место: {lec[1]}\n"
            f"⏰ Время: {time_display}\n"
            f"🎓 Лектор: {lecturer_name}")
            
    await call.message.edit_text(text, reply_markup=specific_lecture_kb(lec_id))
    await call.answer()

@router.callback_query(F.data.startswith("mlecd_"))
async def cq_delete_lecture(call: CallbackQuery):
    lec_id = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM lectures WHERE id = ?", (lec_id,))
        await db.execute("DELETE FROM questions WHERE lecture_id = ?", (lec_id,))
        await db.execute("DELETE FROM attendance WHERE lecture_id = ?", (lec_id,))
        await db.commit()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К списку лекций", callback_data="mlec_list")]
    ])
    await call.message.edit_text("✅ Лекция и все связанные данные успешно удалены.", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("mlece_"))
async def cq_edit_lecture(call: CallbackQuery, state: FSMContext):
    lec_id = int(call.data.split("_")[1])
    await state.update_data(edit_lec_id=lec_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Тему", callback_data="eledit_topic")],
        [InlineKeyboardButton(text="Место", callback_data="eledit_place")],
        [InlineKeyboardButton(text="Время", callback_data="eledit_time")],
        [InlineKeyboardButton(text="Лектора", callback_data="eledit_lecturer")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"mlecsel_{lec_id}")]
    ])
    await call.message.edit_text("Что вы хотите изменить?", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("eledit_"))
async def cq_edit_lecture_field(call: CallbackQuery, state: FSMContext):
    field = call.data.split("_")[1]
    await state.update_data(edit_lec_field=field)
    await call.message.delete_reply_markup()
    
    field_names = {
        "topic": "тему", 
        "place": "место", 
        "time": "дату и время (ГГГГ-ММ-ДД ЧЧ:ММ)",
        "lecturer": "username нового лектора (без символа @)"
    }
    
    await call.message.answer(f"Введите нов{('ую' if field in ['topic', 'time'] else 'ое')} <b>{field_names[field]}</b> лекции:", reply_markup=get_back_menu())
    await state.set_state(LectureEditState.wait_value)
    await call.answer()

@router.message(LectureEditState.wait_value)
async def save_edited_lecture(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Отправьте значение текстом.")
        
    data = await state.get_data()
    lec_id = data['edit_lec_id']
    field = data['edit_lec_field']
    val = message.text.strip()
    
    if field == 'time':
        try:
            dt = datetime.strptime(val, "%Y-%m-%d %H:%M")
            val = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return await message.answer("Неверный формат даты! Попробуйте снова (ГГГГ-ММ-ДД ЧЧ:ММ):")
    elif field == 'lecturer':
        val = val.replace("@", "")

    async with aiosqlite.connect(DB_NAME) as db:
        if field == 'topic':
            await db.execute("UPDATE lectures SET topic = ? WHERE id = ?", (val, lec_id))
        elif field == 'place':
            await db.execute("UPDATE lectures SET place = ? WHERE id = ?", (val, lec_id))
        elif field == 'time':
            await db.execute("UPDATE lectures SET time = ? WHERE id = ?", (val, lec_id))
        elif field == 'lecturer':
            await db.execute("UPDATE lectures SET lecturer_username = ? WHERE id = ?", (val, lec_id))
        await db.commit()
        
    await send_manage_lecture_menu(message, lec_id, "✅ Лекция успешно обновлена!")
    await state.clear()

@router.message(LectureState.topic)
async def lecture_topic(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Пожалуйста, введите тему текстом:")
    await state.update_data(topic=message.text.strip())
    await message.answer("Введите <b>место проведения</b>:")
    await state.set_state(LectureState.place)

@router.message(LectureState.place)
async def lecture_place(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Пожалуйста, введите место текстом:")
    await state.update_data(place=message.text.strip())
    await message.answer("Введите дату и время (ГГГГ-ММ-ДД ЧЧ:ММ):")
    await state.set_state(LectureState.time)

@router.message(LectureState.time)
async def lecture_time(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Пожалуйста, введите время текстом:")
    try:
        dt = datetime.strptime(message.text.strip(), "%Y-%m-%d %H:%M")
        data = await state.get_data()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO lectures (topic, place, time, lecturer_username) VALUES (?, ?, ?, ?)",
                (data['topic'], data['place'], dt.strftime("%Y-%m-%d %H:%M:%S"), message.from_user.username)
            )
            await db.commit()
        role = await get_user_role(message.from_user.id)
        await message.answer("Лекция успешно добавлена!", reply_markup=get_main_menu(role))
        await state.clear()
    except ValueError:
        await message.answer("Неверный формат даты! Попробуйте снова (ГГГГ-ММ-ДД ЧЧ:ММ):")

# ================= 3. УПРАВЛЕНИЕ ЛЕТУЧКАМИ (Лектор/Админ) =================
@router.message(F.text == "⚙️ Управление летучками")
async def cmd_manage_letuchka(message: Message):
    role = await get_user_role(message.from_user.id)
    if role not in ['admin', 'lecturer']:
        return
    
    async with aiosqlite.connect(DB_NAME) as db:
        if role == 'admin':
            async with db.execute("SELECT id, topic FROM lectures") as cur:
                lectures = await cur.fetchall()
        else:
            async with db.execute("SELECT id, topic FROM lectures WHERE lecturer_username = ?", (message.from_user.username,)) as cur:
                lectures = await cur.fetchall()
            
    if not lectures:
        return await message.answer("У вас нет доступных лекций.")
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=l[1], callback_data=f"sel_lec_{l[0]}")] for l in lectures
    ])
    await message.answer("Выберите лекцию для управления летучкой:", reply_markup=kb)

@router.callback_query(F.data == "lmbck_main")
async def cq_lmbck_main(call: CallbackQuery):
    role = await get_user_role(call.from_user.id)
    if role not in ['admin', 'lecturer']:
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        if role == 'admin':
            async with db.execute("SELECT id, topic FROM lectures") as cur:
                lectures = await cur.fetchall()
        else:
            async with db.execute("SELECT id, topic FROM lectures WHERE lecturer_username = ?", (call.from_user.username,)) as cur:
                lectures = await cur.fetchall()
                
    if not lectures:
        return await call.message.edit_text("У вас нет доступных лекций.")
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=l[1], callback_data=f"sel_lec_{l[0]}")] for l in lectures
    ])
    await call.message.edit_text("Выберите лекцию для управления летучкой:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("sel_lec_"))
async def open_letuchka_menu(call: CallbackQuery, state: FSMContext):
    lec_id = int(call.data.split("_")[2])
    await state.update_data(lecture_id=lec_id)
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT topic, access_code FROM lectures WHERE id = ?", (lec_id,)) as cur:
            lec = await cur.fetchone()
        async with db.execute("SELECT COUNT(id) FROM questions WHERE lecture_id = ?", (lec_id,)) as cur:
            q_count = (await cur.fetchone())[0]

    code_text = f"<code>{lec[1]}</code>" if lec[1] else "<i>Не установлен</i>"
    text = (f"⚙️ <b>Лекция:</b> {lec[0]}\n"
            f"🔑 <b>Код доступа:</b> {code_text}\n"
            f"📋 <b>Вопросов в базе:</b> {q_count}")
    
    await call.message.edit_text(text, reply_markup=letuchka_menu_kb(lec_id))
    await call.answer()

@router.callback_query(F.data.startswith("lm_"))
async def process_lm_action(call: CallbackQuery, state: FSMContext):
    action, lec_id = call.data.split("_")[1], int(call.data.split("_")[2])
    await state.update_data(lecture_id=lec_id)
    
    if action == "code":
        await call.message.delete_reply_markup()
        await call.message.answer("Отправьте <b>новый секретный код</b> для летучки:", reply_markup=get_back_menu())
        await state.set_state(LetuchkaManage.wait_code)
    elif action == "add":
        await call.message.delete_reply_markup()
        await call.message.answer(
            "Отправьте вопрос.\n\n"
            "<i>💡 Для массового добавления отправьте вопросы списком, начиная строки с цифр (1., 2.):</i>", 
            reply_markup=get_back_menu()
        )
        await state.set_state(LetuchkaManage.wait_add_q)
    elif action == "list":
        await call.message.delete_reply_markup()
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT id, text FROM questions WHERE lecture_id = ? ORDER BY id ASC", (lec_id,)) as cur:
                questions = await cur.fetchall()
        if not questions:
            text = "В этой летучке пока нет вопросов."
        else:
            text = "<b>Список вопросов:</b>\n\n"
            for idx, q in enumerate(questions, start=1):
                text += f"<b>{idx}.</b> {q[1]}\n"
                
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад к летучке", callback_data=f"sel_lec_{lec_id}")]])
        await call.message.edit_text(text, reply_markup=kb)
        await state.clear()
    elif action == "editall":
        await call.message.delete_reply_markup()
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT id, text FROM questions WHERE lecture_id = ? ORDER BY id ASC", (lec_id,)) as cur:
                questions = await cur.fetchall()
        if not questions:
            text_q = "Пока нет вопросов."
        else:
            text_q = "\n".join([f"{idx}. {q[1]}" for idx, q in enumerate(questions, start=1)])
            
        msg_text = (f"<b>Текущие вопросы:</b>\n\n{text_q}\n\n"
                    f"Отправьте <b>весь список вопросов заново</b> (каждый с новой строки с цифрой 1., 2. и т.д.).\n"
                    f"<i>Чтобы удалить все вопросы разом, отправьте цифру 0</i>")
        await call.message.answer(msg_text, reply_markup=get_back_menu())
        await state.set_state(LetuchkaManage.wait_editall_q)
    elif action == "export":
        await export_answers(call, lec_id)
    await call.answer()

async def export_answers(call: CallbackQuery, lecture_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('''
            SELECT u.name, u.surname, u.group_num, q.text, a.answer_text
            FROM answers a
            JOIN questions q ON a.question_id = q.id
            JOIN users u ON a.user_id = u.tg_id
            WHERE q.lecture_id = ?
            ORDER BY u.tg_id, q.id
        ''', (lecture_id,)) as cur:
            rows = await cur.fetchall()
            
    if not rows:
        return await call.message.answer("Ответов по этой лекции еще нет.")
        
    student_data = {}
    for r in rows:
        student_id = f"{r[0]} {r[1]}, {r[2]}"
        if student_id not in student_data:
            student_data[student_id] = []
        student_data[student_id].append(f"В: {r[3]}\nО: {r[4]}")
        
    output_text = ""
    for student, qas in student_data.items():
        output_text += f"Ученик: {student}\n\n"
        output_text += "\n\n".join(qas)
        output_text += "\n\n--------------------------------------------------\n\n"
        
    file_bytes = output_text.encode('utf-8')
    file = BufferedInputFile(file_bytes, filename=f"Ответы_Лекция_{lecture_id}.txt")
    await call.message.answer_document(file, caption="📄 Выгрузка ответов студентов.")

@router.message(LetuchkaManage.wait_code)
async def lm_save_code(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Код должен быть текстовым сообщением.")
    data = await state.get_data()
    lec_id = data['lecture_id']
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE lectures SET access_code = ? WHERE id = ?", (message.text.strip(), lec_id))
        await db.commit()
        
    await send_letuchka_menu(message, lec_id, "✅ Код доступа обновлен!")
    await state.clear()

@router.message(LetuchkaManage.wait_add_q)
async def lm_save_questions(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Отправьте вопросы текстом.")
    data = await state.get_data()
    lec_id = data['lecture_id']
    lines = message.text.split('\n')
    questions_to_add = []
    current_q = []

    for line in lines:
        if re.match(r'^\d+\.\s+', line):
            if current_q:
                questions_to_add.append('\n'.join(current_q).strip())
                current_q = []
            clean_line = re.sub(r'^\d+\.\s+', '', line)
            current_q.append(clean_line)
        else:
            current_q.append(line)
            
    if current_q:
        questions_to_add.append('\n'.join(current_q).strip())
        
    async with aiosqlite.connect(DB_NAME) as db:
        for q_text in questions_to_add:
            if q_text:
                await db.execute("INSERT INTO questions (lecture_id, text) VALUES (?, ?)", (lec_id, q_text))
        await db.commit()
        
    await send_letuchka_menu(message, lec_id, f"✅ Добавлено вопросов: {len(questions_to_add)}")
    await state.clear()

@router.message(LetuchkaManage.wait_editall_q)
async def lm_save_editall_q(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Отправьте список вопросов текстом.")
    data = await state.get_data()
    lec_id = data['lecture_id']
    
    async with aiosqlite.connect(DB_NAME) as db:
        # Удаляем старые вопросы
        await db.execute("DELETE FROM questions WHERE lecture_id = ?", (lec_id,))
        
        # Если юзер не отправил "0" (что означает просто удаление всех вопросов)
        if message.text.strip() != "0":
            lines = message.text.split('\n')
            questions_to_add = []
            current_q = []

            for line in lines:
                if re.match(r'^\d+\.\s+', line):
                    if current_q:
                        questions_to_add.append('\n'.join(current_q).strip())
                        current_q = []
                    clean_line = re.sub(r'^\d+\.\s+', '', line)
                    current_q.append(clean_line)
                else:
                    current_q.append(line)
                    
            if current_q:
                questions_to_add.append('\n'.join(current_q).strip())
                
            for q_text in questions_to_add:
                if q_text:
                    await db.execute("INSERT INTO questions (lecture_id, text) VALUES (?, ?)", (lec_id, q_text))
                    
        await db.commit()
        
    await send_letuchka_menu(message, lec_id, "✅ Список вопросов успешно обновлен!")
    await state.clear()

# ================= Таблица посещаемости =================
@router.message(F.text == "📊 Посещаемость")
async def cmd_attendance(message: Message):
    role = await get_user_role(message.from_user.id)
    if role not in ['admin', 'lecturer']:
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        if role == 'admin':
            async with db.execute("SELECT id, time, topic FROM lectures ORDER BY time ASC") as cur:
                lectures = await cur.fetchall()
        else:
            async with db.execute("SELECT id, time, topic FROM lectures WHERE lecturer_username = ? ORDER BY time ASC", (message.from_user.username,)) as cur:
                lectures = await cur.fetchall()

        if not lectures:
            return await message.answer("Нет проведенных или запланированных лекций для формирования статистики.")
            
        # Теперь выгружаем всех пользователей (Студентов, Лекторов и Админов)
        async with db.execute("SELECT tg_id, surname, name, group_num, role FROM users ORDER BY role, surname, name") as cur:
            users_list = await cur.fetchall()
            
        if not users_list:
            return await message.answer("В базе нет зарегистрированных пользователей.")

        async with db.execute("SELECT user_id, lecture_id, attended FROM attendance") as cur:
            attendance_data = await cur.fetchall()
            
    # Превращаем данные о посещаемости в удобный словарь для быстрого поиска
    att_map = {(row[0], row[1]): row[2] for row in attendance_data}
    
    # Формируем CSV в памяти
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', lineterminator='\n')
    
    # Собираем заголовки колонок
    headers = ["ФИО", "Группа", "Роль"]
    for l in lectures:
        date_str = l[1].split()[0] if l[1] else "Без даты"
        headers.append(f"{date_str} ({l[2]})")
    writer.writerow(headers)
    
    role_dict = {'admin': 'Админ', 'lecturer': 'Лектор', 'student': 'Студент'}
    
    # Собираем строки для всех пользователей
    for u in users_list:
        row = [f"{u[1]} {u[2]}", u[3], role_dict.get(u[4], u[4])]
        for l in lectures:
            status = att_map.get((u[0], l[0]), 0)
            row.append(str(status))
        writer.writerow(row)
        
    # Отправляем как файл (utf-8-sig решает проблему с кодировкой кириллицы в Excel)
    file_bytes = output.getvalue().encode('utf-8-sig')
    file = BufferedInputFile(file_bytes, filename="Таблица_Посещаемости.csv")
    await message.answer_document(file, caption="📊 Таблица посещаемости.\nОткройте файл в Excel (разделитель - точка с запятой).")

# ================= 5. Прохождение летучки (Все роли) =================
@router.message(F.text == "📝 Пройти летучку")
async def cmd_start_test(message: Message, state: FSMContext):
    await message.answer("Введи <b>секретный код</b> с доски/экрана:", reply_markup=get_back_menu())
    await state.set_state(TestState.code)

@router.message(TestState.code)
async def process_test_code(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Код должен быть текстом.")
        
    code = message.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id FROM lectures WHERE access_code = ?", (code,)) as cur:
            lec = await cur.fetchone()
            
        if not lec:
            return await message.answer("Неверный код. Попробуй еще раз:")
            
        lec_id = lec[0]
        
        async with db.execute('''
            SELECT 1 FROM answers a
            JOIN questions q ON a.question_id = q.id
            WHERE a.user_id = ? AND q.lecture_id = ? LIMIT 1
        ''', (message.from_user.id, lec_id)) as cur:
            if await cur.fetchone():
                role = await get_user_role(message.from_user.id)
                await message.answer("Вы уже успешно прошли и сдали эту летучку! Ответы больше не принимаются.", reply_markup=get_main_menu(role))
                return await state.clear()
        
        await db.execute("INSERT OR REPLACE INTO attendance (user_id, lecture_id, attended) VALUES (?, ?, 1)", 
                         (message.from_user.id, lec_id))
        
        async with db.execute("SELECT id, text FROM questions WHERE lecture_id = ? ORDER BY id ASC", (lec_id,)) as cur:
            questions = await cur.fetchall()
        await db.commit()
        
    role = await get_user_role(message.from_user.id)
    
    if not questions:
        await message.answer("Код принят, посещение отмечено! Вопросов к этой лекции нет.", reply_markup=get_main_menu(role))
        return await state.clear()
        
    await state.update_data(questions=questions, current_q=0, lec_id=lec_id)
    await message.answer(f"Посещение отмечено! Начинаем тест.\n\n<b>Вопрос 1:</b>\n{questions[0][1]}")
    await state.set_state(TestState.answering)

@router.message(TestState.answering)
async def process_test_answer(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Пожалуйста, отправь ответ текстом.")
        
    data = await state.get_data()
    questions = data['questions']
    curr_idx = data['current_q']
    q_id = questions[curr_idx][0]
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO answers (question_id, user_id, answer_text) VALUES (?, ?, ?)", 
                         (q_id, message.from_user.id, message.text.strip()))
        await db.commit()
        
    next_idx = curr_idx + 1
    if next_idx < len(questions):
        await state.update_data(current_q=next_idx)
        await message.answer(f"<b>Вопрос {next_idx + 1}:</b>\n{questions[next_idx][1]}")
    else:
        role = await get_user_role(message.from_user.id)
        await message.answer("Летучка завершена! Спасибо за ответы.", reply_markup=get_main_menu(role))
        await state.clear()

# ================= 4. Входящие и Обратная связь =================
@router.message(F.text == "💬 Задать вопрос")
async def cmd_message(message: Message, state: FSMContext):
    await message.answer("Напиши свой вопрос/сообщение организаторам:", reply_markup=get_back_menu())
    await state.set_state(MessageState.text)

@router.callback_query(F.data.startswith("info_ask_"))
async def cq_info_ask_lec(call: CallbackQuery, state: FSMContext):
    lec_id = int(call.data.split("_")[2])
    await state.update_data(ask_lec_id=lec_id)
    await call.message.answer("Напишите ваш вопрос конкретно по этой лекции:", reply_markup=get_back_menu())
    await state.set_state(MessageState.text_specific)
    await call.answer()

@router.message(MessageState.text)
async def process_message_general(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Отправьте сообщение текстом.")
        
    student_id = message.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO messages (sender_id, lecture_id, text) VALUES (?, NULL, ?)", (student_id, message.text.strip()))
        await db.commit()
        
    role = await get_user_role(message.from_user.id)
    await message.answer("Твое сообщение успешно отправлено!", reply_markup=get_main_menu(role))
    await alert_admins_and_lecturer(None)
    await state.clear()

@router.message(MessageState.text_specific)
async def process_message_specific(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Отправьте вопрос текстом.")
        
    data = await state.get_data()
    lec_id = data.get('ask_lec_id')
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO messages (sender_id, lecture_id, text) VALUES (?, ?, ?)", (message.from_user.id, lec_id, message.text.strip()))
        await db.commit()
        
    role = await get_user_role(message.from_user.id)
    await message.answer("Вопрос лектору успешно отправлен!", reply_markup=get_main_menu(role))
    await alert_admins_and_lecturer(lec_id)
    await state.clear()

async def alert_admins_and_lecturer(lecture_id):
    notify_list = [ANYA_ID, MY_ID]
    if lecture_id:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT u.tg_id FROM lectures l JOIN users u ON l.lecturer_username = u.username WHERE l.id = ?", (lecture_id,)) as cur:
                res = await cur.fetchone()
                if res and res[0] not in notify_list:
                    notify_list.append(res[0])
    
    for tg_id in notify_list:
        try:
            await bot.send_message(tg_id, "🔔 У вас новое непрочитанное сообщение в <b>📨 Входящие</b>!")
        except Exception:
            pass

# ================= ВХОДЯЩИЕ =================
@router.message(F.text == "📨 Входящие")
async def cmd_inbox(message: Message):
    role = await get_user_role(message.from_user.id)
    if role not in ['admin', 'lecturer']:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        if role == 'admin':
            query = '''
                SELECT m.id, u.name, u.group_num, l.topic 
                FROM messages m 
                JOIN users u ON m.sender_id = u.tg_id 
                LEFT JOIN lectures l ON m.lecture_id = l.id 
                WHERE m.is_answered = 0
            '''
            async with db.execute(query) as cur:
                messages = await cur.fetchall()
        else:
            query = '''
                SELECT m.id, u.name, u.group_num, l.topic 
                FROM messages m 
                JOIN users u ON m.sender_id = u.tg_id 
                JOIN lectures l ON m.lecture_id = l.id 
                WHERE m.is_answered = 0 AND l.lecturer_username = ?
            '''
            async with db.execute(query, (message.from_user.username,)) as cur:
                messages = await cur.fetchall()
                
    if not messages:
        return await message.answer("Входящих сообщений нет. Все прочитано!")
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"От: {m[1]} ({m[2]})", callback_data=f"inbox_read_{m[0]}")] for m in messages
    ])
    await message.answer("<b>Непрочитанные сообщения:</b>", reply_markup=kb)

@router.callback_query(F.data.startswith("inbox_read_"))
async def inbox_read(call: CallbackQuery, state: FSMContext):
    msg_id = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('''
            SELECT m.text, u.name, u.username, u.group_num, l.topic 
            FROM messages m 
            JOIN users u ON m.sender_id = u.tg_id 
            LEFT JOIN lectures l ON m.lecture_id = l.id 
            WHERE m.id = ?
        ''', (msg_id,)) as cur:
            res = await cur.fetchone()
            
    if not res:
        return await call.answer("Сообщение не найдено.", show_alert=True)
        
    topic = res[4] if res[4] else "Общий вопрос"
    username_str = f" (@{res[2]})" if res[2] else ""
    
    text = (f"👤 <b>От:</b> {res[1]}{username_str}\n"
            f"👥 <b>Группа:</b> {res[3]}\n"
            f"📌 <b>Контекст:</b> {topic}\n\n"
            f"📝 <b>Сообщение:</b>\n{res[0]}")
            
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Ответить", callback_data=f"inbox_reply_{msg_id}")],
        [InlineKeyboardButton(text="✅ Закрыть (прочитано)", callback_data=f"inbox_close_{msg_id}")]
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("inbox_close_"))
async def inbox_close(call: CallbackQuery):
    msg_id = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE messages SET is_answered = 1 WHERE id = ?", (msg_id,))
        await db.commit()
    await call.message.edit_text("✅ Сообщение помечено как прочитанное.")
    await call.answer()

@router.callback_query(F.data.startswith("inbox_reply_"))
async def inbox_reply(call: CallbackQuery, state: FSMContext):
    msg_id = int(call.data.split("_")[2])
    await state.update_data(msg_id=msg_id)
    await call.message.delete_reply_markup()
    await call.message.answer("Напишите ответ пользователю:", reply_markup=get_back_menu())
    await state.set_state(InboxState.wait_reply)
    await call.answer()

@router.message(InboxState.wait_reply)
async def inbox_send_reply(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("Отправьте ответ текстом.")
        
    data = await state.get_data()
    msg_id = data['msg_id']
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT sender_id, text FROM messages WHERE id = ?", (msg_id,)) as cur:
            res = await cur.fetchone()
            
    if not res:
        return await message.answer("Сообщение не найдено.")
        
    student_id, orig_text = res[0], res[1]
    
    reply_to_student = (f"🔔 <b>Вам ответили на вопрос:</b>\n<i>{orig_text}</i>\n\n"
                        f"✉️ <b>Ответ от организатора:</b>\n{message.text}")
    try:
        await bot.send_message(student_id, reply_to_student)
    except Exception:
        pass 
        
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE messages SET is_answered = 1 WHERE id = ?", (msg_id,))
        await db.commit()
        
    role = await get_user_role(message.from_user.id)
    await message.answer("✅ Ваш ответ успешно отправлен!", reply_markup=get_main_menu(role))
    await state.clear()

# ================= Оповещения за 3 и 1 день =================
async def notify_upcoming_lectures():
    today = datetime.now()
    target_3_days = (today + timedelta(days=3)).strftime("%Y-%m-%d")
    target_1_day = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('''
            SELECT l.topic, l.place, l.time, DATE(l.time), u.name, u.surname, l.lecturer_username 
            FROM lectures l
            LEFT JOIN users u ON l.lecturer_username = u.username
            WHERE DATE(l.time) = ? OR DATE(l.time) = ?
        ''', (target_3_days, target_1_day)) as cur:
            lectures = await cur.fetchall()
            
        if not lectures:
            return
            
        async with db.execute("SELECT tg_id FROM users") as cur:
            all_users = await cur.fetchall()
            
    for lec in lectures:
        topic, place, time_str, lec_date, name, surname, username = lec
        
        lecturer_name = f"{name} {surname} (@{username})" if name else f"@{username}"
        time_text = "Через 3 дня" if lec_date == target_3_days else "Завтра"
        time_display = time_str[:16] if time_str else "Не указано"
            
        msg = (f"🔔 <b>Напоминание!</b>\n{time_text} состоится лекция: <b>{topic}</b>\n"
               f"📍 Место: {place}\n⏰ Время: {time_display}\n🎓 Лектор: {lecturer_name}")
        
        for (user_id,) in all_users:
            try:
                await bot.send_message(user_id, msg)
            except Exception:
                pass

# ================= Запуск =================
async def main():
    await init_db()
    
    scheduler = AsyncIOScheduler(timezone='Europe/Moscow')
    scheduler.add_job(notify_upcoming_lectures, trigger='cron', hour=10, minute=0)
    scheduler.start()
    
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())