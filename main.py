# main.py
import os
import asyncio
import re
import logging
import sys
import json
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
if not BOT_TOKEN:  # ✅ CORRIGÉ : API_TOKEN → BOT_TOKEN
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

# NOUVEAU : Historiques pour la commande /history
finalized_messages_history: List[Dict] = []
MAX_HISTORY_SIZE = 50
prediction_history: List[Dict] = []

# NOUVEAU : Canaux de redirection pour les prédictions
REDIRECTION_CHANNELS: List[int] = []  # Liste des IDs de canaux pour redirection

# ============================================================================
# CLASSES
# ============================================================================

@dataclass
class SuitCycleTracker:
    """Tracker pour suivre les cycles d'une couleur."""
    suit: str
    cycle_numbers: List[int] = field(default_factory=list)
    current_tour: int = 1
    miss_counter: int = 0  # Nombre de tours complétés sans trouver la couleur
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
            old_cycle = self.cycle_numbers[self.last_cycle_index] if self.last_cycle_index >= 0 else 'N/A'
            new_cycle = self.cycle_numbers[new_index]
            logger.info(f"🔄 {self.suit} avance: cycle #{old_cycle} → #{new_cycle} (jeu #{game_number})")
            self.last_cycle_index = new_index
    
    def get_current_cycle_target(self) -> Optional[int]:
        """Retourne le numéro de cycle actuel (base pour le tour en cours)."""
        if self.last_cycle_index >= 0 and self.last_cycle_index < len(self.cycle_numbers):
            return self.cycle_numbers[self.last_cycle_index]
        
        for i, num in enumerate(self.cycle_numbers):
            if num >= current_game_number:
                self.last_cycle_index = max(0, i - 1)
                return self.cycle_numbers[self.last_cycle_index] if self.last_cycle_index >= 0 else num
        return self.cycle_numbers[-1] if self.cycle_numbers else None
    
    def get_numbers_to_check_this_tour(self) -> List[int]:
        """
        Retourne les 3 numéros à vérifier pour le tour actuel.
        Tour 1: cycle actuel, cycle+1, cycle+2
        Tour 2: cycle suivant, cycle_suivant+1, cycle_suivant+2
        """
        if self.current_tour == 1:
            current_cycle = self.get_current_cycle_target()
            if current_cycle is None:
                return []
            return [current_cycle, current_cycle + 1, current_cycle + 2]
        else:
            next_idx = self.last_cycle_index + 1
            if next_idx < len(self.cycle_numbers):
                next_cycle = self.cycle_numbers[next_idx]
                return [next_cycle, next_cycle + 1, next_cycle + 2]
            return []
    
    def is_number_in_current_tour(self, game_number: int) -> bool:
        """Vérifie si le numéro fait partie du tour actuel."""
        self.update_to_current_game(game_number)
        return game_number in self.get_numbers_to_check_this_tour()
    
    def process_verification(self, game_number: int, suit_found: bool) -> Optional[int]:
        """
        Traite la vérification d'un numéro.
        NOUVELLE LOGIQUE:
        - Tour 1 trouvé → Reset complet, prochain cycle = nouveau Tour 1
        - Tour 1 manqué → Passe Tour 2 (vérifie cycle suivant)
        - Tour 2 trouvé → Reset compteur, MAIS prochain cycle = nouveau Tour 1 (pas reset complet!)
        - Tour 2 manqué → Prédiction
        """
        self.update_to_current_game(game_number)
        
        if not self.is_number_in_current_tour(game_number):
            return None
        
        if game_number in self.tour_checked_numbers:
            return None
        
        self.tour_checked_numbers.add(game_number)
        self.verification_history[game_number] = suit_found
        
        if suit_found:
            logger.info(f"✅ {self.suit} trouvé au jeu #{game_number} (Tour {self.current_tour})")
            
            if self.current_tour == 1:
                # Tour 1 trouvé → Reset complet, attendre prochain cycle
                logger.info(f"🔄 {self.suit} Tour 1 trouvé → Reset complet, prochain cycle devient Tour 1")
                self.reset()
            else:
                # Tour 2 trouvé → Reset compteur mais continue! Le prochain cycle devient Tour 1
                logger.info(f"🔄 {self.suit} Tour 2 trouvé → Reset compteur, cycle suivant devient nouveau Tour 1")
                self.reset_after_tour2_found()
            return None
        
        # Pas trouvé
        tour_misses = len(self.tour_checked_numbers)
        logger.info(f"❌ {self.suit} manqué au jeu #{game_number} (Tour {self.current_tour}, {tour_misses}/{NUMBERS_PER_TOUR})")
        
        # Tour terminé (3 numéros vérifiés)
        if tour_misses >= NUMBERS_PER_TOUR:
            self.miss_counter += 1
            logger.info(f"📊 {self.suit} Tour {self.current_tour} terminé - Manques: {self.miss_counter}/{CONSECUTIVE_FAILURES_NEEDED}")
            
            # Assez de manques pour prédire?
            if self.miss_counter >= CONSECUTIVE_FAILURES_NEEDED:
                pred_idx = self.last_cycle_index + self.miss_counter
                if pred_idx < len(self.cycle_numbers):
                    pred_num = self.cycle_numbers[pred_idx]
                    self.pending_prediction = pred_num
                    logger.info(f"🔮 {self.suit} PRÉDICTION pour #{pred_num} (après {self.miss_counter} tour(s) échoué(s))")
                    self.reset_after_prediction()
                    return pred_num
            
            # Passer au tour suivant
            if self.current_tour < CONSECUTIVE_FAILURES_NEEDED:
                self.current_tour += 1
                self.tour_checked_numbers.clear()
                self.last_cycle_index += 1  # Avance au cycle suivant pour Tour 2
                logger.info(f"🔄 {self.suit} passe au Tour {self.current_tour} (cycle suivant)")
            else:
                logger.warning(f"⚠️ {self.suit} tous tours terminés mais pas de prédiction")
                self.reset()
        
        return None
    
    def reset(self):
        """Reset complet - quand Tour 1 trouvé."""
        self.current_tour = 1
        self.miss_counter = 0
        self.tour_checked_numbers.clear()
        self.pending_prediction = None
        self.verification_history.clear()
    
    def reset_after_tour2_found(self):
        """
        Reset après avoir trouvé au Tour 2.
        Le prochain cycle devient le nouveau Tour 1.
        """
        self.current_tour = 1
        self.miss_counter = 0
        self.tour_checked_numbers.clear()
        self.verification_history.clear()
        # On avance d'un cycle car on vient de finir le Tour 2 sur le cycle suivant
        self.last_cycle_index += 1
    
    def reset_after_prediction(self):
        """Reset après création d'une prédiction."""
        self.current_tour = 1
        self.miss_counter = 0
        self.tour_checked_numbers.clear()
        self.verification_history.clear()

# ============================================================================
# FONCTIONS D'HISTORIQUE
# ============================================================================

def add_to_history(game_number: int, message_text: str, first_group: str, suits_found: List[str]):
    """Ajoute un message finalisé à l'historique."""
    global finalized_messages_history
    
    entry = {
        'timestamp': datetime.now(),
        'game_number': game_number,
        'message_text': message_text[:200],
        'first_group': first_group,
        'suits_found': suits_found,
        'predictions_verified': []
    }
    
    finalized_messages_history.insert(0, entry)
    
    if len(finalized_messages_history) > MAX_HISTORY_SIZE:
        finalized_messages_history = finalized_messages_history[:MAX_HISTORY_SIZE]

def add_prediction_to_history(game_number: int, suit: str, verification_games: List[int]):
    """Ajoute une prédiction à l'historique."""
    global prediction_history
    
    prediction_history.insert(0, {
        'predicted_game': game_number,
        'suit': suit,
        'predicted_at': datetime.now(),
        'verification_games': verification_games,
        'status': 'en_cours',
        'verified_by': []
    })
    
    if len(prediction_history) > MAX_HISTORY_SIZE:
        prediction_history = prediction_history[:MAX_HISTORY_SIZE]

def update_prediction_in_history(game_number: int, suit: str, verified_by_game: int, 
                                verified_by_group: str, rattrapage_level: int, final_status: Optional[str] = None):
    """Met à jour l'historique quand une prédiction est vérifiée."""
    global finalized_messages_history, prediction_history
    
    for pred in prediction_history:
        if pred['predicted_game'] == game_number and pred['suit'] == suit:
            pred['verified_by'].append({
                'game_number': verified_by_game,
                'first_group': verified_by_group,
                'rattrapage_level': rattrapage_level
            })
            if final_status:
                pred['status'] = final_status
            break
    
    for msg in finalized_messages_history:
        if msg['game_number'] == verified_by_game:
            msg['predictions_verified'].append({
                'predicted_game': game_number,
                'suit': suit,
                'rattrapage_level': rattrapage_level
            })
            break

# ============================================================================
# INITIALISATION
# ============================================================================

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
# GESTION DES PRÉDICTIONS ET REDIRECTION
# ============================================================================

async def send_prediction_to_channel(channel_id: int, game_number: int, suit: str, is_rattrapage: int = 0) -> Optional[int]:
    """Envoie une prédiction à un canal spécifique."""
    try:
        msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {SUIT_DISPLAY.get(suit, suit)} : en cours...."""
        
        sent = await client.send_message(channel_id, msg)
        logger.info(f"✅ Prédiction envoyée à {channel_id}: #{game_number} {suit}")
        return sent.id
        
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction à {channel_id}: {e}")
        return None

async def send_prediction(game_number: int, suit: str, is_rattrapage: int = 0) -> Optional[int]:
    """Envoie une prédiction au canal principal et aux canaux de redirection."""
    global last_prediction_time
    
    try:
        if suit in suit_block_until and datetime.now() < suit_block_until[suit]:
            logger.info(f"🔒 {suit} bloqué")
            return None
        
        # Canal principal
        if not PREDICTION_CHANNEL_ID:
            logger.warning("⚠️ Canal prédiction principal non configuré")
            return None
        
        sent_main = await send_prediction_to_channel(PREDICTION_CHANNEL_ID, game_number, suit, is_rattrapage)
        
        if sent_main:
            last_prediction_time = datetime.now()
            
            pending_predictions[game_number] = {
                'suit': suit,
                'message_id': sent_main,
                'status': 'en_cours',
                'rattrapage': is_rattrapage,
                'original_game': game_number if is_rattrapage == 0 else None,
                'awaiting_rattrapage': 0,
                'sent_time': datetime.now(),
                'redirected_to': []  # Stocke les IDs des messages redirigés
            }
            
            # Redirection vers les autres canaux
            if is_rattrapage == 0:  # Seulement pour les prédictions originales, pas les mises à jour
                for redirect_channel_id in REDIRECTION_CHANNELS:
                    try:
                        sent_redirect = await send_prediction_to_channel(redirect_channel_id, game_number, suit, is_rattrapage)
                        if sent_redirect:
                            pending_predictions[game_number]['redirected_to'].append({
                                'channel_id': redirect_channel_id,
                                'message_id': sent_redirect
                            })
                    except Exception as e:
                        logger.error(f"❌ Erreur redirection vers {redirect_channel_id}: {e}")
            
            # Ajouter à l'historique
            if is_rattrapage == 0:
                verification_games = [game_number, game_number + 1, game_number + 2]
                add_prediction_to_history(game_number, suit, verification_games)
            
            logger.info(f"✅ Prédiction envoyée: #{game_number} {suit} (+{len(REDIRECTION_CHANNELS)} redirections)")
            return sent_main
        
        return None
        
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        return None

async def update_prediction_in_channel(channel_id: int, message_id: int, game_number: int, suit: str, status: str):
    """Met à jour une prédiction dans un canal spécifique."""
    try:
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
        
        await client.edit_message(channel_id, message_id, new_msg)
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur update message dans {channel_id}: {e}")
        return False

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
                update_prediction_in_history(game_number, target_suit, game_number, first_group, 0, 'gagne_r0')
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
                final_status = f'gagne_r{awaiting}'
                update_prediction_in_history(original_game, target_suit, game_number, first_group, awaiting, final_status)
                return True
            else:
                if awaiting < 2:
                    pred['awaiting_rattrapage'] = awaiting + 1
                    logger.info(f"❌ R{awaiting} échoué, attente #{original_game + awaiting + 1}")
                    return False
                else:
                    logger.info(f"❌ R2 échoué, perdu")
                    await update_prediction_message(original_game, '❌', False)
                    update_prediction_in_history(original_game, target_suit, game_number, first_group, 2, 'perdu')
                    return False
    
    return False

async def update_prediction_message(game_number: int, status: str, trouve: bool, rattrapage: int = 0):
    """Met à jour le statut d'une prédiction dans tous les canaux."""
    if game_number not in pending_predictions:
        return
    
    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    
    # Mettre à jour le canal principal
    await update_prediction_in_channel(PREDICTION_CHANNEL_ID, msg_id, game_number, suit, status)
    
    # Mettre à jour les canaux de redirection
    for redirect in pred.get('redirected_to', []):
        await update_prediction_in_channel(redirect['channel_id'], redirect['message_id'], game_number, suit, status)
    
    pred['status'] = status
    
    if trouve:
        logger.info(f"✅ Gagné: #{game_number} {status}")
    else:
        logger.info(f"❌ Perdu: #{game_number}")
        block_suit(suit, 5)
    
    del pending_predictions[game_number]

# ============================================================================
# TRAITEMENT DES MESSAGES
# ============================================================================

async def process_game_result(game_number: int, message_text: str):
    """Traite un résultat de jeu finalisé."""
    global current_game_number, last_source_game_number
    
    current_game_number = game_number
    last_source_game_number = game_number
    
    for tracker in cycle_trackers.values():
        tracker.update_to_current_game(game_number)
    
    groups = extract_parentheses_groups(message_text)
    if not groups:
        logger.warning(f"⚠️ Pas de groupe trouvé dans #{game_number}")
        return
    
    first_group = groups[0]
    suits_in_first = get_suits_in_group(first_group)
    
    logger.info(f"📊 Jeu #{game_number}: {suits_in_first} dans '{first_group[:30]}...'")
    
    add_to_history(game_number, message_text, first_group, suits_in_first)
    
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
# COMMANDES ADMIN - CORRIGÉES
# ============================================================================

async def cmd_redirect(event):
    """Gère la redirection des prédictions vers d'autres canaux."""
    # ✅ CORRIGÉ : Vérifier admin d'abord, pas de blocage par type de chat
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    # Debug log pour voir si la commande arrive
    logger.info(f"📨 Commande /redirect reçue de {event.sender_id}: {event.message.message}")
    
    global REDIRECTION_CHANNELS
    
    message_text = event.message.message.strip()
    parts = message_text.split()
    
    if len(parts) == 1:
        # Afficher la liste actuelle
        if not REDIRECTION_CHANNELS:
            await event.respond("📭 Aucun canal de redirection configuré.\n\nUsage:\n`/redirect add -1001234567890`\n`/redirect remove -1001234567890`\n`/redirect list`")
            return
        
        lines = ["📡 **CANAUX DE REDIRECTION**", f"Canal principal: `{PREDICTION_CHANNEL_ID}`", "", "**Canaux de redirection:**"]
        for i, chan_id in enumerate(REDIRECTION_CHANNELS, 1):
            lines.append(f"{i}. `{chan_id}`")
        
        lines.extend(["", f"**Total:** {len(REDIRECTION_CHANNELS)} canaux", "", "Commandes:", "`/redirect add [ID]` - Ajouter", "`/redirect remove [ID]` - Retirer", "`/redirect clear` - Vider la liste"])
        await event.respond("\n".join(lines))
        return
    
    subcommand = parts[1].lower()
    
    if subcommand == 'list':
        if not REDIRECTION_CHANNELS:
            await event.respond("📭 Liste vide")
            return
        
        lines = ["📡 **LISTE DES REDIRECTIONS**", ""]
        for i, chan_id in enumerate(REDIRECTION_CHANNELS, 1):
            status = "✅" if client else "⏳"
            lines.append(f"{i}. `{chan_id}` {status}")
        await event.respond("\n".join(lines))
    
    elif subcommand == 'add' and len(parts) >= 3:
        try:
            chan_id = int(parts[2])
            if chan_id in REDIRECTION_CHANNELS:
                await event.respond(f"⚠️ Canal `{chan_id}` déjà dans la liste")
                return
            
            # Vérifier que le canal existe et est accessible
            try:
                await client.get_entity(chan_id)
            except Exception as e:
                await event.respond(f"❌ Canal inaccessible: {e}\nVérifiez que le bot est membre du canal.")
                return
            
            REDIRECTION_CHANNELS.append(chan_id)
            await event.respond(f"✅ Canal `{chan_id}` ajouté!\n\n**Total:** {len(REDIRECTION_CHANNELS)} canaux")
            logger.info(f"Admin ajoute redirection: {chan_id}")
            
        except ValueError:
            await event.respond("❌ ID invalide. Format: `-1001234567890`")
    
    elif subcommand == 'remove' and len(parts) >= 3:
        try:
            chan_id = int(parts[2])
            if chan_id in REDIRECTION_CHANNELS:
                REDIRECTION_CHANNELS.remove(chan_id)
                await event.respond(f"✅ Canal `{chan_id}` retiré!\n\n**Total:** {len(REDIRECTION_CHANNELS)} canaux")
                logger.info(f"Admin retire redirection: {chan_id}")
            else:
                await event.respond(f"⚠️ Canal `{chan_id}` non trouvé dans la liste")
        except ValueError:
            await event.respond("❌ ID invalide")
    
    elif subcommand == 'clear':
        count = len(REDIRECTION_CHANNELS)
        REDIRECTION_CHANNELS.clear()
        await event.respond(f"🗑️ {count} canaux supprimés de la liste")
        logger.info(f"Admin clear redirections: {count} canaux")
    
    elif subcommand == 'test' and len(parts) >= 3:
        try:
            chan_id = int(parts[2])
            test_msg = """⏳BACCARAT AI 🤖⏳ [TEST REDIRECTION]

PLAYER : TEST ♠️ : TEST EN COURS...."""
            
            sent = await client.send_message(chan_id, test_msg)
            await asyncio.sleep(1)
            await client.delete_messages(chan_id, [sent.id])
            await event.respond(f"✅ Test réussi pour `{chan_id}`")
            
        except Exception as e:
            await event.respond(f"❌ Test échoué: {e}")
    
    else:
        await event.respond("""📖 **COMMANDE /redirect**

**Usage:**
`/redirect` - Voir la liste
`/redirect list` - Lister les canaux
`/redirect add -1001234567890` - Ajouter un canal
`/redirect remove -1001234567890` - Retirer un canal
`/redirect clear` - Vider la liste
`/redirect test -1001234567890` - Tester un canal

**Note:** Le bot doit être admin dans les canaux de redirection.""")

async def cmd_history(event):
    """Affiche l'historique des 5 derniers messages finalisés et prédictions."""
    # ✅ CORRIGÉ : Vérifier admin uniquement
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    lines = [
        "📜 **HISTORIQUE DES 5 DERNIERS MESSAGES FINALISÉS**",
        "═══════════════════════════════════════",
        ""
    ]
    
    recent_messages = finalized_messages_history[:5]
    
    if not recent_messages:
        lines.append("❌ Aucun message dans l'historique")
    else:
        for i, msg in enumerate(recent_messages, 1):
            time_str = msg['timestamp'].strftime('%H:%M:%S')
            game_num = msg['game_number']
            group = msg['first_group']
            suits = ', '.join([SUIT_DISPLAY.get(s, s) for s in msg['suits_found']]) if msg['suits_found'] else 'Aucune'
            
            verif_indicator = ""
            if msg['predictions_verified']:
                verif_details = []
                for v in msg['predictions_verified']:
                    suit_display = SUIT_DISPLAY.get(v['suit'], v['suit'])
                    if v['rattrapage_level'] == 0:
                        verif_details.append(f"✅0️⃣ #{v['predicted_game']}{suit_display}")
                    elif v['rattrapage_level'] == 1:
                        verif_details.append(f"✅1️⃣ #{v['predicted_game']}{suit_display}")
                    elif v['rattrapage_level'] == 2:
                        verif_details.append(f"✅2️⃣ #{v['predicted_game']}{suit_display}")
                verif_indicator = "\n   🔍 Vérification: " + " | ".join(verif_details)
            
            lines.append(
                f"{i}. 🕐 `{time_str}` | **Jeu #{game_num}**\n"
                f"   📝 `{group}`\n"
                f"   🎨 Couleurs: {suits}{verif_indicator}"
            )
            lines.append("")
    
    lines.append("🔮 **PRÉDICTIONS RÉCENTES**")
    lines.append("───────────────────────────────────────")
    
    recent_predictions = prediction_history[:5]
    
    if not recent_predictions:
        lines.append("❌ Aucune prédiction dans l'historique")
    else:
        for pred in recent_predictions:
            pred_game = pred['predicted_game']
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            status = pred['status']
            pred_time = pred['predicted_at'].strftime('%H:%M:%S')
            
            if status == 'en_cours':
                status_str = "⏳ En cours..."
            elif status == 'gagne_r0':
                status_str = "✅0️⃣ GAGNÉ direct"
            elif status == 'gagne_r1':
                status_str = "✅1️⃣ GAGNÉ R1"
            elif status == 'gagne_r2':
                status_str = "✅2️⃣ GAGNÉ R2"
            elif status == 'perdu':
                status_str = "❌ PERDU"
            else:
                status_str = f"❓ {status}"
            
            lines.append(f"🎯 **#{pred_game}** {suit} | {status_str}")
            lines.append(f"   🕐 Prédit à: {pred_time}")
            
            if pred['verified_by']:
                lines.append("   📋 Vérifié par:")
                for v in pred['verified_by']:
                    r_text = f"R{v['rattrapage_level']}" if v['rattrapage_level'] > 0 else "Direct"
                    lines.append(f"      • Jeu #{v['game_number']} ({r_text}): `{v['first_group']}`")
            else:
                verif_games = pred['verification_games']
                if status == 'en_cours':
                    pending_games = [g for g in verif_games if g > current_game_number]
                    checked_games = [g for g in verif_games if g <= current_game_number]
                    
                    if checked_games:
                        lines.append(f"   ✅ Déjà vérifié: {', '.join(['#' + str(g) for g in checked_games])}")
                    if pending_games:
                        lines.append(f"   ⏳ En attente: {', '.join(['#' + str(g) for g in pending_games])}")
            
            lines.append("")
    
    lines.append("═══════════════════════════════════════")
    lines.append("💡 **Légende:**")
    lines.append("• Messages finalisés = ✅ contenant #N[numéro]")
    lines.append("• 1er groupe = Contenu entre premières parenthèses")
    lines.append("• Prédiction créée quand compteur atteint le nombre de tours")
    
    await event.respond("\n".join(lines))

async def cmd_status(event):
    """Affiche les compteurs détaillés."""
    # ✅ CORRIGÉ : Vérifier admin uniquement
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    lines = [
        "📈 **COUNTERS DE MANQUES DES CYCLES**",
        "",
        f"🎮 Dernier jeu: #{current_game_number}",
        f"📋 Prédictions actives: {len(pending_predictions)}",
        f"⏳ En attente finalisation: {len(waiting_finalization)}",
        f"📡 Canaux de redirection: {len(REDIRECTION_CHANNELS)}",
        ""
    ]
    
    for suit in ALL_SUITS:
        if suit not in cycle_trackers:
            continue
        
        tracker = cycle_trackers[suit]
        tracker.update_to_current_game(current_game_number)
        
        current = tracker.get_current_cycle_target()
        to_check = tracker.get_numbers_to_check_this_tour()
        checked = tracker.tour_checked_numbers
        
        progress = len(checked)
        bar = f"[{'█' * progress}{'░' * (NUMBERS_PER_TOUR - progress)}]"
        
        if tracker.pending_prediction:
            emoji, status = "🔮", f"PRÉDICTION #{tracker.pending_prediction}"
        elif tracker.current_tour == 2:
            emoji, status = "⚠️", f"Tour 2 en cours"
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
            
            redirect_count = len(pred.get('redirected_to', []))
            redirect_info = f" [+{redirect_count}↗️]" if redirect_count > 0 else ""
            
            lines.append(f"• #{num} {suit} ({type_str}): {status_str}{redirect_info}")
        lines.append("")
    
    lines.extend([
        "**Légende:**",
        "✅=Trouvé ❌=Manqué ⏳=Attente 🔮=Prédiction ⚠️=Tour2",
        "↗️=Nombre de redirections"
    ])
    
    await event.respond("\n".join(lines))

async def cmd_help(event):
    """Affiche l'aide."""
    # ✅ CORRIGÉ : Pas de vérification de groupe, aide disponible partout
    help_text = f"""📖 **BACCARAT AI - AIDE**

**Système ({NUMBERS_PER_TOUR} numéros/tour, {CONSECUTIVE_FAILURES_NEEDED} tours):**

• Tour 1: vérifie cycle, cycle+1, cycle+2
  → Trouvé: Reset, prochain cycle = nouveau Tour 1
  → Manqué: Passe Tour 2
• Tour 2: vérifie cycle_suivant, +1, +2
  → Trouvé: Reset compteur, cycle_suivant+1 = nouveau Tour 1
  → Manqué: PRÉDICTION

**Rattrapages:** ✅0️⃣ ✅1️⃣ ✅2️⃣ ❌

**Commandes:**
/status - Voir les compteurs
/history - Voir l'historique
/redirect - Gérer les redirections (20+ canaux)
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
    # ✅ CORRIGÉ : Vérifier admin uniquement
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel")
    await event.respond("✅ Reset effectué!")

async def cmd_set_tours(event):
    """Change le nombre de tours."""
    # ✅ CORRIGÉ : Vérifier admin uniquement
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
    # ✅ CORRIGÉ : Vérifier admin uniquement
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
    
    redirect_status = []
    for chan_id in REDIRECTION_CHANNELS[:5]:  # Max 5 dans l'affichage
        try:
            await client.get_entity(chan_id)
            redirect_status.append(f"✅ `{chan_id}`")
        except:
            redirect_status.append(f"❌ `{chan_id}`")
    
    redirect_text = "\n".join(redirect_status) if redirect_status else "Aucun"
    if len(REDIRECTION_CHANNELS) > 5:
        redirect_text += f"\n... et {len(REDIRECTION_CHANNELS) - 5} autres"
    
    msg = f"""📡 **CONFIGURATION**

**Source:** `{SOURCE_CHANNEL_ID}` {src_status}
**Prédiction:** `{PREDICTION_CHANNEL_ID}` {pred_status}
**Admin:** `{ADMIN_ID}`
**Port:** `{PORT}`

**Redirections ({len(REDIRECTION_CHANNELS)}):**
{redirect_text}

**Cycles:** ♠️+5 ❤️+6 ♦️+6 ♣️+7
**Paramètres:** {CONSECUTIVE_FAILURES_NEEDED} tours, {NUMBERS_PER_TOUR} num/tour"""
    
    await event.respond(msg)

async def cmd_test(event):
    """Test d'envoi - ENVOIE VRAIMENT DANS LE CANAL PRÉDICTION."""
    # ✅ CORRIGÉ : Vérifier admin uniquement
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    await event.respond("🧪 Test en cours...")
    
    try:
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ Canal prédiction non configuré (PREDICTION_CHANNEL_ID)")
            return
        
        # ✅ NOUVEAU : Envoie une vraie prédiction test dans le canal
        test_game_number = 99999
        test_suit = '♠'
        
        # Envoyer la prédiction test
        test_msg = f"""⏳BACCARAT AI 🤖⏳ [TEST]

PLAYER : {test_game_number} {SUIT_DISPLAY.get(test_suit, test_suit)} : TEST EN COURS...."""
        
        sent = await client.send_message(PREDICTION_CHANNEL_ID, test_msg)
        logger.info(f"✅ Test envoyé au canal {PREDICTION_CHANNEL_ID}, msg_id: {sent.id}")
        
        # Attendre 3 secondes puis mettre à jour comme gagné
        await asyncio.sleep(3)
        
        result_msg = f"""⏳BACCARAT AI 🤖⏳ [TEST]

PLAYER : {test_game_number} {SUIT_DISPLAY.get(test_suit, test_suit)} : ✅0️⃣ TEST RÉUSSI"""
        
        await client.edit_message(PREDICTION_CHANNEL_ID, sent.id, result_msg)
        
        # Attendre 2 secondes puis supprimer
        await asyncio.sleep(2)
        await client.delete_messages(PREDICTION_CHANNEL_ID, [sent.id])
        
        await event.respond(f"✅ **TEST RÉUSSI**\n\nMessage envoyé au canal `{PREDICTION_CHANNEL_ID}`, modifié et supprimé.\n\nLe système fonctionne correctement !")
        
    except Exception as e:
        logger.error(f"❌ Erreur test: {e}")
        await event.respond(f"❌ Échec du test: {e}\n\nVérifiez que:\n1. PREDICTION_CHANNEL_ID est correct\n2. Le bot est admin du canal\n3. Le canal existe")

async def cmd_announce(event):
    """Annonce personnalisée."""
    # ✅ CORRIGÉ : Vérifier admin uniquement
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
    # Commandes admin - ordre important
    client.add_event_handler(cmd_redirect, events.NewMessage(pattern=r'^/redirect'))
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history, events.NewMessage(pattern=r'^/history$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_channels, events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_test, events.NewMessage(pattern=r'^/test$'))
    client.add_event_handler(cmd_set_tours, events.NewMessage(pattern=r'^/set_tours'))
    client.add_event_handler(cmd_announce, events.NewMessage(pattern=r'^/announce'))
    
    # Messages du canal source
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
                logger.error(f"❌ Canal prédition: {e}")
        
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
