#!/usr/bin/env python3
# AzerothCore local dashboard - standard library only.
# Binds ONLY to 127.0.0.1 and only allows operations inside PROJECT_ROOT.

from __future__ import annotations
import datetime as dt
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT_ROOT = Path(r"C:\azerothcore-playerbots").resolve()
HOST = "127.0.0.1"
PORT = 8765
ACTIVE_CONF_DIR = "/azerothcore/env/dist/etc/modules"
ALLOWED_EDIT_SUFFIXES = {".conf", ".dist", ".yml", ".yaml", ".env", ".txt", ".json"}
DASHBOARD_REPO = "lucasdedango/azerothcore-local-dashboard"
MODULES_REPO = "lucasdedango/azerothcore-custom-modules"
GITHUB_BRANCH = "main"
UPDATE_MANIFEST = "dashboard-update-manifest.json"
UPDATE_STATE = ".dashboard-update-state.json"
REQUIRED_DASHBOARD_FILES = frozenset((
    UPDATE_MANIFEST,
    "dashboard_server.py",
    "dashboard.html",
))
SUPPORTED_MANIFEST_VERSION = 1
SUPPORTED_UPDATE_POLICIES = frozenset(("replace",))
MAX_UPDATE_FILES = 100
MAX_UPDATE_BYTES = 10 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_BRANCHES = 100
MAX_MODULE_SQL_BYTES = 1024 * 1024
FORBIDDEN_UPDATE_ROOTS = frozenset((
    "modules", "env", "config-backups", "dashboard-backups", "module-backups",
    "docker", "docker-data", "data", "mysql-data",
))
UPDATE_CACHE_SECONDS = 300
CATALOGUE_URL = "https://raw.githubusercontent.com/azerothcore/azerothcore.github.io/master/data/catalogue.json"
CATALOGUE_PAGE = "https://www.azerothcore.org/catalogue.html#/"
MAX_CATALOGUE_BYTES = 5 * 1024 * 1024
_archive_cache = {}
_archive_cache_lock = threading.Lock()
_dashboard_update_lock = threading.Lock()
_module_operation_lock = threading.Lock()
_catalogue_cache = None
_catalogue_cache_time = 0.0

def run(cmd, timeout=45, shell=False):
    try:
        p = subprocess.run(
            cmd, cwd=PROJECT_ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, shell=shell, encoding="utf-8", errors="replace"
        )
        return {"ok": p.returncode == 0, "code": p.returncode, "output": p.stdout}
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return {"ok": False, "code": -1, "output": f"Timeout\n{out}"}
    except Exception as e:
        return {"ok": False, "code": -1, "output": str(e)}


def _remove_temporary_tree(path):
    """Remove a tree, including read-only Git files on Windows."""
    path = Path(path)
    if not path.exists():
        return None

    def make_writable_and_retry(function, target, _error):
        os.chmod(target, 0o700)
        function(target)

    try:
        shutil.rmtree(path, onerror=make_writable_and_retry)
        return None
    except OSError as e:
        return f"Suppression complète du dossier impossible ({path}): {e}"


def _validate_cpp_module_tree(module_path):
    """Accept both current AzerothCore modules and the legacy CMake layout."""
    module_path = Path(module_path)
    if (module_path / "CMakeLists.txt").is_file():
        return
    source_root = module_path / "src"
    if source_root.is_dir() and any(path.is_file() for path in source_root.rglob("*.cpp")):
        return
    raise RuntimeError(
        "Le dépôt ne ressemble pas à un module C++ AzerothCore: aucun fichier .cpp "
        "dans src et aucun CMakeLists.txt à la racine; opération annulée."
    )


def _valid_branch_name(branch):
    return (isinstance(branch, str) and 0 < len(branch) <= 200
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) is not None
            and ".." not in branch.split("/") and not branch.endswith("/"))


def dashboard_branches(force=False):
    """Return the repository branches exposed by GitHub, with main first."""
    url = f"https://api.github.com/repos/{DASHBOARD_REPO}/branches?per_page={MAX_BRANCHES}"
    request = urllib.request.Request(url, headers={
        "User-Agent": "AzerothCore-Local-Dashboard",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(512 * 1024 + 1)
    if len(payload) > 512 * 1024:
        raise RuntimeError("Liste des branches GitHub trop volumineuse.")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Réponse GitHub invalide: {e}") from e
    if not isinstance(data, list):
        raise RuntimeError("GitHub n'a pas renvoyé de liste de branches.")
    names = sorted({item.get("name") for item in data if isinstance(item, dict)
                    and _valid_branch_name(item.get("name"))})
    if GITHUB_BRANCH not in names:
        raise RuntimeError(f"La branche principale {GITHUB_BRANCH!r} est introuvable.")
    return [GITHUB_BRANCH] + [name for name in names if name != GITHUB_BRANCH]


def _require_dashboard_branch(branch):
    if not _valid_branch_name(branch):
        raise RuntimeError("Nom de branche invalide.")
    if branch not in dashboard_branches():
        raise RuntimeError("Branche absente de la liste publiée par GitHub.")
    return branch


def github_archive(repo, branch=GITHUB_BRANCH, force=False):
    """Return a GitHub branch archive without extracting untrusted paths."""
    key = (repo, branch)
    now = time.monotonic()
    with _archive_cache_lock:
        cached = _archive_cache.get(key)
        if cached and not force and now - cached[0] < UPDATE_CACHE_SECONDS:
            return cached[1]

    url = f"https://codeload.github.com/{repo}/zip/refs/heads/{urllib.parse.quote(branch, safe='/')}"
    request = urllib.request.Request(url, headers={"User-Agent": "AzerothCore-Local-Dashboard"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(25 * 1024 * 1024 + 1)
    if len(payload) > 25 * 1024 * 1024:
        raise RuntimeError("Archive GitHub trop volumineuse (limite: 25 Mo).")

    files = {}
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            parts = Path(info.filename).parts
            if len(parts) < 2 or ".." in parts:
                continue
            relative = Path(*parts[1:]).as_posix()
            files[relative] = archive.read(info)
    if not files:
        raise RuntimeError("L'archive GitHub reçue est vide.")
    with _archive_cache_lock:
        _archive_cache[key] = (now, files)
    return files


def dashboard_update_archive(branch=GITHUB_BRANCH, force=False):
    """Download and validate the remote manifest, returning only its files."""
    archive = github_archive(DASHBOARD_REPO, branch=branch, force=force)
    raw_manifest = archive.get(UPDATE_MANIFEST)
    if raw_manifest is None:
        raise RuntimeError(f"Manifeste distant absent: {UPDATE_MANIFEST}")
    if len(raw_manifest) > MAX_MANIFEST_BYTES:
        raise RuntimeError("Manifeste distant trop volumineux.")
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Manifeste distant JSON invalide: {e}") from e
    if not isinstance(manifest, dict):
        raise RuntimeError("Le manifeste distant doit être un objet JSON.")
    if manifest.get("version") != SUPPORTED_MANIFEST_VERSION:
        raise RuntimeError(f"Version de manifeste non supportée: {manifest.get('version')!r}")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Le manifeste distant ne contient aucune liste de fichiers valide.")
    if len(entries) > MAX_UPDATE_FILES:
        raise RuntimeError(f"Le manifeste dépasse la limite de {MAX_UPDATE_FILES} fichiers.")

    selected = {}
    normalized_paths = set()
    total_size = 0
    root = PROJECT_ROOT.resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Chaque entrée du manifeste doit être un objet.")
        name = entry.get("path")
        description = entry.get("description")
        policy = entry.get("update_policy")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise RuntimeError("Chaque chemin du manifeste doit être une chaîne relative non vide.")
        windows_path = PureWindowsPath(name)
        if "\\" in name or PurePosixPath(name).is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise RuntimeError(f"Chemin absolu ou non portable refusé: {name}")
        parts = PurePosixPath(name).parts
        if ".." in parts or "." in parts or not parts:
            raise RuntimeError(f"Chemin non sûr refusé: {name}")
        if parts[0].casefold() in FORBIDDEN_UPDATE_ROOTS or name.casefold() == UPDATE_STATE.casefold():
            raise RuntimeError(f"Répertoire protégé refusé dans le manifeste: {name}")
        destination = (root / Path(*parts)).resolve()
        if destination == root or root not in destination.parents:
            raise RuntimeError(f"Chemin hors du projet refusé: {name}")
        normalized = PurePosixPath(*parts).as_posix().casefold()
        if normalized in normalized_paths:
            raise RuntimeError(f"Chemin dupliqué dans le manifeste: {name}")
        if not isinstance(description, str) or not description.strip():
            raise RuntimeError(f"Description manquante pour: {name}")
        if policy not in SUPPORTED_UPDATE_POLICIES:
            raise RuntimeError(f"Politique de mise à jour non supportée pour {name}: {policy!r}")
        if name not in archive:
            raise RuntimeError(f"Fichier déclaré absent de l'archive distante: {name}")
        total_size += len(archive[name])
        if total_size > MAX_UPDATE_BYTES:
            raise RuntimeError("Les fichiers déclarés dépassent la limite totale de 10 Mo.")
        normalized_paths.add(normalized)
        selected[name] = archive[name]

    missing_required = sorted(REQUIRED_DASHBOARD_FILES - selected.keys())
    if missing_required:
        raise RuntimeError("Manifeste distant incomplet, fichiers indispensables absents: " + ", ".join(missing_required))
    return selected


def _same_file(path, expected):
    try:
        return path.is_file() and path.read_bytes() == expected
    except OSError:
        return False


def _git_module_status(module_path):
    """Use a module's own Git remote when its directory is an independent clone."""
    try:
        top = subprocess.run(
            ["git", "-C", str(module_path), "rev-parse", "--show-toplevel"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, encoding="utf-8", errors="replace",
        )
        if top.returncode or Path(top.stdout.strip()).resolve() != module_path.resolve():
            return None
        details = {}
        for key, command in {
            "local": ["git", "-C", str(module_path), "rev-parse", "HEAD"],
            "remote": ["git", "-C", str(module_path), "remote", "get-url", "origin"],
            "branch": ["git", "-C", str(module_path), "branch", "--show-current"],
            "dirty": ["git", "-C", str(module_path), "status", "--porcelain"],
        }.items():
            proc = subprocess.run(
                command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=8, encoding="utf-8", errors="replace",
            )
            if proc.returncode:
                return None
            details[key] = proc.stdout.strip()
        branch = details["branch"] or "HEAD"
        lookup = subprocess.run(
            ["git", "ls-remote", details["remote"], f"refs/heads/{branch}"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, encoding="utf-8", errors="replace",
        )
        if lookup.returncode or not lookup.stdout.strip():
            raise RuntimeError(lookup.stderr.strip() or f"Branche distante introuvable: {branch}")
        remote_sha = lookup.stdout.split()[0]
        return {
            "name": module_path.name,
            "installed": True,
            "up_to_date": details["local"] == remote_sha and not details["dirty"],
            "changed_files": len(details["dirty"].splitlines()),
            "source": details["remote"],
            "branch": branch,
        }
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"Git {module_path.name}: {e}") from e


def _dashboard_update_state():
    try:
        data = json.loads((PROJECT_ROOT / UPDATE_STATE).read_text(encoding="utf-8"))
        if (isinstance(data, dict) and _valid_branch_name(data.get("branch"))
                and isinstance(data.get("managed_files"), list)):
            return data
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return {"branch": GITHUB_BRANCH, "managed_files": []}


def dashboard_update_status(branch=GITHUB_BRANCH):
    _require_dashboard_branch(branch)
    remote = dashboard_update_archive(branch=branch)
    changed = [name for name, content in remote.items() if not _same_file(PROJECT_ROOT / name, content)]
    return {
        "ok": True,
        "up_to_date": not changed,
        "changed_files": changed,
        "compared_files": len(remote),
        "repository": f"https://github.com/{DASHBOARD_REPO}",
        "branch": branch,
        "installed_branch": _dashboard_update_state()["branch"],
    }


def module_update_statuses():
    remote = github_archive(MODULES_REPO)
    remote_modules = sorted({
        parts[1]
        for name in remote
        if len(parts := Path(name).parts) >= 3 and parts[0] == "modules"
    })
    local_root = PROJECT_ROOT / "modules"
    local_modules = {p.name for p in local_root.iterdir() if p.is_dir()} if local_root.is_dir() else set()
    result = []
    for module in sorted(set(remote_modules) | local_modules):
        prefix = f"modules/{module}/"
        tracked = {name[len(prefix):]: data for name, data in remote.items() if name.startswith(prefix)}
        local = local_root / module
        own_git = _git_module_status(local) if local.is_dir() else None
        if own_git:
            result.append(own_git)
            continue
        if not tracked:
            result.append({
                "name": module,
                "installed": True,
                "up_to_date": None,
                "changed_files": 0,
                "source": "Source distante inconnue",
            })
            continue
        changed = [rel for rel, data in tracked.items() if not _same_file(local / Path(rel), data)]
        result.append({
            "name": module,
            "installed": local.is_dir(),
            "up_to_date": local.is_dir() and not changed,
            "changed_files": len(changed),
            "source": f"https://github.com/{MODULES_REPO}",
        })
    return result


def catalogue_modules(force=False):
    """Return installable C++ modules from AzerothCore's curated catalogue."""
    global _catalogue_cache, _catalogue_cache_time
    now = time.monotonic()
    if _catalogue_cache is not None and not force and now - _catalogue_cache_time < UPDATE_CACHE_SECONDS:
        return [dict(item, installed=(PROJECT_ROOT / "modules" / item["name"]).is_dir()) for item in _catalogue_cache]

    request = urllib.request.Request(CATALOGUE_URL, headers={"User-Agent": "AzerothCore-Local-Dashboard"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(MAX_CATALOGUE_BYTES + 1)
    if len(payload) > MAX_CATALOGUE_BYTES:
        raise RuntimeError("Catalogue AzerothCore trop volumineux.")
    try:
        document = json.loads(payload.decode("utf-8"))
        entries = document["organizations"]["azerothcore"]["azerothcore-module"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as e:
        raise RuntimeError(f"Format du catalogue AzerothCore invalide: {e}") from e
    if not isinstance(entries, list):
        raise RuntimeError("La liste des modules du catalogue est invalide.")

    modules = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        full_name = entry.get("full_name", "")
        name = entry.get("name", "")
        branch = entry.get("default_branch", "")
        catalogue_id = entry.get("id")
        topics = entry.get("topics", [])
        # Only conventional GitHub-hosted C++ modules are eligible. Tools, Lua and
        # SQL catalogue entries intentionally remain manual installs.
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name)
                or not re.fullmatch(r"mod-[A-Za-z0-9_.-]+", name)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
                or not isinstance(catalogue_id, int) or isinstance(catalogue_id, bool)
                or ".." in branch.split("/")
                or "azerothcore-module" not in topics
                or entry.get("archived") is True or entry.get("disabled") is True):
            continue
        modules.append({
            "name": name,
            "full_name": full_name,
            "description": str(entry.get("description") or "")[:500],
            "branch": branch,
            "source": f"https://github.com/{full_name}",
            "catalogue_url": f"{CATALOGUE_PAGE}details/{catalogue_id}",
            "stars": int(entry.get("stargazers_count") or 0),
        })
    modules.sort(key=lambda item: (-item["stars"], item["name"].casefold()))
    _catalogue_cache, _catalogue_cache_time = modules, now
    return [dict(item, installed=(PROJECT_ROOT / "modules" / item["name"]).is_dir()) for item in modules]


def install_catalogue_module(full_name):
    """Clone one validated catalogue module atomically into PROJECT_ROOT/modules."""
    if not _module_operation_lock.acquire(blocking=False):
        return {"ok": False, "code": -1, "output": "Une opération sur un module est déjà en cours."}
    stage_root = None
    result = None
    try:
        matches = [item for item in catalogue_modules(force=True) if item["full_name"] == full_name]
        if len(matches) != 1:
            raise ValueError("Ce dépôt n'est pas un module C++ installable du catalogue AzerothCore.")
        module = matches[0]
        modules_root = PROJECT_ROOT / "modules"
        modules_root.mkdir(parents=True, exist_ok=True)
        destination = modules_root / module["name"]
        if any(child.name.casefold() == module["name"].casefold() for child in modules_root.iterdir()):
            raise FileExistsError(f"Un dossier nommé {module['name']} existe déjà dans modules.")

        stage_root = Path(tempfile.mkdtemp(prefix=".dashboard-install-", dir=modules_root))
        staged = stage_root / module["name"]
        clone = run([
            "git", "clone", "--depth", "1", "--single-branch", "--branch", module["branch"],
            module["source"] + ".git", str(staged),
        ], timeout=180)
        if not clone["ok"]:
            details = clone["output"].strip() or f"Git a quitté avec le code {clone['code']} sans message."
            raise RuntimeError("git clone a échoué; aucun module n'a été installé.\n\n" + details)
        _validate_cpp_module_tree(staged)
        os.replace(staged, destination)
        result = {
            "ok": True,
            "code": 0,
            "output": f"{module['name']} installé depuis {module['source']}. Lancez maintenant un Rebuild AzerothCore.",
            "module": module,
        }
    except Exception as e:
        result = {"ok": False, "code": -1, "output": str(e)}
    finally:
        cleanup_error = None
        if stage_root is not None:
            cleanup_error = _remove_temporary_tree(stage_root)
        _module_operation_lock.release()
        if cleanup_error:
            result["output"] += "\n\n" + cleanup_error
    return result


def update_local_module(name):
    """Stage, validate and replace one known module while retaining a full backup."""
    if not _module_operation_lock.acquire(blocking=False):
        return {"ok": False, "code": -1, "output": "Une opération sur un module est déjà en cours."}
    stage_root = None
    backup = None
    destination = None
    try:
        destination = _validated_local_module(name)
        status = next((item for item in module_update_statuses()
                       if item["name"] == name and item["installed"]), None)
        if not status or status["up_to_date"] is None:
            raise RuntimeError("La source distante de ce module n'est pas connue; mise à jour automatique refusée.")
        if status["up_to_date"]:
            raise RuntimeError(f"{name} est déjà à jour.")

        modules_root = destination.parent
        stage_root = Path(tempfile.mkdtemp(prefix=".dashboard-update-", dir=modules_root))
        staged = stage_root / name
        if status.get("branch"):
            result = run([
                "git", "clone", "--depth", "1", "--single-branch", "--branch", status["branch"],
                status["source"], str(staged),
            ], timeout=180)
            if not result["ok"]:
                raise RuntimeError("git clone a échoué; le module local est inchangé.\n\n" + result["output"])
        else:
            archive = github_archive(MODULES_REPO, force=True)
            prefix = f"modules/{name}/"
            files = {path[len(prefix):]: content for path, content in archive.items()
                     if path.startswith(prefix) and len(path) > len(prefix)}
            if not files:
                raise RuntimeError("Le module n'existe plus dans le dépôt custom; mise à jour refusée.")
            for relative, content in files.items():
                target = staged / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

        _validate_cpp_module_tree(staged)

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = PROJECT_ROOT / "module-backups" / stamp / name
        backup.parent.mkdir(parents=True, exist_ok=False)
        os.replace(destination, backup)
        try:
            os.replace(staged, destination)
        except Exception:
            os.replace(backup, destination)
            raise
        return {
            "ok": True,
            "code": 0,
            "output": f"{name} mis à jour. Ancienne version sauvegardée dans {backup}. Lancez un Rebuild AzerothCore.",
            "backup": str(backup),
        }
    except Exception as e:
        return {"ok": False, "code": -1, "output": str(e)}
    finally:
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)
        _module_operation_lock.release()


def _validated_local_module(name):
    """Return a real, direct child of modules; never follow a user supplied path."""
    if not isinstance(name, str) or not re.fullmatch(r"mod-[A-Za-z0-9_.-]+", name):
        raise ValueError("Nom de module invalide.")
    modules_root = (PROJECT_ROOT / "modules").resolve()
    candidate = modules_root / name
    if not candidate.is_dir() or candidate.is_symlink() or candidate.resolve().parent != modules_root:
        raise FileNotFoundError(f"Module local introuvable: {name}")
    return candidate


def _sql_database_from_path(path, sql_root):
    parts = {part.casefold() for part in path.relative_to(sql_root).parts[:-1]}
    databases = [
        db for db in ("world", "characters", "auth")
        if any(part in (db, f"db-{db}", f"db_{db}") for part in parts)
    ]
    return f"acore_{databases[0]}" if len(databases) == 1 else None


def _sql_proposal_id(source, database, sql, source_content):
    source_digest = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
    material = f"{source}\0{database}\0{sql}\0{source_digest}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def _infer_sql_cleanup(source, database, content):
    """Infer only tightly bounded cleanup statements from one install script."""
    proposals = []
    variables = {
        name.casefold(): int(value)
        for name, value in re.findall(r"(?im)^\s*SET\s+@([A-Za-z0-9_]+)\s*:?=\s*(\d+)\s*;", content)
    }

    # A created table is owned by the module with reasonable confidence, but
    # dropping it destroys all rows and must remain an explicit high-risk choice.
    for match in re.finditer(
            r"(?is)\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([A-Za-z0-9_]+)`?\s*\(", content):
        table = match.group(1)
        sql = f"DROP TABLE IF EXISTS `{table}`;"
        proposals.append({
            "id": _sql_proposal_id(source, database, sql, content),
            "source": source,
            "database": database,
            "sql": sql,
            "tables": [table],
            "risk": "high",
            "destructive": True,
            "title": f"Supprimer la table {table}",
            "explanation": "Le script crée cette table. Sa suppression effacera définitivement toutes ses lignes.",
        })

    # Reuse only DELETE statements whose predicate is demonstrably bounded to
    # literal keys or a numeric range. Never infer an unbounded DELETE.
    delete_pattern = re.compile(
        r"(?is)\bDELETE\s+FROM\s+`?([A-Za-z0-9_]+)`?\s+WHERE\s+(.+?)\s*;"
    )
    for match in delete_pattern.finditer(content):
        table, predicate = match.group(1), match.group(2).strip()
        if not re.search(rf"(?is)\bINSERT\s+INTO\s+`?{re.escape(table)}`?\b", content[match.end():]):
            continue
        bounded = bool(re.fullmatch(
            r"`?[A-Za-z0-9_]+`?\s+IN\s*\(\s*(?:'(?:[^'\\]|\\.)*'|\d+)"
            r"(?:\s*,\s*(?:'(?:[^'\\]|\\.)*'|\d+))*\s*\)", predicate, re.I | re.S
        ))
        set_prefix = ""
        between = re.fullmatch(
            r"`?[A-Za-z0-9_]+`?\s+BETWEEN\s+@([A-Za-z0-9_]+)(?:\s*\+\s*\d+)?"
            r"\s+AND\s+@\1(?:\s*\+\s*\d+)?", predicate, re.I | re.S
        )
        if between and between.group(1).casefold() in variables:
            variable = between.group(1)
            set_prefix = f"SET @{variable}:={variables[variable.casefold()]};\n"
            bounded = True
        if not bounded:
            continue
        sql = f"{set_prefix}DELETE FROM `{table}` WHERE {predicate};"
        proposals.append({
            "id": _sql_proposal_id(source, database, sql, content),
            "source": source,
            "database": database,
            "sql": sql,
            "tables": [table],
            "risk": "medium",
            "destructive": True,
            "title": f"Supprimer les lignes de {table} attribuées au module",
            "explanation": "Le script d'installation supprime cette même sélection avant de la réinsérer. Vérifiez que ces clés ne sont pas partagées.",
        })
    return proposals


def module_removal_plan(name):
    """Describe removable artifacts without changing the module or a database."""
    module = _validated_local_module(name)
    config_names = set()
    conf_dir = module / "conf"
    if conf_dir.is_dir():
        for path in conf_dir.iterdir():
            if path.is_file() and path.name.endswith(".conf.dist"):
                # A module source generally ships foo.conf.dist while the active
                # container can contain both foo.conf and foo.conf.dist.
                config_names.update((path.name.removesuffix(".dist"), path.name))
            elif path.is_file() and path.name.endswith(".conf"):
                config_names.update((path.name, path.name + ".dist"))

    active = []
    try:
        available = {item["path"] for item in list_active_configs()}
        active = sorted(config_names & available)
    except Exception:
        # The folder can still be removed when Docker is unavailable. The UI
        # explicitly reports that no active config was discovered.
        active = []

    sql = []
    discovered_sql = []
    inferred_sql = []
    # AzerothCore modules can use either the legacy sql/ tree or the current
    # data/sql/ layout. Only an explicitly named uninstall script is safe to
    # execute: installation scripts often contain DELETE statements used to
    # make an INSERT idempotent, which does not make them uninstall scripts.
    for sql_root in (module / "sql", module / "data" / "sql"):
        if not sql_root.is_dir():
            continue
        for path in sql_root.rglob("*.sql"):
            relative = path.relative_to(module).as_posix()
            discovered_sql.append(relative)
            database = _sql_database_from_path(path, sql_root)
            lowered = path.name.casefold()
            if any(word in lowered for word in ("uninstall", "remove", "delete", "drop")):
                if database:
                    preview = (path.read_text(encoding="utf-8", errors="replace")
                               if path.stat().st_size <= MAX_MODULE_SQL_BYTES
                               else "-- Script trop volumineux pour être prévisualisé dans le dashboard.")
                    sql.append({"path": relative, "database": database, "content": preview})
                continue
            if database and path.stat().st_size <= MAX_MODULE_SQL_BYTES:
                content = path.read_text(encoding="utf-8", errors="replace")
                inferred_sql.extend(_infer_sql_cleanup(relative, database, content))
    sql_notice = ""
    if discovered_sql and not sql and inferred_sql:
        sql_notice = (
            f"{len(discovered_sql)} script(s) SQL d'installation ou de mise à jour détecté(s). "
            f"L'assistant a préparé {len(inferred_sql)} proposition(s) de nettoyage à vérifier; "
            "elles ne proviennent pas d'un script de désinstallation officiel."
        )
    elif discovered_sql and not sql:
        sql_notice = (
            f"{len(discovered_sql)} script(s) SQL d'installation ou de mise à jour détecté(s), "
            "mais aucun script de désinstallation explicite et attribuable à une base. "
            "Aucun nettoyage suffisamment borné n'a pu être déduit."
        )
    return {
        "name": name,
        "folder": f"modules/{name}",
        "configs": active,
        "sql": sorted(sql, key=lambda item: item["path"]),
        "inferred_sql": sorted(inferred_sql, key=lambda item: (item["database"], item["source"], item["title"])),
        "sql_notice": sql_notice,
    }


def _execute_module_uninstall_sql(module, scripts):
    if not docker_available() or not container_running("ac-database"):
        raise RuntimeError("La base ac-database doit être démarrée pour supprimer le SQL du module.")
    for item in scripts:
        sql_path = module / Path(item["path"])
        payload = sql_path.read_bytes()
        command = ["docker", "exec", "-i", "ac-database", "mysql", "-uroot", "-ppassword", item["database"]]
        proc = subprocess.run(command, cwd=PROJECT_ROOT, input=payload, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        if proc.returncode:
            output = proc.stdout.decode("utf-8", "replace")
            raise RuntimeError(f"Échec du script {item['path']}; le dossier du module a été conservé.\n\n{output}")


def _backup_inferred_sql_tables(name, proposals):
    if not docker_available() or not container_running("ac-database"):
        raise RuntimeError("La base ac-database doit être démarrée pour sauvegarder puis nettoyer le SQL du module.")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = PROJECT_ROOT / "db-backups" / "module-removal" / stamp / name
    backup_dir.mkdir(parents=True, exist_ok=False)
    for database, table in sorted({(p["database"], table) for p in proposals for table in p["tables"]}):
        command = ["docker", "exec", "ac-database", "mysqldump", "-uroot", "-ppassword",
                   "--single-transaction", "--skip-lock-tables", database, table]
        proc = subprocess.run(command, cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)
        if proc.returncode:
            error = proc.stderr.decode("utf-8", "replace")
            raise RuntimeError(f"Sauvegarde de {database}.{table} impossible; aucun SQL déduit n'a été exécuté.\n\n{error}")
        (backup_dir / f"{database}--{table}.sql").write_bytes(proc.stdout)
    return backup_dir


def _execute_inferred_sql(name, proposals, backup=None):
    backup = backup or _backup_inferred_sql_tables(name, proposals)
    for item in proposals:
        command = ["docker", "exec", "-i", "ac-database", "mysql", "-uroot", "-ppassword", item["database"]]
        proc = subprocess.run(command, cwd=PROJECT_ROOT, input=item["sql"].encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        if proc.returncode:
            output = proc.stdout.decode("utf-8", "replace")
            raise RuntimeError(
                f"Échec du nettoyage déduit « {item['title']} ». Le dossier du module est conservé. "
                f"Sauvegarde disponible dans {backup}.\n\n{output}"
            )
    return backup


def remove_local_module(name, remove_configs=False, remove_sql=False, inferred_sql_ids=None,
                        sql_confirmation="", confirmation=""):
    """Remove one module after an exact-name confirmation and optional cleanup."""
    if confirmation != name:
        return {"ok": False, "code": -1, "output": "Confirmation invalide: saisissez exactement le nom du module."}
    if not _module_operation_lock.acquire(blocking=False):
        return {"ok": False, "code": -1, "output": "Une opération sur un module est déjà en cours."}
    try:
        module = _validated_local_module(name)
        plan = module_removal_plan(name)
        inferred_sql_ids = inferred_sql_ids if isinstance(inferred_sql_ids, list) else []
        if not all(isinstance(item, str) for item in inferred_sql_ids):
            raise RuntimeError("Sélection SQL déduite invalide.")
        available_inferred = {item["id"]: item for item in plan["inferred_sql"]}
        if len(set(inferred_sql_ids)) != len(inferred_sql_ids) or any(
                item not in available_inferred for item in inferred_sql_ids):
            raise RuntimeError("Le plan SQL a changé ou contient une sélection inconnue; relancez l'analyse.")
        selected_inferred = [available_inferred[item] for item in inferred_sql_ids]
        if selected_inferred and sql_confirmation != f"SUPPRIMER SQL {name}":
            raise RuntimeError("Confirmation SQL invalide; aucun nettoyage SQL déduit n'a été exécuté.")
        # Complete every inferred-table dump before any selected SQL (including
        # an official uninstall script) is allowed to modify the database.
        inferred_backup = _backup_inferred_sql_tables(name, selected_inferred) if selected_inferred else None
        if remove_sql:
            if not plan["sql"]:
                raise RuntimeError("Aucun script SQL de désinstallation explicite et attribuable à une base n'a été trouvé.")
            _execute_module_uninstall_sql(module, plan["sql"])
        if selected_inferred:
            _execute_inferred_sql(name, selected_inferred, backup=inferred_backup)

        removed_configs = []
        if remove_configs:
            for config_name in plan["configs"]:
                content = read_active_config(config_name)
                backup_text(f"REMOVED-ACTIVE-{config_name}", content)
                result = run(["docker", "exec", "ac-worldserver", "rm", "--", f"{ACTIVE_CONF_DIR}/{config_name}"], timeout=20)
                if not result["ok"]:
                    raise RuntimeError(f"Suppression de {config_name} impossible; le dossier du module a été conservé.\n\n{result['output']}")
                removed_configs.append(config_name)

        removal_error = _remove_temporary_tree(module)
        if removal_error:
            raise RuntimeError(
                removal_error + " Les éventuelles configurations actives sélectionnées ont déjà été "
                "sauvegardées avant leur suppression."
            )
        details = [f"dossier {plan['folder']}"]
        if removed_configs:
            details.append("config(s) active(s) sauvegardée(s) puis supprimée(s): " + ", ".join(removed_configs))
        if remove_sql:
            details.append("script(s) SQL exécuté(s): " + ", ".join(item["path"] for item in plan["sql"]))
        if selected_inferred:
            details.append(f"{len(selected_inferred)} nettoyage(s) SQL déduit(s) exécuté(s), sauvegarde: {inferred_backup}")
        return {"ok": True, "code": 0, "output": f"{name} supprimé ({'; '.join(details)}). Lancez un Rebuild AzerothCore.", "removed": plan}
    except Exception as e:
        return {"ok": False, "code": -1, "output": str(e)}
    finally:
        _module_operation_lock.release()


def install_dashboard_update(branch=GITHUB_BRANCH):
    """Install one published branch and remove files owned only by the prior branch."""
    if not _dashboard_update_lock.acquire(blocking=False):
        return {"ok": False, "code": -1, "output": "Une mise à jour est déjà en cours."}
    try:
        branch = _require_dashboard_branch(branch)
        remote = dashboard_update_archive(branch=branch, force=True)
        state = _dashboard_update_state()
        previous_files = set()
        for name in state.get("managed_files", []):
            if not isinstance(name, str):
                continue
            parts = PurePosixPath(name).parts
            candidate = (PROJECT_ROOT / Path(*parts)).resolve() if parts else PROJECT_ROOT
            if (parts and ".." not in parts and "." not in parts
                    and parts[0].casefold() not in FORBIDDEN_UPDATE_ROOTS
                    and candidate != PROJECT_ROOT and PROJECT_ROOT in candidate.parents):
                previous_files.add(name)
        removed = sorted(previous_files - set(remote))

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = PROJECT_ROOT / "dashboard-backups" / stamp
        stage = Path(tempfile.mkdtemp(prefix="ac-dashboard-update-"))
        replaced = []
        existed = {}
        state_path = PROJECT_ROOT / UPDATE_STATE
        old_state = state_path.read_bytes() if state_path.is_file() else None
        try:
            for name, content in remote.items():
                staged = stage / name
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(content)
            backup.mkdir(parents=True, exist_ok=False)
            existed = {name: (PROJECT_ROOT / name).is_file() for name in remote}
            for name in removed:
                destination = PROJECT_ROOT / name
                if destination.is_file():
                    saved = backup / name
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, saved)
            for name in remote:
                destination = PROJECT_ROOT / name
                saved = backup / name
                if existed[name]:
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, saved)
                destination.parent.mkdir(parents=True, exist_ok=True)
                replaced.append(name)
                os.replace(stage / name, destination)
            for name in removed:
                (PROJECT_ROOT / name).unlink(missing_ok=True)
            new_state = {"version": 1, "branch": branch, "managed_files": sorted(remote)}
            staged_state = stage / UPDATE_STATE
            staged_state.write_text(json.dumps(new_state, indent=2) + "\n", encoding="utf-8")
            os.replace(staged_state, state_path)
        except Exception:
            for name in reversed(replaced):
                saved = backup / name
                destination = PROJECT_ROOT / name
                if existed.get(name) and saved.is_file():
                    shutil.copy2(saved, destination)
                elif not existed.get(name):
                    destination.unlink(missing_ok=True)
            for name in removed:
                saved = backup / name
                if saved.is_file():
                    destination = PROJECT_ROOT / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(saved, destination)
            if old_state is None:
                state_path.unlink(missing_ok=True)
            else:
                state_path.write_bytes(old_state)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        with _archive_cache_lock:
            _archive_cache.pop((DASHBOARD_REPO, branch), None)
        removed_text = f" {len(removed)} fichier(s) propre(s) à l'ancienne branche supprimé(s)." if removed else ""
        return {
            "ok": True,
            "code": 0,
            "output": f"Dashboard mis à jour depuis {branch}.{removed_text} Sauvegarde: {backup}\nRedémarrez le dashboard pour charger le nouveau backend.",
            "backup": str(backup),
            "branch": branch,
            "removed_files": removed,
            "restart_required": True,
        }
    except Exception as e:
        return {"ok": False, "code": -1, "output": f"Mise à jour annulée: {e}"}
    finally:
        _dashboard_update_lock.release()

def docker_available():
    return run(["docker", "info"], timeout=8)["ok"]

def container_exists(name):
    r = run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=8)
    return r["ok"] and name in r["output"].splitlines()

def container_running(name):
    r = run(["docker", "ps", "--format", "{{.Names}}"], timeout=8)
    return r["ok"] and name in r["output"].splitlines()

def safe_host_path(rel):
    # User-facing paths are always relative to the project root.
    p = (PROJECT_ROOT / rel).resolve()
    if p != PROJECT_ROOT and PROJECT_ROOT not in p.parents:
        raise ValueError("Path outside project root")
    return p

def backup_text(label, content):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = PROJECT_ROOT / "config-backups" / f"dashboard-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in label)
    (d / safe).write_text(content, encoding="utf-8")
    return str(d)

def list_host_configs():
    result = []
    for name in [".env", "docker-compose.yml", "compose.yml", "docker-compose.override.yml", "compose.override.yml"]:
        p = PROJECT_ROOT / name
        if p.is_file():
            result.append({"source": "host", "path": name, "label": name})

    modules = PROJECT_ROOT / "modules"
    if modules.is_dir():
        for p in sorted(modules.glob("*/conf/*")):
            if p.is_file() and (p.name.endswith(".conf") or p.name.endswith(".conf.dist")):
                result.append({
                    "source": "host",
                    "path": str(p.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                    "label": str(p.relative_to(PROJECT_ROOT))
                })
    return result

def list_active_configs():
    if not docker_available() or not container_exists("ac-worldserver"):
        return []

    # docker cp works on stopped containers too.
    tmp = Path(tempfile.mkdtemp(prefix="ac-dashboard-"))
    try:
        dest = tmp / "modules"
        dest.mkdir()
        r = run(["docker", "cp", f"ac-worldserver:{ACTIVE_CONF_DIR}/.", str(dest)], timeout=20)
        if not r["ok"]:
            return []
        items = []
        for p in sorted(dest.iterdir()):
            if p.is_file() and (p.name.endswith(".conf") or p.name.endswith(".conf.dist")):
                items.append({"source": "container", "path": p.name, "label": f"ACTIVE: {p.name}"})
        return items
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def read_active_config(name):
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise ValueError("Invalid config name")
    tmp = Path(tempfile.mkdtemp(prefix="ac-dashboard-"))
    try:
        f = tmp / name
        r = run(["docker", "cp", f"ac-worldserver:{ACTIVE_CONF_DIR}/{name}", str(f)], timeout=15)
        if not r["ok"] or not f.exists():
            raise FileNotFoundError(r["output"])
        return f.read_text(encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def write_active_config(name, content):
    old = read_active_config(name)
    backup_text(f"ACTIVE-{name}", old)
    tmp = Path(tempfile.mkdtemp(prefix="ac-dashboard-"))
    try:
        f = tmp / name
        f.write_text(content, encoding="utf-8")
        r = run(["docker", "cp", str(f), f"ac-worldserver:{ACTIVE_CONF_DIR}/{name}"], timeout=20)
        if not r["ok"]:
            raise RuntimeError(r["output"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _new_console_flags():
    return getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

def launch_powershell_file(relative_path, title="AzerothCore"):
    script = safe_host_path(relative_path)
    if not script.is_file():
        return {"ok": False, "code": -1, "output": f"Script introuvable: {script}"}

    # IMPORTANT: pass -File and the script path as SEPARATE argv items.
    # Do not embed quotes in a cmd.exe command string.
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoExit",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", str(script),
        ],
        cwd=str(PROJECT_ROOT),
        creationflags=_new_console_flags()
    )
    return {"ok": True, "code": 0, "output": f"Lancé: {script}"}

def launch_powershell_command(command, title="AzerothCore"):
    # Used for simple commands such as live Docker logs.
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoExit",
            "-NoProfile",
            "-Command", command,
        ],
        cwd=str(PROJECT_ROOT),
        creationflags=_new_console_flags()
    )
    return {"ok": True, "code": 0, "output": f"Lancé: {command}"}

def open_path(path):
    p = safe_host_path(path)
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer.exe", str(p)])
    return {"ok": True, "code": 0, "output": str(p)}

ACTIONS = {
    "start": lambda: run(["docker", "compose", "up", "-d"], timeout=90),
    "stop": lambda: run(["docker", "compose", "stop"], timeout=90),
    "restart_world": lambda: run(["docker", "compose", "restart", "ac-worldserver"], timeout=90),
    "restart_all": lambda: run(["docker", "compose", "restart"], timeout=90),
    "recreate_world": lambda: run(["docker", "compose", "up", "-d", "--force-recreate", "ac-worldserver"], timeout=120),
    "validate_compose": lambda: run(["docker", "compose", "config"], timeout=45),
    "patch_gather": lambda: launch_powershell_file(
        r"modules\mod-server-customization\apply-playerbots-gather-only.ps1",
        "Patch gather"
    ),
    "patch_selfbot": lambda: launch_powershell_file(
        r"modules\mod-server-customization\apply-playerbots-selfbot-lock.ps1",
        "Patch selfbot"
    ),
    "rebuild": lambda: launch_powershell_file(
        r"rebuild-azerothcore.ps1",
        "AzerothCore rebuild"
    ),
    "patch_rebuild": lambda: launch_powershell_file(
        r"patch-and-rebuild.ps1",
        "Patch + rebuild"
    ),
    "logs_live": lambda: launch_powershell_command(
        "docker compose logs -f ac-worldserver",
        "Worldserver logs"
    ),
    "open_root": lambda: open_path("."),
    "open_modules": lambda: open_path("modules"),
    "open_config_backups": lambda: open_path("config-backups"),
    "open_db_backups": lambda: open_path("db-backups"),
}


def env_name_from_ac_key(key):
    # AzerothCore Docker mapping:
    # AC_ prefix, dots -> underscores, camelCase/PascalCase boundary -> underscore, uppercase.
    # Example MaxPrimaryTradeSkill -> AC_MAX_PRIMARY_TRADE_SKILL
    s = key.replace(".", "_")
    s = __import__("re").sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = __import__("re").sub(r"__+", "_", s)
    return "AC_" + s.upper()

def parse_conf_options(text):
    options = []
    pending_comments = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if pending_comments and pending_comments[-1] != "":
                pending_comments.append("")
            continue
        if line.startswith("#") or line.startswith(";"):
            cleaned = line[1:].strip()
            if cleaned:
                pending_comments.append(cleaned)
            continue
        m = __import__("re").match(r"^([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$", raw)
        if not m:
            pending_comments = []
            continue
        key, default = m.group(1), m.group(2)
        desc = " ".join(x for x in pending_comments[-8:] if x)
        options.append({
            "key": key,
            "default": default,
            "env": env_name_from_ac_key(key),
            "description": desc[:1200],
        })
        pending_comments = []
    return options

def read_worldserver_dist():
    candidates = [
        PROJECT_ROOT / "env" / "dist" / "etc" / "worldserver.conf.dist",
        PROJECT_ROOT / "conf" / "dist" / "worldserver.conf.dist",
        PROJECT_ROOT / "src" / "server" / "worldserver" / "worldserver.conf.dist",
    ]
    for p in candidates:
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace"), str(p)

    if docker_available() and container_exists("ac-worldserver"):
        tmp = Path(tempfile.mkdtemp(prefix="ac-dashboard-worldconf-"))
        try:
            f = tmp / "worldserver.conf.dist"
            for container_path in [
                "/azerothcore/env/dist/etc/worldserver.conf.dist",
                "/azerothcore/conf/dist/worldserver.conf.dist",
            ]:
                r = run(["docker", "cp", f"ac-worldserver:{container_path}", str(f)], timeout=15)
                if r["ok"] and f.exists():
                    return f.read_text(encoding="utf-8", errors="replace"), f"ac-worldserver:{container_path}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    raise FileNotFoundError("worldserver.conf.dist introuvable dans le repo ou le conteneur.")

def _leading_spaces(s):
    return len(s) - len(s.lstrip(" "))

def add_or_update_override_env(env_name, value, original_key):
    override = PROJECT_ROOT / "docker-compose.override.yml"
    if override.exists():
        old = override.read_text(encoding="utf-8", errors="replace")
    else:
        old = ""

    backup_text("docker-compose.override.yml", old)

    # Quote as a YAML string and escape embedded quotes/backslashes.
    safe_value = str(value).replace("\\", "\\\\").replace('"', '\\"')
    setting_line = f'      {env_name}: "{safe_value}" # {original_key}'

    lines = old.splitlines()
    found_existing = False
    env_re = __import__("re").compile(rf"^(\s*){__import__('re').escape(env_name)}\s*:")
    for i, line in enumerate(lines):
        if env_re.match(line):
            indent = env_re.match(line).group(1)
            lines[i] = f'{indent}{env_name}: "{safe_value}" # {original_key}'
            found_existing = True
            break

    if not found_existing:
        # Find services -> ac-worldserver -> environment mapping.
        world_i = env_i = None
        world_indent = None
        for i, line in enumerate(lines):
            if __import__("re").match(r"^\s*ac-worldserver\s*:\s*$", line):
                world_i = i
                world_indent = _leading_spaces(line)
                break

        if world_i is not None:
            for j in range(world_i + 1, len(lines)):
                stripped = lines[j].strip()
                indent = _leading_spaces(lines[j])
                if stripped and indent <= world_indent:
                    break
                if __import__("re").match(r"^\s*environment\s*:\s*$", lines[j]):
                    env_i = j
                    env_indent = indent
                    break

        if env_i is not None:
            # Reject list-style environment blocks; our project uses mapping style.
            for j in range(env_i + 1, len(lines)):
                stripped = lines[j].strip()
                indent = _leading_spaces(lines[j])
                if stripped and indent <= env_indent:
                    break
                if stripped.startswith("- "):
                    raise RuntimeError("Le bloc environment de ac-worldserver est en syntaxe liste. Ajout automatique annulé pour éviter d'endommager le YAML.")

            insert_at = env_i + 1
            while insert_at < len(lines):
                stripped = lines[insert_at].strip()
                indent = _leading_spaces(lines[insert_at])
                if stripped and indent <= env_indent:
                    break
                insert_at += 1
            lines.insert(insert_at, setting_line)
        elif world_i is not None:
            insert_at = world_i + 1
            child_indent = " " * (world_indent + 2)
            lines[insert_at:insert_at] = [
                f"{child_indent}environment:",
                setting_line
            ]
        else:
            prefix = []
            if old.strip():
                prefix = lines + [""]
            lines = prefix + [
                "services:",
                "  ac-worldserver:",
                "    environment:",
                setting_line,
            ]

    new_text = "\n".join(lines).rstrip() + "\n"
    override.write_text(new_text, encoding="utf-8")

    # Validate. If invalid, restore exact previous content.
    validate = run(["docker", "compose", "config"], timeout=45)
    if not validate["ok"]:
        override.write_text(old, encoding="utf-8")
        raise RuntimeError("docker compose config a échoué; le fichier précédent a été restauré.\n\n" + validate["output"])

    return {
        "ok": True,
        "output": f"{env_name} ajouté/mis à jour dans docker-compose.override.yml.\n\n{validate['output'][-2500:]}",
        "env": env_name,
        "key": original_key,
        "value": value,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AzerothCoreDashboard/0.1"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, p):
        p = Path(p)
        if not p.exists() or not p.is_file():
            self.send_error(404)
            return
        data = p.read_bytes()
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body_json(self):
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        if u.path == "/":
            return self.send_file(PROJECT_ROOT / "dashboard.html")
        if u.path == "/docs/commands":
            return self.send_file(PROJECT_ROOT / "azerothcore-commandes-joueur.html")
        if u.path == "/docs/maintenance":
            return self.send_file(PROJECT_ROOT / "azerothcore-maintenance-guide.html")

        if u.path == "/api/status":
            docker = docker_available()
            raw = run(["docker", "compose", "ps"], timeout=15) if docker else {"ok": False, "output": "Docker unavailable", "code": -1}
            services = {}
            for name in ["ac-database", "ac-authserver", "ac-worldserver"]:
                services[name] = {
                    "exists": container_exists(name) if docker else False,
                    "running": container_running(name) if docker else False,
                }
            return self.send_json({"docker": docker, "services": services, "raw": raw["output"]})

        if u.path == "/api/updates":
            try:
                branch = q.get("branch", [GITHUB_BRANCH])[0]
                return self.send_json({
                    "ok": True,
                    "dashboard": dashboard_update_status(branch),
                    "modules": module_update_statuses(),
                })
            except Exception as e:
                return self.send_json({"ok": False, "output": f"Vérification impossible: {e}"}, 502)

        if u.path == "/api/dashboard/branches":
            try:
                state = _dashboard_update_state()
                return self.send_json({"ok": True, "branches": dashboard_branches(),
                                       "main": GITHUB_BRANCH, "installed": state["branch"]})
            except Exception as e:
                return self.send_json({"ok": False, "output": f"Branches indisponibles: {e}"}, 502)

        if u.path == "/api/modules/catalogue":
            try:
                modules = catalogue_modules(force=q.get("refresh", ["0"])[0] == "1")
                return self.send_json({"ok": True, "source": CATALOGUE_PAGE, "modules": modules})
            except Exception as e:
                return self.send_json({"ok": False, "output": f"Catalogue indisponible: {e}"}, 502)

        if u.path == "/api/modules/removal-plan":
            try:
                return self.send_json({"ok": True, "plan": module_removal_plan(q.get("name", [""])[0])})
            except Exception as e:
                return self.send_json({"ok": False, "output": str(e)}, 400)

        if u.path == "/api/logs":
            tail = max(20, min(1000, int(q.get("tail", ["200"])[0])))
            r = run(["docker", "compose", "logs", f"--tail={tail}", "ac-worldserver"], timeout=20)
            return self.send_json(r)

        if u.path == "/api/configs":
            return self.send_json({"host": list_host_configs(), "active": list_active_configs()})

        if u.path == "/api/config":
            source = q.get("source", ["host"])[0]
            path = q.get("path", [""])[0]
            try:
                if source == "container":
                    content = read_active_config(path)
                else:
                    p = safe_host_path(path)
                    content = p.read_text(encoding="utf-8", errors="replace")
                return self.send_json({"ok": True, "source": source, "path": path, "content": content})
            except Exception as e:
                return self.send_json({"ok": False, "output": str(e)}, 400)

        if u.path == "/api/ac_options":
            try:
                text, source = read_worldserver_dist()
                opts = parse_conf_options(text)
                return self.send_json({"ok": True, "source": source, "count": len(opts), "options": opts})
            except Exception as e:
                return self.send_json({"ok": False, "output": str(e)}, 400)

        if u.path == "/api/override":
            try:
                p = PROJECT_ROOT / "docker-compose.override.yml"
                content = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
                return self.send_json({"ok": True, "content": content})
            except Exception as e:
                return self.send_json({"ok": False, "output": str(e)}, 400)

        self.send_error(404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            data = self.body_json()
        except Exception as e:
            return self.send_json({"ok": False, "output": f"Invalid JSON: {e}"}, 400)

        if u.path == "/api/action":
            name = data.get("name", "")
            fn = ACTIONS.get(name)
            if not fn:
                return self.send_json({"ok": False, "output": "Unknown action"}, 400)
            try:
                return self.send_json(fn())
            except Exception as e:
                return self.send_json({"ok": False, "output": str(e)}, 500)

        if u.path == "/api/dashboard/update":
            if data.get("confirmed") is not True:
                return self.send_json({"ok": False, "output": "Confirmation explicite manquante."}, 400)
            result = install_dashboard_update(str(data.get("branch", GITHUB_BRANCH)))
            return self.send_json(result, 200 if result["ok"] else 500)

        if u.path == "/api/modules/install":
            if data.get("confirmed") is not True:
                return self.send_json({"ok": False, "output": "Confirmation explicite manquante."}, 400)
            full_name = str(data.get("full_name", "")).strip()
            result = install_catalogue_module(full_name)
            return self.send_json(result, 200 if result["ok"] else 400)

        if u.path == "/api/modules/update":
            if data.get("confirmed") is not True:
                return self.send_json({"ok": False, "output": "Confirmation explicite manquante."}, 400)
            result = update_local_module(str(data.get("name", "")).strip())
            return self.send_json(result, 200 if result["ok"] else 400)

        if u.path == "/api/modules/remove":
            name = str(data.get("name", "")).strip()
            result = remove_local_module(
                name,
                remove_configs=data.get("remove_configs") is True,
                remove_sql=data.get("remove_sql") is True,
                inferred_sql_ids=data.get("inferred_sql_ids"),
                sql_confirmation=str(data.get("sql_confirmation", "")),
                confirmation=str(data.get("confirmation", "")),
            )
            return self.send_json(result, 200 if result["ok"] else 400)

        if u.path == "/api/config":
            source = data.get("source", "host")
            path = data.get("path", "")
            content = data.get("content", "")
            try:
                if source == "container":
                    write_active_config(path, content)
                else:
                    p = safe_host_path(path)
                    if not p.exists():
                        raise FileNotFoundError(path)
                    old = p.read_text(encoding="utf-8", errors="replace")
                    backup_text(str(p.relative_to(PROJECT_ROOT)), old)
                    p.write_text(content, encoding="utf-8")
                return self.send_json({"ok": True, "output": "Saved with automatic backup."})
            except Exception as e:
                return self.send_json({"ok": False, "output": str(e)}, 400)

        if u.path == "/api/override/add":
            key = str(data.get("key", "")).strip()
            value = str(data.get("value", "")).strip()
            if not key:
                return self.send_json({"ok": False, "output": "Option AzerothCore manquante."}, 400)
            try:
                expected_env = env_name_from_ac_key(key)
                supplied = str(data.get("env", expected_env)).strip()
                if supplied != expected_env:
                    return self.send_json({"ok": False, "output": f"Nom env inattendu. Attendu: {expected_env}"}, 400)
                return self.send_json(add_or_update_override_env(expected_env, value, key))
            except Exception as e:
                return self.send_json({"ok": False, "output": str(e)}, 400)

        self.send_error(404)

def main():
    if not PROJECT_ROOT.exists():
        print(f"Project folder not found: {PROJECT_ROOT}")
        input("Press Enter...")
        return
    print(f"AzerothCore dashboard: http://{HOST}:{PORT}/")
    print("Local-only server. Ctrl+C to stop.")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

if __name__ == "__main__":
    main()
