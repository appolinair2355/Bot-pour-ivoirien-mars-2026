# 📦 Fichiers du Projet BACCARAT AI 🤖

## Fichiers Principaux (OBLIGATOIRES)

| Fichier | Taille | Description | Sur Render |
|---------|--------|-------------|------------|
| **config.py** | ~3.5 KB | Configuration cycles, canaux, paramètres | ✅ Déployé |
| **main.py** | ~16 KB | Code principal du bot | ✅ Déployé |
| **requirements.txt** | ~0.2 KB | Dépendances Python | ✅ Déployé |
| **render.yaml** | ~1 KB | Configuration Render.com | ✅ Déployé (optionnel mais recommandé) |

## Fichiers de Configuration (LOCAL)

| Fichier | Description | Où l'ouvrir |
|---------|-------------|-------------|
| **.env** | Variables secrètes (API, tokens) | Local + Render Dashboard |
| **.env.example** | Template pour .env | Local |
| **.gitignore** | Fichiers ignorés par Git | Local |

## Documentation

| Fichier | Description | Où l'ouvrir |
|---------|-------------|-------------|
| **README.md** | Documentation générale | Local + GitHub |
| **RENDER_DEPLOY.md** | Guide déploiement Render | Local + GitHub |

## Structure Finale sur Render

```
/mnt/kimi/output/
├── config_final.py          → Renommer → config.py
├── baccarat_ai_final.py     → Renommer → main.py
├── requirements.txt         → Garder
├── render.yaml              → Garder
├── .env.example             → Copier → .env (local)
├── .gitignore               → Garder
├── README.md                → Garder
└── RENDER_DEPLOY.md         → Garder
```

## 🚀 Commandes de déploiement

```bash
# 1. Renommer les fichiers
mv config_final.py config.py
mv baccarat_ai_final.py main.py

# 2. Initialiser Git
git init
git add .
git commit -m "BACCARAT AI v1.0"

# 3. Pousser sur GitHub
git remote add origin https://github.com/USER/REPO.git
git push -u origin main

# 4. Sur Render.com → New Web Service → Connecter GitHub
```

## ⚙️ Configuration Render.com

Dans le dashboard Render, section **Environment Variables**:

```
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxyz
ADMIN_ID=123456789
SOURCE_CHANNEL_ID=-1002682552255
PREDICTION_CHANNEL_ID=-1002543915361
TELEGRAM_SESSION=(vide au début, rempli après premier lancement)
PORT=10000
```

## 🔧 Health Check

Le bot expose un endpoint de health check sur:
- Local: http://localhost:10000/health
- Render: https://votre-service.onrender.com/health

Réponse attendue: `OK`
