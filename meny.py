import telebot
from telebot import types
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, GetMessageReactionsListRequest, GetPollVotesRequest
import asyncio
import threading
import queue
import logging
import sqlite3
import os
import glob

logging.basicConfig(level=logging.ERROR)

# جلب التوكن والبيانات من Railway (Environment Variables)
BOT_TOKEN = os.environ.get('BOT_TOKEN')
API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')

if not BOT_TOKEN or not API_ID or not API_HASH:
    print("❌ خطأ: تأكد من إعداد BOT_TOKEN و API_ID و API_HASH في إعدادات السيرفر (Railway).")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)
user_data = {}
user_queues = {}
user_clients = {}

SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ----------------- قاعدة البيانات ----------------- #
def init_db():
    conn = sqlite3.connect('transferred_members.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS transferred (
                    user_id INTEGER PRIMARY KEY,
                    date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                 )''')
    conn.commit()
    conn.close()

def is_transferred(user_id):
    conn = sqlite3.connect('transferred_members.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM transferred WHERE user_id=?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res is not None

def mark_transferred(user_id):
    conn = sqlite3.connect('transferred_members.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO transferred (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

init_db()

# ----------------- دوال مساعدة ----------------- #
def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)

def get_queue_input(user_id):
    q = user_queues.get(user_id)
    if not q:
        q = queue.Queue()
        user_queues[user_id] = q
    return q.get()

def get_all_sessions():
    return glob.glob(os.path.join(SESSIONS_DIR, "*.session"))

# ----------------- أوامر البوت ----------------- #

@bot.message_handler(commands=['start'])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("تسجيل الدخول (إضافة حساب)", "نقل الأعضاء (Multi-Account)")
    bot.send_message(message.chat.id, "أهلاً بك يا وحش! 👋\n\nمن هنا تقدر تضيف حساباتك وتنقل بيهم كلهم مع بعض.", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text == "تسجيل الدخول (إضافة حساب)")
def login_step_1(message):
    user_id = message.from_user.id
    user_data[user_id] = {'temp_session_name': f"session_{user_id}_{int(asyncio.get_event_loop().time())}"}
    user_queues[user_id] = queue.Queue()
    msg = bot.send_message(message.chat.id, "3️⃣ ادخل رقم الهاتف (مثال: +201012345678):")
    bot.register_next_step_handler(msg, start_login_thread)

def start_login_thread(message):
    user_id = message.from_user.id
    user_data[user_id]['phone'] = message.text.strip()
    bot.send_message(message.chat.id, "⏳ جاري الاتصال بتيليجرام...")
    thread = threading.Thread(target=run_async, args=(login_and_wait(user_id, message.chat.id),))
    thread.start()

async def login_and_wait(user_id, chat_id):
    phone = user_data[user_id]['phone']
    session_name = os.path.join(SESSIONS_DIR, user_data[user_id]['temp_session_name'])
    
    client = TelegramClient(session_name, int(API_ID), API_HASH)
    await client.connect()
    
    try:
        sent_code = await client.send_code_request(phone)
        bot.send_message(chat_id, "📩 ادخل كود التحقق (OTP) اللي جالك:")
        code = await asyncio.get_event_loop().run_in_executor(None, get_queue_input, user_id)
        
        try:
            await client.sign_in(phone, code, phone_code_hash=sent_code.phone_code_hash)
        except errors.SessionPasswordNeededError:
            bot.send_message(chat_id, "🔐 الحساب عليه تحقق بخطوتين. ادخل الباسورد:")
            password = await asyncio.get_event_loop().run_in_executor(None, get_queue_input, user_id)
            await client.sign_in(password=password)

        bot.send_message(chat_id, "✅ تم الدخول وحفظ الحساب بنجاح! تقدر تضيف حساب تاني أو تبدأ النقل.")
    except Exception as e:
        bot.send_message(chat_id, f"❌ خطأ: {str(e)}")
    finally:
        await client.disconnect()

# ----------------- استقبال الرسائل النصية للتحقق ----------------- #
@bot.message_handler(func=lambda msg: msg.from_user.id in user_queues and not (msg.text == "تسجيل الدخول (إضافة حساب)" or msg.text == "نقل الأعضاء (Multi-Account)"))
def handle_queue_message(message):
    user_id = message.from_user.id
    user_queues[user_id].put(message.text.strip())
    bot.send_message(message.chat.id, "✅ تم الاستلام...")

# ----------------- نظام النقل ----------------- #

@bot.message_handler(func=lambda msg: msg.text == "نقل الأعضاء (Multi-Account)")
def transfer_start(message):
    user_id = message.from_user.id
    sessions = get_all_sessions()
    
    if not sessions:
        bot.send_message(message.chat.id, "❌ مفيش حسابات مسجلة! اضغط على 'تسجيل الدخول' الأول.")
        return
    
    bot.send_message(message.chat.id, f"✅ تم العثور على {len(sessions)} حساب. هيتم توزيع الأعضاء عليهم بالتساوي.")
    
    msg = bot.send_message(message.chat.id, "🔗 ادخل يوزر/لينك الكروب المصدر (اللي هنسحب منه):")
    bot.register_next_step_handler(msg, transfer_source)

def transfer_source(message):
    user_id = message.from_user.id
    user_data[user_id]['source'] = message.text.strip()
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("كل الأعضاء الظاهرين", "من الرسائل")
    markup.add("من التفاعلات (الريأكشن)", "من الاستفتاءات (التصويت)")
    msg = bot.send_message(message.chat.id, "🎯 عايز تسحبهم إزاي؟", reply_markup=markup)
    bot.register_next_step_handler(msg, transfer_method)

def transfer_method(message):
    user_id = message.from_user.id
    text = message.text
    if "كل الأعضاء" in text:
        user_data[user_id]['method'] = 'all'
    elif "الرسائل" in text:
        user_data[user_id]['method'] = 'messages'
    elif "الاستفتاءات" in text:
        user_data[user_id]['method'] = 'polls'
    else:
        user_data[user_id]['method'] = 'reactions'
        
    msg = bot.send_message(message.chat.id, "🎯 ادخل يوزر/لينك الكروب الهدف (اللي هننقل ليه):")
    bot.register_next_step_handler(msg, transfer_target)

def transfer_target(message):
    user_id = message.from_user.id
    user_data[user_id]['target'] = message.text.strip()
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("إضافة مباشرة (لازم أدمن)", "إرسال لينك في الخاص")
    msg = bot.send_message(message.chat.id, "🚀 طريقة النقل؟", reply_markup=markup)
    bot.register_next_step_handler(msg, transfer_action)

def transfer_action(message):
    user_id = message.from_user.id
    user_data[user_id]['action'] = 'invite' if "إضافة" in message.text else 'dm'
    
    msg = bot.send_message(message.chat.id, "⏱ ادخل وقت الانتظار بين كل عضو (بالثواني):", reply_markup=types.ReplyKeyboardRemove())
    bot.register_next_step_handler(msg, start_process)

def start_process(message):
    user_id = message.from_user.id
    try:
        sleep_time = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ ادخل رقم صحيح.")
        return

    user_data[user_id]['sleep'] = sleep_time
    bot.send_message(message.chat.id, "⏳ جاري صيد الأعضاء وتوزيعهم على الحسابات...")
    
    thread = threading.Thread(target=run_async, args=(process_transfer(user_id, message.chat.id),))
    thread.start()

async def process_transfer(user_id, chat_id):
    data = user_data[user_id]
    source = data['source']
    target = data['target']
    method = data['method']
    action = data['action']
    sleep_time = data['sleep']

    sessions = get_all_sessions()
    if not sessions:
        bot.send_message(chat_id, "❌ مفيش حسابات مسجلة.")
        return

    clients = []
    bot.send_message(chat_id, "🔄 جاري فتح الحسابات...")
    for sess in sessions:
        try:
            client = TelegramClient(sess, int(API_ID), API_HASH)
            await client.connect()
            clients.append(client)
        except Exception:
            pass

    if not clients:
        bot.send_message(chat_id, "❌ فشل فتح أي حساب!")
        return

    bot.send_message(chat_id, f"✅ تم فتح {len(clients)} حساب. جاري صيد الأعضاء...")

    targets = set()

    try:
        if method == 'all':
            participants = await clients[0].get_participants(source)
            for p in participants:
                targets.add(p.id)
        elif method == 'messages':
            async for msg in clients[0].iter_messages(source, limit=500):
                if msg.sender_id:
                    targets.add(msg.sender_id)
        elif method == 'polls':
            async for msg in clients[0].iter_messages(source, limit=100):
                if msg.poll and msg.poll.poll.public_voters:
                    offset = ''
                    while True:
                        res = await clients[0](GetPollVotesRequest(
                            peer=source, id=msg.id, limit=100, offset=offset
                        ))
                        for u in res.users:
                            targets.add(u.id)
                        offset = res.next_offset
                        if not offset:
                            break
        elif method == 'reactions':
            async for msg in clients[0].iter_messages(source, limit=50):
                if msg.reactions:
                    for reaction in msg.reactions.results:
                        res = await clients[0](GetMessageReactionsListRequest(
                            peer=source, id=msg.id, reaction=reaction.reaction, limit=100
                        ))
                        for u in res.users:
                            targets.add(u.id)
    except Exception as e:
        bot.send_message(chat_id, f"❌ خطأ في الصيد: {str(e)}")

    valid_targets = []
    for uid in targets:
        if not is_transferred(uid):
            valid_targets.append(uid)

    total = len(valid_targets)
    if total == 0:
        bot.send_message(chat_id, "⚠️ مفيش أعضاء جداد متاحين.")
        for c in clients:
            await c.disconnect()
        return

    bot.send_message(chat_id, f"📊 لقيت {total} عضو. جاري التوزيع على {len(clients)} حساب...")

    success = 0
    fail = 0
    current_client_idx = 0
    semaphore = asyncio.Semaphore(len(clients))

    async def transfer_one(uid):
        nonlocal success, fail, current_client_idx
        async with semaphore:
            idx = current_client_idx % len(clients)
            current_client_idx += 1
            client = clients[idx]

            try:
                if action == 'invite':
                    await client(InviteToChannelRequest(target, [uid]))
                else:
                    invite_link = await client(ExportChatInviteRequest(target))
                    await client.send_message(uid, f"أهلاً بك! انضم لينا هنا: {invite_link.link}")
                
                mark_transferred(uid)
                success += 1
                if success % 10 == 0:
                    bot.send_message(chat_id, f"🔄 تم النقل: {success} | فشل: {fail}")
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                try:
                    idx2 = current_client_idx % len(clients)
                    current_client_idx += 1
                    client2 = clients[idx2]
                    if action == 'invite':
                        await client2(InviteToChannelRequest(target, [uid]))
                    else:
                        invite_link = await client2(ExportChatInviteRequest(target))
                        await client2.send_message(uid, f"أهلاً بك: {invite_link.link}")
                    mark_transferred(uid)
                    success += 1
                except Exception:
                    fail += 1
            except Exception:
                fail += 1
            
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    tasks = [transfer_one(uid) for uid in valid_targets]
    await asyncio.gather(*tasks)

    bot.send_message(chat_id, f"✅ انتهت العملية!\nنجح: {success}\nفشل: {fail}")
    
    for c in clients:
        await c.disconnect()

if __name__ == "__main__":
    print("البوت شغال...")
    bot.polling(non_stop=True, skip_pending=True, timeout=60)
