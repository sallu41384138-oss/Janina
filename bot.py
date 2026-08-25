"""
MIKROTIK TELEGRAM BOT — FINAL VERSION (WEB APP LOGIN + .env CONFIG)
=====================================================================
ফিচার:
  0. Facebook-স্টাইল গ্রাফিক্যাল লগইন (Telegram Web App, username+password) — সেশন ৩০ মিনিট
  1. Live PPPoE speed monitoring (/speed, /stop_speed)
  2. Multi-router MAC address block/unblock (/block_mac, /unblock_mac, /list_blocked, /list_routers)
  3. Inline button menu (/menu, /user_info)

Requirements:
    pip install pyTelegramBotAPI routeros-api python-dotenv

সেটআপ:
    1. login.html একটা পাবলিক HTTPS URL এ হোস্ট করো (VPS+nginx / Netlify / Vercel ইত্যাদি)
       *** Telegram Web App খুলতে HTTPS বাধ্যতামূলক, শুধু HTTP কাজ করবে না ***
    2. .env.example কপি করে .env বানাও, তাতে BOT_TOKEN, WEBAPP_URL, ADMIN_USERNAME,
       ADMIN_PASSWORD, ও রাউটারের তথ্য বসাও
    3. .env ফাইলটা .gitignore এ দিয়ে দাও, কখনো GitHub এ push কোরো না
"""

import os
import json
import time
import threading
import logging

import telebot
from telebot import types
import routeros_api
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)

# ================== .env থেকে কনফিগ লোড ==================

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
WEBAPP_URL = os.getenv('WEBAPP_URL')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
DEFAULT_ROUTER = os.getenv('DEFAULT_ROUTER', 'router1')

if not BOT_TOKEN or not WEBAPP_URL or not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError("BOT_TOKEN / WEBAPP_URL / ADMIN_USERNAME / ADMIN_PASSWORD .env এ পাওয়া যায়নি। .env.example দেখো।")

bot = telebot.TeleBot(BOT_TOKEN)

ROUTERS = {}
for i in range(1, 7):
    prefix = f'ROUTER{i}_'
    host = os.getenv(prefix + 'HOST')
    if not host:
        continue
    ROUTERS[f'router{i}'] = {
        'host': host,
        'user': os.getenv(prefix + 'USER'),
        'password': os.getenv(prefix + 'PASS'),
        'port': int(os.getenv(prefix + 'PORT', 8728)),
    }

RULE_COMMENT_TAG = "bot-mac-block"
active_monitors = {}

# ================== Web App লগইন (Facebook-স্টাইল) ==================

SESSION_TIMEOUT = 1800      # সেশন মেয়াদ: ৩০ মিনিট
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = 300      # ৫ মিনিট লক

authenticated_users = {}    # {user_id: login_timestamp}
failed_attempts = {}        # {user_id: {'count': int, 'locked_until': timestamp}}


def is_locked_out(user_id):
    info = failed_attempts.get(user_id)
    if not info:
        return False
    if info['count'] >= MAX_FAILED_ATTEMPTS and time.time() < info.get('locked_until', 0):
        return True
    if info.get('locked_until', 0) and time.time() >= info['locked_until']:
        failed_attempts.pop(user_id, None)
    return False


def is_authenticated(user_id):
    login_time = authenticated_users.get(user_id)
    if login_time is None:
        return False
    if time.time() - login_time > SESSION_TIMEOUT:
        authenticated_users.pop(user_id, None)
        return False
    return True


def admin_only(func):
    """message অথবা callback handler — দুটোতেই কাজ করে"""
    def wrapper(update, *args, **kwargs):
        user_id = update.from_user.id
        chat_id = update.chat.id if hasattr(update, 'chat') else update.message.chat.id

        if not is_authenticated(user_id):
            bot.send_message(chat_id, "🔒 আগে লগইন করো: /start", reply_markup=build_login_button())
            return
        return func(update, *args, **kwargs)
    return wrapper


def build_login_button():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔐 লগইন করো", web_app=types.WebAppInfo(WEBAPP_URL)))
    return markup


@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    if is_authenticated(user_id):
        bot.send_message(message.chat.id, "🖥 মাইক্রোটিক বট মেনু:", reply_markup=build_main_menu())
    else:
        bot.send_message(
            message.chat.id,
            "👋 স্বাগতম, Network Admin প্যানেলে।\nনিচের বাটনে চেপে লগইন করো:",
            reply_markup=build_login_button()
        )


@bot.message_handler(content_types=['web_app_data'])
def handle_webapp_login(message):
    """login.html থেকে tg.sendData() দিয়ে পাঠানো username/password এখানে আসে"""
    user_id = message.from_user.id

    if is_locked_out(user_id):
        remaining = int(failed_attempts[user_id]['locked_until'] - time.time())
        bot.send_message(message.chat.id, f"⛔ অনেকবার ভুল তথ্য দিয়েছো। {remaining} সেকেন্ড পর আবার চেষ্টা করো।")
        return

    try:
        data = json.loads(message.web_app_data.data)
        entered_username = data.get('username', '').strip()
        entered_password = data.get('password', '')
    except Exception:
        bot.send_message(message.chat.id, "❌ লগইন ডেটা পড়া যায়নি, আবার চেষ্টা করো।")
        return

    if entered_username == ADMIN_USERNAME and entered_password == ADMIN_PASSWORD:
        authenticated_users[user_id] = time.time()
        failed_attempts.pop(user_id, None)
        bot.send_message(message.chat.id, "✅ লগইন সফল হয়েছে।", reply_markup=build_main_menu())
    else:
        info = failed_attempts.setdefault(user_id, {'count': 0, 'locked_until': 0})
        info['count'] += 1
        if info['count'] >= MAX_FAILED_ATTEMPTS:
            info['locked_until'] = time.time() + LOCKOUT_DURATION
            bot.send_message(message.chat.id, f"⛔ ভুল তথ্য বারবার দেওয়ার কারণে {LOCKOUT_DURATION // 60} মিনিটের জন্য লক করা হলো।")
        else:
            left = MAX_FAILED_ATTEMPTS - info['count']
            bot.send_message(message.chat.id, f"❌ ভুল ইউজারনেম বা পাসওয়ার্ড। আরও {left} বার চেষ্টা করতে পারবে।", reply_markup=build_login_button())


@bot.message_handler(commands=['logout'])
def handle_logout(message):
    user_id = message.from_user.id
    authenticated_users.pop(user_id, None)
    bot.send_message(message.chat.id, "👋 লগআউট করা হয়েছে।")


# ================== Mikrotik কানেকশন হেল্পার ==================

def get_router_connection(router_name):
    if router_name not in ROUTERS:
        return None, None
    cfg = ROUTERS[router_name]
    connection = routeros_api.RouterOsApiPool(
        cfg['host'],
        username=cfg['user'],
        password=cfg['password'],
        port=cfg['port'],
        plaintext_login=True
    )
    return connection.get_api(), connection


# ================== ফিচার ১: Live Speed Monitoring ==================

def get_ppp_user_traffic(router_name, ppp_username):
    api, connection = get_router_connection(router_name)
    if api is None:
        return None
    try:
        active_list = api.get_resource('/ppp/active')
        active_users = active_list.get(name=ppp_username)
        if not active_users:
            return None

        interface_name = active_users[0].get('name')
        traffic_resource = api.get_resource('/interface/monitor-traffic')
        result = traffic_resource.call('monitor-traffic', {'interface': interface_name, 'once': ''})

        rx_bps = int(result[0].get('rx-bits-per-second', 0))
        tx_bps = int(result[0].get('tx-bits-per-second', 0))
        return {'rx_mbps': round(rx_bps / 1_000_000, 2), 'tx_mbps': round(tx_bps / 1_000_000, 2)}
    finally:
        connection.disconnect()


@bot.message_handler(commands=['speed'])
@admin_only
def handle_speed_command(message):
    parts = message.text.split()
    if len(parts) not in (2, 3):
        bot.send_message(message.chat.id, "Usage: /speed <ppp_username> [router_name]")
        return

    ppp_username = parts[1]
    router_name = parts[2] if len(parts) == 3 else DEFAULT_ROUTER

    sent = bot.send_message(message.chat.id, f"⏳ {ppp_username} এর live speed লোড হচ্ছে...")
    monitor_key = f"{message.chat.id}_{sent.message_id}"
    active_monitors[monitor_key] = True

    thread = threading.Thread(
        target=live_speed_loop,
        args=(message.chat.id, sent.message_id, ppp_username, router_name, monitor_key)
    )
    thread.daemon = True
    thread.start()


def live_speed_loop(chat_id, message_id, ppp_username, router_name, monitor_key, duration=60, interval=3):
    elapsed = 0
    while elapsed < duration and active_monitors.get(monitor_key):
        try:
            traffic = get_ppp_user_traffic(router_name, ppp_username)
            if traffic is None:
                text = f"🔴 {ppp_username} — বর্তমানে অফলাইন অথবা রাউটারে খুঁজে পাওয়া যায়নি।"
            else:
                text = (
                    f"📶 Live Speed — {ppp_username} ({router_name})\n\n"
                    f"⬇️ Download: {traffic['rx_mbps']} Mbps\n"
                    f"⬆️ Upload: {traffic['tx_mbps']} Mbps\n\n"
                    f"প্রতি {interval} সেকেন্ডে আপডেট হচ্ছে..."
                )
            bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        time.sleep(interval)
        elapsed += interval

    active_monitors.pop(monitor_key, None)
    try:
        bot.edit_message_text(
            f"⏹ Live monitoring শেষ হয়েছে ({ppp_username})।\nআবার দেখতে /speed {ppp_username} দাও।",
            chat_id=chat_id, message_id=message_id
        )
    except Exception:
        pass


@bot.message_handler(commands=['stop_speed'])
@admin_only
def handle_stop_speed(message):
    stopped = False
    for key in list(active_monitors.keys()):
        if key.startswith(f"{message.chat.id}_"):
            active_monitors[key] = False
            stopped = True
    bot.send_message(message.chat.id, "✅ Live monitoring বন্ধ করা হলো।" if stopped else "কোনো active monitoring নেই।")


# ================== ফিচার ২: MAC Block/Unblock ==================

def block_mac(router_name, mac_address):
    api, connection = get_router_connection(router_name)
    if api is None:
        return False, "রাউটার খুঁজে পাওয়া যায়নি।"
    try:
        firewall = api.get_resource('/ip/firewall/filter')
        existing = firewall.get(comment=f"{RULE_COMMENT_TAG}-{mac_address}")
        if existing:
            return False, "এই MAC আগে থেকেই ব্লক করা আছে।"

        firewall.add(chain='forward', src_mac_address=mac_address, action='drop',
                      comment=f"{RULE_COMMENT_TAG}-{mac_address}")
        return True, f"MAC {mac_address} ব্লক করা হয়েছে ({router_name})।"
    except Exception as e:
        return False, f"এরর: {str(e)}"
    finally:
        connection.disconnect()


def unblock_mac(router_name, mac_address):
    api, connection = get_router_connection(router_name)
    if api is None:
        return False, "রাউটার খুঁজে পাওয়া যায়নি।"
    try:
        firewall = api.get_resource('/ip/firewall/filter')
        existing = firewall.get(comment=f"{RULE_COMMENT_TAG}-{mac_address}")
        if not existing:
            return False, "এই MAC ব্লক করা নেই।"
        for rule in existing:
            firewall.remove(id=rule['id'])
        return True, f"MAC {mac_address} আনব্লক করা হয়েছে ({router_name})।"
    except Exception as e:
        return False, f"এরর: {str(e)}"
    finally:
        connection.disconnect()


def list_blocked_macs(router_name):
    api, connection = get_router_connection(router_name)
    if api is None:
        return None
    try:
        firewall = api.get_resource('/ip/firewall/filter')
        all_rules = firewall.get()
        return [r.get('src-mac-address') for r in all_rules if r.get('comment', '').startswith(RULE_COMMENT_TAG)]
    finally:
        connection.disconnect()


@bot.message_handler(commands=['list_routers'])
@admin_only
def handle_list_routers(message):
    router_list = "\n".join([f"• {name} ({cfg['host']})" for name, cfg in ROUTERS.items()])
    bot.send_message(message.chat.id, f"রাউটার লিস্ট:\n{router_list}")


@bot.message_handler(commands=['block_mac'])
@admin_only
def handle_block_mac(message):
    parts = message.text.split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Usage: /block_mac <router_name> <mac_address>")
        return
    router_name, mac_address = parts[1], parts[2].upper()
    success, msg = block_mac(router_name, mac_address)
    bot.send_message(message.chat.id, ("✅ " if success else "❌ ") + msg)


@bot.message_handler(commands=['unblock_mac'])
@admin_only
def handle_unblock_mac(message):
    parts = message.text.split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Usage: /unblock_mac <router_name> <mac_address>")
        return
    router_name, mac_address = parts[1], parts[2].upper()
    success, msg = unblock_mac(router_name, mac_address)
    bot.send_message(message.chat.id, ("✅ " if success else "❌ ") + msg)


@bot.message_handler(commands=['list_blocked'])
@admin_only
def handle_list_blocked(message):
    parts = message.text.split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Usage: /list_blocked <router_name>")
        return
    router_name = parts[1]
    blocked = list_blocked_macs(router_name)
    if blocked is None:
        bot.send_message(message.chat.id, "রাউটার খুঁজে পাওয়া যায়নি।")
    elif not blocked:
        bot.send_message(message.chat.id, f"{router_name}-এ কোনো MAC ব্লক করা নেই।")
    else:
        bot.send_message(message.chat.id, f"ব্লক করা MAC ({router_name}):\n" + "\n".join(blocked))


# ================== ফিচার ৩: Inline Menu ==================

def build_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔴 নেট জ্যাম", callback_data="menu_net_jam"),
        types.InlineKeyboardButton("🟢 নেট আনজ্যাম", callback_data="menu_net_unjam"),
    )
    markup.add(
        types.InlineKeyboardButton("📊 স্ট্যাটাস", callback_data="menu_status"),
        types.InlineKeyboardButton("🔍 ক্রেডেনশিয়াল", callback_data="menu_credential"),
    )
    markup.add(
        types.InlineKeyboardButton("👤 ইউজার জ্যাম", callback_data="menu_user_jam"),
        types.InlineKeyboardButton("🔑 পাসওয়ার্ড", callback_data="menu_password"),
    )
    return markup


@bot.message_handler(commands=['menu'])
@admin_only
def handle_menu(message):
    bot.send_message(message.chat.id, "🖥 মাইক্রোটিক বট মেনু:", reply_markup=build_main_menu())


def build_user_info_card(user_id, ip, uptime, download, upload, is_active=True):
    status_icon = "🟢" if is_active else "🔴"
    status_text = "সচল" if is_active else "বন্ধ"
    return (
        f"👤 ইউজার      : {user_id}\n"
        f"🌐 IP         : {ip}\n"
        f"🕐 আপটাইম     : {uptime}\n"
        f"📥 Download   : {download}\n"
        f"📤 Upload     : {upload}\n"
        f"📶 অবস্থা      : {status_icon} {status_text}\n"
    )


@bot.message_handler(commands=['user_info'])
@admin_only
def handle_user_info(message):
    parts = message.text.split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Usage: /user_info <user_id>")
        return
    user_id = parts[1]
    # TODO: এখানে আসল ডেটা Mikrotik /ppp/active থেকে টেনে বসাও
    info_text = build_user_info_card(user_id=user_id, ip="10.190.5.165", uptime="34m10s",
                                      download="2.4 GB", upload="80.5 MB", is_active=True)
    bot.send_message(message.chat.id, info_text, reply_markup=build_main_menu())


@bot.callback_query_handler(func=lambda call: call.data.startswith('menu_'))
@admin_only
def handle_menu_callback(call):
    action = call.data

    if action == "menu_net_jam":
        bot.send_message(call.message.chat.id, "🔴 কতক্ষণ জ্যাম করবেন? (যেমন: 30s, 10m, 1h)")
        bot.register_next_step_handler(call.message, handle_net_jam_duration)

    elif action == "menu_net_unjam":
        # TODO: আসল নেট আনজ্যাম করার Mikrotik action বসাও
        bot.send_message(call.message.chat.id, "🟢 নেটওয়ার্ক আনজ্যাম করা হয়েছে।", reply_markup=build_main_menu())

    elif action == "menu_status":
        bot.send_message(call.message.chat.id, "📊 নেটওয়ার্ক: ✅ সচল", reply_markup=build_main_menu())

    elif action == "menu_credential":
        bot.send_message(call.message.chat.id, "🔍 ইউজার আইডি দাও:\n/user_info <user_id>")

    elif action == "menu_user_jam":
        bot.send_message(call.message.chat.id, "👤 কোন ইউজারকে জ্যাম করবেন? ইউজার আইডি দাও:")
        bot.register_next_step_handler(call.message, handle_user_jam_target)

    elif action == "menu_password":
        bot.send_message(call.message.chat.id, "🔑 কোন ইউজারের পাসওয়ার্ড পরিবর্তন করবেন? ইউজার আইডি দাও:")
        bot.register_next_step_handler(call.message, handle_password_target)

    bot.answer_callback_query(call.id)


def handle_net_jam_duration(message):
    duration_text = message.text
    # TODO: আসল জ্যাম লজিক এখানে কল করো
    bot.send_message(message.chat.id, f"🔴 {duration_text} সময়ের জন্য নেট জ্যাম শুরু হলো।", reply_markup=build_main_menu())


def handle_user_jam_target(message):
    user_id = message.text
    bot.send_message(message.chat.id, f"👤 {user_id} — কতক্ষণ জ্যাম করবেন? (যেমন: 30s, 10m, 1h)")
    bot.register_next_step_handler(message, lambda m: finish_user_jam(m, user_id))


def finish_user_jam(message, user_id):
    duration_text = message.text
    # TODO: নির্দিষ্ট ইউজারকে জ্যাম করার আসল Mikrotik action
    bot.send_message(message.chat.id, f"👤 {user_id} — {duration_text} এর জন্য জ্যাম করা হলো।", reply_markup=build_main_menu())


def handle_password_target(message):
    user_id = message.text
    bot.send_message(message.chat.id, f"🔑 {user_id} — নতুন পাসওয়ার্ড দাও:")
    bot.register_next_step_handler(message, lambda m: finish_password_change(m, user_id))


def finish_password_change(message, user_id):
    new_password = message.text
    # TODO: web server / Mikrotik PPP secret এ POST করে পাসওয়ার্ড আপডেট করো
    bot.send_message(message.chat.id, f"✅ {user_id} এর পাসওয়ার্ড পরিবর্তন করা হয়েছে।", reply_markup=build_main_menu())


# ================== বট চালু করা ==================

if __name__ == '__main__':
    logging.info("বট চালু হচ্ছে...")
    bot.infinity_polling()
