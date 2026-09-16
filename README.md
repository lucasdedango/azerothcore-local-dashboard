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
- indicateur automatique de mise à jour du dashboard, avec mise à jour confirmée en deux clics et sauvegarde locale
- état et mise à jour confirmée de chaque module depuis le dépôt custom groupé ou depuis son propre dépôt Git
- recherche, installation et suppression confirmées des modules C++ conventionnels, avec liens directs vers leur fiche du catalogue officiel AzerothCore et leur dépôt GitHub

## Sécurité

- serveur HTTP lié uniquement à `127.0.0.1`
- chemins d'édition hôte limités à `C:\azerothcore-playerbots`
- aucun bouton `docker compose down -v`
- rebuild lancé via le `rebuild.bat` fourni
- les actions interactives sont ouvertes dans une vraie console Windows
- la vérification des versions ne modifie aucun module
- l'installateur refuse les URL libres : il clone uniquement un dépôt encore présent dans la catégorie `azerothcore-module`, avec un nom `mod-*`, puis accepte la structure actuelle (`src` contenant du C++) ou l'ancienne structure avec un `CMakeLists.txt` racine
- l'installation est atomique, n'exécute aucun code du module et ne lance automatiquement ni SQL ni rebuild
- la mise à jour prépare et vérifie la nouvelle version avant remplacement, conserve une sauvegarde complète dans `module-backups` et ne lance automatiquement ni SQL, ni modification de config, ni rebuild
- la suppression exige de saisir exactement le nom du module ; les configs actives `.conf` et `.conf.dist` sont sauvegardées avant suppression, les fichiers Git en lecture seule sont gérés sous Windows, et l'assistant SQL laisse tout décoché par défaut, distingue les scripts officiels des propositions bornées déduites, puis sauvegarde les tables avant d'exécuter une proposition déduite
- l'auto-update permet de choisir une branche GitHub publiée, ne remplace que les fichiers déclarés par son manifeste contrôlé et les sauvegarde dans `dashboard-backups`
- le retour à `main` supprime uniquement les fichiers suivis qui avaient été ajoutés par la branche de test, après les avoir sauvegardés ; les fichiers non déclarés restent intacts

## Fichiers

- `dashboard-update-manifest.json` : source de vérité des fichiers que l'auto-update est autorisé à comparer et installer
- `dashboard.bat` : lance l'interface locale sous Windows
- `dashboard_server.py` : fournit le backend HTTP local
- `dashboard.html` : fournit l'interface web locale
- `README.md` : documente l'installation et les fichiers du dashboard
- `rebuild.bat` : point d'entrée Windows de la reconstruction
- `rebuild-azerothcore.ps1` : réalise la reconstruction d'AzerothCore
- `patch-and-rebuild.ps1` : applique les patchs Playerbots prévus puis lance la reconstruction
- `azerothcore-maintenance-guide.html` : documente les workflows de maintenance

`azerothcore-commandes-joueur.html` est une cheat sheet accessible depuis le dashboard, mais n'est pas actuellement gérée par l'auto-update. Un fichier présent dans Git mais absent de `dashboard-update-manifest.json` n'est volontairement ni comparé ni installé.

## Ajouter un fichier géré par l'auto-update

Chaque nouveau fichier géré doit :

1. être ajouté au dépôt ;
2. être déclaré dans `dashboard-update-manifest.json` avec la politique prise en charge `replace` ;
3. avoir une `description` expliquant précisément son rôle ;
4. être ajouté à la section « Fichiers » ci-dessus avec son chemin et son utilité ;
5. être accompagné d'un test si son installation modifie le workflow du dashboard.

- `-File`
- `C:\azerothcore-playerbots\rebuild-azerothcore.ps1`

Le chemin n'est donc plus transmis avec des guillemets littéraux.

`Patch + Rebuild` utilise désormais un vrai fichier `patch-and-rebuild.ps1`, ce qui supprime aussi les chaînes de commandes imbriquées.
