# config.py
"""
Configuration BACCARAT AI 🤖
"""

import os

def parse_channel_id(env_var: str, default: str) -> int:
    """Parse l'ID de canal Telegram."""
    value = os.getenv(env_var) or default
    try:
        channel_id = int(value)
        if channel_id > 0 and len(str(channel_id)) >= 10:
            channel_id = -channel_id
        return channel_id
    except:
        return int(default)

# Canaux Telegram
SOURCE_CHANNEL_ID = parse_channel_id('SOURCE_CHANNEL_ID', '-1002682552255')
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1003336559159')

# Authentification
ADMIN_ID = int(os.getenv('ADMIN_ID') or '0')
API_ID = int(os.getenv('API_ID') or '0')
API_HASH = os.getenv('API_HASH') or ''
BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
TELEGRAM_SESSION = os.getenv('TELEGRAM_SESSION', '')

# Serveur (Render utilise le port 10000 par défaut)
PORT = int(os.getenv('PORT') or '10000')

# Paramètres du système de prédiction
CONSECUTIVE_FAILURES_NEEDED = int(os.getenv('FAILURES_NEEDED', '2'))  # 2 tours avant prédiction
NUMBERS_PER_TOUR = 3  # 3 numéros vérifiés par tour  ← MANQUAIT CETTE LIGNE !

# Cycles des couleurs
SUIT_CYCLES = {
    '♠': {'start': 1, 'interval': 5},   # Pique: 1, 6, 11, 16...
    '♥': {'start': 1, 'interval': 6},   # Cœur: 1, 7, 13, 19...
    '♦': {'start': 1, 'interval': 6},   # Carreau: 1, 7, 13, 19...
    '♣': {'start': 1, 'interval': 7},   # Trèfle: 1, 8, 15, 22...
}

ALL_SUITS = ['♠', '♥', '♦', '♣']

SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}
