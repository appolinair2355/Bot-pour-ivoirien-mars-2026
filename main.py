import os
import asyncio
import re
import logging
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime, timedelta, timezone, time
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_CYCLES, ALL_SUITS, SUIT_DISPLAY, CONSECUTIVE_FAILURES_NEEDED
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0: logger.error("API_ID manquant"); exit(1)
if not API_HASH: logger.error("API_HASH manquant"); exit(1)
if not BOT_TOKEN: logger.error("BOT_TOKEN manquant"); exit(1)

# ============================================================================
# STRUCTURE DE DONNÉES POUR LE SUIVI DES CYCLES
# ============================================================================

@dataclass
class SuitCycleTracker:
    """Tracker complet pour une couleur dans son cycle."""
    suit: str
    cycle_numbers: List[int] = field(default_factory=list)
    last_checked_index: int = -1
    miss_counter: int = 0
    current_tour: int = 1
    verification_history: List[dict] = field(default_factory=list)
    pending_prediction: Optional[int] = None

    def get_display_name(self) -> str:
        return {'♠': '♠️ Pique', '♥': '❤️ Cœur', '♦': '♦️ Carreau', '♣': '♣️ Trèfle'}[self.suit]

    def get_current_target(self) -> Optional[int]:
        if 0 <= self.last_checked_index < len(self.cycle_numbers):
            return self.cycle_numbers[self.last_checked_index]
        return None

    def get_next_target(self) -> Optional[int]:
        next_idx = self.last_checked_index + 1
        if next_idx < len(self.cycle_numbers):
            return self.cycle_numbers[next_idx]
        return None

    def process_verification(self, game_number: int, suit_found: bool) -> Optional[int]:
        """Traite une vérification et retourne le numéro de prédiction si déclenchée."""
        if game_number not in self.cycle_numbers:
            return None

        self.last_checked_index = self.cycle_numbers.index(game_number)

        self.verification_history.append({
            'game': game_number,
            'tour': self.current_tour,
            'found': suit_found,
            'miss_count_before': self.miss_counter,
            'time': datetime.now().isoformat()
        })

        if suit_found:
            # ✅ Trouvé ! Reset
            self.miss_counter = 0
            self.current_tour = 1
            self.pending_prediction = None
            return None

        # ❌ Manqué
        self.miss_counter += 1

        if self.miss_counter >= CONSECUTIVE_FAILURES_NEEDED:
            # 2 manques → PRÉDICTION au prochain numéro
            pred_idx = self.last_checked_index + 1
            if pred_idx < len(self.cycle_numbers):
                pred_num = self.cycle_numbers[pred_idx]
                self.pending_prediction = pred_num
                self.miss_counter = 0
                self.current_tour = 1
                return pred_num
        else:
            self.current_tour = 2

        return None

    def get_status_bar(self) -> str:
        filled = "█" * self.miss_counter
        empty = "░" * (CONSECUTIVE_FAILURES_NEEDED - self.miss_counter)
        return f"[{filled}{empty}]"

    def get_status_display(self) -> str:
        current = self.get_current_target()

        lines = [
            f"📊 {self.get_display_name()}",
            f"   ├─ 🎯 Numéro du cycle analysé: #{current if current else 'Aucun'}",
            f"   ├─ 📉 Compteur de manques: {self.miss_counter}/{CONSECUTIVE_FAILURES_NEEDED} {self.get_status_bar()}",
            f"   ├─ 🔄 Tour: {self.current_tour}/{CONSECUTIVE_FAILURES_NEEDED}",
        ]

        if self.pending_prediction:
            lines.append(f"   └─ 🔮 PRÉDICTION: Jouer #{self.pending_prediction}")
        elif self.miss_counter == 1:
            next_check = self.get_next_target()
            lines.append(f"   └─ ⏳ Prochaine vérif: #{next_check if next_check else 'N/A'}")
        else:
            lines.append(f"   └─ ✅ En attente")

        return "\n".join(lines)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

# Trackers pour chaque couleur
cycle_trackers: Dict[str, SuitCycleTracker] = {}

# Historique des jeux reçus: {game_number: [suits]}
game_history: Dict[int, List[str]] = {}

# Prédictions actives
pending_predictions: Dict[int, dict] = {}
queued_predictions: Dict[int, dict] = {}

# État
current_game_number = 0
last_source_game_number = 0
prediction_channel_ok = False
client = None

# Blocage par couleur
suit_block_until: Dict[str, datetime] = {}
suit_consecutive_predictions: Dict[str, int] = {}

# ============================================================================
# INITIALISATION DES TRACKERS
# ============================================================================

def initialize_trackers(max_game: int = 2000):
    """Crée les trackers pour chaque couleur avec leurs cycles."""
    global cycle_trackers

    for suit, config in SUIT_CYCLES.items():
        start = config['start']
        interval = config['interval']

        # Générer les numéros du cycle
        cycle_nums = []
        n = start
        while n <= max_game:
            cycle_nums.append(n)
            n += interval

        cycle_trackers[suit] = SuitCycleTracker(
            suit=suit,
            cycle_numbers=cycle_nums
        )

        logger.info(f"📊 {suit}: {len(cycle_nums)} numéros générés (1 à {max(cycle_nums) if cycle_nums else 0})")

# ============================================================================
# FONCTIONS DE TRAITEMENT
# ============================================================================

def is_prediction_time_allowed():
    now = datetime.now()
    if now.minute >= 30:
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        wait_minutes = 60 - now.minute
        return False, f"🚫 Bloqué (H:30-H:59). Prochaine: {next_hour.strftime('%H:%M')}"
    return True, f"✅ Autorisé ({now.strftime('%H:%M')})"

def extract_game_number(message: str) -> Optional[int]:
    # Format #N123 ou #N 123
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Format numéro isolé
    match = re.search(r"(?:^|[^\d])(\d{1,4})(?:[^\d]|$)", message)
    if match:
        num = int(match.group(1))
        if 1 <= num <= 2000:
            return num
    return None

def extract_suits_from_message(message: str) -> List[str]:
    """Extrait les couleurs présentes dans un message."""
    suits = []
    normalized = message.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')

    for suit in ALL_SUITS:
        if suit in normalized:
            suits.append(suit)
    return suits

def can_predict_suit(suit: str) -> tuple[bool, str]:
    """Vérifie si une couleur peut être prédite (pas bloquée)."""
    now = datetime.now()

    if suit in suit_block_until:
        if now < suit_block_until[suit]:
            remaining = suit_block_until[suit] - now
            return False, f"Bloqué ({remaining.seconds//60}min)"
        else:
            del suit_block_until[suit]

    return True, "OK"

def block_suit(suit: str, minutes: int = 5):
    """Bloque une couleur pendant X minutes."""
    suit_block_until[suit] = datetime.now() + timedelta(minutes=minutes)
    logger.info(f"🔒 {suit} bloqué {minutes}min")

# ============================================================================
# ENVOI DES PRÉDICTIONS (NOUVEAU FORMAT)
# ============================================================================

async def send_prediction(game_number: int, suit: str, reason: str = ""):
    """Envoie une prédiction avec le format demandé."""
    try:
        # Format demandé:
        # ⏳BACCARAT AI 🤖⏳
        # 
        # PLAYER : 761 ♠️ : en cours....

        msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {SUIT_DISPLAY.get(suit, suit)} : en cours...."""

        if not PREDICTION_CHANNEL_ID:
            logger.warning("⚠️ Canal non configuré")
            return None

        sent_msg = await client.send_message(PREDICTION_CHANNEL_ID, msg)

        pending_predictions[game_number] = {
            'message_id': sent_msg.id,
            'suit': suit,
            'status': 'en_cours',
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"✅ Prédiction envoyée: #{game_number} - {suit}")
        return sent_msg.id

    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        return None

async def update_prediction_result(game_number: int, found: bool):
    """Met à jour une prédiction avec le résultat."""
    if game_number not in pending_predictions:
        return

    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']

    status = "✅ Trouvé" if found else "❌ Manqué"

    new_msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {SUIT_DISPLAY.get(suit, suit)} : {status}"""

    try:
        await client.edit_message(PREDICTION_CHANNEL_ID, msg_id, new_msg)

        if found:
            logger.info(f"✅ Prédiction gagnante: #{game_number}")
        else:
            logger.info(f"❌ Prédiction perdue: #{game_number}")
            block_suit(suit, 5)  # Bloquer 5min après échec

        del pending_predictions[game_number]

    except Exception as e:
        logger.error(f"Erreur update: {e}")

# ============================================================================
# LOGIQUE PRINCIPALE
# ============================================================================

async def process_game_result(game_number: int, appeared_suits: List[str]):
    """Traite le résultat d'un jeu et met à jour les trackers."""
    global current_game_number, last_source_game_number

    current_game_number = game_number
    last_source_game_number = game_number
    game_history[game_number] = appeared_suits

    logger.info(f"📊 Jeu #{game_number} reçu: {appeared_suits if appeared_suits else 'aucune couleur'}")

    # 1. Vérifier si ce jeu correspond à une prédiction active
    if game_number in pending_predictions:
        pred_suit = pending_predictions[game_number]['suit']
        found = pred_suit in appeared_suits
        await update_prediction_result(game_number, found)
        return  # C'était un jeu de prédiction, on ne fait pas d'analyse cycle

    # 2. Analyser chaque couleur pour ce numéro
    predictions_triggered = []

    for suit, tracker in cycle_trackers.items():
        # Ce numéro fait-il partie du cycle de cette couleur?
        if game_number not in tracker.cycle_numbers:
            continue

        # Vérifier si la couleur était attendue
        suit_found = suit in appeared_suits

        # Traiter la vérification
        pred_num = tracker.process_verification(game_number, suit_found)

        if pred_num:
            # Une prédiction a été déclenchée
            predictions_triggered.append((suit, pred_num, tracker.miss_counter))
            logger.info(f"🎯 {suit}: 2 manques détectés → Prédiction #{pred_num}")

    # 3. Envoyer les prédictions déclenchées
    if predictions_triggered:
        can_send, time_msg = is_prediction_time_allowed()
        if not can_send:
            logger.info(f"⏰ {time_msg}")
            return

        for suit, pred_num, _ in predictions_triggered:
            can_pred, reason = can_predict_suit(suit)
            if not can_pred:
                logger.info(f"🚫 {suit} non prédit: {reason}")
                continue

            await send_prediction(pred_num, suit, "2 manques consécutifs")

async def handle_message(event):
    """Gère les nouveaux messages."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        if chat_id != SOURCE_CHANNEL_ID:
            return

        message_text = event.message.message
        game_number = extract_game_number(message_text)

        if game_number is None:
            return

        appeared_suits = extract_suits_from_message(message_text)
        await process_game_result(game_number, appeared_suits)

    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_status(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Réservé admin")
        return

    msg = "📈 **COMPTERS DE MANQUES DES CYCLES**\n\n"
    msg += f"🎮 Dernier jeu: #{current_game_number}\n\n"

    for suit in ALL_SUITS:
        if suit in cycle_trackers:
            tracker = cycle_trackers[suit]
            msg += tracker.get_status_display() + "\n\n"

    if pending_predictions:
        msg += "**🔮 PRÉDICTIONS ACTIVES:**\n"
        for num, pred in pending_predictions.items():
            msg += f"• #{num}: {pred['suit']} - {pred['status']}\n"

    await event.respond(msg)

async def cmd_help(event):
    if event.is_group or event.is_channel: return
    await event.respond("""📖 **BACCARAT AI - Aide**

**Système de comptage:**
Chaque couleur a un cycle fixe:
• ♠️ Pique: tous les 5 jeux
• ❤️ Cœur: tous les 6 jeux  
• ♦️ Carreau: tous les 6 jeux
• ♣️ Trèfle: tous les 7 jeux

**Compteur de manques:**
• 📉 Compte quand la couleur manque son numéro
• 🔄 Tour 1: 1er manque
• 🔄 Tour 2: 2ème manque
• 🔮 Prédiction après 2 manques

**Format:**
⏳BACCARAT AI 🤖⏳
PLAYER : {numéro} {couleur} : en cours....

**Commandes:**
/status - Voir les compteurs
/help - Cette aide
""")

def setup_handlers():
    client.add_event_handler(cmd_status, events.NewMessage(pattern='/status'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern='/help'))
    client.add_event_handler(handle_message, events.NewMessage())

# ============================================================================
# DÉMARRAGE
# ============================================================================

async def start_bot():
    global client, prediction_channel_ok

    session_string = os.getenv('TELEGRAM_SESSION', '')
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()

        # Initialiser les trackers
        initialize_trackers(2000)

        if PREDICTION_CHANNEL_ID:
            try:
                await client.get_entity(PREDICTION_CHANNEL_ID)
                prediction_channel_ok = True
                logger.info("✅ Canal prédiction OK")
            except Exception as e:
                logger.error(f"❌ Canal prédiction: {e}")

        return True
    except Exception as e:
        logger.error(f"Erreur démarrage: {e}")
        return False

async def main():
    try:
        if not await start_bot():
            return

        # Serveur web minimal
        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', PORT).start()

        logger.info("🤖 BACCARAT AI démarré")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté")
