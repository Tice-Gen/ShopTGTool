import hashlib
import logging
import os
import threading
import time
from html import escape

import requests
import telebot
from flask import Flask, jsonify, request
from sqlalchemy import BigInteger, Boolean, Column, Float, ForeignKey, Integer, String, UniqueConstraint, create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


BTN_INCOME = "💰 Мой доход"
BTN_LIST_ITEMS = "📦 Список товаров"
BTN_ADD_ITEM = "➕ Добавить товар"
BTN_DELETE_ITEM = "❌ Удалить товар"
BTN_BTC_RATE = "⚙️ Курс BTC"
BTN_LOGOUT = "🚪 Выйти"
BTN_START = "🗒 START"
BTN_FORGOT_PASSWORD = "🤷‍♂️ Забыл пароль"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_database_url(raw_url: str) -> str:
    if raw_url.startswith("postgresql+"):
        return raw_url
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql+psycopg://", 1)
    if raw_url.startswith("postgresql://"):
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw_url


BOT_TOKEN = require_env("BOT_TOKEN")
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", os.getenv("DB_NAME", "shop_data.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram/webhook").strip() or "/telegram/webhook"
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = f"/{WEBHOOK_PATH}"


def get_webhook_base_url() -> str:
    explicit = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit

    koyeb_domain = os.getenv("KOYEB_PUBLIC_DOMAIN", "").strip()
    if koyeb_domain:
        return f"https://{koyeb_domain}"

    return ""


WEBHOOK_BASE_URL = get_webhook_base_url()
APP_MODE = os.getenv("APP_MODE", "webhook" if WEBHOOK_BASE_URL else "polling").strip().lower()
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_SECRET = (
    os.getenv("WEBHOOK_SECRET", "").strip()
    or hashlib.sha256(f"{BOT_TOKEN}:webhook".encode("utf-8")).hexdigest()
)

if DATABASE_URL:
    ENGINE = create_engine(
        normalize_database_url(DATABASE_URL),
        future=True,
        pool_pre_ping=True,
    )
else:
    ENGINE = create_engine(
        f"sqlite:///{SQLITE_DB_PATH}",
        future=True,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False},
    )

SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, expire_on_commit=False, future=True)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    user_id = Column(BigInteger, primary_key=True)
    username = Column(String)
    password = Column(String)
    btc_rate = Column(Float, default=0.0, nullable=False)
    balance_rub = Column(Float, default=0.0, nullable=False)
    is_logged_in = Column(Boolean, default=False, nullable=False)

    items = relationship("Item", back_populates="user")


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_items_user_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    name = Column(String, nullable=False)
    price = Column(Float, nullable=False)

    user = relationship("User", back_populates="items")


def init_db() -> None:
    Base.metadata.create_all(bind=ENGINE)


def session() -> Session:
    return SessionLocal()


bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)


BTC_CACHE = {
    "rate": None,
    "updated_at": 0.0,
}
BTC_CACHE_TTL = 300

_webhook_ready = False
_webhook_lock = threading.Lock()


def main_menu_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        BTN_INCOME,
        BTN_LIST_ITEMS,
        BTN_ADD_ITEM,
        BTN_DELETE_ITEM,
        BTN_BTC_RATE,
        BTN_LOGOUT,
    )
    return markup


def logged_out_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(BTN_START, BTN_FORGOT_PASSWORD)
    return markup


def get_btc_rate_cached():
    now = time.time()

    if BTC_CACHE["rate"] and now - BTC_CACHE["updated_at"] < BTC_CACHE_TTL:
        return BTC_CACHE["rate"], False

    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "rub"},
            timeout=5,
        )
        response.raise_for_status()
        rate = float(response.json()["bitcoin"]["rub"])

        BTC_CACHE["rate"] = rate
        BTC_CACHE["updated_at"] = now
        return rate, True
    except Exception:
        logger.exception("BTC API request failed")
        return None, False


def update_user_btc_rate(user_id: int):
    rate, updated = get_btc_rate_cached()
    if rate is None:
        return None, False

    with SessionLocal.begin() as db:
        user = db.get(User, user_id)
        if user:
            user.btc_rate = rate

    return rate, updated


def is_logged_in(user_id: int) -> bool:
    with session() as db:
        user = db.get(User, user_id)
        return bool(user and user.is_logged_in)


def logout_user(user_id: int) -> None:
    with SessionLocal.begin() as db:
        user = db.get(User, user_id)
        if user:
            user.is_logged_in = False


def ensure_webhook() -> None:
    global _webhook_ready

    if APP_MODE != "webhook" or _webhook_ready:
        return

    with _webhook_lock:
        if _webhook_ready:
            return

        webhook_base_url = get_webhook_base_url()
        if not webhook_base_url:
            raise RuntimeError(
                "Webhook mode requires WEBHOOK_BASE_URL or KOYEB_PUBLIC_DOMAIN."
            )

        desired_url = f"{webhook_base_url}{WEBHOOK_PATH}"
        current_info = bot.get_webhook_info()

        if current_info.url != desired_url:
            logger.info("Updating Telegram webhook to %s", desired_url)
            bot.remove_webhook()
            bot.set_webhook(
                url=desired_url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=["message"],
                drop_pending_updates=False,
            )
        else:
            logger.info("Telegram webhook already configured: %s", desired_url)

        _webhook_ready = True


@app.before_request
def bootstrap_webhook():
    if APP_MODE == "webhook":
        ensure_webhook()


@app.get("/")
def index():
    return jsonify(
        {
            "ok": True,
            "service": "telegram-shop-bot",
            "mode": APP_MODE,
            "storage": "postgres" if DATABASE_URL else "sqlite",
        }
    )


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "mode": APP_MODE})


@app.post(WEBHOOK_PATH)
def telegram_webhook():
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header_secret != WEBHOOK_SECRET:
        return ("forbidden", 403)

    payload = request.get_json(silent=True)
    if not payload:
        return ("bad request", 400)

    update = telebot.types.Update.de_json(payload)
    bot.process_new_updates([update])
    return ("ok", 200)


@bot.message_handler(commands=["start"])
def start(message):
    with session() as db:
        user = db.get(User, message.from_user.id)

    if user and user.is_logged_in:
        bot.send_message(message.chat.id, "Вы уже вошли.", reply_markup=main_menu_keyboard())
    elif user:
        msg = bot.send_message(
            message.chat.id,
            "Введите пароль:\n\nЕсли не помните пароль, нажмите 🤷‍♂️ Забыл пароль",
            reply_markup=logged_out_keyboard(),
        )
        bot.register_next_step_handler(msg, process_login)
    else:
        msg = bot.send_message(
            message.chat.id,
            "Введите имя:",
            reply_markup=logged_out_keyboard(),
        )
        bot.register_next_step_handler(msg, process_register_name)


def process_register_name(message):
    text = (message.text or "").strip()

    if text in (BTN_START, "/start"):
        start(message)
        return
    if text == BTN_FORGOT_PASSWORD:
        forgot_password(message)
        return
    if not text:
        msg = bot.send_message(message.chat.id, "Имя не должно быть пустым. Введите имя ещё раз:")
        bot.register_next_step_handler(msg, process_register_name)
        return

    msg = bot.send_message(message.chat.id, "Введите пароль:")
    bot.register_next_step_handler(msg, process_register_password, text)


def process_register_password(message, name):
    password = (message.text or "").strip()
    if not password:
        msg = bot.send_message(message.chat.id, "Пароль не должен быть пустым. Введите пароль ещё раз:")
        bot.register_next_step_handler(msg, process_register_password, name)
        return

    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    with SessionLocal.begin() as db:
        user = db.get(User, message.from_user.id)
        if user is None:
            user = User(
                user_id=message.from_user.id,
                username=name,
                password=password,
                is_logged_in=True,
            )
            db.add(user)
        else:
            user.username = name
            user.password = password
            user.is_logged_in = True

    bot.send_message(
        message.chat.id,
        "Регистрация завершена.",
        reply_markup=main_menu_keyboard(),
    )


def process_login(message):
    text = (message.text or "").strip()

    if text in (BTN_START, "/start"):
        start(message)
        return
    if text == BTN_FORGOT_PASSWORD:
        forgot_password(message)
        return
    if not text:
        msg = bot.send_message(
            message.chat.id,
            "Пароль не должен быть пустым. Введите пароль ещё раз:",
            reply_markup=logged_out_keyboard(),
        )
        bot.register_next_step_handler(msg, process_login)
        return

    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    with SessionLocal.begin() as db:
        user = db.get(User, message.from_user.id)

        if user and user.password == text:
            user.is_logged_in = True
            success = True
        else:
            success = False

    if success:
        bot.send_message(message.chat.id, "Вход выполнен.", reply_markup=main_menu_keyboard())
    else:
        msg = bot.send_message(
            message.chat.id,
            "Неверный пароль, попробуйте снова или нажмите 🤷‍♂️ Забыл пароль",
            reply_markup=logged_out_keyboard(),
        )
        bot.register_next_step_handler(msg, process_login)


@bot.message_handler(func=lambda m: m.text == BTN_BTC_RATE)
def btc_rate(message):
    if not is_logged_in(message.from_user.id):
        return

    rate, updated = update_user_btc_rate(message.from_user.id)
    if rate is None:
        bot.send_message(message.chat.id, "❌ Не удалось получить курс BTC.")
        return

    status = "🔄 обновлён" if updated else "📦 из кеша"
    bot.send_message(
        message.chat.id,
        f"🪙 <b>Bitcoin</b>\n1 BTC = <b>{rate:,.2f} RUB</b>\n{status}",
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda m: m.text == BTN_INCOME)
def income(message):
    if not is_logged_in(message.from_user.id):
        return

    update_user_btc_rate(message.from_user.id)

    with session() as db:
        user = db.get(User, message.from_user.id)

    if not user:
        bot.send_message(message.chat.id, "Ошибка: аккаунт не найден.")
        return

    balance = float(user.balance_rub or 0)
    rate = float(user.btc_rate or 0)
    btc_total = balance / rate if rate > 0 else 0

    bot.send_message(
        message.chat.id,
        f"💵 Баланс: {balance:.2f} RUB\n"
        f"📈 Курс BTC: {rate:.2f} RUB\n"
        f"🪙 Итого: <b>{btc_total:.6f} BTC</b>",
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda m: m.text == BTN_LIST_ITEMS)
def list_items(message):
    if not is_logged_in(message.from_user.id):
        return

    with session() as db:
        items = db.execute(
            select(Item).where(Item.user_id == message.from_user.id).order_by(Item.id)
        ).scalars().all()

    if not items:
        bot.send_message(message.chat.id, "Список товаров пуст.")
        return

    lines = ["Ваши товары:"]
    for item in items:
        lines.append(f"🔹 <b>{escape(item.name)}</b> — {item.price:.2f} RUB")
    lines.append("")
    lines.append("<i>Для продажи напишите +Название, для возврата -Название</i>")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")


@bot.message_handler(func=lambda m: m.text == BTN_ADD_ITEM)
def add_item_step1(message):
    if not is_logged_in(message.from_user.id):
        return

    msg = bot.send_message(message.chat.id, "Введите название товара (например: g2):")
    bot.register_next_step_handler(msg, add_item_step2)


def add_item_step2(message):
    item_name = (message.text or "").strip()
    if not item_name:
        msg = bot.send_message(message.chat.id, "Название не должно быть пустым. Введите название ещё раз:")
        bot.register_next_step_handler(msg, add_item_step2)
        return

    msg = bot.send_message(message.chat.id, f"Введите цену для '{item_name}' в рублях:")
    bot.register_next_step_handler(msg, add_item_step3, item_name)


def add_item_step3(message, item_name):
    try:
        price = float((message.text or "").replace(",", "."))
    except ValueError:
        bot.send_message(
            message.chat.id,
            "❌ Ошибка! Цена должна быть числом. Попробуйте добавить товар заново.",
        )
        return

    try:
        with SessionLocal.begin() as db:
            db.add(Item(user_id=message.from_user.id, name=item_name, price=price))
    except IntegrityError:
        bot.send_message(
            message.chat.id,
            f"❌ Товар '{item_name}' уже существует.",
        )
        return

    bot.send_message(
        message.chat.id,
        f"✅ Товар '{item_name}' добавлен с ценой {price:.2f} RUB.",
    )


@bot.message_handler(func=lambda m: m.text == BTN_DELETE_ITEM)
def delete_item_step1(message):
    if not is_logged_in(message.from_user.id):
        return

    msg = bot.send_message(message.chat.id, "Введите название товара для удаления:")
    bot.register_next_step_handler(msg, delete_item_step2)


def delete_item_step2(message):
    item_name = (message.text or "").strip()
    if not item_name:
        msg = bot.send_message(message.chat.id, "Название не должно быть пустым. Введите название ещё раз:")
        bot.register_next_step_handler(msg, delete_item_step2)
        return

    with SessionLocal.begin() as db:
        result = db.execute(
            delete(Item).where(
                Item.user_id == message.from_user.id,
                Item.name == item_name,
            )
        )

    if result.rowcount > 0:
        bot.send_message(message.chat.id, f"🗑 Товар '{item_name}' удалён.")
    else:
        bot.send_message(message.chat.id, f"❌ Товар '{item_name}' не найден.")


@bot.message_handler(
    func=lambda message: isinstance(message.text, str)
    and (message.text.startswith("+") or message.text.startswith("-"))
)
def handle_transaction(message):
    if not is_logged_in(message.from_user.id):
        return

    operation = message.text[0]
    item_name = message.text[1:].strip()

    if not item_name:
        bot.send_message(message.chat.id, "❌ После знака + или - нужно указать название товара.")
        return

    with SessionLocal.begin() as db:
        item = db.execute(
            select(Item).where(
                Item.user_id == message.from_user.id,
                Item.name == item_name,
            )
        ).scalar_one_or_none()

        user = db.get(User, message.from_user.id)

        if not item or not user:
            item_exists = False
            new_balance = None
            price = None
        else:
            price = float(item.price)
            if operation == "+":
                user.balance_rub += price
                action_text = "продажа"
            else:
                user.balance_rub -= price
                action_text = "возврат"

            new_balance = float(user.balance_rub)
            item_exists = True

    if item_exists:
        bot.send_message(
            message.chat.id,
            f"✅ Записано: {action_text} <b>{escape(item_name)}</b> ({price:.2f} RUB).\n"
            f"Текущий баланс: {new_balance:.2f} RUB",
            parse_mode="HTML",
        )
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Товар '{item_name}' не найден в базе. Сначала добавьте его через меню.",
        )


@bot.message_handler(func=lambda m: m.text == BTN_LOGOUT)
def logout(message):
    logout_user(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "Вы вышли из аккаунта.",
        reply_markup=logged_out_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == BTN_START)
def start_via_button(message):
    start(message)


@bot.message_handler(func=lambda m: m.text == BTN_FORGOT_PASSWORD)
def forgot_password(message):
    msg = bot.send_message(
        message.chat.id,
        "Введите новый пароль — он станет паролем вашего аккаунта:",
        reply_markup=logged_out_keyboard(),
    )
    bot.register_next_step_handler(msg, process_reset_password)


def process_reset_password(message):
    new_password = (message.text or "").strip()
    if not new_password:
        msg = bot.send_message(message.chat.id, "Пароль не должен быть пустым. Введите новый пароль ещё раз:")
        bot.register_next_step_handler(msg, process_reset_password)
        return

    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    with SessionLocal.begin() as db:
        user = db.get(User, message.from_user.id)
        if not user:
            exists = False
        else:
            user.password = new_password
            user.is_logged_in = False
            exists = True

    if not exists:
        bot.send_message(
            message.chat.id,
            "Аккаунт не найден. Нажмите 🗒 START, чтобы зарегистрироваться.",
            reply_markup=logged_out_keyboard(),
        )
        return

    bot.send_message(
        message.chat.id,
        "✅ Пароль обновлён. Нажмите 🗒 START, чтобы войти.",
        reply_markup=logged_out_keyboard(),
    )


@bot.message_handler(func=lambda m: (m.text is not None) and not is_logged_in(m.from_user.id))
def fallback_not_logged_in(message):
    text = (message.text or "").strip()
    if text in ("/start", BTN_START, BTN_FORGOT_PASSWORD):
        return

    bot.send_message(
        message.chat.id,
        "Вы не вошли в аккаунт.\n"
        "Нажмите 🗒 START, чтобы войти или зарегистрироваться, "
        "или 🤷‍♂️ Забыл пароль, чтобы сбросить пароль.",
        reply_markup=logged_out_keyboard(),
    )


def main():
    init_db()
    logger.info("Bot started in %s mode", APP_MODE)

    if APP_MODE == "webhook":
        logger.info("Running development webhook server on 0.0.0.0:%s", PORT)
        app.run(host="0.0.0.0", port=PORT)
        return

    bot.remove_webhook()
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


init_db()


if __name__ == "__main__":
    main()
