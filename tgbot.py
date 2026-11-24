import logging
import os
import sqlite3
from datetime import datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

# ==================== КОНФИГ ====================

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DB_PATH = "bot.db"

# Рефералы
REFS_FOR_PACK = 5          # приглашённых для кнопки «Создать пак»
REFS_FOR_CHANNEL = 10      # приглашённых для кнопки «Опубликовать в канале»

# Публикация в канале
MIN_CHANNEL_DAYS = 7       # минимальный срок, дней
MAX_CHANNEL_DAYS = 365     # максимальный срок, дней
CHANNEL_BASE_PRICE = 100   # стоимость за MIN_CHANNEL_DAYS дней
PRICE_PER_EXTRA_DAY = 1    # надбавка за каждый день сверх минимума

# Кнопки меню
BUTTON_REF = "🔗 Реферальная ссылка"
BUTTON_PACK = "🎨 Создать пак"
BUTTON_CHANNEL = "📢 Опубликовать в канале"
BUTTON_ORDERS = "📦 Заказы"
MENU_BUTTONS = [BUTTON_REF, BUTTON_PACK, BUTTON_CHANNEL, BUTTON_ORDERS]

# Тексты
TEXT_WELCOME = "Привет! Приглашайте друзей по своей реферальной ссылке и открывайте новые возможности."
TEXT_NOT_ENOUGH_REFS = "Недостаточно рефералов"
TEXT_WAIT_ADMIN = "Ждите, администрация свяжется"
TEXT_NO_ORDERS = "Ещё ни одного заказа. Не время ли сделать заказ?"
TEXT_CANCELLED = "Отменено."
TEXT_FORWARDED = "Сообщение передано администрации."

# ================================================

STATE_ASK_DAYS = "ask_days"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    referrer_id INTEGER,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,      -- 'pack' или 'channel'
    days       INTEGER,
    price      INTEGER,
    created_at TEXT
);
"""


# ==================== БАЗА ДАННЫХ ====================

def now() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M")


def db_execute(sql, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.fetchall() if fetch else cur.lastrowid
    finally:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.close()


def get_user(user_id):
    rows = db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetch=True)
    return rows[0] if rows else None


def ensure_user(user, referrer_id=None) -> bool:
    """Регистрирует пользователя. Возвращает True, если он новый."""
    if get_user(user.id):
        db_execute(
            "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
            (user.username, user.full_name, user.id),
        )
        return False
    db_execute(
        "INSERT INTO users (user_id, username, full_name, referrer_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user.id, user.username, user.full_name, referrer_id, now()),
    )
    return True


def count_referrals(user_id) -> int:
    rows = db_execute(
        "SELECT COUNT(*) AS c FROM users WHERE referrer_id = ?", (user_id,), fetch=True
    )
    return rows[0]["c"]


def channel_price(days: int) -> int:
    return CHANNEL_BASE_PRICE + (days - MIN_CHANNEL_DAYS) * PRICE_PER_EXTRA_DAY


def create_order(user_id, kind, days=None, price=None) -> int:
    return db_execute(
        "INSERT INTO orders (user_id, kind, days, price, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, kind, days, price, now()),
    )


def get_order(order_id):
    rows = db_execute("SELECT * FROM orders WHERE id = ?", (order_id,), fetch=True)
    return rows[0] if rows else None


def describe_order(o) -> str:
    if o["kind"] == "pack":
        return "Создать пак"
    return f"Публикация в канале на {o['days']} дн., стоимость {o['price']}"


def user_label(user_id, row=None) -> str:
    if row is None:
        return f"id {user_id}"
    name = row["full_name"] or f"id {user_id}"
    if row["username"]:
        name += f" @{row['username']}"
    return f"{name} (id {user_id})"


# ==================== КЛАВИАТУРЫ ====================

def main_menu():
    return ReplyKeyboardMarkup(
        [[BUTTON_REF], [BUTTON_PACK, BUTTON_CHANNEL], [BUTTON_ORDERS]],
        resize_keyboard=True,
    )


def chat_button(user_id):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✉️ Написать пользователю", callback_data=f"chat:{user_id}")]]
    )


# ==================== КОМАНДЫ ====================

async def cmd_start(update, context):
    user = update.effective_user
    referrer_id = None

    if context.args:
        try:
            candidate = int(context.args[0])
        except ValueError:
            candidate = None
        if candidate and candidate != user.id and get_user(candidate):
            referrer_id = candidate

    is_new = ensure_user(user, referrer_id)
    if is_new and referrer_id:
        try:
            await context.bot.send_message(
                referrer_id,
                f"🎉 По вашей ссылке присоединился новый пользователь! "
                f"Приглашено: {count_referrals(referrer_id)}",
            )
        except TelegramError:
            pass

    await update.message.reply_text(TEXT_WELCOME, reply_markup=main_menu())


async def cmd_cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text(TEXT_CANCELLED, reply_markup=main_menu())


# ==================== КНОПКИ МЕНЮ ====================

async def on_menu_button(update, context):
    ensure_user(update.effective_user)
    context.user_data.pop("state", None)
    text = update.message.text

    if text == BUTTON_REF:
        await show_referral(update, context)
    elif text == BUTTON_PACK:
        await order_pack(update, context)
    elif text == BUTTON_CHANNEL:
        await start_channel_order(update, context)
    elif text == BUTTON_ORDERS:
        await show_orders(update, context)


async def show_referral(update, context):
    user = update.effective_user
    link = f"https://t.me/{context.bot.username}?start={user.id}"
    await update.message.reply_text(
        f"🔗 Ваша реферальная ссылка:\n{link}\n\n"
        f"Приглашено: {count_referrals(user.id)}\n\n"
        f"• {REFS_FOR_PACK} приглашённых — открывает «{BUTTON_PACK}»\n"
        f"• {REFS_FOR_CHANNEL} приглашённых — открывает «{BUTTON_CHANNEL}»",
        reply_markup=main_menu(),
    )


async def order_pack(update, context):
    user = update.effective_user
    if count_referrals(user.id) < REFS_FOR_PACK:
        await update.message.reply_text(TEXT_NOT_ENOUGH_REFS, reply_markup=main_menu())
        return

    order_id = create_order(user.id, "pack")
    await update.message.reply_text(TEXT_WAIT_ADMIN, reply_markup=main_menu())
    await notify_admin(context, order_id)


async def start_channel_order(update, context):
    user = update.effective_user
    if count_referrals(user.id) < REFS_FOR_CHANNEL:
        await update.message.reply_text(TEXT_NOT_ENOUGH_REFS, reply_markup=main_menu())
        return

    context.user_data["state"] = STATE_ASK_DAYS
    await update.message.reply_text(
        f"На сколько дней опубликовать в канале? "
        f"Минимум {MIN_CHANNEL_DAYS}, максимум {MAX_CHANNEL_DAYS}.\n"
        f"Стоимость: {channel_price(MIN_CHANNEL_DAYS)} за {MIN_CHANNEL_DAYS} дн., "
        f"+{PRICE_PER_EXTRA_DAY} за каждый следующий день.\n\n"
        "Отмена — /cancel"
    )


async def handle_days(update, context):
    try:
        days = int((update.message.text or "").strip())
    except ValueError:
        days = None

    if days is None or not (MIN_CHANNEL_DAYS <= days <= MAX_CHANNEL_DAYS):
        await update.message.reply_text(
            f"Введите целое число дней от {MIN_CHANNEL_DAYS} до {MAX_CHANNEL_DAYS} или /cancel."
        )
        return

    context.user_data.pop("state", None)
    user = update.effective_user
    order_id = create_order(user.id, "channel", days, channel_price(days))
    await update.message.reply_text(TEXT_WAIT_ADMIN, reply_markup=main_menu())
    await notify_admin(context, order_id)


async def show_orders(update, context):
    user = update.effective_user

    # Админ видит все заказы и выбирает, кому писать
    if user.id == ADMIN_ID:
        rows = db_execute("SELECT * FROM orders ORDER BY id DESC LIMIT 30", fetch=True)
        if not rows:
            await update.message.reply_text(TEXT_NO_ORDERS, reply_markup=main_menu())
            return

        buttons = []
        for o in rows:
            who_row = get_user(o["user_id"])
            who = (
                f"@{who_row['username']}"
                if who_row and who_row["username"]
                else str(o["user_id"])
            )
            buttons.append([
                InlineKeyboardButton(
                    f"#{o['id']} · {who} · {describe_order(o)}",
                    callback_data=f"chat:{o['user_id']}",
                )
            ])
        await update.message.reply_text(
            "Заказы. Нажмите на заказ, чтобы написать его автору:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Обычный пользователь видит только свои заказы
    rows = db_execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC", (user.id,), fetch=True
    )
    if not rows:
        await update.message.reply_text(TEXT_NO_ORDERS, reply_markup=main_menu())
        return

    lines = [f"#{o['id']} · {describe_order(o)} · {o['created_at']}" for o in rows]
    await update.message.reply_text(
        "Ваши заказы:\n\n" + "\n".join(lines), reply_markup=main_menu()
    )


# ==================== УВЕДОМЛЕНИЯ И ДИАЛОГ С АДМИНОМ ====================

async def notify_admin(context, order_id):
    o = get_order(order_id)
    text = (
        f"🆕 Заказ #{o['id']}\n"
        f"Пользователь: {user_label(o['user_id'], get_user(o['user_id']))}\n"
        f"Услуга: {describe_order(o)}"
    )
    await context.bot.send_message(ADMIN_ID, text, reply_markup=chat_button(o["user_id"]))


async def on_chat_button(update, context):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Недоступно", show_alert=True)
        return

    await query.answer()
    target = int(query.data.split(":", 1)[1])
    context.user_data["target"] = target
    await query.message.reply_text(
        f"Диалог с {user_label(target, get_user(target))} открыт.\n"
        "Все ваши сообщения будут уходить этому пользователю. /cancel — завершить."
    )


async def on_message(update, context):
    user = update.effective_user
    msg = update.message
    ensure_user(user)

    if context.user_data.get("state") == STATE_ASK_DAYS:
        await handle_days(update, context)
        return

    # Админ пишет выбранному пользователю
    if user.id == ADMIN_ID:
        target = context.user_data.get("target")
        if not target:
            await msg.reply_text(
                "Сначала выберите, кому писать: «📦 Заказы» "
                "или кнопка «✉️ Написать пользователю» под уведомлением."
            )
            return
        try:
            await context.bot.copy_message(
                chat_id=target, from_chat_id=msg.chat_id, message_id=msg.message_id
            )
            await msg.reply_text("✅ Отправлено")
        except TelegramError as e:
            await msg.reply_text(f"Не удалось отправить: {e}")
        return

    # Пользователь пишет боту — сообщение уходит админу
    await context.bot.copy_message(
        chat_id=ADMIN_ID, from_chat_id=msg.chat_id, message_id=msg.message_id
    )
    await context.bot.send_message(
        ADMIN_ID,
        f"💬 Сообщение от {user_label(user.id, get_user(user.id))}",
        reply_markup=chat_button(user.id),
    )
    await msg.reply_text(TEXT_FORWARDED, reply_markup=main_menu())


# ==================== ЗАПУСК ====================

async def on_error(update, context):
    logging.error("Ошибка при обработке обновления", exc_info=context.error)


def main():
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    private_message = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE

    app.add_handler(CommandHandler("start", cmd_start, filters=private_message))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private_message))
    app.add_handler(
        MessageHandler(private_message & filters.Text(MENU_BUTTONS), on_menu_button)
    )
    app.add_handler(CallbackQueryHandler(on_chat_button, pattern=r"^chat:\d+$"))
    app.add_handler(MessageHandler(private_message & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    base_url = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
    path = "telegram/webhook"

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        url_path=path,
        webhook_url=f"{base_url}/{path}",
        secret_token=os.environ["WEBHOOK_SECRET"],
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
