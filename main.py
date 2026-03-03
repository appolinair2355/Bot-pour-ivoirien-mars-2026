import os
import asyncio
import re
import logging
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime, timedelta
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
client = None
suit_block_until: Dict[str, datetime] = {}

# Nombre de tours configurable
CONSECUTIVE_FAILURES_NEEDED = int(os.getenv('FAILURES_NEEDED', '2'))

# Timestamp de la dernière prédiction envoyée (pour auto-reset 1h)
last_prediction_time: Optional[datetime] = None

# Fuseau horaire Bénin = UTC+1
BENIN_UTC_OFFSET = timedelta(hours=1)

# Numéros de jeux déjà traités (pour ne jamais ignorer un numéro)
processed_games: set = set()
# Numéros en attente de finalisation
waiting_for_finalization: Dict[int, str] = {}

# ============================================================================
# DÉTECTION DES MESSAGES FINALISÉS
# ============================================================================

def is_message_finalized(message: str) -> bool:
    if '⏰' in message:
        return False
    if any(indicator in message for indicator in ['✅', '🔰']):
        return True
    return False

def extract_parentheses_groups(message: str) -> List[str]:
    scored_groups = re.findall(r"(\d+)?\(([^)]*)\)", message)
    if scored_groups:
        return [f"{score}:{content}" if score else content for score, content in scored_groups]
    return re.findall(r"\(([^)]*)\)", message)

def get_suits_in_group(group_str: str) -> List[str]:
    if ':' in group_str:
        group_str = group_str.split(':', 1)[1]
    suits = []
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    for suit in ALL_SUITS:
        if suit in normalized:
            suits.append(suit)
    return suits

# ============================================================================
# SUIVI DES CYCLES
# ============================================================================

@dataclass
class SuitCycleTracker:
    suit: str
    cycle_numbers: List[int] = field(default_factory=list)
    last_checked_index: int = -1
    miss_counter: int = 0
    current_tour: int = 1

    def get_display_name(self) -> str:
        return SUIT_DISPLAY.get(self.suit, self.suit)

    def get_current_target(self) -> Optional[int]:
        if self.last_checked_index >= 0 and self.last_checked_index < len(self.cycle_numbers):
            return self.cycle_numbers[self.last_checked_index]
        return None

    def get_next_target(self) -> Optional[int]:
        next_idx = self.last_checked_index + 1
        if next_idx < len(self.cycle_numbers):
            return self.cycle_numbers[next_idx]
        return None

    def process_verification(self, game_number: int, suit_found: bool) -> Optional[int]:
        if game_number not in self.cycle_numbers:
            return None

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
            self.current_tour = self.miss_counter + 1

        return None

def initialize_trackers(max_game: int = 2000):
    global cycle_trackers
    for suit, config in SUIT_CYCLES.items():
        start = config['start']
        interval = config['interval']
        cycle_nums = list(range(start, max_game + 1, interval))
        cycle_trackers[suit] = SuitCycleTracker(suit=suit, cycle_numbers=cycle_nums)
        logger.info(f"📊 {suit}: {len(cycle_nums)} numéros (jusqu'à {max(cycle_nums)})")

# ============================================================================
# ENVOI DES PRÉDICTIONS
# ============================================================================

async def send_prediction(game_number: int, suit: str, is_rattrapage: int = 0) -> Optional[int]:
    global last_prediction_time
    try:
        # Vérifier si la couleur est bloquée
        if suit in suit_block_until:
            if datetime.now() < suit_block_until[suit]:
                logger.info(f"🔒 {suit} bloqué, prédiction ignorée")
                return None
            else:
                del suit_block_until[suit]

        msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {SUIT_DISPLAY.get(suit, suit)} : en cours...."""

        if not PREDICTION_CHANNEL_ID:
            logger.warning("⚠️ Canal non configuré")
            return None

        sent_msg = await client.send_message(PREDICTION_CHANNEL_ID, msg)

        pending_predictions[game_number] = {
            'suit': suit,
            'message_id': sent_msg.id,
            'status': 'en_cours',
            'rattrapage': is_rattrapage,
            'original_game': game_number if is_rattrapage == 0 else None,
            'check_count': 0
        }

        last_prediction_time = datetime.now()
        logger.info(f"✅ Prédiction envoyée: #{game_number} - {suit} (rattrapage: {is_rattrapage})")
        return sent_msg.id

    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        return None

# ============================================================================
# VÉRIFICATION DES PRÉDICTIONS ✅0️⃣ ✅1️⃣ ✅2️⃣ ❌
# ============================================================================

async def check_prediction_result(game_number: int, first_group: str):
    # 1. VÉRIFICATION POUR LE JEU ORIGINAL (rattrapage 0)
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0 and pred['status'] == 'en_cours':
            target_suit = pred['suit']
            suits_in_result = get_suits_in_group(first_group)
            logger.info(f"🔍 Vérification #{game_number} (original): {target_suit} dans {suits_in_result}")

            if target_suit in suits_in_result:
                await update_prediction_message(game_number, '✅0️⃣', trouve=True)
                return True
            else:
                # Échec → lancer rattrapage 1
                next_game = game_number + 1
                logger.info(f"❌ #{game_number} échoué, rattrapage 1 sur #{next_game}")
                pending_predictions[game_number]['rattrapage'] = 1
                pending_predictions[game_number]['check_count'] = 1
                rattrapage_tracking[next_game] = game_number
                return False

    # 2. VÉRIFICATION POUR LES RATTRAPAGES
    if game_number in rattrapage_tracking:
        original_game = rattrapage_tracking[game_number]
        if original_game in pending_predictions:
            pred = pending_predictions[original_game]
            rattrapage_num = pred.get('rattrapage', 0)
            target_suit = pred['suit']
            suits_in_result = get_suits_in_group(first_group)

            logger.info(f"🔍 Vérification rattrapage {rattrapage_num} (#{game_number}): {target_suit} dans {suits_in_result}")

            if target_suit in suits_in_result:
                status = f'✅{rattrapage_num}️⃣'
                await update_prediction_message(original_game, status, trouve=True, rattrapage=rattrapage_num)
                # Nettoyer le tracking
                del rattrapage_tracking[game_number]
                return True
            else:
                # Nettoyer l'ancien tracking
                del rattrapage_tracking[game_number]

                if rattrapage_num < 2:
                    next_rattrapage = rattrapage_num + 1
                    next_game = original_game + next_rattrapage
                    logger.info(f"❌ Rattrapage {rattrapage_num} échoué, lancement {next_rattrapage} sur #{next_game}")
                    pending_predictions[original_game]['rattrapage'] = next_rattrapage
                    pending_predictions[original_game]['check_count'] = next_rattrapage
                    rattrapage_tracking[next_game] = original_game
                    return False
                else:
                    logger.info(f"❌ Rattrapage 2 échoué, prédiction perdue")
                    await update_prediction_message(original_game, '❌', trouve=False)
                    return False

    return False

async def update_prediction_message(game_number: int, status: str, trouve: bool, rattrapage: int = 0):
    if game_number not in pending_predictions:
        logger.warning(f"⚠️ Pas de prédiction trouvée pour #{game_number}")
        return

    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']

    # PAS de "GAGNÉ" — juste le statut emoji
    if status == '✅0️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} ✅0️⃣"
    elif status == '✅1️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} ✅1️⃣"
    elif status == '✅2️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} ✅2️⃣"
    else:
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} ❌"

    new_msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {result_line}"""

    try:
        await client.edit_message(PREDICTION_CHANNEL_ID, msg_id, new_msg)
        pred['status'] = status

        if trouve:
            logger.info(f"✅ Prédiction gagnante: #{game_number} - {status}")
        else:
            logger.info(f"❌ Prédiction perdue: #{game_number}")
            block_suit(suit, 5)

        del pending_predictions[game_number]

    except Exception as e:
        logger.error(f"❌ Erreur mise à jour message: {e}")

def block_suit(suit: str, minutes: int = 5):
    suit_block_until[suit] = datetime.now() + timedelta(minutes=minutes)
    logger.info(f"🔒 {suit} bloqué {minutes}min")

# ============================================================================
# TRAITEMENT DES MESSAGES ENTRANTS
# ============================================================================

async def process_game_result(game_number: int, message_text: str):
    global current_game_number, last_source_game_number

    # Marquer comme traité
    processed_games.add(game_number)
    # Retirer de la liste d'attente si présent
    if game_number in waiting_for_finalization:
        del waiting_for_finalization[game_number]

    current_game_number = game_number
    last_source_game_number = game_number

    groups = extract_parentheses_groups(message_text)
    if not groups:
        logger.warning(f"⚠️ Jeu #{game_number}: Aucun groupe de parenthèses trouvé")
        return

    first_group = groups[0]
    suits_in_first = get_suits_in_group(first_group)
    logger.info(f"📊 Jeu #{game_number} finalisé: {suits_in_first} dans '{first_group}'")

    # 1. VÉRIFIER LES PRÉDICTIONS ACTIVES
    await check_prediction_result(game_number, first_group)

    # 2. ANALYSE DES CYCLES (pour nouvelles prédictions)
    for suit, tracker in cycle_trackers.items():
        if game_number not in tracker.cycle_numbers:
            continue
        suit_found = suit in suits_in_first
        pred_num = tracker.process_verification(game_number, suit_found)
        if pred_num:
            await send_prediction(pred_num, suit, is_rattrapage=0)

async def handle_message(event):
    """Gère les NOUVEAUX messages du canal source."""
    await _process_source_event(event)

async def handle_edited_message(event):
    """Gère les messages ÉDITÉS du canal source (attente de finalisation)."""
    await _process_source_event(event)

async def _process_source_event(event):
    """Traitement commun pour nouveaux messages et messages édités."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        if chat_id != SOURCE_CHANNEL_ID:
            return

        message_text = event.message.message

        # Extraire le numéro de jeu
        match = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
        if not match:
            match = re.search(r"(?:^|[^\d])(\d{1,4})(?:[^\d]|$)", message_text)
        if not match:
            return

        game_number = int(match.group(1))

        # Si déjà traité, ignorer
        if game_number in processed_games:
            return

        # VÉRIFIER SI FINALISÉ (contient ✅ ou 🔰, pas ⏰)
        if not is_message_finalized(message_text):
            # Pas encore finalisé → mettre en attente
            if game_number not in waiting_for_finalization:
                waiting_for_finalization[game_number] = message_text
                logger.info(f"⏳ #{game_number} en attente de finalisation...")
            return

        # Message finalisé → traiter
        logger.info(f"✅ Message #{game_number} finalisé, traitement...")
        await process_game_result(game_number, message_text)

    except Exception as e:
        logger.error(f"❌ Erreur traitement message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ============================================================================
# AUTO-RESET SYSTÈME
# ============================================================================

async def auto_reset_loop():
    """
    Boucle d'auto-reset:
    1. Si aucune prédiction envoyée depuis 1h → reset tout ce qui bloque
    2. À 1h00 du matin heure Bénin → reset complet quotidien
    """
    global last_prediction_time
    last_daily_reset_date = None

    while True:
        try:
            await asyncio.sleep(60)  # Vérifier toutes les minutes

            now = datetime.utcnow()
            benin_now = now + BENIN_UTC_OFFSET

            # === RESET QUOTIDIEN À 1H00 BÉNIN ===
            benin_date = benin_now.date()
            if benin_now.hour == 1 and benin_now.minute == 0 and last_daily_reset_date != benin_date:
                last_daily_reset_date = benin_date
                logger.info("🔄 RESET QUOTIDIEN 1H00 BÉNIN - Nettoyage complet")
                await perform_full_reset("Reset quotidien 1h00 Bénin")
                continue

            # === RESET AUTO APRÈS 1H SANS PRÉDICTION ===
            if last_prediction_time:
                elapsed = datetime.now() - last_prediction_time
                if elapsed > timedelta(hours=1) and (pending_predictions or suit_block_until or waiting_for_finalization):
                    logger.info(f"⏰ 1h sans prédiction ({elapsed}), auto-reset des blocages")
                    await perform_auto_unblock()

        except Exception as e:
            logger.error(f"❌ Erreur auto_reset_loop: {e}")

async def perform_auto_unblock():
    """Débloquer tout ce qui empêche les prédictions après 1h d'inactivité."""
    global last_prediction_time

    # Effacer les blocages de couleurs
    suit_block_until.clear()
    logger.info("🔓 Tous les blocages de couleurs supprimés")

    # Effacer les prédictions en attente (stagnantes)
    old_predictions = list(pending_predictions.keys())
    for gn in old_predictions:
        del pending_predictions[gn]
    logger.info(f"🗑️ {len(old_predictions)} prédiction(s) en attente supprimée(s)")

    # Effacer les rattrapages
    rattrapage_tracking.clear()
    logger.info("🗑️ Rattrapages en cours supprimés")

    # Effacer les jeux en attente de finalisation
    waiting_for_finalization.clear()
    logger.info("🗑️ Jeux en attente de finalisation supprimés")

    # Reset le timer
    last_prediction_time = None

    # Envoyer notification au canal
    try:
        if PREDICTION_CHANNEL_ID and client:
            await client.send_message(PREDICTION_CHANNEL_ID,
                "🔄 **AUTO-RESET** — 1h sans activité, tous les blocages ont été supprimés. Prédictions relancées.")
    except Exception as e:
        logger.error(f"Erreur notification auto-reset: {e}")

async def perform_full_reset(reason: str = "Reset complet"):
    """Reset complet quotidien — remet tout à zéro."""
    global current_game_number, last_source_game_number, last_prediction_time

    # Effacer tout
    pending_predictions.clear()
    rattrapage_tracking.clear()
    suit_block_until.clear()
    waiting_for_finalization.clear()
    processed_games.clear()

    # Réinitialiser les trackers
    for tracker in cycle_trackers.values():
        tracker.last_checked_index = -1
        tracker.miss_counter = 0
        tracker.current_tour = 1

    current_game_number = 0
    last_source_game_number = 0
    last_prediction_time = None

    logger.info(f"🔄 {reason} — Tout a été réinitialisé")

    # Notification canal
    try:
        if PREDICTION_CHANNEL_ID and client:
            benin_now = datetime.utcnow() + BENIN_UTC_OFFSET
            await client.send_message(PREDICTION_CHANNEL_ID,
                f"""🔄 **RESET COMPLET** 🔄

📅 {benin_now.strftime('%d/%m/%Y %H:%M')} (Heure Bénin)
🗑️ Compteurs de costumes réinitialisés
🔓 Tous les blocages supprimés
🎯 Prêt pour une nouvelle session

⏳BACCARAT AI 🤖⏳""")
    except Exception as e:
        logger.error(f"Erreur notification reset: {e}")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    msg = f"""📈 **COUNTERS DE MANQUES DES CYCLES**

🎮 Dernier jeu: #{current_game_number}
📋 File d'attente: {len(pending_predictions)} prédiction(s)

"""

    for suit in ALL_SUITS:
        if suit in cycle_trackers:
            tracker = cycle_trackers[suit]
            current = tracker.get_current_target()

            filled = "█" * tracker.miss_counter
            empty = "░" * (CONSECUTIVE_FAILURES_NEEDED - tracker.miss_counter)
            progress_bar = f"[{filled}{empty}]"

            current_display = f"#{current}" if current else "#Aucun"

            msg += f"""📊 {tracker.get_display_name()}
   ├─ 🎯 Numéro du cycle analysé: {current_display}
   ├─ 📉 Compteur de manques: {tracker.miss_counter}/{CONSECUTIVE_FAILURES_NEEDED} {progress_bar}
   ├─ 🔄 Tour: {tracker.current_tour}/{CONSECUTIVE_FAILURES_NEEDED}
   └─ ✅ En attente

"""

    # Prédictions actives
    if pending_predictions:
        msg += "**🔮 PRÉDICTIONS ACTIVES:**\n"
        for num, pred in sorted(pending_predictions.items()):
            ratt = f" (R{pred['rattrapage']})" if pred.get('rattrapage', 0) > 0 else ""
            msg += f"• #{num}{ratt}: {pred['suit']} - {pred['status']}\n"
    else:
        msg += "**🔮 Aucune prédiction active**\n"

    # Couleurs bloquées
    if suit_block_until:
        msg += "\n**🔒 COULEURS BLOQUÉES:**\n"
        for s, until in suit_block_until.items():
            remaining = until - datetime.now()
            if remaining.total_seconds() > 0:
                msg += f"• {SUIT_DISPLAY.get(s, s)}: encore {int(remaining.total_seconds()//60)}min\n"

    await event.respond(msg)

async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    help_text = """📖 **BACCARAT AI - AIDE COMPLÈTE**

═══════════════════════════════════════
🎮 **SYSTÈME DE PRÉDICTION**
═══════════════════════════════════════

**Cycles des couleurs:**
• ♠️ Pique: tous les 5 jeux (1, 6, 11, 16...)
• ❤️ Cœur: tous les 6 jeux (1, 7, 13, 19...)
• ♦️ Carreau: tous les 6 jeux (1, 7, 13, 19...)
• ♣️ Trèfle: tous les 7 jeux (1, 8, 15, 22...)

**Système de rattrapages:**
• ✅0️⃣ = Trouvé au numéro prédit
• ✅1️⃣ = Trouvé au numéro+1 (rattrapage 1)
• ✅2️⃣ = Trouvé au numéro+2 (rattrapage 2)
• ❌ = Perdu après 3 tentatives

**Détection des messages:**
Le bot attend que chaque message soit **finalisé** (✅ ou 🔰) avant de traiter.
Les messages édités sont aussi surveillés.

═══════════════════════════════════════
🔧 **COMMANDES ADMIN**
═══════════════════════════════════════

**/status** — Compteurs de costumes + prédictions actives
**/set_tours [1-5]** — Nombre de manques avant prédiction
**/channels** — Canaux configurés
**/test** — Test d'envoi
**/announce [message]** — Annonce au canal
**/reset** — Reset complet manuel
**/help** — Cette aide

═══════════════════════════════════════
🔄 **AUTO-RESET**
═══════════════════════════════════════

• Après **1h sans prédiction** → déblocage automatique
• À **1h00 du matin** (heure Bénin) → reset complet quotidien

═══════════════════════════════════════
👨‍💻 **DÉVELOPPEUR**
═══════════════════════════════════════

**Sossou Kouamé**
📱 WhatsApp: +229 01 95 50 15 64 😂

⏳BACCARAT AI 🤖⏳
"""
    await event.respond(help_text)

async def cmd_reset(event):
    """Commande manuelle de reset complet."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    await perform_full_reset("Reset manuel par admin")
    await event.respond("✅ **Reset complet effectué!** Compteurs, blocages et prédictions réinitialisés.")

async def cmd_set_tours(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    global CONSECUTIVE_FAILURES_NEEDED

    try:
        match = re.search(r'/set_tours\s+(\d+)', event.message.message)
        if not match:
            await event.respond("📖 Usage: `/set_tours [1-5]`\nEx: `/set_tours 2`")
            return

        new_value = int(match.group(1))
        if new_value < 1 or new_value > 5:
            await event.respond("❌ Entre 1 et 5 uniquement")
            return

        old_value = CONSECUTIVE_FAILURES_NEEDED
        CONSECUTIVE_FAILURES_NEEDED = new_value

        for tracker in cycle_trackers.values():
            tracker.miss_counter = 0
            tracker.current_tour = 1

        await event.respond(f"✅ Changé: {old_value} → {new_value} tours\nLes compteurs ont été réinitialisés.")

    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

async def cmd_channels(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    source_status = "❌ Inaccessible"
    prediction_status = "❌ Inaccessible"

    try:
        if SOURCE_CHANNEL_ID:
            await client.get_entity(SOURCE_CHANNEL_ID)
            source_status = "✅ Accessible"
    except:
        pass

    try:
        if PREDICTION_CHANNEL_ID:
            await client.get_entity(PREDICTION_CHANNEL_ID)
            prediction_status = "✅ Accessible"
    except:
        pass

    msg = f"""📡 **CANAUX CONFIGURÉS**

🔹 **Canal Source**
   ├─ ID: `{SOURCE_CHANNEL_ID}`
   └─ Statut: {source_status}

🔹 **Canal Prédiction**
   ├─ ID: `{PREDICTION_CHANNEL_ID}`
   └─ Statut: {prediction_status}

🔹 **Admin ID**: `{ADMIN_ID}`

💡 Vérifiez que le bot est membre des canaux avec les bonnes permissions."""
    await event.respond(msg)

async def cmd_test(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    await event.respond("🧪 **TEST EN COURS**...")

    try:
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ Canal non configuré")
            return

        try:
            entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            canal_nom = getattr(entity, 'title', 'Sans titre')
        except Exception as e:
            await event.respond(f"❌ Accès canal impossible: {e}")
            return

        test_msg = """⏳BACCARAT AI 🤖⏳ [TEST]

PLAYER : 9999 ♠️ : TEST EN COURS...."""
        sent = await client.send_message(PREDICTION_CHANNEL_ID, test_msg)
        await asyncio.sleep(2)

        result_msg = """⏳BACCARAT AI 🤖⏳ [TEST]

PLAYER : 9999 ♠️ ✅0️⃣"""
        await client.edit_message(PREDICTION_CHANNEL_ID, sent.id, result_msg)
        await asyncio.sleep(2)
        await client.delete_messages(PREDICTION_CHANNEL_ID, [sent.id])

        await event.respond(f"""✅ **TEST RÉUSSI!**

📋 Résultats:
   ├─ Canal: {canal_nom}
   ├─ Envoi: ✅ OK
   ├─ Modification: ✅ OK
   └─ Suppression: ✅ OK

🎯 L'envoi automatique fonctionne!""")

    except Exception as e:
        await event.respond(f"❌ Échec: {e}")

async def cmd_announce(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Réservé admin")
        return

    full_message = event.message.message
    if full_message.strip() in ['/announce', '/annonce']:
        await event.respond("📢 Usage: `/announce Votre message`")
        return

    parts = full_message.split(' ', 1)
    if len(parts) < 2:
        await event.respond("❌ Fournissez un message.")
        return

    user_text = parts[1].strip()
    if len(user_text) > 500:
        await event.respond("❌ Trop long (max 500)")
        return

    try:
        now = datetime.now()
        announce_msg = f"""╔══════════════════════════════════════╗
║     📢 ANNONCE OFFICIELLE 📢          ║
╠══════════════════════════════════════╣

{user_text}

╠══════════════════════════════════════╣
║  📅 {now.strftime('%d/%m/%Y')}  🕐 {now.strftime('%H:%M')}
╠══════════════════════════════════════╣
║  👨‍💻 Développé par: **Sossou Kouamé**
║  📱 WhatsApp: **+229 01 95 50 15 64** 😂
╚══════════════════════════════════════╝

⏳BACCARAT AI 🤖⏳"""

        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ Canal non configuré")
            return

        sent = await client.send_message(PREDICTION_CHANNEL_ID, announce_msg)
        await event.respond(f"✅ Annonce envoyée (ID: {sent.id})")

    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

# ============================================================================
# SETUP & DÉMARRAGE
# ============================================================================

def setup_handlers():
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_channels, events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_test, events.NewMessage(pattern=r'^/test$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_set_tours, events.NewMessage(func=lambda e: e.message.message.strip().startswith('/set_tours')))
    client.add_event_handler(cmd_announce, events.NewMessage(func=lambda e: e.message.message.strip().startswith('/announce')))

    # Messages entrants (nouveaux)
    client.add_event_handler(handle_message, events.NewMessage())
    # Messages ÉDITÉS (pour attraper les finalisations)
    client.add_event_handler(handle_edited_message, events.MessageEdited())

async def start_bot():
    global client, prediction_channel_ok

    session_string = os.getenv('TELEGRAM_SESSION', '')
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()
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

        # Lancer la boucle d'auto-reset en arrière-plan
        asyncio.create_task(auto_reset_loop())

        # Serveur web
        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', PORT).start()

        logger.info("🤖 BACCARAT AI avec rattrapages + auto-reset démarré!")
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
