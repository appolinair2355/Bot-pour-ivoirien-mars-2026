import os
import asyncio
import re
import logging
import sys
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime, timedelta, time
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_CYCLES, ALL_SUITS, SUIT_DISPLAY
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0: logger.error("API_ID manquant"); exit(1)
if not API_HASH: logger.error("API_HASH manquant"); exit(1)
if not BOT_TOKEN: logger.error("BOT_TOKEN manquant"); exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================
cycle_trackers: Dict[str, any] = {}
game_history: Dict[int, List[str]] = {}
pending_predictions: Dict[int, dict] = {}
rattrapage_tracking: Dict[int, int] = {}
current_game_number = 0
last_source_game_number = 0
prediction_channel_ok = False
client = TelegramClient(StringSession(os.getenv('TELEGRAM_SESSION', '')), API_ID, API_HASH)
suit_block_until: Dict[str, datetime] = {}
CONSECUTIVE_FAILURES_NEEDED = int(os.getenv('FAILURES_NEEDED', '2'))
last_prediction_time = datetime.now()

# ============================================================================
# DÉTECTION DES MESSAGES FINALISÉS
# ============================================================================
def is_message_finalized(message: str) -> bool:
    if '⏰' in message or '⏳' in message:
        return False
    if any(indicator in message for indicator in ['✅', '🔰', '▶️', 'FINAL', 'RÉSULTAT', '🔵#R']):
        return True
    if re.search(r'#N\s*\d+', message, re.IGNORECASE):
        lower_msg = message.lower()
        if any(word in lower_msg for word in ['en cours', 'attente', 'pending', 'wait']):
            return False
        return True
    return False

def extract_parentheses_groups(message: str) -> List[str]:
    scored_groups = re.findall(r"(\d+)?\(([^)]*)\)", message)
    if scored_groups:
        return [f"{score}:{content}" if score else content for score, content in scored_groups]
    return re.findall(r"\(([^)]*)\)", message)

def get_suits_in_group(group_str: str) -> List[str]:
    if ':' in group_str: group_str = group_str.split(':', 1)[1]
    suits = []
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥').replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    for suit in ALL_SUITS:
        if suit in normalized: suits.append(suit)
    return suits

@dataclass
class SuitCycleTracker:
    suit: str
    cycle_numbers: List[int] = field(default_factory=list)
    last_checked_index: int = -1
    miss_counter: int = 0
    current_tour: int = 1
    def process_verification(self, game_number: int, suit_found: bool) -> Optional[int]:
        if game_number not in self.cycle_numbers: return None
        self.last_checked_index = self.cycle_numbers.index(game_number)
        if suit_found:
            self.miss_counter = 0
            self.current_tour = 1
            return None
        self.miss_counter += 1
        if self.miss_counter >= CONSECUTIVE_FAILURES_NEEDED:
            pred_idx = self.last_checked_index + 1
            if pred_idx < len(self.cycle_numbers):
                pred_num = self.cycle_numbers[pred_idx]
                self.miss_counter = 0
                self.current_tour = 1
                return pred_num
        else:
            self.current_tour = 2
        return None

def initialize_trackers(max_game: int = 5000):
    global cycle_trackers
    for suit, config in SUIT_CYCLES.items():
        cycle_nums = list(range(config['start'], max_game + 1, config['interval']))
        cycle_trackers[suit] = SuitCycleTracker(suit=suit, cycle_numbers=cycle_nums)

async def send_prediction(game_number: int, suit: str, is_rattrapage: int = 0) -> Optional[int]:
    global last_prediction_time
    try:
        if suit in suit_block_until and datetime.now() < suit_block_until[suit]: return None
        msg = f"⏳BACCARAT AI 🤖⏳\n\nPLAYER : {game_number} {SUIT_DISPLAY.get(suit, suit)} : en cours...."
        if not PREDICTION_CHANNEL_ID: return None
        last_prediction_time = datetime.now()
        sent_msg = await client.send_message(PREDICTION_CHANNEL_ID, msg)
        pending_predictions[game_number] = {'suit': suit, 'message_id': sent_msg.id, 'status': 'en_cours', 'rattrapage': is_rattrapage, 'start_time': datetime.now()}
        return sent_msg.id
    except Exception as e:
        logger.error(f"Error send_prediction: {e}")
        return None

async def check_prediction_result(game_number: int, first_group: str):
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            if pred['suit'] in get_suits_in_group(first_group):
                await update_prediction_message(game_number, '✅0️⃣', True)
                return True
            else:
                await create_rattrapage(game_number, game_number + 1, 1)
                return False
    for orig, pred in list(pending_predictions.items()):
        r_num = pred.get('rattrapage', 0)
        if r_num > 0 and game_number == orig + r_num:
            if pred['suit'] in get_suits_in_group(first_group):
                await update_prediction_message(orig, f'✅{r_num}️⃣', True, r_num)
                return True
            else:
                if r_num < 2:
                    await create_rattrapage(orig, orig + r_num + 1, r_num + 1)
                else:
                    await update_prediction_message(orig, '❌', False)
                return False
    return False

async def create_rattrapage(orig: int, next_g: int, num: int):
    if orig in pending_predictions:
        pending_predictions[orig]['rattrapage'] = num

async def update_prediction_message(game: int, status: str, trouve: bool, rattrapage: int = 0):
    if game not in pending_predictions: return
    pred = pending_predictions[game]
    suit = pred['suit']
    res = f"{SUIT_DISPLAY.get(suit, suit)} {status}" + (" GAGNÉ" if trouve and status != '✅0️⃣' else (" :❌ PERDU 😭" if not trouve else ""))
    new_msg = f"⏳BACCARAT AI 🤖⏳\n\nPLAYER : {game} {res}"
    try:
        await client.edit_message(PREDICTION_CHANNEL_ID, pred['message_id'], new_msg)
        if not trouve: suit_block_until[suit] = datetime.now() + timedelta(minutes=5)
        del pending_predictions[game]
    except Exception as e: logger.error(f"Error update_msg: {e}")

async def process_game_result(game_number: int, message_text: str):
    global current_game_number
    current_game_number = game_number
    groups = extract_parentheses_groups(message_text)
    if not groups: return
    first_group = groups[0]
    suits = get_suits_in_group(first_group)
    await check_prediction_result(game_number, first_group)
    for suit, tracker in cycle_trackers.items():
        pred = tracker.process_verification(game_number, suit in suits)
        if pred: await send_prediction(pred, suit)

@client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
@client.on(events.MessageEdited(chats=SOURCE_CHANNEL_ID))
async def handle_message(event):
    msg = event.message.message
    if not is_message_finalized(msg): return
    match = re.search(r"#N\s*(\d+)", msg, re.IGNORECASE)
    if match: await process_game_result(int(match.group(1)), msg)

async def auto_reset_task():
    global last_prediction_time
    while True:
        try:
            now = datetime.now()
            if now - last_prediction_time > timedelta(hours=1):
                pending_predictions.clear()
                for t in cycle_trackers.values(): t.miss_counter = 0
                last_prediction_time = now
            if now.hour == 0 and now.minute == 0: # 1 AM Benin is 0 UTC
                pending_predictions.clear()
                initialize_trackers()
                if PREDICTION_CHANNEL_ID: await client.send_message(PREDICTION_CHANNEL_ID, "🔄 **RESET QUOTIDIEN**")
                await asyncio.sleep(61)
            await asyncio.sleep(60)
        except: await asyncio.sleep(60)

async def main():
    try:
        await client.start(bot_token=BOT_TOKEN)
        initialize_trackers()
        asyncio.create_task(auto_reset_task())
        await client.run_until_disconnected()
    except Exception as e: logger.error(f"Fatal: {e}")

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--zip':
        with zipfile.ZipFile('deploy_render.zip', 'w') as z:
            for f in ['main.py', 'config.py', 'requirements.txt', 'replit.md']:
                if os.path.exists(f): z.write(f)
    else: asyncio.run(main())
