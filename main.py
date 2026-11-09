import logging
logging.basicConfig(level=logging.INFO)

import asyncio
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv


load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не найден BOT_TOKEN в файле .env")


ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))


bot = Bot(token=TOKEN)
dp = Dispatcher()


DB_FILE = "bot.db"


# ---------- НАСТРОЙКИ РАСПИСАНИЯ ----------
WORK_START = 10   # первый слот 10:00
WORK_END = 20     # последний слот 19:00 (заканчивается в 20:00)
DAYS_AHEAD = 7    # на сколько дней вперёд можно записаться
REMIND_BEFORE_HOURS = 2   # за сколько часов до сеанса напоминать
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ---------- БАЗА ДАННЫХ ----------
@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                duration INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                client_name TEXT NOT NULL,
                service_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                slot TEXT NOT NULL,
                UNIQUE (day, slot)
            )
            """
        )
        try:
            conn.execute(
                "ALTER TABLE bookings ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        count = conn.execute("SELECT COUNT(*) FROM services").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO services (name, duration) VALUES (?, ?)",
                [
                    ("Классический массаж", 60),
                    ("Спина и шея", 30),
                    ("Расслабляющий массаж", 90),
                ],
            )
    print("База данных готова")


def db_get_services() -> list:
    with db() as conn:
        return conn.execute(
            "SELECT id, name, duration FROM services ORDER BY id"
        ).fetchall()


def db_get_service(service_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT id, name, duration FROM services WHERE id = ?", (service_id,)
        ).fetchone()


def db_get_busy_slots(day: str) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT slot FROM bookings WHERE day = ?", (day,)
        ).fetchall()
    return [r[0] for r in rows]


def db_add_booking(user_id: int, client_name: str, service_id: int, day: str, slot: str) -> bool:
    start = datetime.fromisoformat(f"{day} {slot}")
    # если до сеанса уже меньше 2 часов, напоминание не нужно
    reminded = 1 if start - datetime.now() <= timedelta(hours=REMIND_BEFORE_HOURS) else 0
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO bookings (user_id, client_name, service_id, day, slot, reminded) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, client_name, service_id, day, slot, reminded),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def db_get_bookings_for_day(day: str) -> list:
    with db() as conn:
        return conn.execute(
            "SELECT b.id, b.slot, b.client_name, b.user_id, s.name "
            "FROM bookings b JOIN services s ON s.id = b.service_id "
            "WHERE b.day = ? ORDER BY b.slot",
            (day,),
        ).fetchall()


def db_get_booking(booking_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT b.id, b.user_id, b.day, b.slot, b.client_name, s.name "
            "FROM bookings b JOIN services s ON s.id = b.service_id "
            "WHERE b.id = ?",
            (booking_id,),
        ).fetchone()


def db_delete_booking(booking_id: int):
    with db() as conn:
        conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))


def db_get_unreminded_bookings() -> list:
    with db() as conn:
        return conn.execute(
            "SELECT b.id, b.user_id, b.day, b.slot, s.name "
            "FROM bookings b JOIN services s ON s.id = b.service_id "
            "WHERE b.reminded = 0"
        ).fetchall()


def db_mark_reminded(booking_id: int):
    with db() as conn:
        conn.execute("UPDATE bookings SET reminded = 1 WHERE id = ?", (booking_id,))


# ---------- ВСПОМОГАТЕЛЬНОЕ ----------
def day_label(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"


def free_slots(day_str: str) -> list:
    busy = db_get_busy_slots(day_str)
    now = datetime.now()
    is_today = day_str == now.date().isoformat()
    result = []
    for hour in range(WORK_START, WORK_END):
        if is_today and hour <= now.hour:
            continue  # это время сегодня уже прошло
        slot = f"{hour:02d}:00"
        if slot not in busy:
            result.append(slot)
    return result


# ---------- КЛАВИАТУРЫ ----------
CANCEL_ROW = [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]


def kb_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Записаться", callback_data="book")]
    ])


def kb_services():
    rows = []
    for sid, name, duration in db_get_services():
        rows.append([InlineKeyboardButton(
            text=f"{name} ({duration} мин)", callback_data=f"srv:{sid}"
        )])
    rows.append(CANCEL_ROW)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_days():
    today = date.today()
    rows, row = [], []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        row.append(InlineKeyboardButton(text=day_label(d), callback_data=f"day:{d.isoformat()}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(CANCEL_ROW)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_slots(slots: list):
    rows, row = [], []
    for slot in slots:
        row.append(InlineKeyboardButton(text=slot, callback_data=f"slot:{slot}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Другой день", callback_data="back_days")])
    rows.append(CANCEL_ROW)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_confirm():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm")],
        CANCEL_ROW,
    ])


def kb_admin_days():
    today = date.today()
    rows, row = [], []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        row.append(InlineKeyboardButton(text=day_label(d), callback_data=f"adm_day:{d.isoformat()}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_bookings(bookings: list):
    rows = []
    for bid, slot, client_name, user_id, service_name in bookings:
        rows.append([InlineKeyboardButton(
            text=f"❌ {slot} {client_name}", callback_data=f"adm_del:{bid}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ К дням", callback_data="adm_days")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_confirm_delete(booking_id: int, day: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отменить", callback_data=f"adm_yes:{booking_id}")],
        [InlineKeyboardButton(text="⬅️ Нет", callback_data=f"adm_day:{day}")],
    ])


# ---------- СОСТОЯНИЯ (FSM) ----------
class Booking(StatesGroup):
    service = State()
    day = State()
    slot = State()
    confirm = State()


# ---------- ОБРАБОТЧИКИ ----------
@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Здравствуйте! Здесь можно записаться на массаж.",
        reply_markup=kb_start(),
    )


@dp.callback_query(F.data == "book")
async def book_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Booking.service)
    await callback.message.edit_text("Выберите услугу:", reply_markup=kb_services())
    await callback.answer()


@dp.callback_query(Booking.service, F.data.startswith("srv:"))
async def choose_service(callback: CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split(":")[1])
    await state.update_data(service_id=service_id)
    await state.set_state(Booking.day)
    await callback.message.edit_text("Выберите день:", reply_markup=kb_days())
    await callback.answer()


@dp.callback_query(Booking.day, F.data.startswith("day:"))
async def choose_day(callback: CallbackQuery, state: FSMContext):
    day_str = callback.data.split(":")[1]
    slots = free_slots(day_str)
    if not slots:
        await callback.answer("На этот день свободного времени нет, выберите другой.", show_alert=True)
        return
    await state.update_data(day=day_str)
    await state.set_state(Booking.slot)
    d = date.fromisoformat(day_str)
    await callback.message.edit_text(
        f"{day_label(d)}. Выберите время:", reply_markup=kb_slots(slots)
    )
    await callback.answer()


@dp.callback_query(Booking.slot, F.data == "back_days")
async def back_to_days(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Booking.day)
    await callback.message.edit_text("Выберите день:", reply_markup=kb_days())
    await callback.answer()


@dp.callback_query(Booking.slot, F.data.startswith("slot:"))
async def choose_slot(callback: CallbackQuery, state: FSMContext):
    slot = callback.data.split(":", 1)[1]   # "slot:14:00" -> "14:00"
    await state.update_data(slot=slot)
    data = await state.get_data()
    service = db_get_service(data["service_id"])
    d = date.fromisoformat(data["day"])
    await state.set_state(Booking.confirm)
    await callback.message.edit_text(
        "Проверьте запись:\n\n"
        f"Услуга: {service[1]} ({service[2]} мин)\n"
        f"День: {day_label(d)}\n"
        f"Время: {slot}",
        reply_markup=kb_confirm(),
    )
    await callback.answer()


@dp.callback_query(Booking.confirm, F.data == "confirm")
async def confirm_booking(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ok = db_add_booking(
        callback.from_user.id,
        callback.from_user.full_name,
        data["service_id"],
        data["day"],
        data["slot"],
    )
    if not ok:
        # кто-то успел занять это время раньше
        slots = free_slots(data["day"])
        if slots:
            await state.set_state(Booking.slot)
            await callback.message.edit_text(
                "Это время только что заняли. Выберите другое:",
                reply_markup=kb_slots(slots),
            )
        else:
            await state.set_state(Booking.day)
            await callback.message.edit_text(
                "На этот день мест больше нет. Выберите другой день:",
                reply_markup=kb_days(),
            )
        await callback.answer()
        return

    service = db_get_service(data["service_id"])
    d = date.fromisoformat(data["day"])
    await state.clear()
    await callback.message.edit_text(
        "✅ Вы записаны!\n\n"
        f"Услуга: {service[1]}\n"
        f"День: {day_label(d)}\n"
        f"Время: {data['slot']}",
        reply_markup=kb_start(),
    )
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                "🆕 Новая запись\n\n"
                f"Клиент: {callback.from_user.full_name}\n"
                f"Услуга: {service[1]}\n"
                f"День: {day_label(d)}\n"
                f"Время: {data['slot']}",
            )
        except Exception:
            pass
    await callback.answer("Запись создана")


@dp.callback_query(F.data == "cancel")
async def cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Запись отменена.", reply_markup=kb_start())
    await callback.answer()


# ---------- ЧАСТЬ ДЛЯ МАСТЕРА ----------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def admin_day_text(day_str: str, bookings: list) -> str:
    d = date.fromisoformat(day_str)
    if not bookings:
        return f"{day_label(d)}: записей нет."
    lines = [f"{day_label(d)}, записи:", ""]
    for bid, slot, client_name, user_id, service_name in bookings:
        lines.append(f"{slot} — {client_name}, {service_name}")
    lines.append("")
    lines.append("Чтобы отменить запись, нажмите на неё.")
    return "\n".join(lines)


@dp.message(Command("myid"))
async def my_id(message: types.Message):
    await message.answer(f"Ваш ID: {message.from_user.id}")


@dp.message(Command("admin"))
async def admin_panel(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для мастера.")
        return
    await state.clear()
    await message.answer("Записи. Выберите день:", reply_markup=kb_admin_days())


@dp.callback_query(F.data == "adm_days")
async def admin_days(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text("Записи. Выберите день:", reply_markup=kb_admin_days())
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_day:"))
async def admin_day(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    day_str = callback.data.split(":")[1]
    bookings = db_get_bookings_for_day(day_str)
    await callback.message.edit_text(
        admin_day_text(day_str, bookings), reply_markup=kb_admin_bookings(bookings)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_del:"))
async def admin_ask_delete(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    booking_id = int(callback.data.split(":")[1])
    booking = db_get_booking(booking_id)
    if not booking:
        await callback.answer("Эта запись уже удалена", show_alert=True)
        return
    _, user_id, day, slot, client_name, service_name = booking
    d = date.fromisoformat(day)
    await callback.message.edit_text(
        "Отменить запись?\n\n"
        f"{day_label(d)}, {slot}\n"
        f"Клиент: {client_name}\n"
        f"Услуга: {service_name}",
        reply_markup=kb_admin_confirm_delete(booking_id, day),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_yes:"))
async def admin_delete(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    booking_id = int(callback.data.split(":")[1])
    booking = db_get_booking(booking_id)
    if not booking:
        await callback.answer("Эта запись уже удалена", show_alert=True)
        return
    _, user_id, day, slot, client_name, service_name = booking
    db_delete_booking(booking_id)

    d = date.fromisoformat(day)
    try:
        await bot.send_message(
            user_id,
            f"К сожалению, ваша запись на {day_label(d)} в {slot} ({service_name}) "
            "отменена мастером. Чтобы записаться снова, нажмите /start",
        )
    except Exception:
        pass  # клиент мог заблокировать бота

    bookings = db_get_bookings_for_day(day)
    await callback.message.edit_text(
        "Запись отменена, клиент получил уведомление.\n\n" + admin_day_text(day, bookings),
        reply_markup=kb_admin_bookings(bookings),
    )
    await callback.answer("Отменено")


# Этот обработчик должен стоять ПОСЛЕДНИМ: ловит нажатия на старые кнопки
@dp.callback_query()
async def old_button(callback: CallbackQuery):
    await callback.answer("Эта кнопка устарела. Нажмите /start", show_alert=True)


# ---------- НАПОМИНАНИЯ ----------
async def send_reminders():
    now = datetime.now()
    for booking_id, user_id, day, slot, service_name in db_get_unreminded_bookings():
        start = datetime.fromisoformat(f"{day} {slot}")
        if start <= now:
            db_mark_reminded(booking_id)  # сеанс уже прошёл, напоминать поздно
            continue
        if start - now <= timedelta(hours=REMIND_BEFORE_HOURS):
            try:
                await bot.send_message(
                    user_id,
                    "⏰ Напоминание\n\n"
                    f"Сегодня в {slot} у вас {service_name}.\n"
                    "Ждём вас!",
                )
            except Exception:
                pass  # клиент мог заблокировать бота
            db_mark_reminded(booking_id)


async def reminder_loop():
    while True:
        try:
            await send_reminders()
        except Exception:
            logging.exception("Ошибка при отправке напоминаний")
        await asyncio.sleep(60)  # проверяем раз в минуту


async def main():
    init_db()
    reminder_task = asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


asyncio.run(main())