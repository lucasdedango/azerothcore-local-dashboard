param(
    [string]$RepoRoot = "C:\azerothcore-playerbots"
)

$ErrorActionPreference = "Stop"
Set-Location $RepoRoot

function Ask-YesNo([string]$Prompt, [bool]$Default = $true) {
    $suffix = if ($Default) { "[Y/n]" } else { "[y/N]" }
    $answer = Read-Host "$Prompt $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim().ToLower().StartsWith("y")
}

function Test-DockerDaemon {
    docker info *> $null
    return ($LASTEXITCODE -eq 0)
}

function Ensure-DockerDaemon {
    if (Test-DockerDaemon) {
        return
    }

    Write-Warning "Docker Desktop / Docker Engine is not running."

    $dockerDesktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"

    if (Test-Path $dockerDesktop) {
        if (Ask-YesNo "Start Docker Desktop now?" $true) {
            Write-Host "Starting Docker Desktop..." -ForegroundColor Cyan
            Start-Process -FilePath $dockerDesktop | Out-Null

            $deadline = (Get-Date).AddSeconds(120)
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Seconds 3
                if (Test-DockerDaemon) {
                    Write-Host "Docker Engine is ready." -ForegroundColor Green
                    return
                }
            }

            throw "Docker Desktop was started but the Docker Engine did not become ready within 120 seconds."
        }
    }

    throw "Docker Engine is unavailable. Start Docker Desktop, then run this script again."
}

function Test-ContainerExists([string]$Name) {
    $names = @(docker ps -a --format "{{.Names}}" 2>$null)
    return ($names -contains $Name)
}

function Test-ContainerRunning([string]$Name) {
    $names = @(docker ps --format "{{.Names}}" 2>$null)
    return ($names -contains $Name)
}

function Wait-ForMysql {
    param(
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        docker exec -e MYSQL_PWD=password ac-database mysqladmin ping -uroot --silent *> $null
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Get-ModuleConfigInfo {
    $items = @()
    $modulesRoot = Join-Path $RepoRoot "modules"
    if (!(Test-Path $modulesRoot)) { return $items }

    Get-ChildItem -Path $modulesRoot -Directory | Sort-Object Name | ForEach-Object {
        $module = $_
        $confDir = Join-Path $module.FullName "conf"
        if (!(Test-Path $confDir)) { return }

        Get-ChildItem -Path $confDir -Filter "*.conf.dist" -File -ErrorAction SilentlyContinue | ForEach-Object {
            $dist = $_
            $items += [pscustomobject]@{
                Module       = $module.Name
                DistName     = $dist.Name
                ActiveName   = ($dist.Name -replace '\.dist$','')
                HostDistPath = $dist.FullName
            }
        }
    }

    return $items
}

function Get-ConfigKeys {
    param([string[]]$Lines)

    $keys = @{}
    foreach ($line in $Lines) {
        # Ignore comments; keep normal "Key = value" settings.
        if ($line -match '^\s*([^#;][A-Za-z0-9_.-]*)\s*=') {
            $keys[$matches[1]] = $true
        }
    }
    return $keys
}

function Merge-MissingConfigSettings {
    param(
        [string]$ActivePath,
        [string]$DistPath
    )

    if (!(Test-Path $ActivePath) -or !(Test-Path $DistPath)) {
        return @()
    }

    $activeLines = @(Get-Content -LiteralPath $ActivePath)
    $distLines   = @(Get-Content -LiteralPath $DistPath)

    $activeKeys = Get-ConfigKeys -Lines $activeLines
    $missingLines = @()
    $missingKeys = @()

    foreach ($line in $distLines) {
        if ($line -match '^\s*([^#;][A-Za-z0-9_.-]*)\s*=') {
            $key = $matches[1]
            if (!$activeKeys.ContainsKey($key)) {
                $missingLines += $line
                $missingKeys += $key
                $activeKeys[$key] = $true
            }
        }
    }

    if ($missingLines.Count -gt 0) {
        Add-Content -LiteralPath $ActivePath -Value ""
        Add-Content -LiteralPath $ActivePath -Value "# -----------------------------------------------------------------------------"
        Add-Content -LiteralPath $ActivePath -Value "# Settings automatically added from the newer .conf.dist by rebuild helper"
        Add-Content -LiteralPath $ActivePath -Value "# Existing settings above were preserved and were NOT overwritten."
        Add-Content -LiteralPath $ActivePath -Value "# -----------------------------------------------------------------------------"
        foreach ($line in $missingLines) {
            Add-Content -LiteralPath $ActivePath -Value $line
        }
    }

    return @($missingKeys)
}

function Get-ModuleSqlFiles {
    $items = @()
    $modulesRoot = Join-Path $RepoRoot "modules"
    if (!(Test-Path $modulesRoot)) { return $items }

    Get-ChildItem -Path $modulesRoot -Directory | Sort-Object Name | ForEach-Object {
        $module = $_
        $sqlRoot = Join-Path $module.FullName "data\sql"
        if (!(Test-Path $sqlRoot)) { return }

        # Mimic AzerothCore's module updater:
        # it inspects each immediate data/sql/<db-dir> directory, and if its
        # directory name contains "world", "characters" or "auth", every .sql below
        # it is recursively treated as a MODULE update.
        Get-ChildItem -Path $sqlRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $dbDir = $_
            $dirName = $dbDir.Name.ToLowerInvariant()
            $database = $null
            $dbName = $null

            if ($dirName.Contains("characters")) {
                $database = "characters"
                $dbName = "acore_characters"
            }
            elseif ($dirName.Contains("world")) {
                $database = "world"
                $dbName = "acore_world"
            }
            elseif ($dirName.Contains("auth")) {
                $database = "auth"
                $dbName = "acore_auth"
            }

            if (!$database) { return }

            Get-ChildItem -Path $dbDir.FullName -Recurse -File -Filter "*.sql" -ErrorAction SilentlyContinue | ForEach-Object {
                $sqlFile = $_
                $items += [pscustomobject]@{
                    Module   = $module.Name
                    Database = $database
                    DbName   = $dbName
                    FileName = $sqlFile.Name
                    FullName = $sqlFile.FullName
                }
            }
        }
    }

    return $items
}

function Backup-AzerothDatabases {
    param([string]$BackupRoot)

    New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null

    $targets = @(
        @{ Db="acore_auth";       File="acore_auth.sql" },
        @{ Db="acore_characters"; File="acore_characters.sql" },
        @{ Db="acore_world";      File="acore_world.sql" },
        @{ Db="acore_playerbots"; File="acore_playerbots.sql" }
    )

    foreach ($t in $targets) {
        $db = $t.Db
        $file = Join-Path $BackupRoot $t.File
        Write-Host "Backing up $db ..." -ForegroundColor Cyan

        # cmd /c avoids PowerShell 5.1 text redirection mangling mysqldump output.
        $cmd = "docker exec -e MYSQL_PWD=password ac-database mysqldump -uroot --single-transaction --routines --triggers --events --set-gtid-purged=OFF $db > `"$file`""
        cmd /c $cmd

        if ($LASTEXITCODE -ne 0) {
            throw "Database backup failed for $db"
        }
    }

    Write-Host "Database backup complete:" -ForegroundColor Green
    Write-Host "  $BackupRoot"
}

function Show-DbUpdateLogSummary {
    Write-Host ""
    Write-Host "Database updater log summary:" -ForegroundColor Cyan

    $logs = docker compose logs --since=10m ac-worldserver 2>$null
    $matches = $logs | Select-String -Pattern "Applying update|Reapplying update|Re-hashing update|database is up-to-date|Applied .*quer|sql.updates|ERROR|FATAL|failed" -CaseSensitive:$false

    if ($matches) {
        $matches | Select-Object -Last 120
    }
    else {
        Write-Host "No obvious database-updater messages found in the last 10 minutes."
    }
}

function Wait-ForWorldserver {
    param([int]$TimeoutSeconds = 90)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $running = docker inspect -f "{{.State.Running}}" ac-worldserver 2>$null
        if ($LASTEXITCODE -eq 0 -and "$running".Trim() -eq "true") {
            # Database updates happen early in startup. Give them a little time,
            # but do not wait for hundreds of Playerbots to finish logging in.
            Start-Sleep -Seconds 5
            return $true
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Test-ModuleSqlUpdaterState {
    param([object[]]$SqlFiles)

    if (!$SqlFiles -or $SqlFiles.Count -eq 0) {
        return
    }

    Write-Host ""
    Write-Host "=== Module SQL updater verification ===" -ForegroundColor Cyan
    Write-Host "Checking each module SQL filename + SHA1 against the target DB 'updates' table."
    Write-Host ""

    $ok = 0
    $missing = 0
    $mismatch = 0
    $errors = 0

    # AzerothCore requires update filenames to be unique inside each DB.
    $duplicates = $SqlFiles | Group-Object DbName, FileName | Where-Object { $_.Count -gt 1 }
    if ($duplicates) {
        Write-Warning "Duplicate module SQL filenames detected. AzerothCore's updater also rejects duplicate filenames:"
        foreach ($d in $duplicates) {
            Write-Host ("  - {0} ({1} copies)" -f $d.Name, $d.Count) -ForegroundColor Yellow
        }
    }

    foreach ($s in ($SqlFiles | Sort-Object DbName, FileName, Module)) {
        $escapedName = $s.FileName.Replace("'", "''")
        # MYSQL_PWD avoids the mysql CLI password warning on stderr, which Windows PowerShell 5.1 can promote to NativeCommandError.
        $query = "SELECT CONCAT(``name``,'|',IFNULL(``hash``,''),'|',``state``) FROM ``updates`` WHERE ``name``='$escapedName' LIMIT 1;"

        $row = docker exec -e MYSQL_PWD=password ac-database mysql -N -B -uroot $s.DbName -e $query
        $mysqlExit = $LASTEXITCODE

        if ($mysqlExit -ne 0) {
            Write-Host ("[ERROR] {0} / {1} -> could not query {2}.updates" -f $s.Module, $s.FileName, $s.DbName) -ForegroundColor Red
            $errors++
            continue
        }

        if ([string]::IsNullOrWhiteSpace(($row -join ""))) {
            Write-Host ("[NOT TRACKED] {0} / {1} -> {2}" -f $s.Module, $s.FileName, $s.DbName) -ForegroundColor Yellow
            $missing++
            continue
        }

        $line = ($row | Select-Object -First 1).Trim()
        $parts = $line.Split("|")
        $dbHash = if ($parts.Count -ge 2) { $parts[1].Trim().ToUpperInvariant() } else { "" }
        $state  = if ($parts.Count -ge 3) { $parts[2].Trim() } else { "?" }

        try {
            $localHash = (Get-FileHash -LiteralPath $s.FullName -Algorithm SHA1).Hash.ToUpperInvariant()
        }
        catch {
            Write-Host ("[ERROR] {0} / {1} -> local SHA1 failed: {2}" -f $s.Module, $s.FileName, $_.Exception.Message) -ForegroundColor Red
            $errors++
            continue
        }

        if ([string]::IsNullOrWhiteSpace($dbHash)) {
            Write-Host ("[TRACKED, NO HASH] {0} / {1} -> state={2}" -f $s.Module, $s.FileName, $state) -ForegroundColor Yellow
            $mismatch++
        }
        elseif ($dbHash -eq $localHash) {
            Write-Host ("[OK] {0} / {1} -> {2}, state={3}, hash={4}" -f $s.Module, $s.FileName, $s.DbName, $state, $localHash.Substring(0,7)) -ForegroundColor Green
            $ok++
        }
        else {
            Write-Host ("[HASH MISMATCH] {0} / {1}" -f $s.Module, $s.FileName) -ForegroundColor Red
            Write-Host ("    DB:    {0}" -f $dbHash)
            Write-Host ("    FILE:  {0}" -f $localHash)
            $mismatch++
        }
    }

    Write-Host ""
    Write-Host ("SQL verification summary: OK={0}, not tracked={1}, hash mismatch/no-hash={2}, errors={3}" -f $ok, $missing, $mismatch, $errors)

    if ($missing -eq 0 -and $mismatch -eq 0 -and $errors -eq 0) {
        Write-Host "All detected module SQL files are recorded by AzerothCore with the current SHA1." -ForegroundColor Green
    }
    else {
        Write-Warning "One or more module SQL files are not confirmed current. Check the updater log above before doing anything manually."
    }
}

Write-Host ""
Write-Host "=== AzerothCore rebuild helper v3.2 ===" -ForegroundColor Cyan
Write-Host "Repo: $RepoRoot"
Write-Host ""

if (!(Test-Path (Join-Path $RepoRoot "docker-compose.yml")) -and !(Test-Path (Join-Path $RepoRoot "compose.yml"))) {
    Write-Warning "No compose file detected in $RepoRoot. Continuing anyway."
}

# Docker must be available even if every AzerothCore container is stopped.
Ensure-DockerDaemon

# 1) Detect containers
$worldExists  = Test-ContainerExists "ac-worldserver"
$dbExists     = Test-ContainerExists "ac-database"
$worldRunning = Test-ContainerRunning "ac-worldserver"
$dbRunning    = Test-ContainerRunning "ac-database"

# 2) Back up every active module config.
# docker cp works even when the container exists but is stopped.
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$configBackupDir = Join-Path $RepoRoot "config-backups\$stamp"
$hadBackup = @{}

if ($worldExists) {
    New-Item -ItemType Directory -Force -Path $configBackupDir | Out-Null

    Write-Host "Backing up active module configs from ac-worldserver..." -ForegroundColor Cyan
    docker cp "ac-worldserver:/azerothcore/env/dist/etc/modules/." $configBackupDir | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to copy module configs from ac-worldserver."
    }

    Get-ChildItem -Path $configBackupDir -Filter "*.conf" -File -ErrorAction SilentlyContinue | ForEach-Object {
        $hadBackup[$_.Name] = $true
    }

    if ($hadBackup.Count -gt 0) {
        Write-Host "Backed up active module configs to:" -ForegroundColor Green
        Write-Host "  $configBackupDir"
        if (!$worldRunning) {
            Write-Host "  (ac-worldserver was stopped; docker cp from the stopped container was used.)" -ForegroundColor DarkGray
        }
    }
    else {
        Write-Warning "The ac-worldserver container exists, but no active *.conf files were found in its module config directory."
    }
}
else {
    Write-Warning "ac-worldserver container does not exist yet, so there are no active module configs to preserve."
}

# 3) Detect current .conf.dist files and merge ONLY newly introduced settings
$configs = @(Get-ModuleConfigInfo)

Write-Host ""
Write-Host "Module config files detected:" -ForegroundColor Cyan

foreach ($c in $configs) {
    $backupPath = Join-Path $configBackupDir $c.ActiveName

    if ($hadBackup.ContainsKey($c.ActiveName) -and (Test-Path $backupPath)) {
        $added = @(Merge-MissingConfigSettings -ActivePath $backupPath -DistPath $c.HostDistPath)

        if ($added.Count -gt 0) {
            Write-Host ("- {0}: {1} -> added {2} missing setting(s)" -f $c.Module, $c.ActiveName, $added.Count) -ForegroundColor Green
            foreach ($key in $added) {
                Write-Host ("    + {0}" -f $key) -ForegroundColor DarkGreen
            }
        }
        else {
            Write-Host ("- {0}: {1} -> already contains every current setting" -f $c.Module, $c.ActiveName)
        }
    }
    else {
        Write-Host ("- {0}: {1} -> NEW/MISSING active config; will create from .conf.dist after build" -f $c.Module, $c.ActiveName) -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Config policy:" -ForegroundColor Cyan
Write-Host "- Existing values are preserved."
Write-Host "- Settings newly introduced by a module's .conf.dist are appended automatically."
Write-Host "- Missing active configs are created automatically from .conf.dist."

# 4) Detect module SQL exactly like the current AzerothCore module updater
$sqlFiles = @(Get-ModuleSqlFiles)
$hasSql = $sqlFiles.Count -gt 0

if ($hasSql) {
    Write-Host ""
    Write-Host "Module SQL detected:" -ForegroundColor Yellow
    $sqlFiles | Group-Object Module | Sort-Object Name | ForEach-Object {
        $g = $_.Group
        $world = @($g | Where-Object Database -eq "world").Count
        $chars = @($g | Where-Object Database -eq "characters").Count
        $auth  = @($g | Where-Object Database -eq "auth").Count
        Write-Host ("- {0}: world={1}, characters={2}, auth={3}, total={4}" -f $_.Name, $world, $chars, $auth, $g.Count)
    }

    Write-Host ""
    Write-Host "The script will NOT execute these SQL files manually."
    Write-Host "After startup it will verify AzerothCore's own 'updates' table and SHA1 for every module SQL file."

    if (!$dbRunning -and $dbExists) {
        if (Ask-YesNo "ac-database is stopped. Start it temporarily so a DB backup can be made?" $true) {
            docker start ac-database | Out-Host
            if ($LASTEXITCODE -ne 0) {
                throw "Could not start ac-database."
            }

            if (!(Wait-ForMysql -TimeoutSeconds 60)) {
                throw "ac-database started, but MySQL did not become ready within 60 seconds."
            }

            $dbRunning = $true
        }
    }

    if ($dbRunning) {
        if (Ask-YesNo "Create a full DB backup before rebuilding?" $true) {
            $dbBackupDir = Join-Path $RepoRoot "db-backups\$stamp"
            Backup-AzerothDatabases -BackupRoot $dbBackupDir
        }
    }
    elseif (!$dbExists) {
        Write-Warning "ac-database container does not exist yet, so an automatic DB backup cannot be made."
    }
    else {
        Write-Warning "ac-database is stopped and was not started, so the DB backup was skipped."
    }
}

# 5) Optional module helper/patch scripts
$patchScripts = @(Get-ChildItem -Path (Join-Path $RepoRoot "modules") -Recurse -File -Filter "*.ps1" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'apply|patch|setup|install' })

if ($patchScripts.Count -gt 0) {
    Write-Host ""
    Write-Host "Module helper/patch scripts detected:" -ForegroundColor Cyan

    foreach ($p in $patchScripts) {
        $rel = $p.FullName.Substring($RepoRoot.Length).TrimStart('\')

        if (Ask-YesNo "Run $rel ?" $false) {
            & powershell.exe -ExecutionPolicy Bypass -File $p.FullName -RepoRoot $RepoRoot

            if ($LASTEXITCODE -ne 0) {
                throw "Patch/helper failed: $rel"
            }
        }
    }
}

# 6) Stop worldserver only
if ($worldRunning -and (Ask-YesNo "Stop ac-worldserver before build?" $true)) {
    docker stop ac-worldserver | Out-Host
}

# 7) Build
Write-Host ""
Write-Host "Building ac-worldserver..." -ForegroundColor Cyan
Write-Host "(Containers may be stopped; only the Docker Engine itself must be running.)" -ForegroundColor DarkGray
docker compose --progress=plain build ac-worldserver
if ($LASTEXITCODE -ne 0) {
    throw "Build failed. Existing DB volumes were not touched."
}

# 8) Recreate worldserver
Write-Host ""
Write-Host "Recreating ac-worldserver..." -ForegroundColor Cyan
docker compose up -d --force-recreate ac-worldserver
if ($LASTEXITCODE -ne 0) {
    throw "Failed to recreate ac-worldserver."
}

# 9) Restore old configs, now enriched with any missing settings
if (Test-Path $configBackupDir) {
    Get-ChildItem -Path $configBackupDir -Filter "*.conf" -File | ForEach-Object {
        docker cp $_.FullName "ac-worldserver:/azerothcore/env/dist/etc/modules/$($_.Name)" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to restore config: $($_.Name)"
        }
        Write-Host "Restored/merged $($_.Name)" -ForegroundColor Green
    }
}

# 10) Automatically create any active config that did not exist before
foreach ($c in $configs) {
    if ($hadBackup.ContainsKey($c.ActiveName)) {
        continue
    }

    $cmd = "if [ -f '/azerothcore/env/dist/etc/modules/$($c.DistName)' ] && [ ! -f '/azerothcore/env/dist/etc/modules/$($c.ActiveName)' ]; then cp '/azerothcore/env/dist/etc/modules/$($c.DistName)' '/azerothcore/env/dist/etc/modules/$($c.ActiveName)'; fi"
    docker exec ac-worldserver sh -c $cmd

    if ($LASTEXITCODE -eq 0) {
        Write-Host "Created $($c.ActiveName) from $($c.DistName)" -ForegroundColor Green
    }
    else {
        Write-Warning "Could not create $($c.ActiveName)"
    }
}

# 11) Restart so restored/new configs are loaded
Write-Host ""
Write-Host "Restarting worldserver to load restored/merged configs..." -ForegroundColor Cyan
docker restart ac-worldserver | Out-Host

if (!(Wait-ForWorldserver -TimeoutSeconds 90)) {
    Write-Warning "worldserver did not reach a running state within the timeout."
}

Write-Host ""
Write-Host "Containers:" -ForegroundColor Cyan
docker compose ps

# 12) Show updater output and verify SQL tracking/hash
if ($hasSql) {
    Show-DbUpdateLogSummary
    Test-ModuleSqlUpdaterState -SqlFiles $sqlFiles
}

# 13) Optional logs
if (Ask-YesNo "Show the last 80 worldserver log lines?" $true) {
    docker compose logs --tail=80 ac-worldserver
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "- No Docker volumes were deleted or recreated."
Write-Host "- Existing module config VALUES were preserved."
Write-Host "- Missing config KEYS from newer .conf.dist files were appended automatically."
Write-Host "- Missing active configs were created from their .conf.dist."
if ($hasSql) {
    Write-Host "- Module SQL was left to AzerothCore's updater, then checked against DB updates.name + SHA1."
}
