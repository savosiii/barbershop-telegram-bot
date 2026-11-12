import asyncio
import os
import sqlite3
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
bot = Bot(os.getenv("BOT_TOKEN"))
OWNER_ID = int(os.getenv("OWNER_ID"))
dp = Dispatcher()

SERVICES = {
    "cut": ("Стрижка", 1500),
    "beard": ("Борода", 800),
    "combo": ("Стрижка + борода", 2100),
}
TIMES = ["10:00", "12:00", "14:00", "16:00", "18:00"]

db = sqlite3.connect("bookings.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, name TEXT, service TEXT, date TEXT, time TEXT,
    reminded INTEGER DEFAULT 0)""")
# Если база была создана старой версией бота — дорисуем нужную колонку
cols = [r[1] for r in db.execute("PRAGMA table_info(bookings)")]
if "reminded" not in cols:
    db.execute("ALTER TABLE bookings ADD COLUMN reminded INTEGER DEFAULT 0")
db.commit()


def is_busy(d, t):
    row = db.execute("SELECT 1 FROM bookings WHERE date=? AND time=?", (d, t))
    return row.fetchone() is not None


class Booking(StatesGroup):
    service = State()
    date = State()
    time = State()


menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Записаться")],
        [KeyboardButton(text="Мои записи")],
        [KeyboardButton(text="Прайс"), KeyboardButton(text="Контакты")],
    ],
    resize_keyboard=True,
)


def fmt_date(d):
    """2026-11-15 -> 15.11.2026"""
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! 👋\nЗдесь можно записаться к барберу, посмотреть прайс "
        "или узнать, где нас найти. Выбирайте:", reply_markup=menu)


@dp.message(F.text.lower() == "прайс")
async def price(m: Message):
    lines = [f"• {t} — {p} ₽" for t, p in SERVICES.values()]
    await m.answer("Что делаем и за сколько:\n" + "\n".join(lines), reply_markup=menu)


@dp.message(F.text.lower() == "контакты")
async def contacts(m: Message):
    await m.answer("📍 ул. Примерная, 1\n📞 +7 900 000-00-00\n🕙 Ежедневно 10:00–20:00",
                   reply_markup=menu)


@dp.message(F.text.lower() == "записаться")
async def book_start(m: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    for key, (title, p) in SERVICES.items():
        kb.button(text=f"{title} — {p} ₽", callback_data=f"svc:{key}")
    kb.adjust(1)
    await state.set_state(Booking.service)
    await m.answer("Что записываем?", reply_markup=kb.as_markup())


@dp.callback_query(Booking.service, F.data.startswith("svc:"))
async def choose_service(c: CallbackQuery, state: FSMContext):
    await state.update_data(service=c.data.split(":")[1])
    kb = InlineKeyboardBuilder()
    for i in range(1, 8):
        d = date.today() + timedelta(days=i)
        kb.button(text=d.strftime("%d.%m"), callback_data=f"date:{d.isoformat()}")
    kb.adjust(4)
    await state.set_state(Booking.date)
    await c.message.edit_text("На какой день?", reply_markup=kb.as_markup())
    await c.answer()


@dp.callback_query(Booking.date, F.data.startswith("date:"))
async def choose_date(c: CallbackQuery, state: FSMContext):
    d = c.data.split(":")[1]
    await state.update_data(date=d)
    free = [t for t in TIMES if not is_busy(d, t)]
    if not free:
        await c.answer("Этот день полностью забит — посмотрите другой",
                       show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for t in free:
        kb.button(text=t, callback_data=f"time:{t}")
    kb.adjust(3)
    await state.set_state(Booking.time)
    await c.message.edit_text("Время свободное:", reply_markup=kb.as_markup())
    await c.answer()


@dp.callback_query(Booking.time, F.data.startswith("time:"))
async def choose_time(c: CallbackQuery, state: FSMContext):
    t = c.data.split(":", 1)[1]
    data = await state.get_data()
    if is_busy(data["date"], t):
        await c.answer("Упс, это время только что забили. Другое возьмёте?",
                       show_alert=True)
        return
    title, p = SERVICES[data["service"]]
    name = c.from_user.full_name
    db.execute("INSERT INTO bookings (user_id, name, service, date, time) VALUES (?,?,?,?,?)",
               (c.from_user.id, name, title, data["date"], t))
    db.commit()
    await state.clear()
    await c.message.edit_text(
        f"Готово, вы записаны! ✅\n{title}, {fmt_date(data['date'])} в {t}.\n"
        f"Если планы поменяются — запись можно отменить в «Моих записях».")
    # отдельным сообщением вернём постоянную клавиатуру —
    # она не цепляется к edit_text, а клиенты Telegram её теряют после перезапуска
    await c.message.answer("Кнопки меню на месте 👇", reply_markup=menu)
    await bot.send_message(
        OWNER_ID,
        f"🔔 Новая запись: {name}\n{title}, {fmt_date(data['date'])} в {t}")
    await c.answer()


@dp.message(F.text.lower() == "мои записи")
async def my_bookings(m: Message):
    rows = db.execute(
        "SELECT id, service, date, time FROM bookings "
        "WHERE user_id=? AND date>=? ORDER BY date, time",
        (m.from_user.id, date.today().isoformat())).fetchall()
    if not rows:
        await m.answer("Пока записей нет. Хотите записаться — жмите «Записаться».",
                       reply_markup=menu)
        return
    kb = InlineKeyboardBuilder()
    for bid, svc, d, t in rows:
        kb.button(text=f"❌ Отменить: {svc}, {fmt_date(d)} {t}",
                  callback_data=f"cancel:{bid}")
    kb.adjust(1)
    await m.answer("Ваши записи — отменить можно кнопкой ниже:",
                   reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_booking(c: CallbackQuery):
    bid = int(c.data.split(":")[1])
    row = db.execute("SELECT user_id, service, date, time FROM bookings WHERE id=?",
                     (bid,)).fetchone()
    if not row or row[0] != c.from_user.id:
        await c.answer("Не нашёл такую запись", show_alert=True)
        return
    db.execute("DELETE FROM bookings WHERE id=?", (bid,))
    db.commit()
    await c.message.edit_text("Запись отменена. Если передумаете — мы тут.")
    await c.message.answer("Кнопки меню на месте 👇", reply_markup=menu)
    await bot.send_message(
        OWNER_ID,
        f"🚫 Клиент отменил запись\n{row[1]}, {fmt_date(row[2])} в {row[3]}")
    await c.answer()


# ---------- Команды для владельца ----------

@dp.message(Command("today"))
async def today_schedule(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    rows = db.execute(
        "SELECT name, service, date, time FROM bookings WHERE date=? ORDER BY time",
        (date.today().isoformat(),)).fetchall()
    if not rows:
        await m.answer("На сегодня записей нет — можно выдохнуть.")
        return
    text = "\n".join(f"{t} — {name}, {svc}" for name, svc, d, t in rows)
    await m.answer(f"📋 Сегодня к вам идут:\n{text}")


@dp.message(Command("week"))
async def week_schedule(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    rows = db.execute(
        "SELECT date, time, name, service FROM bookings "
        "WHERE date BETWEEN ? AND ? ORDER BY date, time",
        (date.today().isoformat(),
         (date.today() + timedelta(days=7)).isoformat())).fetchall()
    if not rows:
        await m.answer("На неделю вперёд записей нет.")
        return
    text = "\n".join(f"{fmt_date(d)} {t} — {name}, {svc}" for d, t, name, svc in rows)
    await m.answer(f"📅 Расписание на неделю:\n{text}")


# ---------- Любое другое сообщение ----------

@dp.message()
async def anything_else(m: Message):
    """Поймает всё, что не совпало с кнопками и командами выше."""
    await m.answer("Я пока понимаю только кнопки ниже 👇 "
                   "(или команды /start, /today, /week)", reply_markup=menu)


# ---------- Напоминания клиентам за день до визита ----------

async def reminder_loop():
    while True:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        rows = db.execute(
            "SELECT id, user_id, service, date, time FROM bookings "
            "WHERE date=? AND reminded=0", (tomorrow,)).fetchall()
        for bid, uid, svc, d, t in rows:
            try:
                await bot.send_message(
                    uid, f"Напоминаем: завтра в {t} у вас {svc.lower()}. "
                         f"До встречи! ✂️")
                db.execute("UPDATE bookings SET reminded=1 WHERE id=?", (bid,))
                db.commit()
            except Exception:
                pass  # пользователь заблокировал бота — пропускаем
        await asyncio.sleep(3600)  # проверяем раз в час


async def main():
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
