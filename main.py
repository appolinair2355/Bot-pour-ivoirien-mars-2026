import os
import asyncio
import re
import logging
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set
from datetime import datetime, timedelta
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError
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

# Variables pour le mode hyper serré (modifiables à chaud)
hyper_serré_active = False
hyper_serré_h = 5

# Historiques pour la commande /history
finalized_messages_history: List[Dict] = []
MAX_HISTORY_SIZE = 50
prediction_history: List[Dict] = []

# ============================================================================
# FONCTION UTILITAIRE - Conversion ID Canal
# ============================================================================

def normalize_channel_id(channel_id) -> int:
    """
    Normalise l'ID du canal pour Telethon.
    Les canaux doivent avoir le format -100xxxxxxxxxx
    """
    if not channel_id:
        return None
    
    # Convertir en string pour manipulation
    channel_str = str(channel_id)
    
    # Si déjà au format -100, retourner tel quel
    if channel_str.startswith('-100'):
        return int(channel_str)
    
    # Si commence par - mais pas -100, c'est probablement déjà un ID de groupe
    if channel_str.startswith('-'):
        return int(channel_str)
    
    # Si c'est un ID positif, ajouter le préfixe -100 (format canal)
    # Note: Les vrais IDs de canal sont stockés comme -100{id_reel}
    return int(f"-100{channel_str}")

async def resolve_channel(entity_id):
    """
    Résout l'entité canal et vérifie l'accès.
    Retourne l'entité ou None si inaccessible.
    """
    try:
        if not entity_id:
            return None
        
        # Normaliser l'ID
        normalized_id = normalize_channel_id(entity_id)
        
        # Essayer de récupérer l'entité
        entity = await client.get_entity(normalized_id)
        
        # Vérifier si c'est un canal
        if hasattr(entity, 'broadcast') and entity.broadcast:
            logger.info(f"✅ Canal résolu: {entity.title} (ID: {normalized_id})")
            return entity
        
        # Si c'est un groupe (megagroup)
        if hasattr(entity, 'megagroup') and entity.megagroup:
            logger.info(f"✅ Groupe résolu: {entity.title} (ID: {normalized_id})")
            return entity
            
        return entity
        
    except Exception as e:
        logger.error(f"❌ Impossible de résoudre le canal {entity_id}: {e}")
        return None

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
        """Met à jour le last_cycle_index pour pointer sur le bon cycle."""
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
            
            if self.last_cycle_index >= 0 and new_index > self.last_cycle_index:
                if self.miss_counter == 0 and self.current_tour == 1:
                    self.tour_checked_numbers.clear()
                    self.verification_history.clear()
            
            self.last_cycle_index = new_index
    
    def get_current_cycle_target(self) -> Optional[int]:
        """Retourne le numéro de cycle actuel."""
        if self.last_cycle_index >= 0 and self.last_cycle_index < len(self.cycle_numbers):
            return self.cycle_numbers[self.last_cycle_index]
        
        for i, num in enumerate(self.cycle_numbers):
            if num >= current_game_number:
                self.last_cycle_index = max(0, i - 1)
                return self.cycle_numbers[self.last_cycle_index] if self.last_cycle_index >= 0 else num
        return self.cycle_numbers[-1] if self.cycle_numbers else None
    
    def get_numbers_to_check_this_tour(self) -> List[int]:
        """Retourne les numéros à vérifier pour le tour actuel."""
        global hyper_serré_active, hyper_serré_h
        
        current_cycle = self.get_current_cycle_target()
        if current_cycle is None:
            return []
        
        if hyper_serré_active:
            count = hyper_serré_h
        else:
            count = NUMBERS_PER_TOUR
        
        return [current_cycle + i for i in range(count)]
    
    def is_number_in_current_tour(self, game_number: int) -> bool:
        """Vérifie si le numéro fait partie du tour actuel."""
        self.update_to_current_game(game_number)
        return game_number in self.get_numbers_to_check_this_tour()
    
    def process_verification(self, game_number: int, suit_found: bool) -> Optional[int]:
        """Traite la vérification d'un numéro."""
        global hyper_serré_active, hyper_serré_h
        
        self.update_to_current_game(game_number)
        
        if not self.is_number_in_current_tour(game_number):
            return None
        
        if game_number in self.tour_checked_numbers:
            return None
        
        self.tour_checked_numbers.add(game_number)
        self.verification_history[game_number] = suit_found
        
        if suit_found:
            logger.info(f"✅ {self.suit} trouvé au jeu #{game_number} - RESET")
            self.reset()
            return None
        
        tour_misses = len(self.tour_checked_numbers)
        
        if hyper_serré_active:
            needed = hyper_serré_h
            mode_str = f"hyper serré (h={hyper_serré_h})"
        else:
            needed = NUMBERS_PER_TOUR
            mode_str = f"standard ({NUMBERS_PER_TOUR})"
            
        logger.info(f"❌ {self.suit} manqué au jeu #{game_number} ({tour_misses}/{needed}) - Mode {mode_str}")
        
        if tour_misses >= needed:
            self.miss_counter += 1
            logger.info(f"📊 {self.suit} Tour terminé - Manques: {self.miss_counter}/{CONSECUTIVE_FAILURES_NEEDED}")
            
            if self.miss_counter >= CONSECUTIVE_FAILURES_NEEDED:
                current_cycle = self.get_current_cycle_target()
                if current_cycle is not None:
                    
                    if hyper_serré_active:
                        pred_num = current_cycle + hyper_serré_h + 1
                        calc_detail = f"cycle #{current_cycle} + h({hyper_serré_h}) + 1 = {pred_num}"
                    else:
                        interval = SUIT_CYCLES[self.suit]['interval']
                        pred_num = current_cycle + interval - 1
                        calc_detail = f"cycle #{current_cycle} + intervalle({interval}) - 1 = {pred_num}"
                    
                    self.pending_prediction = pred_num
                    logger.info(f"🔮 {self.suit} PRÉDICTION pour #{pred_num} ({calc_detail})")
                    self.reset_after_prediction()
                    return pred_num
            
            if self.current_tour < CONSECUTIVE_FAILURES_NEEDED:
                self.current_tour += 1
                self.tour_checked_numbers.clear()
                self.last_cycle_index += 1
                next_cycle = self.get_current_cycle_target()
                logger.info(f"🔄 {self.suit} passe au Tour {self.current_tour} (cycle #{next_cycle})")
            else:
                logger.warning(f"⚠️ {self.suit} tous tours terminés mais pas de prédiction")
                self.reset()
        
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
    """Vérifie si le message est finalisé."""
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
# GESTION DES PRÉDICTIONS - CORRIGÉ
# ============================================================================

async def send_prediction(game_number: int, suit: str, is_rattrapage: int = 0) -> Optional[int]:
    """Envoie une prédiction au canal configuré."""
    global last_prediction_time
    
    try:
        # Vérifier si la couleur est bloquée
        if suit in suit_block_until and datetime.now() < suit_block_until[suit]:
            logger.info(f"🔒 {suit} bloqué, prédiction annulée")
            return None
        
        # VÉRIFICATION CRITIQUE: Canal configuré ?
        if not PREDICTION_CHANNEL_ID:
            logger.error("❌ PREDICTION_CHANNEL_ID non configuré dans config.py!")
            return None
        
        # RÉSOLUTION DU CANAL avec normalisation d'ID
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error(f"❌ Impossible d'accéder au canal {PREDICTION_CHANNEL_ID}")
            logger.error("   Vérifiez que:")
            logger.error("   1. L'ID est correct (format: -100xxxxxxxxxx ou juste les chiffres)")
            logger.error("   2. Le bot est administrateur du canal")
            logger.error("   3. Le bot a les permissions d'envoi de messages")
            return None
        
        # Préparer le message
        msg = f"""⏳BACCARAT AI 🤖⏳

PLAYER : {game_number} {SUIT_DISPLAY.get(suit, suit)} : en cours...."""
        
        # ENVOI avec gestion d'erreurs spécifiques
        try:
            sent = await client.send_message(prediction_entity, msg)
            last_prediction_time = datetime.now()
            
            # Stockage de la prédiction
            pending_predictions[game_number] = {
                'suit': suit,
                'message_id': sent.id,
                'status': 'en_cours',
                'rattrapage': is_rattrapage,
                'original_game': game_number if is_rattrapage == 0 else None,
                'awaiting_rattrapage': 0,
                'sent_time': datetime.now()
            }
            
            # Historique
            if is_rattrapage == 0:
                verification_games = [game_number, game_number + 1, game_number + 2]
                add_prediction_to_history(game_number, suit, verification_games)
                logger.info(f"📋 Prédiction #{game_number} {suit}: vérification sur {verification_games}")
            
            logger.info(f"✅ Prédiction envoyée avec succès: #{game_number} {suit} au canal {prediction_entity.id}")
            return sent.id
            
        except ChatWriteForbiddenError:
            logger.error(f"❌ Bot n'a pas la permission d'écrire dans le canal {PREDICTION_CHANNEL_ID}")
            logger.error("   → Le bot doit être administrateur avec droit d'envoi de messages")
            return None
        except UserBannedInChannelError:
            logger.error(f"❌ Bot banni du canal {PREDICTION_CHANNEL_ID}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Erreur inattendue envoi prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

async def check_prediction_result(game_number: int, first_group: str) -> bool:
    """Vérifie si une prédiction est gagnante."""
    suits_in_result = get_suits_in_group(first_group)
    
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
                logger.info(f"❌ #{game_number} échoué, attente rattrapage #{game_number + 1}")
                return False
    
    for original_game, pred in list(pending_predictions.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        if awaiting > 0 and game_number == original_game + awaiting:
            target_suit = pred['suit']
            logger.info(f"🔍 Vérif rattrapage R{awaiting} #{game_number}: {target_suit}")
            
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
                    logger.info(f"❌ R2 échoué, prédiction perdue")
                    await update_prediction_message(original_game, '❌', False)
                    update_prediction_in_history(original_game, target_suit, game_number, first_group, 2, 'perdu')
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
        # Résoudre le canal à nouveau pour l'édition
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error("❌ Canal de prédiction non accessible pour mise à jour")
            return
            
        await client.edit_message(prediction_entity, msg_id, new_msg)
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
    
    if await check_prediction_result(game_number, first_group):
        return
    
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
        
        # Normaliser l'ID source pour comparaison
        normalized_source = normalize_channel_id(SOURCE_CHANNEL_ID)
        if chat_id != normalized_source:
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
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity and client and client.is_connected():
            await client.send_message(
                prediction_entity,
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

async def cmd_h(event):
    """Commande /h - définit le nombre h de numéros à vérifier en mode hyper serré."""
    global hyper_serré_active, hyper_serré_h
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    try:
        parts = event.message.message.split()
        
        if len(parts) == 1:
            mode_str = "✅ ACTIF" if hyper_serré_active else "❌ INACTIF"
            
            if hyper_serré_active:
                example = f"Cycle 596 échoue sur 596-600 → Prédit 602 (596+5+1)"
            else:
                example = f"Cycle 1020 échoue sur 1020-1022 → Prédit 1025 (1020+6-1)"
            
            await event.respond(
                f"📊 **MODE HYPER SERRÉ**\n\n"
                f"Statut: {mode_str}\n"
                f"h = {hyper_serré_h} numéros à vérifier\n\n"
                f"📋 **Fonctionnement actuel:**\n"
                f"• {example}\n"
                f"• Vérification après prédiction: 3 numéros (prédit, +1, +2)\n\n"
                f"**Usage:**\n"
                f"`/h [3-15]` - Activer avec h numéros\n"
                f"`/h off` - Désactiver (mode standard)\n"
                f"`/h on` - Réactiver avec la dernière valeur"
            )
            return
        
        arg = parts[1].lower()
        
        if arg == 'off':
            hyper_serré_active = False
            for tracker in cycle_trackers.values():
                tracker.reset()
            await event.respond(
                f"❌ **Mode hyper serré DÉSACTIVÉ**\n\n"
                f"Retour au mode standard:\n"
                f"• Vérifie {NUMBERS_PER_TOUR} numéros consécutifs\n"
                f"• Prédit: cycle + intervalle - 1\n"
                f"• Exemple: ♥️ cycle 1020 échoue sur 1020-1022 → prédit 1025\n\n"
                f"✅ Compteurs reset pour nouvelle analyse."
            )
            logger.info(f"Admin désactive mode hyper serré")
            return
        
        if arg == 'on':
            hyper_serré_active = True
            for tracker in cycle_trackers.values():
                tracker.reset()
            await event.respond(
                f"✅ **Mode hyper serré ACTIVÉ**\n\n"
                f"h = {hyper_serré_h} numéros à vérifier\n"
                f"Prédiction = cycle + h + 1\n"
                f"Exemple: cycle 596 échoue sur 596-600 → prédit 602\n\n"
                f"✅ Compteurs reset pour nouvelle analyse."
            )
            logger.info(f"Admin active mode hyper serré (h={hyper_serré_h})")
            return
        
        try:
            h_val = int(arg)
            if not 3 <= h_val <= 15:
                await event.respond("❌ h doit être entre 3 et 15")
                return
            
            old_h = hyper_serré_h
            hyper_serré_h = h_val
            hyper_serré_active = True
            
            for tracker in cycle_trackers.values():
                tracker.reset()
            
            example_cycle = 596
            example_pred = example_cycle + h_val + 1
            
            await event.respond(
                f"✅ **Mode hyper serré configuré**\n\n"
                f"h: {old_h} → **{hyper_serré_h}**\n"
                f"Statut: ✅ ACTIF\n\n"
                f"📋 **Nouvelle logique:**\n"
                f"• Vérifie **{h_val}** numéros consécutifs depuis le cycle\n"
                f"• Si tous échouent → prédit **cycle + h + 1**\n"
                f"• Exemple: cycle {example_cycle} échoue sur {example_cycle}-{example_cycle+h_val-1}\n"
                f"  → Prédiction: **{example_pred}**\n"
                f"• Vérification: {example_pred}, {example_pred+1}, {example_pred+2}\n\n"
                f"✅ Compteurs reset pour nouvelle analyse."
            )
            logger.info(f"Admin set h={h_val} (hyper serré)")
            
        except ValueError:
            await event.respond("❌ Usage: `/h [3-15]`, `/h on` ou `/h off`")
            
    except Exception as e:
        logger.error(f"Erreur cmd_h: {e}")
        await event.respond(f"❌ Erreur: {e}")

async def cmd_history(event):
    """Affiche l'historique des 5 derniers messages finalisés et prédictions."""
    if event.is_group or event.is_channel:
        return
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
    global hyper_serré_active, hyper_serré_h
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    if hyper_serré_active:
        mode_str = f"🔥 HYPER SERRÉ (h={hyper_serré_h})"
        count_needed = hyper_serré_h
        pred_formula = f"cycle + {hyper_serré_h} + 1"
    else:
        mode_str = f"📊 STANDARD ({NUMBERS_PER_TOUR} num/tour)"
        count_needed = NUMBERS_PER_TOUR
        pred_formula = "cycle + intervalle - 1"
    
    lines = [
        "📈 **COUNTERS DE MANQUES DES CYCLES**",
        f"Mode: {mode_str}",
        f"Formule prédiction: {pred_formula}",
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
        tracker.update_to_current_game(current_game_number)
        
        current = tracker.get_current_cycle_target()
        to_check = tracker.get_numbers_to_check_this_tour()
        checked = tracker.tour_checked_numbers
        
        progress = len(checked)
        
        bar_filled = '█' * progress
        bar_empty = '░' * (count_needed - progress)
        bar = f"[{bar_filled}{bar_empty}]"
        
        if tracker.pending_prediction:
            emoji, status = "🔮", f"PRÉDICTION #{tracker.pending_prediction}"
        elif tracker.current_tour == 2:
            emoji, status = "⚠️", f"Tour 2 critique"
        elif progress > 0:
            emoji, status = "⏳", f"Tour {tracker.current_tour} en cours"
        else:
            emoji, status = "✅", "En attente"
        
        nums = []
        for i, n in enumerate(to_check):
            if n in checked:
                found = tracker.verification_history.get(n, False)
                nums.append(f"{'✅' if found else '❌'}{n}")
            else:
                nums.append(f"⏳{n}")
        
        if hyper_serré_active:
            if current:
                pred_num = current + hyper_serré_h + 1
                pred_info = f"Si échec → prédit #{pred_num} (cycle+{hyper_serré_h}+1)"
            else:
                pred_info = "N/A"
        else:
            if current:
                interval = SUIT_CYCLES[suit]['interval']
                pred_num = current + interval - 1
                pred_info = f"Si échec → prédit #{pred_num} (cycle+{interval}-1)"
            else:
                pred_info = "N/A"
        
        lines.extend([
            f"📊 {tracker.get_display_name()} {emoji}",
            f"   ├─ 🎯 Cycle: #{current if current else 'N/A'}",
            f"   ├─ 🔄 Tour: {tracker.current_tour}/{CONSECUTIVE_FAILURES_NEEDED}",
            f"   ├─ 📉 Manques: {tracker.miss_counter}/{CONSECUTIVE_FAILURES_NEEDED}",
            f"   ├─ 🔍 {bar} ({progress}/{count_needed})",
            f"   ├─ 🎲 {' → '.join(nums) if nums else 'N/A'}",
            f"   ├─ 📝 {pred_info}",
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
    """Affiche l'aide complète avec TOUTES les commandes."""
    if event.is_group or event.is_channel:
        return
    
    # Déterminer les exemples selon le mode
    if hyper_serré_active:
        mode_example = f"Hyper serré h={hyper_serré_h}: échec 596-{596+hyper_serré_h-1} → prédit {596+hyper_serré_h+1}"
    else:
        mode_example = "Standard ♥️: échec 1020-1022 → prédit 1025 (vérif 1025-1027)"

    help_text = f"""📖 **BACCARAT AI - AIDE COMPLÈTE**

**🎮 Système de prédiction:**

• **Mode Standard**: 3 échecs consécutifs → prédit cycle+intervalle-1
• **Mode Hyper Serré** (/h): h échecs consécutifs → prédit cycle+h+1

**📋 Exemples:**
• {mode_example}
• **Rattrapages**: ✅0️⃣ (direct) ✅1️⃣ (+1) ✅2️⃣ (+2) ❌ (perdu)

**🔧 Commandes Admin:**

`/status` - Voir les compteurs détaillés de tous les cycles
`/h [n/on/off]` - **Mode hyper serré** (définit le nombre h de vérifications)
`/history` - Historique des 5 derniers messages et prédictions
`/set_tours [1-3]` - Nombre de tours avant prédiction (défaut: 2)
`/reset` - Reset manuel complet du système
`/channels` - Vérifier la configuration des canaux
`/test` - **Test d'envoi** au canal de prédiction
`/announce [message]` - Envoyer une annonce personnalisée
`/help` - Afficher cette aide

**💡 Détails des modes:**

**Mode Standard:**
• Vérifie 3 numéros consécutifs du cycle
• Si tous échouent → prédit le numéro (cycle + intervalle - 1)
• Exemple Cœur (intervalle 6): cycle 1020 échoue → prédit 1025

**Mode Hyper Serré (/h 5):**
• Vérifie h numéros consécutifs (ex: 5 numéros)
• Si tous échouent → prédit (cycle + h + 1)
• Exemple: h=5, cycle 596 échoue sur 596-600 → prédit 602

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
    """Affiche la config et vérifie l'accès aux canaux."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    src_status = pred_status = "❌"
    src_name = pred_name = "Inaccessible"
    
    # Vérifier canal source
    try:
        if SOURCE_CHANNEL_ID:
            src_entity = await resolve_channel(SOURCE_CHANNEL_ID)
            if src_entity:
                src_status = "✅"
                src_name = getattr(src_entity, 'title', 'Sans titre')
    except Exception as e:
        src_status = f"❌ ({str(e)[:30]})"
    
    # Vérifier canal prédiction
    try:
        if PREDICTION_CHANNEL_ID:
            pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
            if pred_entity:
                pred_status = "✅"
                pred_name = getattr(pred_entity, 'title', 'Sans titre')
    except Exception as e:
        pred_status = f"❌ ({str(e)[:30]})"
    
    # Info mode hyper serré
    if hyper_serré_active:
        mode_info = f"🔥 Hyper serré ON (h={hyper_serré_h})"
    else:
        mode_info = f"📊 Standard (prédit cycle+intervalle-1)"
    
    msg = f"""📡 **CONFIGURATION DES CANAUX**

**Canal Source:**
ID: `{SOURCE_CHANNEL_ID}`
Status: {src_status}
Nom: {src_name}

**Canal Prédiction:**
ID: `{PREDICTION_CHANNEL_ID}`
Status: {pred_status}
Nom: {pred_name}

**⚠️ IMPORTANT:** Le bot doit être **administrateur** du canal de prédiction avec droit d'envoi de messages!

**Paramètres:**
Mode: {mode_info}
Tours avant prédiction: {CONSECUTIVE_FAILURES_NEEDED}
Admin ID: `{ADMIN_ID}`

**Cycles configurés:** ♠️+5 ❤️+6 ♦️+6 ♣️+7"""
    
    await event.respond(msg)

async def cmd_test(event):
    """Test d'envoi au canal de prédiction - CORRIGÉ."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return
    
    await event.respond("🧪 Test de connexion au canal de prédiction...")
    
    try:
        # Vérifier si le canal est configuré
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ PREDICTION_CHANNEL_ID non configuré dans config.py")
            return
        
        # Résoudre le canal
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond(
                f"❌ **Canal inaccessible** `{PREDICTION_CHANNEL_ID}`\n\n"
                f"Vérifiez:\n"
                f"1. L'ID est correct (format: -100xxxxxxxxxx)\n"
                f"2. Le bot est administrateur du canal\n"
                f"3. Le bot a les permissions d'envoi"
            )
            return
        
        # Canal accessible, envoyer le test
        test_msg = f"""⏳BACCARAT AI 🤖⏳ [TEST]

PLAYER : 9999 ♠️ : TEST EN COURS....
🕐 {datetime.now().strftime('%H:%M:%S')}"""
        
        sent = await client.send_message(prediction_entity, test_msg)
        await asyncio.sleep(2)
        
        # Mettre à jour le message
        await client.edit_message(
            prediction_entity,
            sent.id,
            f"""⏳BACCARAT AI 🤖⏳ [TEST]

PLAYER : 9999 ♠️ : ✅0️⃣ TEST RÉUSSI
🕐 {datetime.now().strftime('%H:%M:%S')}"""
        )
        
        await asyncio.sleep(2)
        
        # Supprimer le message de test
        await client.delete_messages(prediction_entity, [sent.id])
        
        await event.respond(
            f"✅ **TEST RÉUSSI!**\n\n"
            f"Canal: `{pred_name}` (ID: {prediction_entity.id})\n"
            f"Le bot peut envoyer, modifier et supprimer des messages.\n\n"
            f"Les prédictions fonctionneront correctement."
        )
        
    except ChatWriteForbiddenError:
        await event.respond(
            f"❌ **Permission refusée**\n\n"
            f"Le bot ne peut pas écrire dans le canal.\n"
            f"→ Ajoutez le bot comme **administrateur** avec droit d'envoi de messages."
        )
    except Exception as e:
        logger.error(f"Erreur test: {e}")
        await event.respond(f"❌ Échec du test: {e}")

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
        await event.respond("❌ Trop long (max 500 caractères)")
        return
    
    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond("❌ Canal de prédiction non accessible")
            return
        
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
        
        sent = await client.send_message(prediction_entity, msg)
        await event.respond(f"✅ Annonce envoyée (ID: {sent.id})")
        
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

def setup_handlers():
    """Configure les handlers."""
    client.add_event_handler(cmd_h, events.NewMessage(pattern=r'^/h'))
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history, events.NewMessage(pattern=r'^/history$'))
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
        
        # Vérifier le canal de prédiction au démarrage
        if PREDICTION_CHANNEL_ID:
            try:
                pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
                if pred_entity:
                    prediction_channel_ok = True
                    logger.info(f"✅ Canal prédiction OK: {getattr(pred_entity, 'title', 'Unknown')} (ID: {pred_entity.id})")
                else:
                    logger.error(f"❌ Canal prédition inaccessible: {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur vérification canal prédiction: {e}")
        
        logger.info("🤖 Bot démarré avec succès")
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
        logger.info(f"📊 Mode: {'Hyper serré h=' + str(hyper_serré_h) if hyper_serré_active else 'Standard'}")
        
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
        logger.info("Arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
