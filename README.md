# AzerothCore Local Dashboard

Destination prévue : `C:\azerothcore-playerbots`

## Installation

1. Extraire **le contenu** de cette archive dans `C:\azerothcore-playerbots`.
2. Accepter le remplacement de `rebuild.bat` / `rebuild-azerothcore.ps1` uniquement si tu veux utiliser les versions fournies dans cette archive.
3. Double-cliquer sur `dashboard.bat`.
4. Le navigateur s'ouvre sur `http://127.0.0.1:8765/`.

Le dashboard utilise le lanceur Windows `py` (comme tes autres scripts Python) et uniquement la bibliothèque standard : aucun `pip install` n'est nécessaire.

## Fonctions déjà actives

- état Docker + database/auth/world
- démarrer / arrêter / restart
- restart ou force-recreate du worldserver
- validation `docker compose config`
- lancement du rebuild existant dans une vraie console
- lancement des deux scripts de patch Playerbots
- logs worldserver récents et logs live
- ouverture des dossiers repo / modules / backups
- liens vers les deux HTML fournis
- éditeur des fichiers `.env`, compose YAML et configs des modules
- lecture/écriture des configs actives du conteneur worldserver avec `docker cp`
- backup automatique d'une config avant chaque sauvegarde depuis l'interface
- placeholders visibles pour les futures fonctions prévues

## Sécurité

- serveur HTTP lié uniquement à `127.0.0.1`
- chemins d'édition hôte limités à `C:\azerothcore-playerbots`
- aucun bouton `docker compose down -v`
- rebuild lancé via le `rebuild.bat` fourni
- les actions interactives sont ouvertes dans une vraie console Windows

## Fichiers

- `dashboard.bat` : démarre l'interface
- `dashboard_server.py` : backend local
- `dashboard.html` : interface
- `rebuild.bat` : script fourni
- `rebuild-azerothcore.ps1` : helper fourni
- `azerothcore-commandes-joueur.html` : cheat sheet fournie
- `azerothcore-maintenance-guide.html` : guide fourni


## v0.2

- hover descriptif systématique sur les boutons et onglets
- bouton Rebuild corrigé : appel direct à `rebuild-azerothcore.ps1`
- nouveau bouton `Patch + Rebuild`
- recherche des options natives AzerothCore depuis `worldserver.conf.dist`
- conversion automatique vers les variables Docker `AC_...`
- ajout / mise à jour automatique dans `docker-compose.override.yml`
- backup automatique + validation `docker compose config` + restauration si YAML invalide
- éditeur `.conf` double vue : texte brut + champs visuels `clé = valeur`
- filtrage visuel par clé/commentaire


## v0.2.1 hotfix

- correction du lancement de toutes les consoles Windows depuis le dashboard
- suppression du fragile `cmd /c start ...` qui cassait les guillemets autour des chemins
- ouverture directe avec `cmd.exe /k` + `CREATE_NEW_CONSOLE`
- le dossier de travail est défini directement sur `C:\azerothcore-playerbots`


## v0.2.2 hotfix

Le problème venait encore du quoting Windows, mais cette fois entre `cmd.exe` et le paramètre PowerShell `-File`.

Le dashboard ne fabrique plus une chaîne comme :

`powershell ... -File ".\rebuild-azerothcore.ps1"`

Il lance maintenant directement `powershell.exe` avec une vraie liste d'arguments Windows :

- `-File`
- `C:\azerothcore-playerbots\rebuild-azerothcore.ps1`

Le chemin n'est donc plus transmis avec des guillemets littéraux.

`Patch + Rebuild` utilise désormais un vrai fichier `patch-and-rebuild.ps1`, ce qui supprime aussi les chaînes de commandes imbriquées.
