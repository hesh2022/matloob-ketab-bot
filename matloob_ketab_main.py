import os
import sqlite3
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOOKSTORE_GROUP_ID = int(os.environ["BOOKSTORE_GROUP_ID"])
DB_PATH = os.environ.get("DB_PATH", "matloob_ketab.db")

ASK_BOOK, ASK_AUTHOR = range(2)
STORE_NAME, OFFER_PRICE, OFFER_DELIVERY = range(3)


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reader_chat_id INTEGER NOT NULL,
            book_name TEXT NOT NULL,
            author TEXT NOT NULL,
            group_message_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bookstores (
            telegram_user_id INTEGER PRIMARY KEY,
            store_name TEXT NOT NULL,
            username TEXT
        );
        """)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("اطلب كتاب", callback_data="new_request")]])
    await update.effective_message.reply_text(
        "أهلاً بك في مطلوب كتاب.\nاكتب الكتاب الذي تبحث عنه، ودع المكتبات ترد عليك.",
        reply_markup=kb,
    )


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("ما اسم الكتاب؟")
    return ASK_BOOK


async def receive_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["book_name"] = update.message.text.strip()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("لا أعرف", callback_data="author_unknown")]])
    await update.message.reply_text("ما اسم المؤلف؟", reply_markup=kb)
    return ASK_AUTHOR


async def author_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    return await submit_request(q.message, context, "لا أعرف", q.from_user.id)


async def receive_author(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await submit_request(update.message, context, update.message.text.strip(), update.effective_user.id)


async def submit_request(message, context, author, reader_id):
    book = context.user_data.pop("book_name", None)
    if not book:
        await message.reply_text("ابدأ طلباً جديداً من /start")
        return ConversationHandler.END

    with db() as con:
        cur = con.execute(
            "INSERT INTO requests(reader_chat_id, book_name, author) VALUES(?,?,?)",
            (reader_id, book, author),
        )
        request_id = cur.lastrowid

    text = f"طلب كتاب جديد\n\nاسم الكتاب: {book}\nالمؤلف: {author}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("الكتاب متوفر عندي", callback_data=f"offer:{request_id}")
    ]])
    sent = await context.bot.send_message(BOOKSTORE_GROUP_ID, text, reply_markup=kb)
    with db() as con:
        con.execute("UPDATE requests SET group_message_id=? WHERE id=?", (sent.message_id, request_id))

    await context.bot.send_message(reader_id, "تم إرسال طلبك للمكتبات. هنبلغك أول ما مكتبة ترد عليك.")
    return ConversationHandler.END


async def offer_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    request_id = int(q.data.split(":", 1)[1])
    context.user_data["offer_request_id"] = request_id

    user = q.from_user
    with db() as con:
        store = con.execute("SELECT * FROM bookstores WHERE telegram_user_id=?", (user.id,)).fetchone()

    # Telegram bots cannot always initiate a private chat with a user who has never started the bot.
    try:
        if store:
            await context.bot.send_message(user.id, "ما سعر الكتاب؟")
            context.user_data["offer_state"] = "price"
        else:
            await context.bot.send_message(user.id, "اكتب اسم المكتبة:")
            context.user_data["offer_state"] = "store_name"
    except Exception:
        await q.message.reply_text(
            f"من فضلك افتح البوت على الخاص واضغط Start، ثم اضغط زر «الكتاب متوفر عندي» مرة أخرى."
        )
    return ConversationHandler.END


async def private_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Ignore group text here; group is only for requests/buttons.
    if update.effective_chat.type != "private":
        return
    state = context.user_data.get("offer_state")
    if not state:
        return

    text = update.message.text.strip()
    uid = update.effective_user.id

    if state == "store_name":
        with db() as con:
            con.execute(
                "INSERT OR REPLACE INTO bookstores(telegram_user_id, store_name, username) VALUES(?,?,?)",
                (uid, text, update.effective_user.username),
            )
        context.user_data["offer_state"] = "price"
        await update.message.reply_text("ما سعر الكتاب؟")
        return

    if state == "price":
        context.user_data["offer_price"] = text
        context.user_data["offer_state"] = "delivery"
        await update.message.reply_text("اكتب عنوان المكتبة أو طريقة التوصيل:")
        return

    if state == "delivery":
        request_id = context.user_data.get("offer_request_id")
        price = context.user_data.get("offer_price")
        with db() as con:
            req = con.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            store = con.execute("SELECT * FROM bookstores WHERE telegram_user_id=?", (uid,)).fetchone()
        if not req or not store:
            await update.message.reply_text("تعذر العثور على الطلب. حاول مرة أخرى.")
            context.user_data.clear()
            return

        contact_url = f"https://t.me/{update.effective_user.username}" if update.effective_user.username else None
        offer_text = (
            f"عرض جديد\n\n"
            f"اسم الكتاب: {req['book_name']}\n"
            f"المكتبة: {store['store_name']}\n"
            f"السعر: {price}\n"
            f"عنوان المكتبة / التوصيل: {text}"
        )
        markup = None
        if contact_url:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("تواصل مع المكتبة", url=contact_url)]])
        else:
            offer_text += "\n\nللتواصل: اطلب من المكتبة إرسال اسم مستخدم تيليجرام أو وسيلة تواصل."

        await context.bot.send_message(req["reader_chat_id"], offer_text, reply_markup=markup)
        await update.message.reply_text("تم إرسال عرضك للقارئ.")
        for k in ["offer_state", "offer_request_id", "offer_price"]:
            context.user_data.pop(k, None)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("تم إلغاء العملية.")
    return ConversationHandler.END


def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    reader_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(new_request, pattern=r"^new_request$")],
        states={
            ASK_BOOK: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_book)],
            ASK_AUTHOR: [
                CallbackQueryHandler(author_unknown, pattern=r"^author_unknown$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_author),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(reader_conv)
    app.add_handler(CallbackQueryHandler(offer_clicked, pattern=r"^offer:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, private_text_router))
    app.add_handler(CommandHandler("cancel", cancel))

    log.info("Matloob Ketab bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
