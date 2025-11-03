# bot.py — MarketSafe (финальная, production-ready версия с оплатой и улучшениями)
# Совместимо с aiogram 3.12.0
import asyncio
import re
import html
import logging
import textwrap
import json
import os
from datetime import datetime, timedelta

import aiohttp
from bs4 import BeautifulSoup

from dotenv import load_dotenv
load_dotenv()

from aiogram.types import Message, ContentType
from aiogram import Bot, Dispatcher, types
from aiogram.client.bot import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import LabeledPrice, PreCheckoutQuery, ContentType

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("❌ Переменная окружения BOT_TOKEN не установлена. Установи её и перезапусти бот.")

# Поставь сюда PROVIDER_TOKEN от YooKassa/CloudPayments/и т.д. (получишь в личном кабинете)
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")  # <-- вставь live_... когда получишь

# Файлы для хранения
PREMIUM_DB_FILE = "premium_users.json"
PAYMENTS_LOG_FILE = "payments.log"

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s | %(message)s"
)
logger = logging.getLogger("marketsafe")

# Отдельный логгер для платежей
payments_logger = logging.getLogger("payments")
if not payments_logger.handlers:
    ph = logging.FileHandler(PAYMENTS_LOG_FILE, encoding="utf-8")
    ph.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    payments_logger.addHandler(ph)
    payments_logger.setLevel(logging.INFO)

# ---------------- INIT ----------------
# parse_mode через DefaultBotProperties — совместимо с aiogram 3.12.0
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher(storage=MemoryStorage())

# ---------------- FSM ----------------
class ClaimForm(StatesGroup):
    fio = State()
    contact = State()
    seller = State()
    order_id = State()
    date = State()
    product = State()
    defect = State()
    demand = State()
    amount = State()

class AIStates(StatesGroup):
    question = State()
    legal = State()

# ---------------- STATIC TEXTS ----------------
RIGHTS_TEXT = {
    "buyer": (
        "🟢 *Права покупателя (кратко):*\n\n"
        "• Право на получение товара надлежащего качества.\n"
        "• Право на информацию о товаре и условиях продажи.\n"
        "• Право на проверку товара до покупки.\n"
        "• Право на возврат и обмен (в сроки, установленные законом).\n"
        "• Право на гарантийное обслуживание и возмещение вреда.\n\n"
        "Если нужно — воспользуйтесь «✍️ Автогенератор претензии»."
    ),
    "seller": (
        "🔵 *Права продавца (кратко):*\n\n"
        "• Право требовать оплату за товар/услугу.\n"
        "• Право требовать принятия товара при соблюдении условий договора.\n"
        "• Право на удержание товара до выполнения обязательств.\n"
        "• Право на возврат товара в случае нарушения условий договора.\n\n"
        "Продавцу полезно сохранять доказательства и вести переписку официально."
    )
}

FAQ_TEXT = (
    "❓ *Частые вопросы:*\n\n"
    "— *Можно ли вернуть без чека?* — Да, если есть другие доказательства (скрин заказа, подтверждение в личном кабинете и т.п.).\n"
    "— *Сколько ждать деньги?* — Обычно до 10 рабочих дней после оформления возврата.\n"
    "— *Продавец не отвечает?* — Составьте претензию, затем жалобу в маркетплейс или в Роспотребнадзор."
)

CONTACTS_TEXT = (
    "☎️ *Полезные контакты:*\n\n"
    "Роспотребнадзор: 8 (800) 555-49-43\n"
    "Госуслуги: раздел «Защита прав потребителей»"
)

EXAMPLE_QUESTIONS = [
    "Как вернуть товар без чека?",
    "Продавец не отвечает на возврат брака",
    "Как составить претензию на маркетплейс?",
    "Что делать, если доставка задержана?",
    "Какие мои права как покупателя?"
]

# ---------------- KEYBOARDS ----------------
def main_menu():
    kb = [
        [
            types.InlineKeyboardButton(text="📦 Сроки доставки", callback_data="menu_delivery"),
            types.InlineKeyboardButton(text="🔁 Возврат и обмен", callback_data="menu_returns"),
        ],
        [
            types.InlineKeyboardButton(text="🛒 Как вернуть товар", callback_data="menu_howtoreturn"),
            types.InlineKeyboardButton(text="✍️ Автогенератор претензии", callback_data="menu_generate_claim"),
        ],
        [
            types.InlineKeyboardButton(text="⚖️ Права покупателя", callback_data="menu_rights_buyer"),
            types.InlineKeyboardButton(text="🏷️ Права продавца", callback_data="menu_rights_seller"),
        ],
        [
            types.InlineKeyboardButton(text="🤖 Задать вопрос (ИИ)", callback_data="menu_ask_ai"),
            types.InlineKeyboardButton(text="📚 Юридический анализ", callback_data="menu_legal_ai"),
        ],
        [
            types.InlineKeyboardButton(text="❓ FAQ", callback_data="menu_faq"),
            types.InlineKeyboardButton(text="☎️ Контакты", callback_data="menu_contacts"),
        ],
        # Новые кнопки монетизации/поддержки
        [
            types.InlineKeyboardButton(text="💎 Premium — 299 ₽", callback_data="menu_buy_premium"),
            types.InlineKeyboardButton(text="☕ Поддержать проект — 100 ₽", callback_data="menu_support"),
        ],
        [
            types.InlineKeyboardButton(text="💼 Консультация — 999 ₽", callback_data="menu_consult"),
        ]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def seller_buttons():
    kb = [
        [types.InlineKeyboardButton(text="Ozon", callback_data="seller_ozon"),
         types.InlineKeyboardButton(text="Wildberries", callback_data="seller_wb")],
        [types.InlineKeyboardButton(text="Yandex.Market", callback_data="seller_yandex")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def ai_input_kb():
    kb = [[types.InlineKeyboardButton(text=q, callback_data=f"example_{i}")] for i, q in enumerate(EXAMPLE_QUESTIONS)]
    kb.append([types.InlineKeyboardButton(text="Отменить", callback_data="ai_cancel")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# ---------------- VALIDATORS ----------------
def validate_date_ddmmyyyy(s: str) -> bool:
    return bool(re.match(r"^\d{2}\.\d{2}\.\d{4}$", s.strip()))

def validate_amount(s: str) -> bool:
    return bool(re.match(r"^\d+$", s.strip()))

def validate_contact(s: str) -> bool:
    s = s.strip()
    email_re = r"^[\w\.-]+@[\w\.-]+\.\w{2,}$"
    phone_re = r"^[\+\d][\d\s\-\(\)]{5,}$"
    return bool(re.match(email_re, s)) or bool(re.match(phone_re, s))

# ---------------- PREMIUM STORAGE ----------------
def load_premium_db():
    try:
        if os.path.exists(PREMIUM_DB_FILE):
            with open(PREMIUM_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as ex:
        logger.exception("Failed to load premium DB: %s", ex)
    return {}

def save_premium_db(data):
    try:
        with open(PREMIUM_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as ex:
        logger.exception("Failed to save premium DB: %s", ex)

def add_premium(user_id: int, days: int = 30):
    db = load_premium_db()
    now = datetime.utcnow()
    expiry = now + timedelta(days=days)
    db[str(user_id)] = {"premium_until": expiry.isoformat()}
    save_premium_db(db)
    logger.info("User %s granted premium until %s", user_id, expiry.isoformat())
    payments_logger.info(f"GRANT_PREMIUM | user={user_id} | until={expiry.isoformat()}")

def has_premium(user_id: int) -> bool:
    db = load_premium_db()
    rec = db.get(str(user_id))
    if not rec:
        return False
    try:
        until = datetime.fromisoformat(rec["premium_until"])
        return datetime.utcnow() < until
    except Exception:
        return False

# ---------------- WEB SEARCH ----------------
async def web_search_snippets(query: str, limit: int = 4, timeout: int = 10):
    """
    Быстрый web-поиск через html.duckduckgo.com, возвращает список (title, snippet, url).
    """
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MarketSafeBot/1.0)"}
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=params, headers=headers, timeout=timeout) as resp:
                text = await resp.text()
    except Exception as ex:
        logger.exception("web_search error: %s", ex)
        return {"error": str(ex), "results": []}

    soup = BeautifulSoup(text, "html.parser")
    elems = soup.select(".result") or soup.select(".results") or soup.select("div")
    for e in elems:
        if len(results) >= limit:
            break
        a = e.select_one("a.result__a") or e.select_one("a")
        title = a.get_text(strip=True) if a else ""
        href = a.get("href") if a and a.get("href") else ""
        sni = e.select_one(".result__snippet")
        snippet = sni.get_text(strip=True) if sni else ""
        if title and href:
            results.append((title, snippet, href))
    if not results:
        for a in soup.select("a")[:limit]:
            t = a.get_text(strip=True)
            h = a.get("href", "")
            if t and h:
                results.append((t, "", h))
    return {"error": None, "results": results[:limit]}

# ---------------- LEGAL ANALYZER ----------------
def legal_analyzer(text: str) -> str:
    t = text.lower()
    mapping = {
        "возврат": ("Возврат товара", "Ст. 25 Закона РФ «О защите прав потребителей»",
                    "Можно вернуть товар надлежащего качества в течение 14 дней, если он не подошёл по форме, габаритам, фасону и т.п."),
        "брак": ("Ненадлежащее качество (брак)", "Ст. 18 Закона РФ «О защите прав потребителей»",
                 "Покупатель вправе требовать замены, ремонта, возврата денег или снижения цены."),
        "доставка": ("Нарушение сроков доставки", "Ст. 23.1 Закона РФ «О защите прав потребителей»",
                     "При нарушении сроков можно требовать неустойку, компенсацию и/или расторжение договора."),
        "гарантия": ("Гарантийный ремонт", "Ст. 20 Закона РФ «О защите прав потребителей»",
                     "Гарантийный ремонт должен быть выполнен в разумный срок (не более установленного законом)."),
        "обмен": ("Обмен товара", "Ст. 24 Закона РФ «О защите прав потребителей»",
                 "При обнаружении брака продавец обязан обменять товар либо вернуть деньги."),
    }
    for key, (title, article, desc) in mapping.items():
        if key in t:
            return f"*{title}*\n{article}\n\n{desc}"
    return ("⚖️ Не удалось однозначно определить применимую норму.\n"
            "Опиши ситуацию подробнее, и я попробую точнее подсказать.")

# ---------------- SMART ANSWER ----------------
async def smart_web_answer(query: str, limit: int = 4):
    data = await web_search_snippets(query, limit=limit)
    if data["error"]:
        return f"⚠️ При попытке поиска произошла ошибка: `{html.escape(data['error'])}`"
    results = data.get("results", [])
    if not results:
        return ("Я не нашёл точной информации по запросу. Попробуй переформулировать вопрос, "
                "уточнив: продавца/маркетплейс, дату покупки, характер проблемы (брак/задержка/несоответствие).")

    pool = " ".join((t + ". " + (s or "")) for t, s, _ in results)
    sentences = re.split(r'(?<=[\.\?\!])\s+', pool)
    summary = " ".join(s.strip() for s in sentences if len(s.strip()) > 40)[:900]
    if not summary:
        summary = results[0][0]

    out = [f"🤖 *Короткий ответ по запросу:* _{html.escape(query)}_\n"]
    out.append(textwrap.fill(summary, width=80))
    out.append("\n*Источники:*")
    for i, (title, _, url) in enumerate(results, start=1):
        safe_title = html.escape(title) if title else "Источник"
        safe_url = html.escape(url) if url else ""
        if safe_url:
            out.append(f"{i}. [{safe_title}]({safe_url})")
        else:
            out.append(f"{i}. {safe_title}")
    out.append("\nℹ️ Проверь источники для деталей. Если нужно — уточни вопрос (добавь дату/магазин/артикул).")
    return "\n\n".join(out)

# ---------------- HANDLERS ----------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome = (
        "👋 *Привет!* Я — *MarketSafe* — помощник по возвратам, претензиям и правам.\n\n"
        "Выбери раздел в меню ниже или напиши /cancel для отмены текущего действия."
    )
    await message.answer(welcome, reply_markup=main_menu())

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Возвращаю в главное меню.", reply_markup=main_menu())

@dp.callback_query()
async def cb_menu_handler(query: types.CallbackQuery, state: FSMContext):
    data = query.data or ""
    try:
        # Основные разделы — без изменений (твоя логика)
        if data == "menu_delivery":
            text = ("📦 *Сроки доставки*\n\n"
                    "- Проверяйте дату доставки в письме и в личном кабинете.\n"
                    "- При нарушении сроков можно требовать компенсацию или возврат.\n\n"
                    "_Пример запроса:_ \"Доставка Ozon задержана 3 дня — что делать?\"")
            await query.message.answer(text, reply_markup=main_menu())

        elif data == "menu_returns":
            text = ("🔁 *Возврат и обмен*\n\n"
                    "- Сохраняйте чек и фото состояния товара.\n"
                    "- Для возврата отправьте претензию продавцу; если откажут — жалоба в Роспотребнадзор.\n\n"
                    "_Пример:_ \"Как вернуть товар, если он не пришёл в комплекте?\"")
            await query.message.answer(text, reply_markup=main_menu())

        elif data == "menu_howtoreturn":
            text = ("🛒 *Как вернуть товар (пошагово):*\n"
                    "1) Свяжитесь с продавцом — чат/почта/телефон.\n"
                    "2) Подготовьте доказательства (фото, чек/скрин заказа, трек).\n"
                    "3) Отправьте претензию с требованием вернуть деньги/заменить товар.\n"
                    "4) Если продавец отказывает — жалоба в маркетплейс и Роспотребнадзор.\n\n"
                    "_Нужна помощь с формулировкой претензии?_ Нажмите «✍️ Автогенератор претензии»")
            await query.message.answer(text, reply_markup=main_menu())

        elif data == "menu_generate_claim":
            await query.message.answer("✍️ Давай составим претензию. Введите, пожалуйста, полное ФИО (например: Иванов Иван Иванович):")
            await state.set_state(ClaimForm.fio)

        elif data == "menu_claim":
            await query.message.answer("✍️ Нужна помощь с претензией? Нажми «✍️ Автогенератор претензии» для пошагового заполнения.", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="⚙️ Автогенератор претензии", callback_data="menu_generate_claim")],
                [types.InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
            ]))

        elif data == "menu_rights_buyer":
            await query.message.answer(RIGHTS_TEXT["buyer"], reply_markup=main_menu())

        elif data == "menu_rights_seller":
            await query.message.answer(RIGHTS_TEXT["seller"], reply_markup=main_menu())

        elif data == "menu_faq":
            await query.message.answer(FAQ_TEXT, reply_markup=main_menu())

        elif data == "menu_contacts":
            await query.message.answer(CONTACTS_TEXT, reply_markup=main_menu())

        # выбор магазина в процессе формы
        elif data.startswith("seller_"):
            seller = data.split("_", 1)[1]
            await state.update_data(seller=seller)
            await state.set_state(ClaimForm.order_id)
            await query.message.answer("Введите номер заказа (или артикул):")

        # AI
        elif data == "menu_ask_ai":
            examples = "\n".join(f"- {q}" for q in EXAMPLE_QUESTIONS)
            await query.message.answer(f"🤖 Задайте вопрос — я поищу и сгенерирую понятный ответ.\n\nПримеры:\n{examples}", reply_markup=ai_input_kb())
            await state.set_state(AIStates.question)

        elif data == "menu_legal_ai":
            examples = "\n".join(f"- {q}" for q in EXAMPLE_QUESTIONS)
            await query.message.answer(f"⚖️ Опишите проблему (например: продавец не вернул деньги за брак).\n\nПримеры:\n{examples}", reply_markup=ai_input_kb())
            await state.set_state(AIStates.legal)

        elif data == "ai_cancel":
            await state.clear()
            await query.message.answer("Отменено. Возвращаю в меню.", reply_markup=main_menu())

        elif data == "menu_main":
            await query.message.answer("Возвращаю в главное меню.", reply_markup=main_menu())

        elif data.startswith("example_"):
            try:
                idx = int(data.split("_")[1])
                qtext = EXAMPLE_QUESTIONS[idx]
                await query.message.answer(f"🔎 Обрабатываю пример: {qtext}")
                is_legal = any(k in qtext.lower() for k in ["закон","статья","возврат","брак","гарантия","обмен","нарушение"])
                if is_legal:
                    legal = legal_analyzer(qtext)
                    web = await smart_web_answer(qtext, limit=3)
                    await query.message.answer(f"{legal}\n\n{web}", disable_web_page_preview=True, reply_markup=main_menu())
                else:
                    ans = await smart_web_answer(qtext, limit=4)
                    await query.message.answer(ans, disable_web_page_preview=True, reply_markup=main_menu())
            except Exception as ex:
                logger.exception("example_ handler error: %s", ex)
                await query.message.answer("Не удалось обработать пример.", reply_markup=main_menu())

        # ---------- Новые пункты: покупки и донаты ----------
        elif data == "menu_buy_premium":
            if PROVIDER_TOKEN == "":
                await query.message.answer("⚠️ Оплата ещё не настроена — ожидаем токен платёжного провайдера. Попробуйте позже.", reply_markup=main_menu())
            else:
                prices = [LabeledPrice(label="Premium — 30 дней", amount=29900)]  # сумма в копейках
                payload = f"premium:{query.from_user.id}"
                await bot.send_invoice(
                    chat_id=query.message.chat.id,
                    title="MarketSafe — Premium 30 дней",
                    description="Расширенные функции: приоритет ответов, расширенные шаблоны претензий.",
                    provider_token=PROVIDER_TOKEN,
                    currency="RUB",
                    prices=prices,
                    start_parameter="premium-subscription",
                    payload=payload
                )

        elif data == "menu_support":
            if PROVIDER_TOKEN == "":
                await query.message.answer("⚠️ Оплата ещё не настроена — ожидаем токен платёжного провайдера. Попробуйте позже.", reply_markup=main_menu())
            else:
                prices = [LabeledPrice(label="Поддержать проект", amount=10000)]  # 100 ₽
                payload = f"support:{query.from_user.id}"
                await bot.send_invoice(
                    chat_id=query.message.chat.id,
                    title="Поддержка MarketSafe",
                    description="Спасибо за поддержку проекта — вы помогаете развитию сервиса.",
                    provider_token=PROVIDER_TOKEN,
                    currency="RUB",
                    prices=prices,
                    start_parameter="donate",
                    payload=payload
                )

        elif data == "menu_consult":
            if PROVIDER_TOKEN == "":
                await query.message.answer("⚠️ Оплата ещё не настроена — ожидаем токен платёжного провайдера. Попробуйте позже.", reply_markup=main_menu())
            else:
                prices = [LabeledPrice(label="Консультация юриста", amount=99900)]  # 999 ₽
                payload = f"consult:{query.from_user.id}"
                await bot.send_invoice(
                    chat_id=query.message.chat.id,
                    title="MarketSafe — Консультация",
                    description="Предварительная оплата консультации. После оплаты с вами свяжется специалист (заглушка).",
                    provider_token=PROVIDER_TOKEN,
                    currency="RUB",
                    prices=prices,
                    start_parameter="consultation",
                    payload=payload
                )

        else:
            await query.message.answer("Раздел временно недоступен.", reply_markup=main_menu())

    except Exception as ex:
        logger.exception("Ошибка в cb_menu_handler: %s", ex)
        await query.message.answer("Произошла ошибка при обработке меню. Попробуй позже.", reply_markup=main_menu())
    finally:
        try:
            await query.answer()
        except Exception:
            pass

# ---------------- CLAIM FORM STEPS ----------------
@dp.message(ClaimForm.fio)
async def step_fio(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if len(text) < 3:
        await message.answer("❌ Введите корректное ФИО (минимум 3 символа).")
        return
    fio_clean = " ".join(w.capitalize() for w in text.split())
    await state.update_data(fio=fio_clean)
    await state.set_state(ClaimForm.contact)
    await message.answer("Введите контакт (телефон или e-mail). Пример: +7 912 123-45-67 или user@example.com")

@dp.message(ClaimForm.contact)
async def step_contact(message: types.Message, state: FSMContext):
    c = message.text.strip()
    if not validate_contact(c):
        await message.answer("❌ Неверный формат контакта. Укажите корректный телефон или email.")
        return
    await state.update_data(contact=c)
    await message.answer("Выберите магазин:", reply_markup=seller_buttons())

@dp.message(ClaimForm.order_id)
async def step_order(message: types.Message, state: FSMContext):
    order = message.text.strip()
    if len(order) < 2:
        await message.answer("❌ Слишком короткий номер заказа. Попробуйте ещё раз.")
        return
    await state.update_data(order_id=order)
    data = await state.get_data()
    seller = data.get("seller", "не указан")
    status = f"Статус: информация о заказе не доступна (симуляция). Магазин: {seller}."
    await state.set_state(ClaimForm.date)
    await message.answer(f"{status}\n\nВведите дату покупки в формате ДД.MM.ГГГГ (например: 25.10.2025)")

@dp.message(ClaimForm.date)
async def step_date(message: types.Message, state: FSMContext):
    if not validate_date_ddmmyyyy(message.text):
        await message.answer("❌ Неверный формат даты. Используйте DD.MM.YYYY")
        return
    await state.update_data(date=message.text.strip())
    await state.set_state(ClaimForm.product)
    await message.answer("Напишите название товара (коротко):")

@dp.message(ClaimForm.product)
async def step_product(message: types.Message, state: FSMContext):
    await state.update_data(product=message.text.strip())
    await state.set_state(ClaimForm.defect)
    await message.answer("Кратко опишите проблему (1–3 предложения):")

@dp.message(ClaimForm.defect)
async def step_defect(message: types.Message, state: FSMContext):
    await state.update_data(defect=message.text.strip())
    await state.set_state(ClaimForm.demand)
    await message.answer("Что вы требуете? (возврат / обмен / ремонт / компенсация)")

@dp.message(ClaimForm.demand)
async def step_demand(message: types.Message, state: FSMContext):
    d = message.text.strip()
    await state.update_data(demand=d)
    await state.set_state(ClaimForm.amount)
    await message.answer("Укажите сумму к возврату (только цифры, 0 если нет):")

@dp.message(ClaimForm.amount)
async def step_amount(message: types.Message, state: FSMContext):
    amount_text = message.text.strip()
    if not validate_amount(amount_text):
        await message.answer("❌ Сумма должна содержать только цифры (например: 0 или 1500).")
        return
    await state.update_data(amount=amount_text)
    data = await state.get_data()

    seller_name = html.escape(data.get("seller", "Продавец"))
    fio = html.escape(data.get("fio", ""))
    contact = html.escape(data.get("contact", ""))
    order_id = html.escape(data.get("order_id", ""))
    date = html.escape(data.get("date", ""))
    product = html.escape(data.get("product", ""))
    defect = html.escape(data.get("defect", ""))
    demand = html.escape(data.get("demand", ""))
    amount = html.escape(data.get("amount", ""))

    claim_text = (
        f"📄 *Претензия продавцу*\n\n"
        f"*Кому:* {seller_name}\n"
        f"*От:* {fio} ({contact})\n"
        f"*Заказ №:* {order_id} от {date}\n\n"
        f"*Товар:* {product}\n"
        f"*Описание проблемы:* {defect}\n"
        f"*Требование:* {demand}\n"
        f"*Сумма к возврату:* {amount} руб.\n\n"
        f"Дата составления: {datetime.now().strftime('%d.%m.%Y')}\n\n"
        f"Прошу удовлетворить требования в соответствии с законом о защите прав потребителей."
    )

    await message.answer(claim_text)
    await state.clear()
    await message.answer("Готово — претензия сформирована. Возвращаю в главное меню.", reply_markup=main_menu())

# ---------------- AI HANDLERS ----------------
@dp.message(AIStates.question)
async def ai_question_handler(message: types.Message, state: FSMContext):
    q = message.text.strip()
    if not q:
        await message.answer("Пустой запрос. Напишите, пожалуйста, вопрос.")
        return
    # Пример: проверка премиума для приоритета
    if has_premium(message.from_user.id):
        await message.answer("🔎 (Premium) Ищу информацию с приоритетом...")
    else:
        await message.answer("🔎 Ищу информацию... (это может занять несколько секунд)")
    try:
        answer = await smart_web_answer(q, limit=4)
        await message.answer(answer, disable_web_page_preview=True, reply_markup=main_menu())
    except Exception as ex:
        logger.exception("AI search error: %s", ex)
        await message.answer("⚠️ Произошла ошибка при поиске. Попробуйте позже.", reply_markup=main_menu())
    finally:
        await state.clear()

@dp.message(AIStates.legal)
async def ai_legal_handler(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if not text:
        await message.answer("Опишите проблему, пожалуйста.")
        return
    if has_premium(message.from_user.id):
        await message.answer("⚖️ (Premium) Анализирую юридическую сторону... ⏳")
    else:
        await message.answer("⚖️ Анализирую юридическую сторону... ⏳")
    try:
        legal = legal_analyzer(text)
        web = await smart_web_answer(text, limit=3)
        combined = f"{legal}\n\n{web}"
        await message.answer(combined, disable_web_page_preview=True, reply_markup=main_menu())
    except Exception as ex:
        logger.exception("Legal AI error: %s", ex)
        await message.answer("⚠️ Ошибка при анализе. Попробуйте позже.", reply_markup=main_menu())
    finally:
        await state.clear()

# ---------------- PAYMENTS HANDLERS ----------------
@dp.pre_checkout_query()
async def pre_checkout(pre_checkout: PreCheckoutQuery):
    # Всегда подтверждаем pre_checkout (если нужно — можно проверить payload)
    try:
        await pre_checkout.answer(ok=True)
    except Exception as ex:
        logger.exception("pre_checkout error: %s", ex)

from aiogram import F

@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: Message):
    # 💰 Обработка успешной оплаты
    payment_info = message.successful_payment
    await message.answer(
        f"✅ Оплата прошла успешно!\n"
        f"Сумма: {payment_info.total_amount / 100:.2f} {payment_info.currency}\n"
        f"Спасибо за поддержку проекта MarketSafe ❤️"
    )

    try:
        sp = message.successful_payment
        payload = getattr(sp, "invoice_payload", "")
        from_user = message.from_user
        payments_logger.info(f"SUCCESS_PAYMENT | user={from_user.id} | payload={payload} | provider_payment_charge_id={sp.provider_payment_charge_id if hasattr(sp, 'provider_payment_charge_id') else ''} | total={sp.total_amount if hasattr(sp, 'total_amount') else ''}")

        # payload format: "premium:<user_id>" or "support:<user_id>" or "consult:<user_id>"
        if payload and ":" in payload:
            typ, uid_str = payload.split(":", 1)
            try:
                uid = int(uid_str)
            except ValueError:
                uid = from_user.id

            if typ == "premium":
                add_premium(uid, days=30)
                await message.answer("✅ Оплата подтверждена. Вам выдан Premium на 30 дней. Спасибо за поддержку!", reply_markup=main_menu())
            elif typ == "support":
                await message.answer("☕ Спасибо за поддержку проекта! Ваш вклад очень важен.", reply_markup=main_menu())
            elif typ == "consult":
                # заглушка: пометим, что пользователь оплатил консультацию
                payments_logger.info(f"CONSULT_PAID | user={uid}")
                await message.answer("✅ Оплата за консультацию получена. С вами свяжется наш специалист (заглушка).", reply_markup=main_menu())
            else:
                await message.answer("✅ Оплата получена. Спасибо!", reply_markup=main_menu())
        else:
            await message.answer("✅ Оплата получена. Спасибо!", reply_markup=main_menu())

    except Exception as ex:
        logger.exception("successful_payment handler error: %s", ex)
        try:
            await message.answer("⚠️ Оплата зарегистрирована, но произошла внутренняя ошибка — свяжись с разработчиком.", reply_markup=main_menu())
        except Exception:
            pass

# ---------------- SMART WEB ANSWER WRAPPER ----------------
# у тебя были две версии; оставляем одну корректную
async def smart_web_answer_impl(query: str, limit: int = 4):
    res = await web_search_snippets(query, limit=limit)
    if res["error"]:
        return f"⚠️ Ошибка сети при поиске: `{html.escape(res['error'])}`"
    items = res.get("results", [])
    if not items:
        short_q = " ".join(w for w in query.split() if len(w) > 2)
        if short_q != query:
            res2 = await web_search_snippets(short_q, limit=limit)
            items = res2.get("results", []) if not res2.get("error") else []
    if not items:
        return ("Я не нашёл релевантной информации. Попробуйте переформулировать вопрос:\n"
                "- уточнить продавца/маркетплейс\n- указать даты/артикул\n- описать проблему короче и точнее.")
    pool = " ".join((t + ". " + (s or "")) for t, s, _ in items)
    sentences = re.split(r'(?<=[\.\?\!])\s+', pool)
    summary = " ".join(s.strip() for s in sentences if len(s.strip()) > 40)[:800]
    if not summary:
        summary = items[0][0]
    out_lines = [f"🤖 *Краткий ответ по запросу:* _{html.escape(query)}_\n"]
    out_lines.append(textwrap.fill(summary, width=80))
    out_lines.append("\n*Источники:*")
    for i, (title, _, url) in enumerate(items, 1):
        safe_title = html.escape(title) if title else "Источник"
        safe_url = html.escape(url) if url else ""
        if safe_url:
            out_lines.append(f"{i}. [{safe_title}]({safe_url})")
        else:
            out_lines.append(f"{i}. {safe_title}")
    out_lines.append("\nℹ️ Для уточнения добавьте продавца, дату покупки или артикул.")
    return "\n\n".join(out_lines)

async def smart_web_answer(query: str, limit: int = 4):
    return await smart_web_answer_impl(query, limit)

# ---------------- ERRORS ----------------
@dp.errors()
async def global_error_handler(update, exception):
    logger.exception("Unhandled exception: %s", exception)
    return True

# ---------------- RUN & AUTO-RESTART ----------------
async def run_bot():
    """
    Запускает polling в цикле с автоперезапуском при исключениях.
    Нужен только если ты хочешь, чтобы бот пытался восстанавливаться при падениях.
    """
    backoff = 1
    max_backoff = 30
    while True:
        try:
            logger.info("✅ MarketSafe bot starting polling...")
            await dp.start_polling(bot)
            # если start_polling завершился корректно — выходим
            break
        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot stopped by user/system.")
            break
        except Exception as e:
            logger.exception("Critical error in polling: %s", e)
            logger.info("Restarting polling in %s seconds...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(max_backoff, backoff * 2)
        finally:
            # Попытка корректно закрыть сессии после ошибки
            try:
                await bot.session.close()
            except Exception:
                pass

async def main():
    try:
        await run_bot()
    finally:
        # graceful shutdown: закрываем сессии и storage если возможно
        try:
            await bot.session.close()
        except Exception:
            pass
        try:
            await dp.storage.close()
            await dp.storage.wait_closed()
        except Exception:
            pass
        logger.info("🛑 Bot shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped manually.")
