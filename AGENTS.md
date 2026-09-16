# AGENTS.md

## But du repo

Ce repo contient le dashboard local de gestion du serveur AzerothCore.

Le dashboard tourne uniquement en local et fournit une interface simple pour :
- démarrer / redémarrer / reconstruire le serveur ;
- lancer les patchs ;
- voir les logs ;
- éditer les configs ;
- gérer le `docker-compose.override.yml` ;
- ouvrir les dossiers utiles ;
- accéder aux documentations HTML.

Le backend est volontairement léger et utilise Python standard library.

## Environnement attendu

Projet AzerothCore :

`C:\azerothcore-playerbots`

Python sous Windows :

`py`

Le dashboard doit rester accessible uniquement sur :

`127.0.0.1`

Ne pas l'exposer sur le réseau sans demande explicite.

## Fichiers principaux

Typiquement :
- `dashboard.bat`
- `dashboard_server.py`
- `dashboard.html`
- `rebuild.bat`
- `rebuild-azerothcore.ps1`
- `patch-and-rebuild.ps1`
- `azerothcore-commandes-joueur.html`
- `azerothcore-maintenance-guide.html`

## Règles de sécurité

Priorité absolue : ne pas mettre les données du serveur en danger.

Interdictions :
- jamais `docker compose down -v` ;
- jamais supprimer ou recréer un volume automatiquement ;
- jamais modifier directement les bases sans action explicite ;
- jamais écraser une config sans backup ;
- jamais masquer une erreur de validation Docker/YAML.

Les actions interactives doivent s'ouvrir dans une vraie console Windows visible.

Pour PowerShell, passer `-File` et le chemin comme arguments séparés : éviter les chaînes `cmd /c start ...` fragiles avec guillemets imbriqués.

## Rebuild

Le bouton Rebuild doit lancer directement :

`rebuild-azerothcore.ps1`

Le bouton Patch + Rebuild doit :
1. appliquer le patch selfbot ;
2. appliquer le patch gather / grind ;
3. lancer le rebuild.

Ne pas mélanger le bouton Rebuild simple avec les patchs.

## Édition des configs

Pour les `.conf` :
- toujours faire un backup avant écriture ;
- afficher le contenu brut ;
- proposer une vue visuelle `clé = valeur` ;
- conserver les commentaires quand possible.

Pour `docker-compose.override.yml` :
- afficher le YAML brut ;
- permettre l'ajout / modification des variables `AC_*` ;
- lancer `docker compose config` après modification ;
- restaurer automatiquement l'ancien fichier si la validation échoue.

## Documentation de maintenance

Le repo contient :

`azerothcore-maintenance-guide.html`

Ce fichier fait partie du produit et doit être mis à jour dès qu'un workflow de maintenance change.

Exemples :
- nouvelle procédure de rebuild ;
- nouveau script de patch ;
- changement Docker ;
- backup / restore ;
- diagnostic ;
- nouvelles commandes administrateur.

Éviter d'ajouter dans le guide des commandes SQL ou Docker dangereuses si une méthode plus sûre existe.

## Documentation joueur

`azerothcore-commandes-joueur.html` peut aussi être présent pour accès rapide depuis le dashboard.

La source de vérité fonctionnelle reste le repo des modules custom ; si les commandes changent, synchroniser le HTML ici.

## Développement

Le dashboard doit rester :
- local ;
- simple ;
- sans dépendance Python externe sauf nécessité réelle ;
- compatible Windows ;
- explicite sur ce qu'un bouton va exécuter.

Chaque bouton actif doit avoir un tooltip clair décrivant son effet.

Les nouvelles fonctions doivent préférer des actions bornées et vérifiables plutôt que des commandes shell arbitraires.

Avant livraison :
1. vérifier la syntaxe Python ;
2. vérifier les chemins Windows ;
3. vérifier les commandes lancées ;
4. vérifier qu'aucune action ne touche aux volumes ou DB de manière destructive ;
5. mettre à jour `azerothcore-maintenance-guide.html` si le workflow utilisateur change.


## IMPORTANT
(( exemple repo module custom utilisé (placé a la même racine) : https://github.com/lucasdedango/azerothcore-custom-modules.git ))
