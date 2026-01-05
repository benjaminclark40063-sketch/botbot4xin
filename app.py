# 这是一个 Python 脚本 (V17 - 账号互通 + 内容隔离版)
# 架构变更:
# 1. 用户Key: 读写 global_keys 表 (保持互通，解决 404). 
# 2. 帖子内容: 读写 bot_posts 表 (新增隔离，解决图片报错).
# 3. 订阅列表: 读写 bot_subscribers 表 (保持隔离).

import logging
import asyncio
import json
import string
import hmac
import hashlib
import requests
import os
import time
import psycopg2
from functools import partial
from datetime import datetime
from threading import Thread
from flask import Flask

from telegram import Update, User, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

# ==========================================
# --- 1. 全局配置 ---
# ==========================================

API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") 
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Lp123456!") 
DATABASE_URL = os.getenv("DATABASE_URL") 
PROXY_URL = os.getenv("PROXY_URL", "socks5h://alensuo19133:S2S4JHQwtg@91.210.30.168:50101")

# 提取 Bot ID 用于数据隔离 (例如 "123456")
BOT_ID = API_TOKEN.split(':')[0] if API_TOKEN else "unknown_bot"

API_KEY = "26b5162b3"
API_SECRET = "29d6f839fc552d0d"
SITE_CODE = "1607"
API_REGISTER_URL = "https://ytaztiuu.wgopenapi.com/home/openapiRegister"
API_LOGIN_URL = "https://ytaztiuu.wgopenapi.com/home/openapiLogin"
LOBBY_DOMAIN = "https://u70.vip" 

(AWAIT_PASSWORD, AWAIT_POST_NAME, AWAIT_POST_PHOTO, 
 AWAIT_POST_TEXT_ZH, AWAIT_POST_BUTTONS_ZH, 
 AWAIT_POST_TEXT_EN, AWAIT_POST_BUTTONS_EN,
 AWAIT_POST_TO_BROADCAST) = range(8)

logging.getLogger("httpx").setLevel(logging.WARNING) 
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# --- 2. 数据库模块 (完全隔离逻辑) ---
# ==========================================
def get_db_connection():
    max_retries = 3 
    for attempt in range(max_retries):
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10) # 建议把超时稍微改长一点到10秒
            return conn
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"⚠️ 连接抖动，正在重试 ({attempt+1}/{max_retries})...")
                time.sleep(2) # 失败后多睡一会儿，改到2秒
            else:
                logger.error(f"❌ 彻底失败: {e}")
                raise e

def init_db_check():
    try:
        conn = get_db_connection()
        conn.close()
        logger.info(f"✅ 数据库连接正常 (Bot ID: {BOT_ID})")
    except Exception as e:
        logger.error(f"❌ 数据库连接失败: {e}")

def get_user_data(user_id: int):
    """混合读取：语言隔离，Key互通"""
    lang = 'zh'
    key = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # 1. 读语言 (隔离)
        cur.execute("SELECT language FROM bot_subscribers WHERE user_id = %s AND bot_id = %s", (user_id, BOT_ID))
        res_lang = cur.fetchone()
        if res_lang: lang = res_lang[0]
        # 2. 读Key (共享)
        cur.execute("SELECT user_key FROM global_keys WHERE user_id = %s", (user_id,))
        res_key = cur.fetchone()
        if res_key: key = res_key[0]
        conn.close()
    except Exception as e: logger.error(f"DB Error: {e}")
    return {'lang': lang, 'key': key}

def save_user_info(user: User, lang='zh', user_key=None):
    """
    [V19 更新] 
    在更新 global_keys 时，同步写入 bot_id (记录用户最后一次是从哪个机器人活跃的)
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        username = f"@{user.username}" if user.username else ""
        
        # 1. 写隔离订阅表 (这部分完全没动，保持不变)
        sql_sub = """INSERT INTO bot_subscribers (user_id, bot_id, username, language, interaction_count) VALUES (%s, %s, %s, %s, 1)
            ON CONFLICT (user_id, bot_id) DO UPDATE SET 
            username = EXCLUDED.username, 
            language = EXCLUDED.language,
            interaction_count = bot_subscribers.interaction_count + 1;""" # <--- 关键在这里：原有次数 + 1
        cur.execute(sql_sub, (user.id, BOT_ID, username, lang))
        
        # 2. 写共享表 (修改点：增加了 bot_id)
        if user_key:
            # 情况 A: 有 Key (新注册)，插入或更新 Key + Username + 【Bot_ID】
            sql_key = """INSERT INTO global_keys (user_id, user_key, username, bot_id) VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET 
                user_key = EXCLUDED.user_key,
                username = EXCLUDED.username,
                bot_id = EXCLUDED.bot_id;""" # <--- 增加了这行
            # 参数里增加了 BOT_ID
            cur.execute(sql_key, (user.id, user_key, username, BOT_ID)) 
        else:
            # 情况 B: 无 Key (老用户活跃)，更新 Username + 【Bot_ID】
            sql_update_name = "UPDATE global_keys SET username = %s, bot_id = %s WHERE user_id = %s"
            # 参数里增加了 BOT_ID
            cur.execute(sql_update_name, (username, BOT_ID, user.id))
            
        conn.commit()
        conn.close()
    except Exception as e: logger.error(f"DB Error: {e}")

# --- [关键修改] 帖子读写增加 bot_id 隔离 ---

def get_post_data(post_name, lang):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        col_t = 'text_zh' if lang == 'zh' else 'text_en'
        col_b = 'button_layout_zh' if lang == 'zh' else 'button_layout_en'
        # [V15] 从 bot_posts 读取，并强制匹配 bot_id
        sql = f"SELECT photo_id, {col_t}, {col_b} FROM bot_posts WHERE bot_id = %s AND post_name = %s"
        cur.execute(sql, (BOT_ID, post_name))
        res = cur.fetchone()
        conn.close()
        return res
    except: return None

def save_post_to_db(data):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # [V15] 写入 bot_posts，带上 BOT_ID
        sql = """INSERT INTO bot_posts (bot_id, post_name, photo_id, text_zh, button_layout_zh, text_en, button_layout_en)
            VALUES (%s, %s, %s, %s, %s, %s, %s) 
            ON CONFLICT (bot_id, post_name) DO UPDATE SET
            photo_id = EXCLUDED.photo_id, text_zh = EXCLUDED.text_zh, button_layout_zh = EXCLUDED.button_layout_zh,
            text_en = EXCLUDED.text_en, button_layout_en = EXCLUDED.button_layout_en;"""
        cur.execute(sql, (
            BOT_ID, 
            data['post_name_to_edit'], 
            data.get('post_photo_id'), 
            data.get('post_text_zh'), 
            data.get('post_buttons_zh'), 
            data.get('post_text_en'), 
            data.get('post_buttons_en')
        ))
        conn.commit()
        conn.close()
    except Exception as e: logger.error(f"DB Error: {e}")

# ------------------------------------------

def get_all_subscribers():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM bot_subscribers WHERE bot_id = %s", (BOT_ID,))
        res = [row[0] for row in cur.fetchall()]
        conn.close()
        return res
    except: return []

def get_subscriber_count():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM bot_subscribers WHERE bot_id = %s", (BOT_ID,))
        res = cur.fetchone()[0]
        conn.close()
        return res
    except: return 0

# ==========================================
# --- 3. API 核心 (互通逻辑) ---
# ==========================================
def get_fixed_password(user_id):
    raw = f"{user_id}-{API_SECRET}"
    return hashlib.md5(raw.encode()).hexdigest()[:12] + "Aa1!"

def sync_api_request(url, user_id, proxies, is_login=False):
    username = f"tg{user_id}" # 固定前缀，确保账号互通
    password = get_fixed_password(user_id)
    request_body = { "username": username, "passwd": password, "currency": "USDT" }
    
    sorted_keys = sorted(request_body.keys())
    msg = "&".join([f"{key}={request_body[key]}" for key in sorted_keys])
    sign = hmac.new(API_SECRET.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()

    headers = {'Content-Type': 'application/json; charset=utf-8', 'siteCode': SITE_CODE, 'apiKey': API_KEY, 'apiSign': sign}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(request_body), proxies=proxies, timeout=20.0)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        logger.error(f"API Error: {e}")
        return None

async def register_user_via_api(user: User) -> str | None:
    proxies = {"http": PROXY_URL, "https": PROXY_URL}
    loop = asyncio.get_running_loop()
    func_reg = partial(sync_api_request, url=API_REGISTER_URL, user_id=user.id, proxies=proxies, is_login=False)
    data = await loop.run_in_executor(None, func_reg)
    
    if data and data.get("code") == 0 and data.get("data", {}).get("userkey"):
        return data["data"]["userkey"]
    elif data and data.get("code") == 1013:
        # 尝试登录 (备用逻辑)
        func_log = partial(sync_api_request, url=API_LOGIN_URL, user_id=user.id, proxies=proxies, is_login=True)
        log_data = await loop.run_in_executor(None, func_log)
        if log_data and log_data.get("code") == 0:
            return log_data["data"]["userkey"]
    return None

# ==========================================
# --- 4. 机器人逻辑 (UI) ---
# ==========================================
def parse_button_layout(layout_text: str) -> list:
    keyboard = []
    if not layout_text: return keyboard
    for line in layout_text.strip().split('\n'):
        row = []
        for btn in line.split('|'):
            parts = [p.strip() for p in btn.split('+')]
            if len(parts) == 2:
                t, u = parts
                u = u.replace('webapp://', '', 1) if u.startswith('webapp://') else u
                if not u.startswith('http'): u = 'https://' + u
                row.append(InlineKeyboardButton(t, url=u))
        if row: keyboard.append(row)
    return keyboard

async def send_post_by_name(update: Update, context: ContextTypes.DEFAULT_TYPE, post_name: str) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id: return False
    
    user_data = get_user_data(chat_id)
    lang, user_key = user_data['lang'], user_data['key']
    
    post_data = get_post_data(post_name, lang)
    is_welcome = (post_name == 'welcome')

    if not post_data:
        if is_welcome: default_text = "Welcome!" # 数据库刚初始化时可能是空的
        else: return False
        photo_id, text, button_layout_text = None, default_text, None
    else:
        photo_id, text, button_layout_text = post_data

    keyboard = []
    if user_key:
        url = f"{LOBBY_DOMAIN}/?userkey={user_key}"
        txt = "🎮 点击开始游戏 🎮" if lang == 'zh' else "🎮 Start Game 🎮"
        keyboard.append([InlineKeyboardButton(txt, url=url)])
    
    keyboard.extend(parse_button_layout(button_layout_text))

    if is_welcome:
        keyboard.append([InlineKeyboardButton("🇨🇳 中文", callback_data="set_lang_zh"),
                         InlineKeyboardButton("🇺🇸 English", callback_data="set_lang_en")])

    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    try:
        if update.callback_query: await update.callback_query.message.delete()
        if photo_id: await context.bot.send_photo(chat_id, photo_id, caption=text or "...", parse_mode='Markdown', reply_markup=markup)
        else: await context.bot.send_message(chat_id, text=text or "...", parse_mode='Markdown', reply_markup=markup)
        return True
    except Exception as e:
        logger.error(f"Send Error: {e}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    save_user_info(user)
    user_data = get_user_data(user.id)
    
    if not user_data['key']:
        msg = await update.message.reply_text("正在连接账户..." if user_data['lang']=='zh' else "Connecting account...")
        key = await register_user_via_api(user)
        if key:
            save_user_info(user, user_data['lang'], key)
            await msg.delete()
        else:
            await msg.edit_text("⚠️ 账户连接失败")
            return
    
    cmd = update.message.text.split()[0]
    target = {"/start": "welcome", "/help": "help", "/support": "support"}.get(cmd, "welcome")
    await send_post_by_name(update, context, target)

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    lang = q.data.split('_')[-1]
    save_user_info(q.from_user, lang=lang)
    await send_post_by_name(update, context, 'welcome')

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("输入密码:")
    return AWAIT_PASSWORD

async def check_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message.text == ADMIN_PASSWORD:
        context.user_data['is_admin'] = True
        # 去掉了星号，也去掉了 parse_mode，保证 100% 不报错
        menu_text = (
            f"✅ 管理员已登录 (Bot ID: {BOT_ID})\n\n"
            "=== 内容管理 ===\n"
            "/newpost - 新建帖子\n"
            "/editwelcome - 编辑欢迎语\n\n"
            "=== 运营工具 ===\n"
            "/broadcast - 广播消息\n"
            "/stats - 查看人数\n"
            "/exit_admin - 退出"
        )
        await update.effective_message.reply_text(menu_text)
        return ConversationHandler.END
    await update.effective_message.reply_text("❌ 错误")
    return AWAIT_PASSWORD
async def post_creation_start(update, context): 
    if not context.user_data.get('is_admin'): return ConversationHandler.END
    context.user_data['post_name_to_edit'] = 'welcome'
    await update.effective_message.reply_text("发图或 /skip:")
    return AWAIT_POST_PHOTO
async def receive_post_photo(update, context):
    context.user_data['post_photo_id'] = update.effective_message.photo[-1].file_id
    await update.effective_message.reply_text("中文:")
    return AWAIT_POST_TEXT_ZH
async def skip_post_photo(update, context):
    context.user_data['post_photo_id'] = None
    await update.effective_message.reply_text("中文:")
    return AWAIT_POST_TEXT_ZH
async def receive_post_text_zh(update, context):
    t = update.effective_message.text
    context.user_data['post_text_zh'] = None if t=='/skip' else t
    await update.effective_message.reply_text("按钮:")
    return AWAIT_POST_BUTTONS_ZH
async def receive_post_buttons_zh(update, context):
    t = update.effective_message.text
    context.user_data['post_buttons_zh'] = None if t=='/skip' else t
    await update.effective_message.reply_text("英文:")
    return AWAIT_POST_TEXT_EN
async def receive_post_text_en(update, context):
    t = update.effective_message.text
    context.user_data['post_text_en'] = None if t=='/skip' else t
    await update.effective_message.reply_text("英文按钮:")
    return AWAIT_POST_BUTTONS_EN
async def receive_post_buttons_en_and_save(update, context):
    t = update.effective_message.text
    context.user_data['post_buttons_en'] = None if t=='/skip' else t
    save_post_to_db(context.user_data)
    await update.effective_message.reply_text("✅ 保存成功")
    return ConversationHandler.END

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data.get('is_admin'): return ConversationHandler.END
    await update.effective_message.reply_text("输入帖子ID:")
    return AWAIT_POST_TO_BROADCAST

async def receive_post_to_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.effective_message.text.strip()
    subs = get_all_subscribers()
    if not subs:
        await update.effective_message.reply_text("无用户")
        return ConversationHandler.END
    await update.effective_message.reply_text(f"开始广播 {len(subs)} 人...")
    cnt = 0
    tmp = type('T', (), {'effective_chat': None, 'effective_user': None, 'callback_query': None})()
    for uid in subs:
        tmp.effective_chat = type('C', (), {'id': uid})()
        tmp.effective_user = type('U', (), {'id': uid, 'username': ''})()
        if await send_post_by_name(tmp, context, name): cnt += 1
        await asyncio.sleep(0.05)
    await update.effective_message.reply_text(f"成功: {cnt}")
    return ConversationHandler.END

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get('is_admin'): return
    cnt = get_subscriber_count()
    await update.effective_message.reply_text(f"当前用户数: {cnt}")

async def cancel(update, context): await update.effective_message.reply_text("取消"); return ConversationHandler.END
async def exit_admin(update, context): context.user_data['is_admin'] = False; await update.effective_message.reply_text("退出")

flask_app = Flask(__name__)

@flask_app.route('/')
def health_check(): 
    return "Alive", 200

# === 新增：数据库健康检查接口 ===
@flask_app.route('/db-check')
def db_health_check():
    try:
        # 尝试连接数据库并执行最简单的查询
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        # 如果成功，返回 200
        return "Database is Healthy", 200
    except Exception as e:
        # 如果失败，打印日志并返回 500
        logger.error(f"Health Check Failed: {e}")
        return "Database Error", 500
def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host='0.0.0.0', port=port, use_reloader=False)

def main() -> None:
    # 👇 新增：打印调试信息，让我们看看变量读到了没
    print(f"🔍 DEBUG: 正在检查变量...")
    print(f"🔑 Token 状态: {'✅ 有值' if API_TOKEN else '❌ 空 (None)'}")
    print(f"🛢️ DB 状态: {'✅ 有值' if DATABASE_URL else '❌ 空 (None)'}")

    if not API_TOKEN or not DATABASE_URL:
        print("💥 致命错误: 缺少必要的环境变量，程序即将退出！")
        return  # 👈 只有这里会退出

    print("🚀 变量检查通过，正在启动后台服务...")
    Thread(target=run_flask, daemon=True).start()
    
    init_db_check()
    app = Application.builder().token(API_TOKEN).build()    
    app.add_handler(ConversationHandler(entry_points=[CommandHandler('admin', admin)], states={AWAIT_PASSWORD: [MessageHandler(filters.TEXT, check_password)]}, fallbacks=[CommandHandler('cancel', cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler('newpost', post_creation_start), CommandHandler('editwelcome', post_creation_start)], states={
        AWAIT_POST_PHOTO: [MessageHandler(filters.PHOTO, receive_post_photo), CommandHandler('skip', skip_post_photo)],
        AWAIT_POST_TEXT_ZH: [MessageHandler(filters.TEXT, receive_post_text_zh)],
        AWAIT_POST_BUTTONS_ZH: [MessageHandler(filters.TEXT, receive_post_buttons_zh)],
        AWAIT_POST_TEXT_EN: [MessageHandler(filters.TEXT, receive_post_text_en)],
        AWAIT_POST_BUTTONS_EN: [MessageHandler(filters.TEXT, receive_post_buttons_en_and_save)],
    }, fallbacks=[CommandHandler('cancel', cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler('broadcast', broadcast_start)], states={AWAIT_POST_TO_BROADCAST: [MessageHandler(filters.TEXT, receive_post_to_broadcast)]}, fallbacks=[CommandHandler('cancel', cancel)]))
    
    app.add_handler(CommandHandler(["start", "help", "support"], start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("exit_admin", exit_admin))
    app.add_handler(CallbackQueryHandler(set_language, pattern='^set_lang_'))
    
    print("Bot Started...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()





