# main.py
import os
import asyncio
import re
import logging
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_CYCLES, ALL_SUITS, SUIT_DISPLAY,
    CONSECUTIVE_FAILURES_NEEDED, NUMBERS_PER_TOUR
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0: 
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH: 
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN: 
    logger.error("BOT_TOKEN manquant")
    exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

cycle_trackers: Dict[str, 'SuitCycleTracker'] = {}
pending_predictions: Dict[int, dict] = {}
current_game_number = 0
last_source_game_number = 0
last_prediction_time: Optional[datetime] = None
prediction_channel_ok = False
client = None
suit_block_until: Dict[str, datetime] = {}
waiting_finalization: Dict[int, dict] = {}

# ============================================================================
# CLASSES
# ============================================================================

@dataclass
class SuitCycleTracker:
    """Tracker pour suivre les cycles d'une couleur."""
    suit: str
    cycle_numbers: List[int] = field(default_factory=list)
    current_tour: int = 1
    miss_counter: int = 0
    pending_prediction: Optional[int] = None
    tour_checked_numbers: Set[int] = field(default_factory=set)
    verification_history: Dict[int, bool] = field(default_factory=dict)
    last_cycle_index: int = -1
    
    def get_display_name(self) -> str:
        names = {
            '♠': '♠️ Pique',
            '♥': '❤️ Cœur',
            '♦': '♦️ Carreau',
            '♣': '♣️ Trèfle'
        }
        return names.get(self.suit, self.suit)
    
    def update_to_current_game(self, game_number: int):
        """
        Met à jour le last_cycle_index pour pointer sur le bon cycle
        par rapport au numéro de jeu actuel.
        """
        # Trouver le dernier cycle <= game_number
        new_index = -1
        for i, cycle_num in enumerate(self.cycle_numbers):
            if cycle_num <= game_number:
                new_index = i
            else:
                break
        
        if new_index != self.last_cycle_index and new_index >= 0:
            # On a avancé dans les cycles
            old_cycle = self.cycle_numbers[self.last_cycle_index] if self.last_cycle_index >= 0 else 'N/A'
            new_cycle = self.cycle_numbers[new_index]
            logger.info(f"🔄 {self.suit} avance: cycle #{old_cycle} → #{new_cycle} (jeu #{game_number})")
            
            # Si on change de cycle, reset le tour en cours
            if self.last_cycle_index >= 0 and new_index > self.last_cycle_index:
                # On passe à un nouveau cycle, reset les compteurs
                self.tour_checked_numbers.clear()
                self.verification_history.clear()
                # Ne pas reset miss_counter si on est en plein tour
                if self.current_tour == 1:
                    self.miss_counter = 0
            
            self.last_cycle_index = new_index
    
    def get_current_cycle_target(self) -> Optional[int]:
        """Retourne le numéro de cycle actuel."""
        if self.last_cycle_index >= 0 and self.last_cycle_index < len(self.cycle_numbers):
            return self.cycle_numbers[self.last_cycle_index]
        
        # Initialisation - trouver le premier cycle
        for i, num in enumerate(self.cycle_numbers):
            if num >= current_game_number:
                self.last_cycle_index = max(0, i - 1)
                return self.cycle_numbers[self.last_cycle_index] if self.last_cycle_index >= 0 else num
        return self.cycle_numbers[-1] if self.cycle_numbers else None
    
    def get_numbers_to_check_this_tour(self) -> List[int]:
        """Retourne les 3 numéros à vérifier pour le tour actuel."""
        current_cycle = self.get_current_cycle_target()
        if current_cycle is None:
            return []
        
        if self.current_tour == 1:
            # Tour 1: cycle, cycle+1, cycle+2
            return [current_cycle, current_cycle + 1, current_cycle + 2]
        else:
            # Tour 2: prochain cycle et les 2 suivants
            next_idx = self.last_cycle_index + 1
            if next_idx < len(self.cycle_numbers):
                next_cycle = self.cycle_numbers[next_idx]
                return [next_cycle, next_cycle + 1, next_cycle + 2]
            return []
    
    def is_number_in_current_tour(self, game_number: int) -> bool:
        """Vérifie si le numéro fait partie du tour actuel."""
        # D'abord mettre à jour si nécessaire
        self.update_to_current_game(game_number)
        return game_number in self.get_numbers_to_check_this_tour()
    
    def process_verification(self, game_number: int, suit_found: bool) -> Optional[int]:
        """
        Traite la vérification d'un numéro.
        """
        # Mettre à jour le cycle en fonction du jeu actuel
        self.update_to_current_game(game_number)
        
        # Vérifier si ce numéro est dans le tour actuel
        if not self.is_number_in_current_tour(game_number):
            return None
        
        # Éviter les doublons
        if game_number in self.tour_checked_numbers:
            return None
        
        self.tour_checked_numbers.add(game_number)
        self.verification_history[game_number] = suit_found
        
        if suit_found:
            logger.info(f"✅ {self.suit} trouvé au jeu #{game_number} (Tour {self.current_tour})")
            self.reset()
            return None
        
        # Pas trouvé
        tour_misses = len(self.tour_checked_numbers)
        logger.info(f"❌ {self.suit} manqué au jeu #{game_number} (Tour {self.current_tour}, {tour_misses}/{NUMBERS_PER_TOUR})")
        
        # Tour terminé (3 numéros vérifiés)
        if tour_misses >= NUMBERS_PER_TOUR:
            self.miss_counter += 1
            logger.info(f"📊 {self.suit} Tour {self.current_tour} terminé - Manques: {self.miss_counter}/{CONSECUTIVE_FAILURES_NEEDED}")
            
            # Assez de manques pour prédire
            if self.miss_counter >= CONSECUTIVE_FAILURES_NEEDED:
                pred_idx = self.last_cycle_index + self.miss_counter + 1
                if pred_idx < len(self.cycle_numbers):
                    pred_num = self.cycle_numbers[pred_idx]
                    self.pending_prediction = pred_num
                    logger.info(f"🔮 {self.suit} PRÉDICTION pour #{pred_num}")
                    self.reset_after_prediction()
                    return pred_num
            
            # Passer au tour suivant
            self.current_tour += 1
            self.tour_checked_numbers.clear()
            self.last_cycle_index += 1
            logger.info(f"🔄 {self.suit} passe au Tour {self.current_tour}")
        
        return None
    
    def reset(self):
        """Reset complet."""
        self.current_tour = 1
        self.miss_counter = 0
        self.tour_checked_numbers.clear()
        self.pending_prediction = None
        self.verification_history.clear()
    
    def reset_after_prediction(self):
        """Reset après création d'une prédiction."""
        self.current_tour = 1
        self.miss_counter = 0
        self.tour_checked_numbers.clear()
        self.verification_history.clear()

def initialize_trackers(max_game: int = 3000):
    """Initialise les trackers pour chaque couleur."""
    global cycle_trackers
    
    for suit, config in SUIT_CYCLES.items():
        start = config['start']
        interval = config['interval']
        cycle_nums = list(range(start, max_game + 1, interval))
        
        cycle_trackers[suit] = SuitCycleTracker(suit=suit, cycle_numbers=cycle_nums)
        logger.info(f"📊 {suit}: cycle +{interval}, {len(cycle_nums)} numéros (1 à {max(cycle_nums)})")

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé (contient ✅ ou 🔰)."""
    if '⏰' in message or '⏳' in message:
        return False
    
    lower_msg = message.lower()
    if any(word in lower_msg for word in ['en cours', 'attente', 'pending', 'wait', 'waiting']):
        return False
    
    return '✅' in message or '🔰' in message

def extract_parentheses_groups(message: str) -> List[str]:
    """Extrait le contenu entre parenthèses."""
    scored_groups = re.findall(r"(\d+)?\(([^)]*)\)", message)
    if scored_groups:
        return [f"{score}:{content}" if score else content for score, content in scored_groups]
    return re.findall(r"\(([^)]*)\)", message)

def get_suits_in_group(group_str: str) -> List[str]:
    """Extrait les couleurs d'un groupe."""
    if ':' in group_str:
        group_str = group_str.split(':', 1)[1]
    
    normalized = group_str
    for old, new in [('❤️', '♥'), ('❤', '♥'), ('♥️', '♥'),
                     ('♠️', '♠'), ('♦️', '♦'), ('♣️', '♣')]:
        normalized = normalized.replace(old, new)
    
    return [suit for suit in ALL_SUITS if suit in normalized]

def block_suit(suit: str, minutes: int = 5):
    """Bloque une couleur."""
    suit_block_until[suit] = datetime.now() + timedelta(minutes=minutes)
    logger.info(f"🔒 {suit} bloqué {minutes}min")

# ============================================================================
# GESTION DES PRÉDICTIONS
# ============================================================================

async def send_prediction(game_number: int, suit: str, is_rattrapage: int = 0) -> Optional[int]:
    """Envoie une prédiction."""
    global last_prediction_time
    
    try:
        if suit in suit_block_until and datetime.now() < suit_block_until[suit]:
            logger.info(f"🔒 {suit} bloqué")
            return None
        
        msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {SUIT_DISPLAY.get(suit, suit)} : en cours...."""
        
        if not PREDICTION_CHANNEL_ID:
            logger.warning("⚠️ Canal prédiction non configuré")
            return None
        
        sent = await client.send_message(PREDICTION_CHANNEL_ID, msg)
        last_prediction_time = datetime.now()
        
        pending_predictions[game_number] = {
            'suit': suit,
            'message_id': sent.id,
            'status': 'en_cours',
            'rattrapage': is_rattrapage,
            'original_game': game_number if is_rattrapage == 0 else None,
            'awaiting_rattrapage': 0,
            'sent_time': datetime.now()
        }
        
        logger.info(f"✅ Prédiction envoyée: #{game_number} {suit}")
        return sent.id
        
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        return None

async def check_prediction_result(game_number: int, first_group: str) -> bool:
    """Vérifie si une prédiction est gagnante."""
    suits_in_result = get_suits_in_group(first_group)
    
    # Vérification jeu original
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            logger.info(f"🔍 Vérif #{game_number} original: {target_suit} dans {suits_in_result}")
            
            if target_suit in suits_in_result:
                await update_prediction_message(game_number, '✅0️⃣', True)
                return True
            else:
                pred['awaiting_rattrapage'] = 1
                logger.info(f"❌ #{game_number} échoué, attente #{game_number + 1}")
                return False
    
    # Vérification rattrapages
    for original_game, pred in list(pending_predictions.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        if awaiting > 0 and game_number == original_game + awaiting:
            target_suit = pred['suit']
            logger.info(f"🔍 Vérif rattrapage {awaiting} #{game_number}: {target_suit}")
            
            if target_suit in suits_in_result:
                status = f'✅{awaiting}️⃣'
                await update_prediction_message(original_game, status, True, awaiting)
                return True
            else:
                if awaiting < 2:
                    pred['awaiting_rattrapage'] = awaiting + 1
                    logger.info(f"❌ R{awaiting} échoué, attente #{original_game + awaiting + 1}")
                    return False
                else:
                    logger.info(f"❌ R2 échoué, perdu")
                    await update_prediction_message(original_game, '❌', False)
                    return False
    
    return False

async def update_prediction_message(game_number: int, status: str, trouve: bool, rattrapage: int = 0):
    """Met à jour le statut d'une prédiction."""
    if game_number not in pending_predictions:
        return
    
    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    
    if status == '✅0️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ✅0️⃣ GAGNÉ"
    elif status == '✅1️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ✅1️⃣ GAGNÉ"
    elif status == '✅2️⃣':
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ✅2️⃣ GAGNÉ"
    else:
        result_line = f"{SUIT_DISPLAY.get(suit, suit)} : ❌ PERDU 😭"
    
    new_msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {result_line}"""
    
    try:
        await client.edit_message(PREDICTION_CHANNEL_ID, msg_id, new_msg)
        pred['status'] = status
        
        if trouve:
            logger.info(f"✅ Gagné: #{game_number} {status}")
        else:
            logger.info(f"❌ Perdu: #{game_number}")
            block_suit(suit, 5)
        
        del pending_predictions[game_number]
        
    except Exception as e:
        logger.error(f"❌ Erreur update message: {e}")

# ============================================================================
# TRAITEMENT DES MESSAGES
# ============================================================================

async def process_game_result(game_number: int, message_text: str):
    """Traite un résultat de jeu finalisé."""
    global current_game_number, last_source_game_number
    
    current_game_number = game_number
    last_source_game_number = game_number
    
    # Mettre à jour tous les trackers pour suivre le numéro actuel
    for tracker in cycle_trackers.values():
        tracker.update_to_current_game(game_number)
    
    groups = extract_parentheses_groups(message_text)
    if not groups:
        logger.warning(f"⚠️ Pas de groupe trouvé dans #{game_number}")
        return
    
    first_group = groups[0]
    suits_in_first = get_suits_in_group(first_group)
    
    logger.info(f"📊 Jeu #{game_number}: {suits_in_first} dans '{first_group[:30]}...'")
    
    # 1. Vérifier prédictions actives
    if await check_prediction_result(game_number, first_group):
        return
    
    # 2. Analyse des cycles pour nouvelles prédictions
    for suit, tracker in cycle_trackers.items():
        pred_num = tracker.process_verification(game_number, suit in suits_in_first)
        if pred_num:
            await send_prediction(pred_num, suit, 0)

async def handle_message(event, is_edit: bool = False):
    """Gère les messages entrants et édités."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
        
        if chat_id != SOURCE_CHANNEL_ID:
            return
        
        message_text = event.message.message
        edit_info = " [EDITÉ]" if is_edit else ""
        logger.info(f"📨{edit_info} Msg {event.message.id}: {message_text[:60]}...")
        
        if not is_message_finalized(message_text):
            if '⏰' in message_text:
                match = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
                if match:
                    waiting_finalization[int(match.group(1))] = {
                        'msg_id': event.message.id,
                        'text': message_text
                    }
            logger.info(f"⏳ Non finalisé ignoré")
            return
        
        match = re.search(r"#N\s*(\d+)", message_text, re.IGNORECASE)
        if not match:
            match = re.search(r"(?:^|[^\d])(\d{3,4})(?:[^\d]|$)", message_text)
        
        if not match:
            logger.warning("⚠️ Numéro non trouvé")
            return
        
        game_number = int(match.group(1))
        
        if game_number in waiting_finalization:
            del waiting_finalization[game_number]
        
        await process_game_result(game_number, message_text)
        
    except Exception as e:
        logger.error(f"❌ Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def handle_new_message(event):
    await handle_message(event, False)

async def handle_edited_message(event):
    await handle_message(event, True)

# ============================================================================
# RESET AUTOMATIQUE
# ============================================================================

async def auto_reset_system():
    """Reset automatique après 1h ou à 1h00."""
    global last_prediction_time
    
    while True:
        try:
            now = datetime.now()
            
            if now.hour == 1 and now.minute == 0:
                logger.info("🕐 Reset 1h00")
                await perform_full_reset("🕐 Reset automatique 1h00")
                await asyncio.sleep(60)
            
            if last_prediction_time:
                elapsed = now - last_prediction_time
                if elapsed > timedelta(hours=1) and pending_predictions:
                    logger.info(f"⏰ Reset inactivité ({elapsed.total_seconds()/3600:.1f}h)")
                    await perform_full_reset("⏰ Reset inactivité 1h")
            
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"❌ Erreur auto_reset: {e}")
            await asyncio.sleep(60)

async def perform_full_reset(reason: str):
    """Effectue un reset complet."""
    global pending_predictions, last_prediction_time, waiting_finalization
    
    stats = len(pending_predictions)
    
    for tracker in cycle_trackers.values():
        tracker.reset()
    
    pending_predictions.clear()
    waiting_finalization.clear()
    last_prediction_time = None
    suit_block_until.clear()
    
    logger.info(f"🔄 {reason} - {stats} prédictions cleared")
    
    try:
        if PREDICTION_CHANNEL_ID and client and client.is_connected():
            await client.send_message(
                PREDICTION_CHANNEL_ID,
                f"""🔄 **RESET SYSTÈME**

{reason}

✅ Compteurs remis à zéro
✅ {stats} prédictions cleared
✅ Nouvelle analyse

⏳BACCARAT AI 🤖⏳"""
            )
    except Exception as e:
        logger.error(f"❌ Notif reset failed: {e}")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_status(event):
    """Affiche les compteurs détaillés."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    lines = [
        "📈 **COUNTERS DE MANQUES DES CYCLES**",
        "",
        f"🎮 Dernier jeu: #{current_game_number}",
        f"📋 Prédictions actives: {len(pending_predictions)}",
        f"⏳ En attente finalisation: {len(waiting_finalization)}",
        ""
    ]
    
    for suit in ALL_SUITS:
        if suit not in cycle_trackers:
            continue
        
        tracker = cycle_trackers[suit]
        
        # Forcer la mise à jour au numéro actuel
        tracker.update_to_current_game(current_game_number)
        
        current = tracker.get_current_cycle_target()
        to_check = tracker.get_numbers_to_check_this_tour()
        checked = tracker.tour_checked_numbers
        
        progress = len(checked)
        bar = f"[{'█' * progress}{'░' * (NUMBERS_PER_TOUR - progress)}]"
        
        if tracker.pending_prediction:
            emoji, status = "🔮", f"PRÉDICTION #{tracker.pending_prediction}"
        elif tracker.current_tour == 2:
            emoji, status = "⚠️", f"Tour 2 critique"
        elif progress > 0:
            emoji, status = "⏳", f"Tour {tracker.current_tour} en cours"
        else:
            emoji, status = "✅", "En attente"
        
        nums = []
        for n in to_check:
            if n in checked:
                found = tracker.verification_history.get(n, False)
                nums.append(f"{'✅' if found else '❌'}{n}")
            else:
                nums.append(f"⏳{n}")
        
        lines.extend([
            f"📊 {tracker.get_display_name()} {emoji}",
            f"   ├─ 🎯 Cycle: #{current if current else 'N/A'}",
            f"   ├─ 🔄 Tour: {tracker.current_tour}/{CONSECUTIVE_FAILURES_NEEDED}",
            f"   ├─ 📉 Manques: {tracker.miss_counter}/{CONSECUTIVE_FAILURES_NEEDED}",
            f"   ├─ 🔍 {bar} ({progress}/{NUMBERS_PER_TOUR})",
            f"   ├─ 🎲 {' → '.join(nums) if nums else 'N/A'}",
            f"   └─ 📌 {status}",
            ""
        ])
    
    if pending_predictions:
        lines.append("**🔮 PRÉDICTIONS ACTIVES:**")
        for num, pred in sorted(pending_predictions.items()):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            r = pred.get('rattrapage', 0)
            ar = pred.get('awaiting_rattrapage', 0)
            
            type_str = f"R{r}" if r > 0 else "ORIGINAL"
            if ar > 0:
                status_str = f"attente R{ar} (#{num + ar})"
            else:
                status_str = pred['status']
            
            lines.append(f"• #{num} {suit} ({type_str}): {status_str}")
        lines.append("")
    
    lines.extend([
        "**Légende:**",
        "✅=Trouvé ❌=Manqué ⏳=Attente 🔮=Prédiction ⚠️=Critique"
    ])
    
    await event.respond("\n".join(lines))

async def cmd_help(event):
    """Affiche l'aide."""
    if event.is_group or event.is_channel:
        return
    
    help_text = f"""📖 **BACCARAT AI - AIDE**

**Système ({NUMBERS_PER_TOUR} numéros/tour, {CONSECUTIVE_FAILURES_NEEDED} tours):**

• Tour 1: vérifie cycle, cycle+1, cycle+2
• Tour 2: vérifie next_cycle, next_cycle+1, next_cycle+2
→ Si 2 tours sans trouver = PRÉDICTION

**Rattrapages:** ✅0️⃣ ✅1️⃣ ✅2️⃣ ❌

**Commandes:**
/status - Voir les compteurs
/set_tours [1-3] - Changer tours
/reset - Reset manuel
/channels - Config canaux
/test - Test envoi
/announce [msg] - Annonce
/help - Cette aide

⏳BACCARAT AI 🤖⏳"""
    
    await event.respond(help_text)

async def cmd_reset(event):
    """Reset manuel."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel")
    await event.respond("✅ Reset effectué!")

async def cmd_set_tours(event):
    """Change le nombre de tours."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    global CONSECUTIVE_FAILURES_NEEDED
    
    try:
        match = re.search(r'/set_tours\s+(\d+)', event.message.message)
        if not match:
            await event.respond("Usage: `/set_tours [1-3]`")
            return
        
        val = int(match.group(1))
        if not 1 <= val <= 3:
            await event.respond("❌ Valeur 1-3 uniquement")
            return
        
        old = CONSECUTIVE_FAILURES_NEEDED
        CONSECUTIVE_FAILURES_NEEDED = val
        
        for tracker in cycle_trackers.values():
            tracker.reset()
        
        await event.respond(f"✅ Tours: {old} → {val}\nCompteurs reset")
        logger.info(f"Admin change tours: {old} → {val}")
        
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

async def cmd_channels(event):
    """Affiche la config."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    src_status = pred_status = "❌"
    
    try:
        if SOURCE_CHANNEL_ID:
            await client.get_entity(SOURCE_CHANNEL_ID)
            src_status = "✅"
    except:
        pass
    
    try:
        if PREDICTION_CHANNEL_ID:
            await client.get_entity(PREDICTION_CHANNEL_ID)
            pred_status = "✅"
    except:
        pass
    
    msg = f"""📡 **CONFIGURATION**

**Source:** `{SOURCE_CHANNEL_ID}` {src_status}
**Prédiction:** `{PREDICTION_CHANNEL_ID}` {pred_status}
**Admin:** `{ADMIN_ID}`
**Port:** `{PORT}`

**Cycles:** ♠️+5 ❤️+6 ♦️+6 ♣️+7
**Paramètres:** {CONSECUTIVE_FAILURES_NEEDED} tours, {NUMBERS_PER_TOUR} num/tour"""
    
    await event.respond(msg)

async def cmd_test(event):
    """Test d'envoi."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    await event.respond("🧪 Test...")
    
    try:
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ Canal non configuré")
            return
        
        test_msg = """⏳BACCARAT AI 🤖⏳ [TEST]

PLAYER : 9999 ♠️ : TEST EN COURS...."""
        
        sent = await client.send_message(PREDICTION_CHANNEL_ID, test_msg)
        await asyncio.sleep(2)
        
        await client.edit_message(
            PREDICTION_CHANNEL_ID,
            sent.id,
            """⏳BACCARAT AI 🤖⏳ [TEST]

PLAYER : 9999 ♠️ : ✅0️⃣ TEST OK"""
        )
        await asyncio.sleep(1)
        await client.delete_messages(PREDICTION_CHANNEL_ID, [sent.id])
        
        await event.respond("✅ **TEST RÉUSSI**")
        
    except Exception as e:
        await event.respond(f"❌ Échec: {e}")

async def cmd_announce(event):
    """Annonce personnalisée."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    parts = event.message.message.split(' ', 1)
    if len(parts) < 2:
        await event.respond("Usage: `/announce Message`")
        return
    
    text = parts[1].strip()
    if len(text) > 500:
        await event.respond("❌ Trop long (max 500)")
        return
    
    try:
        now = datetime.now()
        msg = f"""╔══════════════════════════════════════╗
║     📢 ANNONCE OFFICIELLE 📢          ║
╠══════════════════════════════════════╣

{text}

╠══════════════════════════════════════╣
║  📅 {now.strftime('%d/%m/%Y')}  🕐 {now.strftime('%H:%M')}
╠══════════════════════════════════════╣
║  👨‍💻 Sossou Kouamé
║  📱 +229 01 95 50 15 64
╚══════════════════════════════════════╝

⏳BACCARAT AI 🤖⏳"""
        
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ Canal non configuré")
            return
        
        sent = await client.send_message(PREDICTION_CHANNEL_ID, msg)
        await event.respond(f"✅ Annonce envoyée (ID: {sent.id})")
        
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

def setup_handlers():
    """Configure les handlers."""
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_channels, events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_test, events.NewMessage(pattern=r'^/test$'))
    client.add_event_handler(cmd_set_tours, events.NewMessage(pattern=r'^/set_tours'))
    client.add_event_handler(cmd_announce, events.NewMessage(pattern=r'^/announce'))
    
    client.add_event_handler(handle_new_message, events.NewMessage())
    client.add_event_handler(handle_edited_message, events.MessageEdited())

async def start_bot():
    """Démarre le bot."""
    global client, prediction_channel_ok
    
    session = os.getenv('TELEGRAM_SESSION', '')
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    
    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()
        initialize_trackers(3000)
        
        if PREDICTION_CHANNEL_ID:
            try:
                await client.get_entity(PREDICTION_CHANNEL_ID)
                prediction_channel_ok = True
                logger.info("✅ Canal prédition OK")
            except Exception as e:
                logger.error(f"❌ Canal prédiction: {e}")
        
        logger.info("🤖 Bot démarré")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    """Fonction principale."""
    try:
        if not await start_bot():
            return
        
        asyncio.create_task(auto_reset_system())
        logger.info("🔄 Auto-reset démarré")
        
        # Serveur web Render
        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        app.router.add_get('/', lambda r: web.Response(text="BACCARAT AI 🤖 Running"))
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        
        logger.info(f"🌐 Web server port {PORT}")
        logger.info(f"📊 {NUMBERS_PER_TOUR} num/tour, {CONSECUTIVE_FAILURES_NEEDED} tours")
        
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info("🔌 Déconnecté")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
