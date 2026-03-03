# 🚀 Déploiement sur Render.com

## Étape 1: Préparer les fichiers

Assurez-vous d'avoir ces 6 fichiers dans votre repo GitHub:

```
baccarat-ai/
├── config.py           (anciennement config_final.py)
├── main.py             (anciennement baccarat_ai_final.py)
├── requirements.txt
├── render.yaml         ← IMPORTANT pour Render
├── .env.example        (optionnel)
└── README.md
```

## Étape 2: Créer le repo GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/VOTRE_USER/baccarat-ai.git
git push -u origin main
```

## Étape 3: Déployer sur Render

### Option A: Via Dashboard (Recommandé)

1. Allez sur [dashboard.render.com](https://dashboard.render.com)
2. Cliquez **"New +"** → **"Web Service"**
3. Connectez votre repo GitHub
4. Remplissez le formulaire:

| Champ | Valeur |
|-------|--------|
| Name | `baccarat-ai` |
| Runtime | `Python 3` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python main.py` |
| Instance Type | `Free` (ou supérieur) |

5. Cliquez **"Advanced"** et ajoutez:
   - **Health Check Path**: `/health`

6. Ajoutez les **Environment Variables**:
   - `API_ID` = votre_api_id
   - `API_HASH` = votre_api_hash
   - `BOT_TOKEN` = votre_bot_token
   - `ADMIN_ID` = votre_telegram_id
   - `SOURCE_CHANNEL_ID` = -100xxxxxxxxxx
   - `PREDICTION_CHANNEL_ID` = -100xxxxxxxxxx
   - `TELEGRAM_SESSION` = (laisser vide initialement)

7. Cliquez **"Create Web Service"**

### Option B: Via render.yaml (Blueprint)

1. Poussez le `render.yaml` sur GitHub
2. Dashboard Render → **"New +"** → **"Blueprint"**
3. Connectez le repo contenant `render.yaml`
4. Render détecte automatiquement la configuration
5. Remplissez les variables d'environnement manquantes
6. Déployez

## Étape 4: Récupérer la Session String (IMPORTANT)

Au premier démarrage, le bot va générer une session. Vous devez:

1. Regarder les **Logs** dans le dashboard Render
2. Chercher une ligne comme:
   ```
   SESSION_STRING: 1BQANOTEz...longue_chaine...
   ```
3. Copiez cette valeur
4. Allez dans **Environment** → Ajoutez/modifiez `TELEGRAM_SESSION`
5. Redéployez le service

> ⚠️ **Sans la session string**, le bot se reconnectera à chaque redémarrage et risque d'être banni par Telegram pour "session conflict".

## Étape 5: Vérifier le fonctionnement

1. **Health Check**: Allez sur `https://votre-service.onrender.com/health`
   - Doit afficher: `OK`

2. **Logs**: Dans le dashboard, vérifiez que le bot démarre sans erreur

3. **Test**: Envoyez `/status` au bot en privé

## 🔧 Dépannage Render

### Problème: "Session conflict" ou "Another instance running"
**Solution**: Ajoutez la `TELEGRAM_SESSION` dans les variables d'environnement.

### Problème: "Health check failed"
**Solution**: Vérifiez que le serveur web démarre bien sur `0.0.0.0:10000` (déjà configuré dans le code).

### Problème: Le bot ne voit pas les messages
**Solution**: 
- Vérifiez que `SOURCE_CHANNEL_ID` est correct
- Le bot doit être membre du canal source

### Problème: Déploiement échoue
**Solution**: Vérifiez les logs dans l'onglet **Events** du dashboard.

## 📊 Plan Render Recommandé

| Plan | Prix | Avantage |
|------|------|----------|
| **Free** | $0 | 750 heures/mois, s'arrête après inactivité |
| **Starter** | $7/mois | Toujours allumé, meilleure réactivité |
| **Standard** | $25/mois | Plus de RAM/CPU pour gros volumes |

Pour un bot de prédiction, le plan **Starter** ($7/mois) est recommandé pour éviter que le service ne s'arrête.

## 🔗 Liens utiles

- [Dashboard Render](https://dashboard.render.com)
- [Docs Web Services](https://render.com/docs/web-services)
- [Health Checks](https://render.com/docs/health-checks)
