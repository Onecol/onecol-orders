import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
import asyncio
import threading

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DB_PATH = Path(os.getenv("ORDERS_DB_PATH", Path(__file__).with_name("orders.db")))
SITE_DIR = Path(__file__).resolve().parent / "site"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_at TEXT
            )
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)").fetchall()}
        if "source" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN source TEXT NOT NULL DEFAULT 'telegram'")


def format_user(user) -> str:
    name = " ".join(part for part in (user.first_name, user.last_name) if part)
    username = f" (@{user.username})" if user.username else ""
    return f"{name}{username}" if name or username else f"Пользователь {user.id}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Опишите, что вам нужно одним сообщением.\n"
        "Например: «Нужно сделать лендинг для кафе, 5 страниц»."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_db() as db:
        orders = db.execute(
            """
            SELECT id, description, status, created_at
            FROM orders
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (update.effective_user.id,),
        ).fetchall()

    if not orders:
        await update.message.reply_text("У вас пока нет заявок.")
        return

    names = {"pending": "на рассмотрении", "accepted": "принята", "rejected": "отклонена"}
    text = "\n\n".join(
        f"Заявка #{order['id']} — {names[order['status']]}\n{order['description']}"
        for order in orders
    )
    await update.message.reply_text(text)


async def add_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    description = update.message.text.strip()
    if len(description) < 3:
        await update.message.reply_text("Опишите заказ немного подробнее.")
        return

    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO orders (
                user_id, chat_id, username, first_name, description, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                update.effective_user.id,
                update.effective_chat.id,
                update.effective_user.username,
                update.effective_user.first_name,
                description,
                now,
            ),
        )
        order_id = cursor.lastrowid

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Принять",
                    callback_data=f"accept:{order_id}",
                ),
                InlineKeyboardButton(
                    "Отклонить",
                    callback_data=f"reject:{order_id}",
                ),
            ]
        ]
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"Новая заявка #{order_id}\n"
                f"От: {format_user(update.effective_user)}\n"
                f"Chat ID: {update.effective_chat.id}\n\n"
                f"{description}"
            ),
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("Не удалось отправить заявку администратору")
        await update.message.reply_text(
            "Заявка сохранена, но администратор сейчас недоступен. Попробуйте позже."
        )
        return

    await update.message.reply_text(
        f"Заявка #{order_id} отправлена и находится на рассмотрении."
    )


async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if str(update.effective_chat.id) != str(ADMIN_CHAT_ID):
        await query.answer("Только администратор может принимать решения.", show_alert=True)
        return

    action, order_id_text = query.data.split(":", 1)
    order_id = int(order_id_text)

    with get_db() as db:
        order = db.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()

        if not order:
            await query.edit_message_text("Заявка не найдена.")
            return

        if order["status"] != "pending":
            await query.edit_message_text(
                f"Заявка #{order_id} уже обработана: "
                f"{'принята' if order['status'] == 'accepted' else 'отклонена'}."
            )
            return

        new_status = "accepted" if action == "accept" else "rejected"
        db.execute(
            """
            UPDATE orders
            SET status = ?, decided_at = ?
            WHERE id = ?
            """,
            (new_status, datetime.now().isoformat(timespec="seconds"), order_id),
        )

    if action == "accept":
        decision_for_admin = "принята"
        decision_for_user = (
            f"Ваша заявка #{order_id} принята!\n"
            "Мы скоро свяжемся с вами."
        )
    else:
        decision_for_admin = "отклонена"
        decision_for_user = f"К сожалению, ваша заявка #{order_id} отклонена."

    await query.edit_message_text(
        f"{query.message.text}\n\nСтатус: заявка {decision_for_admin}."
    )

    if order["chat_id"] and order["chat_id"] != 0:
        try:
            await context.bot.send_message(chat_id=order["chat_id"], text=decision_for_user)
        except Exception:
            logger.exception("Не удалось уведомить пользователя о решении")


async def list_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if str(update.effective_chat.id) != str(ADMIN_CHAT_ID):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    with get_db() as db:
        orders = db.execute(
            """
            SELECT id, description, status, created_at
            FROM orders
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

    if not orders:
        await update.message.reply_text("Заявок пока нет.")
        return

    names = {"pending": "ожидает", "accepted": "принята", "rejected": "отклонена"}
    text = "\n\n".join(
        f"#{order['id']} — {names[order['status']]}\n{order['description']}"
        for order in orders
    )
    await update.message.reply_text(text)


@app.route("/")
def index():
    return send_from_directory(SITE_DIR, "index.html")


@app.route("/<path:filename>")
def site_files(filename):
    return send_from_directory(SITE_DIR, filename)


@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    contact = str(data.get("contact", "")).strip()
    description = str(data.get("description", "")).strip()

    if len(name) < 2:
        return jsonify(error="Укажите ваше имя."), 400
    if len(contact) < 3:
        return jsonify(error="Укажите способ связи."), 400
    if len(description) < 10:
        return jsonify(error="Опишите задачу подробнее."), 400

    now = datetime.now().isoformat(timespec="seconds")
    full_description = f"{description}\n\nКонтакты: {contact}"

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO orders (
                user_id, chat_id, username, first_name,
                description, status, created_at, source
            ) VALUES (?, 0, ?, ?, ?, 'pending', ?, 'website')
            """,
            (0, contact, name, full_description, now),
        )
        order_id = cursor.lastrowid

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Принять", callback_data=f"accept:{order_id}"),
                InlineKeyboardButton("Отклонить", callback_data=f"reject:{order_id}"),
            ]
        ]
    )

    async def notify_admin():
        await telegram_bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"Новая заявка с сайта #{order_id}\n"
                f"Имя: {name}\n"
                f"Контакт: {contact}\n\n"
                f"{description}"
            ),
            reply_markup=keyboard,
        )

    try:
        asyncio.run(notify_admin())
    except Exception:
        logger.exception("Не удалось отправить заявку с сайта администратору")

    return jsonify(order_id=order_id, message="Заявка отправлена.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Укажите TELEGRAM_BOT_TOKEN в файле .env")
    if not ADMIN_CHAT_ID:
        raise RuntimeError("Укажите ADMIN_CHAT_ID в файле .env")

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("orders", list_orders))
    application.add_handler(
        CallbackQueryHandler(handle_decision, pattern=r"^(accept|reject):\d+$")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, add_order)
    )
    globals()["telegram_bot"] = application.bot

    bot_thread = threading.Thread(
        target=lambda: application.run_polling(allowed_updates=Update.ALL_TYPES),
        name="telegram-bot",
        daemon=True,
    )
    bot_thread.start()

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)


if __name__ == "__main__":
    main()