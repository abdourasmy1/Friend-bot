
import os
import json
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from openai import AsyncOpenAI
from supabase import create_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sagil")

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

db = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
ai = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)

SYSTEM = """
أنت سجيل، صاحب من الشلة، بتتكلم بالمصري الطبيعي.
خليك خفيف الدم، تلقائي، مش رسمي ومش روبوت.
افهم الأسماء والألقاب من سياق الكلام، لكن لا تخترع أسماء
أو معلومات عن الناس. لو فيه خلاف، ابدأ بهزار لطيف لو مناسب،
ولو الموضوع جدي، هدي الجو وساعدهم يتصالحوا من غير إهانة.
لا تدّعي أنك أرسلت رسالة إلا بعد نجاح إرسالها فعلاً.

أجب دائماً بصيغة JSON صحيحة بالشكل:
{"kind":"chat","reply":"ردك بالمصري","recipient":"","message":""}

kind:
- chat: رد عادي.
- send: المستخدم يطلب توصيل رسالة لشخص.
- ignore: الرسالة لا تحتاج رداً.

عند طلب توصيل رسالة، استخرج اسم المستلم ونص الرسالة.
لو التفاصيل ناقصة، اسأل المستخدم عنها بدلاً من التخمين.
لا تطلب إرسال رسائل تهديد أو ابتزاز أو مضايقة.
"""

def rows(table, **kwargs):
    return db.table(table).select("*").match(kwargs).execute().data

def save_user(user):
    name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    )
    existing = rows("bot_users", telegram_id=user.id)
    data = {
        "telegram_id": user.id,
        "display_name": name or user.username or str(user.id),
        "username": user.username,
    }
    if not existing:
        data["private_chat_started"] = False
        data["aliases"] = []
        data["private_memory"] = {}
        db.table("bot_users").insert(data).execute()
    else:
        db.table("bot_users").update({
            "display_name": data["display_name"],
            "username": user.username,
        }).eq("telegram_id", user.id).execute()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)
    db.table("bot_users").update({
        "private_chat_started": True
    }).eq("telegram_id", user.id).execute()
    await update.message.reply_text(
        "أهلاً يا نجم! أنا سجيل صاحبك 😎\n"
        "كلمني بالمصري عادي، ولو عايزني أوصل رسالة لحد "
        "هطلب موافقتك الأول.\n"
        "اكتب /forget لو عايز تمسح سجل محادثتك معايا."
    )

async def forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.table("conversation_memory").delete().eq(
        "telegram_id", user.id
    ).execute()
    db.table("bot_users").update({
        "private_memory": {}
    }).eq("telegram_id", user.id).execute()
    await update.message.reply_text(
        "تمام، مسحت سجل محادثاتك المحفوظ عندي."
    )

async def handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not message.text or not user or user.is_bot:
        return

    # في الجروبات، لا يرد إلا عند مناداته أو الرد على رسالة منه.
    if chat.type in ("group", "supergroup"):
        me = await context.bot.get_me()
        text_lower = message.text.lower()
        mentioned = (
            (me.username and ("@" + me.username.lower()) in text_lower)
            or "سجيل" in message.text
            or (
                message.reply_to_message
                and message.reply_to_message.from_user
                and message.reply_to_message.from_user.id == me.id
            )
        )
        if not mentioned:
            return

        db.table("group_members").upsert({
            "chat_id": chat.id,
            "telegram_id": user.id,
        }).execute()

    save_user(user)

    # الذاكرة منفصلة لكل مستخدم داخل كل محادثة.
    history = db.table("conversation_memory").select(
        "role,content"
    ).eq("chat_id", chat.id).eq(
        "telegram_id", user.id
    ).order("created_at", desc=True).limit(12).execute().data

    history = list(reversed(history))
    history.append({"role": "user", "content": message.text})

    try:
        response = await ai.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM},
                *history,
            ],
            response_format={"type": "json_object"},
            temperature=0.8,
        )
        result = json.loads(response.choices[0].message.content)
    except Exception:
        log.exception("AI request failed")
        await message.reply_text(
            "معلش يا صاحبي، مخي علّق لحظة 😅 جرّب تاني."
        )
        return

    kind = result.get("kind", "chat")
    reply = str(result.get("reply", "")).strip()
    recipient = str(result.get("recipient", "")).strip()
    outgoing = str(result.get("message", "")).strip()

    db.table("conversation_memory").insert([
        {
            "chat_id": chat.id,
            "telegram_id": user.id,
            "role": "user",
            "content": message.text,
        },
        {
            "chat_id": chat.id,
            "telegram_id": user.id,
            "role": "assistant",
            "content": reply or "[طلب توصيل رسالة]",
        },
    ]).execute()

    if kind == "ignore":
        return

    if kind == "send":
        if not recipient or not outgoing:
            await message.reply_text(
                reply or "عايز تبعت لمين، وتقول له إيه يا صاحبي؟"
            )
            return

        people = db.table("bot_users").select(
            "telegram_id,display_name,username,aliases,private_chat_started"
        ).execute().data

        needle = recipient.lstrip("@").casefold()
        matches = []
        for person in people:
            names = [
                person.get("display_name") or "",
                person.get("username") or "",
                *(person.get("aliases") or []),
            ]
            if any(str(n).lstrip("@").casefold() == needle for n in names):
                matches.append(person)

        if len(matches) != 1:
            await message.reply_text(
                "مش قادر أحدد الشخص بثقة. خليه يبدأ معايا الخاص "
                "بـ /start، وسجّل اسمه أو لقبه بشكل واضح."
            )
            return

        person = matches[0]
        if not person.get("private_chat_started"):
            await message.reply_text(
                "صاحبك لازم يفتح البوت ويبعت /start الأول "
                "عشان أقدر أوصل له رسالة."
            )
            return

        pending = db.table("pending_messages").insert({
            "sender_id": user.id,
            "recipient_id": person["telegram_id"],
            "recipient_name": person.get("display_name") or recipient,
            "message_text": outgoing,
            "status": "pending",
        }).execute().data[0]

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "أوافق على الإرسال ✅",
                callback_data=f"approve:{pending['id']}",
            ),
            InlineKeyboardButton(
                "إلغاء ❌",
                callback_data=f"cancel:{pending['id']}",
            ),
        ]])
        await message.reply_text(
            f"هتتبعت لـ {person.get('display_name') or recipient} الرسالة دي:\n\n"
            f"{outgoing}\n\nموافق أبعتها؟",
            reply_markup=keyboard,
        )
        return

    if reply:
        await message.reply_text(reply)

async def decision(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    try:
        action, raw_id = query.data.split(":", 1)
        request_id = int(raw_id)
    except (ValueError, AttributeError):
        return

    found = db.table("pending_messages").select("*").eq(
        "id", request_id
    ).eq("sender_id", user.id).eq("status", "pending").execute().data

    if not found:
        await query.edit_message_text(
            "الطلب ده اتنفذ أو اتلغى قبل كده."
        )
        return

    item = found[0]

    if action == "cancel":
        db.table("pending_messages").update({
            "status": "cancelled"
        }).eq("id", request_id).eq("status", "pending").execute()
        await query.edit_message_text("تمام، ألغيت الإرسال.")
        return

    if action != "approve":
        return

    changed = db.table("pending_messages").update({
        "status": "approved"
    }).eq("id", request_id).eq(
        "status", "pending"
    ).select("id").execute().data

    if not changed:
        await query.edit_message_text("الطلب اتعالج بالفعل.")
        return

    try:
        await context.bot.send_message(
            chat_id=item["recipient_id"],
            text=(
                f"رسالة من {user.first_name}:\n\n"
                f"{item['message_text']}"
            ),
        )
        db.table("pending_messages").update({
            "status": "sent"
        }).eq("id", request_id).execute()
        await query.edit_message_text("وصلت الرسالة بنجاح ✅")
    except Exception:
        log.exception("Telegram message delivery failed")
        db.table("pending_messages").update({
            "status": "failed"
        }).eq("id", request_id).execute()
        await query.edit_message_text(
            "معرفتش أوصل الرسالة. ممكن صاحبك يكون موقف استقبال الرسائل."
        )

async def error_handler(update, context):
    log.exception("Unhandled bot error", exc_info=context.error)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("forget", forget))
    app.add_handler(CallbackQueryHandler(decision))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message
    ))
    app.add_error_handler(error_handler)
    log.info("Sagil is starting")
    app.run_polling()

if __name__ == "__main__":
    main()
