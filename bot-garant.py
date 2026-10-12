import asyncio
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ══════════════════════════ НАСТРОЙКИ ══════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "8763383205:AAFeRlMGdhVBEn8SBsretOjsB_dgmjMV3TM")

# Вход в панель гаранта (владелец): команда  /admin ПАРОЛЬ
# После первого входа бот запоминает вас, дальше хватает просто /admin.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "maksumtop1")

# Необязательно: Telegram ID владельцев через запятую (ADMIN_IDS=111,222)
OWNER_IDS = set()
if os.getenv("ADMIN_IDS"):
    OWNER_IDS = {int(x) for x in os.getenv("ADMIN_IDS").split(",") if x.strip().isdigit()}

# База данных создаётся автоматически рядом с файлом. НЕ УДАЛЯЙТЕ её — там все сделки.
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).resolve().parent / "garant.db"))

# Часовой пояс для времени в сделках: 3 = Москва (UTC+3)
TZ_OFFSET = 3

SUPPORT_TEXT = "Здравствуйте если у вас возникла проблема то отпишитесь сюда @Forevebz"

# Цвета кнопок: danger = красная. Используем только красные.
RED = "danger"

MAX_TEXT = 3500    # максимум символов в описании сделки
MAX_PHOTOS = 10    # максимум скриншотов в сделке

BOT_DESCRIPTION = (
    "🛡 Гарант для безопасных сделок с аккаунтами.\n\n"
    "Продавец создаёт сделку и описывает аккаунт, гарант принимает её и открывает "
    "общий чат: гарант, продавец и покупатель. Оплату подтверждает гарант, "
    "5% от сделки — комиссия гаранта."
)
BOT_SHORT_DESCRIPTION = "🛡 Гарант для безопасных сделок с аккаунтами"

log = logging.getLogger("garant")
esc = html.escape
BOT_USERNAME = ""  # заполняется при запуске

# Кнопки внизу экрана в чате сделки
BTN_PAID = "💸 Я успешно отправил деньги"
BTN_CONFIRM = "✅ Подтвердить"
BTN_DONE = "🏁 Сделка завершена"
BTN_CANCEL = "❌ Отменить сделку"

STATUS = {
    "draft": "✏️ Черновик",
    "new": "🆕 Новая",
    "accepted": "🤝 Принята гарантом",
    "paid": "💸 Покупатель отправил деньги",
    "confirmed": "✅ Оплата подтверждена",
    "done": "🏁 Завершена",
    "canceled": "❌ Отменена",
}
STATUS_ICON = {k: v.split()[0] for k, v in STATUS.items()}
ACTIVE = ("accepted", "paid", "confirmed")
ROLE_LABEL = {"guarantor": "🛡 Гарант", "seller": "🧑‍💼 Продавец", "buyer": "🛒 Покупатель"}
LINE = "➖➖➖➖➖➖➖➖➖➖➖➖"

# ═══════════════════ БАЗА ДАННЫХ ═══════════════════
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


def q(sql, *args):
    return db.execute(sql, args).fetchall()


def q1(sql, *args):
    return db.execute(sql, args).fetchone()


def scalar(sql, *args):
    r = db.execute(sql, args).fetchone()
    return r[0] if r else None


def run(sql, *args):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT,
            name TEXT,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS guarantors(
            id INTEGER PRIMARY KEY,
            owner INTEGER NOT NULL DEFAULT 0,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS deals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            buyer_id INTEGER,
            guarantor_id INTEGER,
            status TEXT NOT NULL DEFAULT 'draft',
            text TEXT NOT NULL DEFAULT '',
            photos TEXT NOT NULL DEFAULT '[]',
            acked INTEGER NOT NULL DEFAULT 0,
            token TEXT NOT NULL,
            created TEXT DEFAULT CURRENT_TIMESTAMP,
            sent TEXT,
            accepted TEXT,
            paid TEXT,
            confirmed TEXT,
            finished TEXT
        );
        CREATE TABLE IF NOT EXISTS deal_msgs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ctx(
            user_id INTEGER PRIMARY KEY,
            deal_id INTEGER NOT NULL
        );
        """
    )
    db.commit()


def touch_user(u):
    run(
        "INSERT INTO users(id, username, name) VALUES(?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name",
        u.id, u.username, u.full_name,
    )


def is_owner(uid) -> bool:
    return uid in OWNER_IDS or q1("SELECT 1 FROM guarantors WHERE id=? AND owner=1", uid) is not None


def is_guarantor(uid) -> bool:
    return uid in OWNER_IDS or q1("SELECT 1 FROM guarantors WHERE id=?", uid) is not None


def guarantor_ids():
    ids = {r["id"] for r in q("SELECT id FROM guarantors")}
    return ids | OWNER_IDS


def add_guarantor(uid, owner=0):
    run("INSERT OR IGNORE INTO guarantors(id, owner) VALUES(?, ?)", uid, owner)
    if owner:
        run("UPDATE guarantors SET owner=1 WHERE id=?", uid)


def set_ctx(uid, deal_id):
    run("INSERT INTO ctx(user_id, deal_id) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET deal_id=excluded.deal_id", uid, deal_id)


def role_of(d, uid):
    if uid == d["guarantor_id"]:
        return "guarantor"
    if uid == d["seller_id"]:
        return "seller"
    if uid == d["buyer_id"]:
        return "buyer"
    return None


def participants(d):
    return [x for x in (d["guarantor_id"], d["seller_id"], d["buyer_id"]) if x]


def log_msg(deal_id, role, content):
    run("INSERT INTO deal_msgs(deal_id, role, content) VALUES(?, ?, ?)", deal_id, role, content)


# ═══════════════════ ТЕКСТЫ И КНОПКИ ═══════════════════
def ib(text, data=None, url=None):
    return InlineKeyboardButton(text=text, callback_data=data, url=url, style=RED)


def kb(*buttons) -> InlineKeyboardMarkup:
    """Кнопки друг под другом, на всю ширину."""
    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons])


def role_kb(role):
    """Кнопки внизу экрана в чате сделки."""
    if role == "guarantor":
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_CONFIRM, style=RED), KeyboardButton(text=BTN_DONE, style=RED)],
                [KeyboardButton(text=BTN_CANCEL, style=RED)],
            ],
            resize_keyboard=True,
        )
    if role == "buyer":
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_PAID, style=RED)]], resize_keyboard=True)
    return ReplyKeyboardRemove()


WELCOME_TEXT = (
    "🛡 Добро пожаловать в Гарант-бот!\n\n"
    "Здесь можно безопасно провести сделку по покупке или продаже аккаунта: "
    "гарант проверит оплату и проследит, чтобы обе стороны получили своё.\n\n"
    "Выберите действие:"
)
GUARANT_TEXT = "Здравствуйте, если вам нужен гарант, нажмите кнопку ниже"
NEW_DEAL_TEXT = (
    "(5% от сделки идет гаранту)\n\n"
    "Опишите, что вы хотите продать, за сколько, и отправьте скриншоты аккаунта "
    "и юзернейм (@username) человека, с которым хотите провести сделку.\n\n"
    "Можно отправить несколько сообщений и фото."
)


def main_kb():
    return kb(ib("Гарант", "g:home"), ib("Поддержка", "g:support"))


def admin_kb(uid):
    n = scalar("SELECT COUNT(*) FROM deals WHERE status='new'") or 0
    buttons = [
        ib(f"Новые сделки ({n})" if n else "Новые сделки", "a:new"),
        ib("История сделок", "a:h:0"),
    ]
    if is_owner(uid):
        buttons.append(ib("Добавить гаранта", "a:addg"))
    return kb(*buttons)


def fmt_dt(s):
    if not s:
        return "—"
    try:
        return (datetime.strptime(s, "%Y-%m-%d %H:%M:%S") + timedelta(hours=TZ_OFFSET)).strftime("%d.%m %H:%M")
    except ValueError:
        return s


def fmt_user(uid):
    if not uid:
        return "—"
    r = q1("SELECT username, name FROM users WHERE id=?", uid)
    name = esc(r["name"]) if r and r["name"] else str(uid)
    uname = f" (@{esc(r['username'])})" if r and r["username"] else ""
    return f'<a href="tg://user?id={uid}">{name}</a>{uname} · <code>{uid}</code>'


def short_user(uid):
    r = q1("SELECT username, name FROM users WHERE id=?", uid)
    s = (f"@{r['username']}" if r and r["username"] else (r["name"] if r and r["name"] else str(uid)))
    return esc(s[:18])


def trim(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def invite_link(d):
    return f"https://t.me/{BOT_USERNAME}?start=deal_{d['id']}_{d['token']}"


def deal_card_text(d) -> str:
    text = (
        f"📄 <b>Сделка #{d['id']}</b> — {STATUS[d['status']]}\n{LINE}\n"
        f"🧑‍💼 Продавец: {fmt_user(d['seller_id'])}\n"
        f"🛒 Покупатель: {fmt_user(d['buyer_id']) if d['buyer_id'] else 'ещё не присоединился'}\n"
        f"🛡 Гарант: {fmt_user(d['guarantor_id']) if d['guarantor_id'] else 'не назначен'}\n\n"
        f"🕓 Создана: {fmt_dt(d['sent'] or d['created'])}\n"
    )
    for key, label in (("accepted", "Принята"), ("paid", "Отметка об оплате"),
                       ("confirmed", "Оплата подтверждена"), ("finished", "Закрыта")):
        if d[key]:
            text += f"🕓 {label}: {fmt_dt(d[key])}\n"
    n_photos = len(json.loads(d["photos"]))
    text += f"\n📝 <b>Описание от продавца</b> (фото: {n_photos}):\n{esc(trim(d['text'], 2200)) or '—'}\n"
    msgs = q("SELECT role, content FROM deal_msgs WHERE deal_id=? ORDER BY id DESC LIMIT 15", d["id"])[::-1]
    if msgs:
        log_text = "\n💬 <b>Переписка</b> (последние сообщения):\n" + "\n".join(
            f"{ROLE_LABEL[m['role']]}: {esc(trim(m['content'], 150))}" for m in msgs
        )
        if len(text) + len(log_text) > 4000:
            log_text = log_text[: max(0, 4000 - len(text))]
        text += log_text
    return text[:4096]


def deal_card_kb(d, viewer):
    buttons = []
    if d["status"] == "new":
        buttons.append(ib("✅ Принять сделку", f"a:acc:{d['id']}"))
        buttons.append(ib("Назад", "a:new"))
    else:
        if d["status"] in ACTIVE and d["guarantor_id"] == viewer:
            buttons.append(ib("💬 Войти в чат сделки", f"d:go:{d['id']}"))
        buttons.append(ib("Назад", "a:h:0"))
    return kb(*buttons)


# ═══════════════════ ОТПРАВКА ═══════════════════
async def show(msg: Message, text, markup):
    """Заменить содержимое сообщения (если нельзя отредактировать — отправить новое)."""
    try:
        await msg.edit_text(text, reply_markup=markup)
        return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    await msg.answer(text, reply_markup=markup)


async def safe_send(bot: Bot, uid, text, **kw):
    try:
        return await bot.send_message(uid, text, **kw)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.warning("Не удалось написать %s: %s", uid, e)
        return None


async def send_photos(bot: Bot, chat_id, photos):
    try:
        if len(photos) == 1:
            await bot.send_photo(chat_id, photos[0])
        elif photos:
            await bot.send_media_group(chat_id, [InputMediaPhoto(media=f) for f in photos[:MAX_PHOTOS]])
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.warning("Не удалось отправить фото %s: %s", chat_id, e)


async def send_deal_card(bot: Bot, chat_id, d, viewer):
    await send_photos(bot, chat_id, json.loads(d["photos"]))
    await bot.send_message(chat_id, deal_card_text(d), reply_markup=deal_card_kb(d, viewer))


# ═══════════════════ РОУТЕРЫ ═══════════════════
common = Router()
admin_r = Router()
user_r = Router()
admin_r.message.filter(lambda m: is_guarantor(m.from_user.id))
admin_r.callback_query.filter(lambda c: is_guarantor(c.from_user.id))

_fails: dict = {}


def too_many_fails(uid: int) -> bool:
    now = time.time()
    _fails[uid] = [t for t in _fails.get(uid, []) if now - t < 600]
    return len(_fails[uid]) >= 5


class NewDeal(StatesGroup):
    collect = State()


class AddG(StatesGroup):
    uid = State()


# ═══════════════════ /start, /id, /admin ═══════════════════
@common.message(CommandStart())
async def cmd_start(msg: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    touch_user(msg.from_user)
    args = command.args or ""
    if args.startswith("deal_"):
        await join_deal(msg, args, bot)
        return
    await msg.answer(WELCOME_TEXT, reply_markup=main_kb())


@common.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(f"Ваш Telegram ID: <code>{msg.from_user.id}</code>")


@common.message(Command("admin"))
async def cmd_admin(msg: Message, command: CommandObject, state: FSMContext):
    uid = msg.from_user.id
    password = (command.args or "").strip()
    if password:  # прячем сообщение с паролем
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
    if not is_guarantor(uid):
        if not password:
            return  # обычным пользователям бот не отвечает
        if too_many_fails(uid):
            await msg.answer("Слишком много попыток. Попробуйте через 10 минут.")
            return
        if not hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            _fails.setdefault(uid, []).append(time.time())
            await msg.answer("Неверный пароль.")
            return
        touch_user(msg.from_user)
        add_guarantor(uid, owner=1)
        await msg.answer("✅ Доступ открыт.")
    await state.clear()
    await msg.answer("🛡 Панель гаранта", reply_markup=admin_kb(uid))


# ═══════════════════ ПОЛЬЗОВАТЕЛЬ: СОЗДАНИЕ СДЕЛКИ ═══════════════════
@user_r.callback_query(F.data == "g:main")
async def g_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, WELCOME_TEXT, main_kb())


@user_r.callback_query(F.data == "g:support")
async def g_support(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, SUPPORT_TEXT, kb(ib("Назад", "g:main")))


@user_r.callback_query(F.data == "g:home")
async def g_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, GUARANT_TEXT, kb(ib("Создать сделку", "g:new"), ib("Назад", "g:main")))


@user_r.callback_query(F.data == "g:new")
async def g_new(cb: CallbackQuery, state: FSMContext):
    touch_user(cb.from_user)
    uid = cb.from_user.id
    run("DELETE FROM deals WHERE seller_id=? AND status='draft'", uid)  # старый черновик не нужен
    cur = run("INSERT INTO deals(seller_id, token) VALUES(?, ?)", uid, secrets.token_hex(4))
    await state.set_state(NewDeal.collect)
    await state.update_data(deal_id=cur.lastrowid)
    await cb.answer()
    await show(cb.message, NEW_DEAL_TEXT, kb(ib("Отмена", "g:cancel")))


@user_r.callback_query(F.data == "g:cancel")
async def g_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    run("DELETE FROM deals WHERE seller_id=? AND status='draft'", cb.from_user.id)
    await cb.answer()
    await show(cb.message, WELCOME_TEXT, main_kb())


@user_r.message(NewDeal.collect, F.text | F.photo)
async def deal_collect(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    deal_id = (await state.get_data()).get("deal_id")
    # дальше до отправки ack — без await, чтобы альбом из нескольких фото не терял данные
    d = q1("SELECT * FROM deals WHERE id=? AND seller_id=? AND status='draft'", deal_id, uid)
    if not d:
        await state.clear()
        await msg.answer("Черновик не найден. Начните заново.", reply_markup=main_kb())
        return
    text = (msg.text or msg.caption or "").strip()
    photos = json.loads(d["photos"])
    notes = []
    if msg.photo:
        if len(photos) >= MAX_PHOTOS:
            notes.append(f"Максимум {MAX_PHOTOS} фото — лишнее не добавлено.")
        else:
            photos.append(msg.photo[-1].file_id)
    new_text = d["text"]
    if text:
        merged = (new_text + "\n" + text).strip()
        if len(merged) > MAX_TEXT:
            notes.append("Описание слишком длинное — часть текста не добавлена.")
        else:
            new_text = merged
    first = not d["acked"]
    db.execute("UPDATE deals SET text=?, photos=?, acked=1 WHERE id=?", (new_text, json.dumps(photos), deal_id))
    db.commit()
    if notes:
        await msg.answer(" ".join(notes))
    if first:
        await msg.answer(
            "✅ Получено! Если нужно, отправьте ещё сообщения и скриншоты.\n"
            "Когда всё готово — нажмите кнопку ниже.",
            reply_markup=kb(ib("Позвать гаранта", f"g:call:{deal_id}"), ib("Отмена", "g:cancel")),
        )


@user_r.message(NewDeal.collect)
async def deal_collect_other(msg: Message):
    await msg.answer("Отправьте текст или фото (скриншоты) аккаунта.")


@user_r.callback_query(F.data.regexp(r"^g:call:\d+$"))
async def g_call(cb: CallbackQuery, state: FSMContext, bot: Bot):
    did = int(cb.data.split(":")[2])
    d = q1("SELECT * FROM deals WHERE id=? AND seller_id=? AND status='draft'", did, cb.from_user.id)
    if not d:
        await cb.answer("Заявка уже отправлена или не найдена.", show_alert=True)
        return
    if not d["text"] and not json.loads(d["photos"]):
        await cb.answer("Сначала опишите сделку и отправьте скриншоты.", show_alert=True)
        return
    run("UPDATE deals SET status='new', sent=CURRENT_TIMESTAMP WHERE id=?", did)
    await state.clear()
    await cb.answer()
    await show(
        cb.message,
        f"✅ Сделка #{did} отправлена гаранту.\n\n"
        "Как только гарант её примет, вам придёт сообщение и откроется общий чат.",
        kb(ib("Главное меню", "g:main")),
    )
    preview = esc(trim(d["text"], 300)) or "(только фото)"
    for gid in guarantor_ids():
        await safe_send(
            bot, gid,
            f"🆕 <b>Новая сделка #{did}</b>\nПродавец: {fmt_user(d['seller_id'])}\n\n{preview}",
            reply_markup=kb(ib("Открыть сделку", f"a:d:{did}")),
        )


# ═══════════════════ ПОКУПАТЕЛЬ: ВХОД ПО ССЫЛКЕ ═══════════════════
async def enter_chat(bot: Bot, uid, d, role):
    """Включает общий чат сделки для участника и показывает кнопки роли."""
    set_ctx(uid, d["id"])
    texts = {
        "guarantor": f"💬 Вы в чате сделки #{d['id']}. Всё, что вы напишете, увидят продавец и покупатель.\n"
                     f"Кнопки внизу — «{BTN_CONFIRM}» (после получения денег) и «{BTN_DONE}».",
        "seller": f"💬 Вы в чате сделки #{d['id']}. Ваши сообщения увидят гарант и покупатель.",
        "buyer": f"💬 Вы в чате сделки #{d['id']}. Ваши сообщения увидят продавец и гарант.\n"
                 f"После перевода денег нажмите кнопку «{BTN_PAID}».",
    }
    await safe_send(bot, uid, texts[role], reply_markup=role_kb(role))


async def join_deal(msg: Message, args: str, bot: Bot):
    m = re.fullmatch(r"deal_(\d+)_([0-9a-f]{8})", args)
    d = q1("SELECT * FROM deals WHERE id=? AND token=?", int(m[1]), m[2]) if m else None
    uid = msg.from_user.id
    if not d or d["status"] not in ACTIVE:
        await msg.answer("Эта ссылка недействительна или сделка уже закрыта.", reply_markup=main_kb())
        return
    role = role_of(d, uid)
    if role:  # продавец или гарант открыл ссылку — просто входит в чат
        await enter_chat(bot, uid, d, role)
        return
    cur = run("UPDATE deals SET buyer_id=? WHERE id=? AND buyer_id IS NULL", uid, d["id"])
    d = q1("SELECT * FROM deals WHERE id=?", d["id"])
    if d["buyer_id"] != uid:
        await msg.answer("К этой сделке уже присоединился другой покупатель.", reply_markup=main_kb())
        return
    set_ctx(uid, d["id"])
    await send_photos(bot, uid, json.loads(d["photos"]))
    await msg.answer(
        f"🛒 Вы присоединились к сделке #{d['id']} как покупатель.\n\n"
        f"📝 Описание от продавца:\n{esc(trim(d['text'], 3000)) or '—'}\n\n"
        "Здесь общий чат: ваши сообщения увидят продавец и гарант. "
        "Гарант пришлёт реквизиты для оплаты.\n"
        f"После перевода денег нажмите кнопку «{BTN_PAID}».",
        reply_markup=role_kb("buyer"),
    )
    if cur.rowcount:
        for other in (d["seller_id"], d["guarantor_id"]):
            await safe_send(bot, other, f"🛒 Покупатель {fmt_user(uid)} присоединился к сделке #{d['id']}. Общий чат открыт.")


@user_r.callback_query(F.data.regexp(r"^d:go:\d+$"))
async def d_go(cb: CallbackQuery, bot: Bot):
    d = q1("SELECT * FROM deals WHERE id=?", int(cb.data.split(":")[2]))
    role = role_of(d, cb.from_user.id) if d else None
    if not role or d["status"] not in ACTIVE:
        await cb.answer("Чат этой сделки недоступен.", show_alert=True)
        return
    await cb.answer()
    await enter_chat(bot, cb.from_user.id, d, role)


# ═══════════════════ ЧАТ СДЕЛКИ: КНОПКИ И ПЕРЕСЫЛКА ═══════════════════
async def finish_deal(bot: Bot, d, status, text):
    run("UPDATE deals SET status=?, finished=CURRENT_TIMESTAMP WHERE id=?", status, d["id"])
    for uid in participants(d):
        run("DELETE FROM ctx WHERE user_id=? AND deal_id=?", uid, d["id"])
        tail = "\n\nПанель: /admin" if uid == d["guarantor_id"] else "\n\nМеню: /start"
        await safe_send(bot, uid, text + tail, reply_markup=ReplyKeyboardRemove())


async def notify(bot: Bot, d, text, exclude=None):
    for uid in participants(d):
        if uid != exclude:
            await safe_send(bot, uid, text)


async def chat_action(msg: Message, bot: Bot, d, role) -> bool:
    """Обрабатывает кнопки чата. True — сообщение было кнопкой, пересылать не нужно."""
    t = msg.text or ""
    did = d["id"]
    if role == "buyer" and t == BTN_PAID:
        if d["status"] != "accepted":
            await msg.answer("Вы уже отметили оплату — гарант проверяет поступление."
                             if d["status"] == "paid" else "Оплата уже подтверждена гарантом.")
            return True
        run("UPDATE deals SET status='paid', paid=CURRENT_TIMESTAMP WHERE id=?", did)
        log_msg(did, "buyer", "💸 Нажал «Я успешно отправил деньги»")
        await msg.answer("Отлично! Гарант проверит поступление и подтвердит оплату.")
        await safe_send(bot, d["guarantor_id"],
                        f"💸 Сделка #{did}: покупатель сообщает, что отправил деньги. "
                        f"Проверьте поступление и нажмите «{BTN_CONFIRM}».")
        await safe_send(bot, d["seller_id"], f"💸 Сделка #{did}: покупатель отметил, что отправил деньги. Ждём подтверждения гаранта.")
        return True
    if role == "guarantor" and t == BTN_CONFIRM:
        if d["status"] not in ("accepted", "paid"):
            await msg.answer("Оплата уже подтверждена." if d["status"] == "confirmed" else "Сделка закрыта.")
            return True
        if not d["buyer_id"]:
            await msg.answer("Покупатель ещё не присоединился к сделке.")
            return True
        run("UPDATE deals SET status='confirmed', confirmed=CURRENT_TIMESTAMP WHERE id=?", did)
        log_msg(did, "guarantor", "✅ Подтвердил получение денег")
        await msg.answer(f"✅ Оплата по сделке #{did} подтверждена. Когда сделка пройдёт — нажмите «{BTN_DONE}».")
        for uid in (d["seller_id"], d["buyer_id"]):
            await safe_send(bot, uid, f"✅ Сделка #{did}: гарант подтвердил получение денег. "
                                      "Продавец, передайте аккаунт покупателю.")
        return True
    if role == "guarantor" and t == BTN_DONE:
        if d["status"] != "confirmed":
            await msg.answer(f"Сначала подтвердите получение денег кнопкой «{BTN_CONFIRM}».")
            return True
        log_msg(did, "guarantor", "🏁 Сделка завершена")
        await finish_deal(bot, d, "done", f"🏁 Сделка #{did} завершена. Спасибо, что пользуетесь гарантом!")
        return True
    if role == "guarantor" and t == BTN_CANCEL:
        log_msg(did, "guarantor", "❌ Сделка отменена")
        await finish_deal(bot, d, "canceled", f"❌ Сделка #{did} отменена гарантом.")
        return True
    return False


@user_r.message(StateFilter(None))
async def chat_message(msg: Message, bot: Bot):
    uid = msg.from_user.id
    if msg.text and msg.text.startswith("/"):
        return
    ctx = q1("SELECT deal_id FROM ctx WHERE user_id=?", uid)
    d = q1("SELECT * FROM deals WHERE id=?", ctx["deal_id"]) if ctx else None
    role = role_of(d, uid) if d else None
    if not d or not role or d["status"] not in ACTIVE:
        if ctx:
            run("DELETE FROM ctx WHERE user_id=?", uid)
        await msg.answer("Выберите действие:", reply_markup=main_kb())
        return
    if await chat_action(msg, bot, d, role):
        return

    # обычное сообщение — пересылаем остальным участникам
    log_msg(d["id"], role, msg.text or f"[{msg.content_type}] {msg.caption or ''}".strip())
    failed = []
    for rid in participants(d):
        if rid == uid:
            continue
        label = ROLE_LABEL[role] + (f" · сделка #{d['id']}" if rid == d["guarantor_id"] else "")
        rctx = q1("SELECT deal_id FROM ctx WHERE user_id=?", rid)
        markup = None
        if not rctx or rctx["deal_id"] != d["id"]:  # получатель сейчас в другом чате — даём кнопку перехода
            markup = kb(ib(f"Перейти в чат сделки #{d['id']}", f"d:go:{d['id']}"))
        try:
            if msg.text:
                await bot.send_message(rid, f"<b>{label}:</b>\n{esc(msg.text)}", reply_markup=markup)
            else:
                cap = f"<b>{label}:</b>" + (f"\n{esc(msg.caption)}" if msg.caption else "")
                try:
                    await bot.copy_message(rid, msg.chat.id, msg.message_id, caption=cap, reply_markup=markup)
                except TelegramBadRequest:
                    await bot.send_message(rid, f"<b>{label}:</b>")
                    await bot.copy_message(rid, msg.chat.id, msg.message_id, reply_markup=markup)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.warning("Не доставлено %s: %s", rid, e)
            failed.append(ROLE_LABEL[role_of(d, rid)])
    if failed:
        await msg.answer("⚠️ Не удалось доставить: " + ", ".join(failed))


# ═══════════════════ ПАНЕЛЬ ГАРАНТА ═══════════════════
@admin_r.callback_query(F.data == "a:menu")
async def a_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, "🛡 Панель гаранта", admin_kb(cb.from_user.id))


@admin_r.callback_query(F.data == "a:cancel")
async def a_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, "🛡 Панель гаранта", admin_kb(cb.from_user.id))


@admin_r.callback_query(F.data == "a:new")
async def a_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    rows = q("SELECT * FROM deals WHERE status='new' ORDER BY id DESC LIMIT 50")
    text = f"🆕 Новые сделки: {len(rows)}" if rows else "🆕 Новых сделок нет."
    buttons = [
        ib(f"#{r['id']} · {short_user(r['seller_id'])} · {trim(r['text'].replace(chr(10), ' '), 22) or 'фото'}",
           f"a:d:{r['id']}")
        for r in rows
    ]
    buttons.append(ib("Назад", "a:menu"))
    await show(cb.message, text, kb(*buttons))


@admin_r.callback_query(F.data.regexp(r"^a:h:\d+$"))
async def a_history(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    page = int(cb.data.split(":")[2])
    per = 8
    total = scalar("SELECT COUNT(*) FROM deals WHERE status!='draft'") or 0
    rows = q("SELECT * FROM deals WHERE status!='draft' ORDER BY id DESC LIMIT ? OFFSET ?", per, page * per)
    text = f"📚 История сделок: {total}" if total else "📚 Сделок пока нет."
    keyboard = [
        [ib(f"#{r['id']} {STATUS_ICON[r['status']]} {short_user(r['seller_id'])} · {fmt_dt(r['sent'] or r['created'])}",
            f"a:d:{r['id']}")]
        for r in rows
    ]
    nav = []
    if page > 0:
        nav.append(ib("⬅️", f"a:h:{page - 1}"))
    if (page + 1) * per < total:
        nav.append(ib("➡️", f"a:h:{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([ib("Назад", "a:menu")])
    await show(cb.message, text, InlineKeyboardMarkup(inline_keyboard=keyboard))


@admin_r.callback_query(F.data.regexp(r"^a:d:\d+$"))
async def a_deal(cb: CallbackQuery, bot: Bot):
    d = q1("SELECT * FROM deals WHERE id=? AND status!='draft'", int(cb.data.split(":")[2]))
    if not d:
        await cb.answer("Сделка не найдена.", show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass
    await send_deal_card(bot, cb.message.chat.id, d, cb.from_user.id)


@admin_r.callback_query(F.data.regexp(r"^a:acc:\d+$"))
async def a_accept(cb: CallbackQuery, bot: Bot):
    did = int(cb.data.split(":")[2])
    uid = cb.from_user.id
    d = q1("SELECT * FROM deals WHERE id=?", did)
    if not d:
        await cb.answer("Сделка не найдена.", show_alert=True)
        return
    if d["seller_id"] == uid:
        await cb.answer("Нельзя принять собственную сделку.", show_alert=True)
        return
    cur = run("UPDATE deals SET status='accepted', guarantor_id=?, accepted=CURRENT_TIMESTAMP "
              "WHERE id=? AND status='new'", uid, did)
    if cur.rowcount == 0:
        await cb.answer("Эту сделку уже приняли или она недоступна.", show_alert=True)
        return
    d = q1("SELECT * FROM deals WHERE id=?", did)
    await cb.answer("Сделка принята")
    link = invite_link(d)
    set_ctx(uid, did)
    set_ctx(d["seller_id"], did)
    await show(cb.message, f"✅ Вы приняли сделку #{did}.", kb(ib("В панель", "a:menu")))
    await cb.message.answer(
        f"💬 Вы в чате сделки #{did}. Всё, что вы напишете, увидят продавец и покупатель.\n\n"
        f"Ссылка для покупателя (отправьте ему, если продавец не передал):\n{link}\n\n"
        f"Кнопки внизу: «{BTN_CONFIRM}» — когда деньги поступили, «{BTN_DONE}» — когда сделка прошла.",
        reply_markup=role_kb("guarantor"),
    )
    await safe_send(
        bot, d["seller_id"],
        f"✅ Гарант успешно принял сделку #{did}!\n\n"
        f"Отправьте эту ссылку покупателю — по ней он попадёт в общий чат (вы, покупатель и гарант):\n{link}\n\n"
        "Пока покупатель не присоединился, вы можете писать гаранту прямо здесь.",
        reply_markup=role_kb("seller"),
    )


@admin_r.callback_query(F.data == "a:addg")
async def a_add_guarantor(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        await cb.answer("Недоступно.", show_alert=True)
        return
    await state.set_state(AddG.uid)
    await cb.answer()
    await show(
        cb.message,
        "Пришлите Telegram ID человека, которого нужно сделать гарантом (число).\n"
        "Узнать ID можно командой /id в боте.",
        kb(ib("Отмена", "a:cancel")),
    )


@admin_r.message(AddG.uid, F.text)
async def a_add_guarantor_id(msg: Message, state: FSMContext, bot: Bot):
    if not is_owner(msg.from_user.id):
        await state.clear()
        return
    t = msg.text.strip()
    if not re.fullmatch(r"\d{1,15}", t) or int(t) == 0:
        await msg.answer("Нужен числовой Telegram ID. Попробуйте ещё раз:", reply_markup=kb(ib("Отмена", "a:cancel")))
        return
    gid = int(t)
    await state.clear()
    if is_guarantor(gid):
        await msg.answer(f"Пользователь {gid} уже гарант.", reply_markup=kb(ib("В панель", "a:menu")))
        return
    add_guarantor(gid)
    sent = await safe_send(bot, gid, "✅ Вам выдан доступ гаранта. Откройте панель командой /admin")
    note = "" if sent else "\n\n⚠️ Написать ему не получилось — пусть сначала запустит бота (/start)."
    await msg.answer(f"✅ Гарант {gid} добавлен.{note}", reply_markup=kb(ib("В панель", "a:menu")))


# ═══════════════════ ЗАПУСК ═══════════════════
async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_routers(common, admin_r, user_r)
    await bot.delete_webhook(drop_pending_updates=False)  # снимаем старый webhook, иначе polling не работает
    BOT_USERNAME = (await bot.get_me()).username
    try:  # описание бота (то, что видно в профиле до нажатия /start)
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
    except Exception as e:
        log.warning("Не удалось обновить описание бота: %s", e)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
