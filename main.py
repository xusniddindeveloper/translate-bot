import asyncio
import logging
import os
import re
import sys

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from deep_translator.exceptions import TooManyRequests
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web
from deep_translator import GoogleTranslator

from config import BOT_TOKEN

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ---------- Sozlamalar ----------

# callback kodi -> (google kodi, ko'rinadigan nomi)
LANGUAGES = {
    "ru": ("ru", "🇷🇺 Rus tili"),
    "en": ("en", "🇬🇧 Ingliz tili"),
    "zh": ("zh-CN", "🇨🇳 Xitoy tili"),
}

# Taqiqlangan so'zlar (kerak bo'lsa ro'yxatni kengaytiring)
BAD_WORDS = {
    "fuck", "shit", "bitch", "asshole", "bastard", "dick",
    "suka", "blyat", "blat", "nahuy", "pidor", "pizda",
    "axmoq", "ahmoq", "tentak", "jindir", "jindi", "sik", "sikaman", "qotoq",
    "onangni", "ammangni", "iflos",
}

MAX_TEXT_LENGTH = 4500

MSG_BAD_COMMAND = "⚠️ Noto'g'ri buyruq kiritildi. Iltimos, to'g'ri gap yoki matn kiriting!"
MSG_BAD_WORDS = "⚠️ Iltimos to'g'ri gap kiriting! Odobsiz so'zlardan foydalanmang."
MSG_NO_LANGUAGE = "⚠️ Avval bosh menyudan tarjima tilini tanlang! /start buyrug'ini bosing."


# ---------- FSM holatlari ----------

class TranslationState(StatesGroup):
    waiting_for_language = State()
    translating = State()


dp = Dispatcher(storage=MemoryStorage())


# ---------- Yordamchi funksiyalar ----------

def language_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=name, callback_data=f"lang:{key}")
        for key, (_, name) in LANGUAGES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons])


def contains_bad_words(text: str) -> bool:
    words = re.findall(r"\w+", text.lower())
    return any(word in BAD_WORDS for word in words)


from deep_translator import MyMemoryTranslator

MYMEMORY_CODES = {"ru": "ru-RU", "en": "en-GB", "zh-CN": "zh-CN"}


def translate_text(text: str, target: str) -> str | None:
    # 1) Google
    try:
        return GoogleTranslator(source="auto", target=target).translate(text)
    except Exception as e:
        logger.warning("Google ishlamadi (%s), MyMemory ishlatiladi", type(e).__name__)

    # 2) Zaxira: MyMemory
    return MyMemoryTranslator(
        source="auto", target=MYMEMORY_CODES.get(target, target)
    ).translate(text)

# ---------- Handlerlar ----------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(TranslationState.waiting_for_language)
    await message.answer(
        f"👋 Salom, <b>{html.quote(message.from_user.full_name)}</b>!\n\n"
        "Men tarjima botman. Qaysi tilga tarjima qilishni xohlaysiz?\n"
        "👇 Tilni tanlang:",
        reply_markup=language_keyboard(),
    )


@dp.callback_query(F.data.startswith("lang:"))
async def choose_language(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key not in LANGUAGES:
        await callback.answer("Noma'lum til!", show_alert=True)
        return

    code, name = LANGUAGES[key]
    await state.set_state(TranslationState.translating)
    await state.update_data(target_lang=code, target_name=name)

    await callback.message.edit_text(
        f"✅ Til tanlandi: <b>{name}</b>\n\n"
        "Endi istalgan matnni yozing, men uni shu tilga tarjima qilaman.\n"
        "🔄 Tilni o'zgartirish uchun /start ni bosing."
    )
    await callback.answer()


# /start dan boshqa barcha buyruqlar -> xato
@dp.message(F.text.startswith("/"))
async def unknown_command(message: Message):
    await message.answer(MSG_BAD_COMMAND)


# Matnli xabarlar -> tarjima
@dp.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target_lang")

    # 1) Til tanlanmagan
    if not target:
        await message.answer(MSG_NO_LANGUAGE)
        return

    text = message.text.strip()

    # 2) Odobsiz so'zlar
    if contains_bad_words(text):
        await message.answer(MSG_BAD_WORDS)
        return

    # 3) Uzunlik tekshiruvi
    if len(text) > MAX_TEXT_LENGTH:
        await message.answer(
            f"⚠️ Matn juda uzun. Iltimos, {MAX_TEXT_LENGTH} belgidan oshmasin."
        )
        return

    # 4) Tarjima
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        result = await asyncio.to_thread(translate_text, text, target)
    except TooManyRequests:
        await message.answer("⏳ Hozir so'rovlar ko'p. 1-2 daqiqadan so'ng qayta urinib ko'ring.")
        return

    if not result:
        await message.answer("❌ Tarjima qilib bo'lmadi. Boshqa matn kiriting.")
        return

    await message.answer(
        f"🌐 <b>Tarjima</b> ({data.get('target_name')}):\n\n"
        f"<blockquote>{html.quote(result)}</blockquote>\n"
        f"📝 <i>Asl matn:</i>\n<code>{html.quote(text)}</code>"
    )


# Matn bo'lmagan xabarlar (rasm, stiker va h.k.)
@dp.message()
async def handle_other(message: Message):
    await message.answer("⚠️ Iltimos, faqat matn yuboring!")


# ---------- Render uchun mini veb-server ----------

async def health(_: web.Request) -> web.Response:
    return web.Response(text="Bot ishlayapti ✅")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Veb-server %s portda ishga tushdi", port)


# ---------- Ishga tushirish ----------

async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.delete_webhook(drop_pending_updates=True)
    await start_web_server()
    logger.info("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi")