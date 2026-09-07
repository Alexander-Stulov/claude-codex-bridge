<#
.SYNOPSIS
    Raise the codex context window for GPT-6 Astra and GPT-5.6 models to 1.05M (Windows).

.DESCRIPTION
    Why a script and not a config line: codex clamps the documented override.

        model_context_window = 1050000      ->  min(1050000, max_context_window)

    and the shipped catalog caps every one of these models below that, so the
    override cannot reach 1,050,000. Out of the box they all run at a 272000 window
    (258400 after codex's 95% headroom) - gpt-6-astra included, despite being the
    flagship. The cap itself lives in the model catalog, and the only supported way
    to change it is to hand codex a whole catalog via `model_catalog_json`. That is
    what this does: it has codex fetch its catalog afresh, copies that, raises the
    cap on every model the bridge dispatches to the 1,050,000 their upstream API
    actually supports, and points config.toml at the copy.

    Run once. Re-run after a codex upgrade, so the copy picks up new models.
    Same behaviour as enable-1m-context.sh; this edition needs only PowerShell
    (Windows PowerShell 5.1, or PowerShell 7.2 or newer) and codex.

.PARAMETER Revert
    Undo: remove model_catalog_json from config.toml and delete the raised catalog.

.PARAMETER NoVerify
    Skip the live check (no API call).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\enable-1m-context.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\enable-1m-context.ps1 -Revert
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\enable-1m-context.ps1 -NoVerify
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Revert,
    [switch]$NoVerify
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$codexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }
$cache   = Join-Path $codexHome "models_cache.json"
$catalog = Join-Path $codexHome "catalog-1m.json"
$config  = Join-Path $codexHome "config.toml"
$targetWindow = 1050000
# Every wire model this bridge can dispatch that supports the 1.05M upstream window.
# A prefix list, not one family: gpt-6-astra ships capped exactly like the GPT-5.6
# seats, so gating on "gpt-5.6-" alone would leave the flagship silently at 258400.
$raise = @("gpt-6-astra", "gpt-5.6-")
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)   # a BOM would break codex's parsers
$script:liftedConfig = $null     # config.toml's bytes while the override is lifted, until it is re-set

function Restore-LiftedConfig {
    if ($null -ne $script:liftedConfig) {
        [System.IO.File]::WriteAllBytes($config, $script:liftedConfig)
        $script:liftedConfig = $null
    }
}

function Fail([string]$msg) {
    Restore-LiftedConfig
    [Console]::Error.WriteLine($msg)
    exit 1
}
trap { Restore-LiftedConfig; break }      # an unexpected error must not leave the override lifted

function Find-Codex {
    # Returns @{ File = <what Start-Process runs>; Shim = <a .cmd/.bat to run through it, or $null> }.
    # PowerShell resolves a bare "codex" to a codex.ps1 shim ahead of codex.exe when an
    # npm install left both, and Start-Process with redirected streams needs a real
    # executable, so prefer .exe, run .cmd/.bat through cmd.exe, and skip .ps1 shims entirely.
    $apps = @(Get-Command codex -CommandType Application -All -ErrorAction SilentlyContinue)
    if ($env:LOCALAPPDATA) {
        # The installer's fixed location, for a shell whose PATH predates the install.
        $default = Join-Path $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin\codex.exe"
        if (Test-Path -LiteralPath $default) { $apps += @([pscustomobject]@{ Source = $default }) }
    }
    foreach ($ext in @(".exe", "")) {
        foreach ($a in $apps) {
            if ([System.IO.Path]::GetExtension($a.Source).ToLowerInvariant() -eq $ext) {
                return @{ File = $a.Source; Shim = $null }
            }
        }
    }
    foreach ($a in $apps) {
        $ext = [System.IO.Path]::GetExtension($a.Source).ToLowerInvariant()
        if ($ext -eq ".cmd" -or $ext -eq ".bat") {
            return @{ File = "$env:ComSpec"; Shim = $a.Source }
        }
    }
    return $null
}

function New-TempDir {
    $d = Join-Path ([System.IO.Path]::GetTempPath()) ("codex-1m-" + [System.IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    return $d
}

function Invoke-Codex([string[]]$arguments, [string]$dir) {
    # Start-Process rather than a bare call: codex exec reads a non-terminal stdin and
    # appends it to the prompt, so it must get an empty, closed stdin or it waits
    # forever from any script. Files for stdout/stderr also keep codex's log lines out
    # of PowerShell's error stream.
    $stdinFile = Join-Path $dir "stdin"; $stdoutFile = Join-Path $dir "stdout"; $stderrFile = Join-Path $dir "stderr"
    [System.IO.File]::WriteAllText($stdinFile, "")
    if ($codex.Shim) {
        # cmd.exe /C strips the first and last quote of its command line once the line
        # holds more than one quoted token, so a quoted shim path plus the quoted prompt
        # came apart into `...codex.cmd" exec ... "Reply` (not recognized). /S makes that
        # stripping unconditional, and one more pair of quotes around the whole line is
        # exactly what it strips.
        $argv = @("/d", "/s", "/c", ('""' + $codex.Shim + '" ' + ($arguments -join " ") + '"'))
    } else {
        $argv = $arguments
    }
    $proc = Start-Process -FilePath $codex.File -ArgumentList $argv -WorkingDirectory $dir `
        -RedirectStandardInput $stdinFile -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile `
        -NoNewWindow -Wait -PassThru
    return @{
        Exit = $proc.ExitCode
        Out  = @([System.IO.File]::ReadAllLines($stdoutFile)) + @([System.IO.File]::ReadAllLines($stderrFile))
        Err  = @([System.IO.File]::ReadAllLines($stderrFile))
    }
}

function Get-Prop($obj, [string]$name) {          # strict mode throws on a missing property
    $p = $obj.PSObject.Properties[$name]
    if ($null -ne $p) { return $p.Value }
    return $null
}

# --- edit config.toml -------------------------------------------------------
# A bare key must appear before the first [table] in TOML, so this inserts at the
# top rather than appending. Always writes through a backup. The path goes in with
# forward slashes: inside a TOML basic string a backslash starts an escape, so
# "C:\Users\..." is not valid TOML - and Windows accepts either separator.
function Set-CatalogConfig([string]$value) {      # empty value removes the key
    $remove = [string]::IsNullOrEmpty($value)
    $lines = @()
    $newline = "`n"
    if (Test-Path -LiteralPath $config) {
        $text = [System.IO.File]::ReadAllText($config)
        if ($text.Contains("`r`n")) { $newline = "`r`n" }
        if ($text.Length -gt 0) { $lines = $text -split "`r?`n" }
        Copy-Item -LiteralPath $config -Destination "$config.bak" -Force
    }
    $entry = 'model_catalog_json = "' + $value.Replace('\', '/') + '"'
    $kept = New-Object System.Collections.Generic.List[string]
    $replaced = $false
    foreach ($line in $lines) {
        if ($line.TrimStart().StartsWith("model_catalog_json")) {
            if (-not $remove -and -not $replaced) { $kept.Add($entry); $replaced = $true }
            continue                                   # drop any stale duplicates
        }
        $kept.Add($line)
    }
    if (-not $remove -and -not $replaced) {
        # before the first [table] header, or at the very top
        $at = 0
        for ($i = 0; $i -lt $kept.Count; $i++) {
            if ($kept[$i].TrimStart().StartsWith("[")) { $at = $i; break }
        }
        $kept.Insert($at, $entry)
        if ($at -eq 0 -and $kept.Count -gt 1) { $kept.Insert(1, "") }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $config) | Out-Null
    $body = $kept -join $newline
    if (-not $body.EndsWith($newline)) { $body += $newline }
    [System.IO.File]::WriteAllText($config, $body, $utf8NoBom)
}

if ($Revert) {
    Set-CatalogConfig ""
    Remove-Item -LiteralPath $catalog -Force -ErrorAction SilentlyContinue
    Write-Host "reverted: model_catalog_json removed from $config (backup at $config.bak)"
    Write-Host "codex is back to its stock catalog - 272000 cap, 258400 effective."
    exit 0
}

$codex = Find-Codex
if (-not $codex) { Fail "the codex CLI is not on PATH (a codex.ps1 shim alone is not enough - install codex natively)" }

# --- refresh the model cache ------------------------------------------------
# codex fetches /models only while model_catalog_json is unset. With the override
# installed it serves the catalog file and never touches models_cache.json, so the
# cache stays frozen at whatever it held the day the override went in, and a re-run
# after a codex upgrade would copy that stale snapshot and never see a model that
# appeared since (how gpt-6-astra stayed at 258400 while the check kept passing on
# gpt-5.6-sol). So lift the override for one `codex debug models` - a catalog
# fetch, no model turn - and put it back. Logged out, codex prints its bundled
# catalog instead and writes nothing, which is why the copy is taken from the cache
# a real fetch writes and never from that output.
if (Test-Path -LiteralPath $config) {
    $hasOverride = $false
    foreach ($line in ([System.IO.File]::ReadAllText($config) -split "`r?`n")) {
        if ($line.TrimStart().StartsWith("model_catalog_json")) { $hasOverride = $true; break }
    }
    if ($hasOverride) {
        $script:liftedConfig = [System.IO.File]::ReadAllBytes($config)
        Set-CatalogConfig ""
    }
}
$refreshDir = New-TempDir
try {
    $refresh = Invoke-Codex @("debug", "models") $refreshDir
} finally {
    Remove-Item -LiteralPath $refreshDir -Recurse -Force -ErrorAction SilentlyContinue
}
if ($refresh.Exit -ne 0) {
    [Console]::Error.WriteLine("WARNING: 'codex debug models' failed, so the model cache could not be refreshed:")
    foreach ($line in ($refresh.Err | Select-Object -Last 2)) { [Console]::Error.WriteLine($line) }
}

if (-not (Test-Path -LiteralPath $cache)) {
    Fail "no model cache at $cache`ncodex did not fetch its catalog - is it logged in (codex login) and online? Fix that and re-run."
}

# --- build the catalog ------------------------------------------------------
# Sourced from the cache codex itself wrote, so the shape always matches the
# installed CLI. Building it from a checked-out models.json does not: the field
# set drifts between versions and codex rejects the file outright.
#
# Not ConvertFrom-Json/ConvertTo-Json: those turn date-looking strings into
# DateTime and write them back in a different shape, and the catalog carries such
# strings. Both parsers below round-trip untouched values byte-for-byte in meaning.
$text = [System.IO.File]::ReadAllText($cache)
$psVersion = $PSVersionTable.PSVersion
$isCore = $psVersion.Major -ge 6
if ($isCore -and $psVersion -lt [version]"7.2") {
    Fail "PowerShell $psVersion has neither JSON parser this script can use; run it with Windows PowerShell 5.1 (powershell.exe) or PowerShell 7.2 or newer."
}
$models = @()
if ($isCore) {
    # PowerShell 7.2+: System.Text.Json's mutable DOM (JsonNode arrived with .NET 6).
    $root = [System.Text.Json.Nodes.JsonNode]::Parse($text)
    $modelsNode = $root["models"]
    if ($null -ne $modelsNode) { $models = @($modelsNode) }
} else {
    # Windows PowerShell 5.1: the .NET Framework serializer; no date munging either.
    Add-Type -AssemblyName System.Web.Extensions
    $ser = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $ser.MaxJsonLength = [int]::MaxValue
    $ser.RecursionLimit = 100
    $root = $ser.DeserializeObject($text)
    if ($root.ContainsKey("models") -and $null -ne $root["models"]) { $models = @($root["models"]) }
}
if ($models.Count -eq 0) { Fail "$cache contains no models - cannot build a catalog from it" }

function Get-Field($m, [string]$name) {
    if ($isCore) { $v = $m[$name]; if ($null -eq $v) { return $null }; return $v.ToString() }
    if ($m.ContainsKey($name)) { return $m[$name] }
    return $null
}
function Set-Field($m, [string]$name, [int]$value) {
    if ($isCore) { $m[$name] = [System.Text.Json.Nodes.JsonValue]::Create($value) } else { $m[$name] = $value }
}

$fetched = [string](Get-Field $root "fetched_at"); if (-not $fetched) { $fetched = "?" }
$clientVersion = [string](Get-Field $root "client_version"); if (-not $clientVersion) { $clientVersion = "?" }
Write-Host "cache: $cache  (fetched $fetched by codex $clientVersion)"
# A fetch that just happened stamps the cache with now; codex also keeps a cache under
# five minutes old as-is. Older than that means the refresh above did not happen.
$age = $null
try {
    $styles = [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal
    $stamp = [DateTime]::ParseExact($fetched.Substring(0, 19), "yyyy-MM-dd'T'HH:mm:ss",
                                    [System.Globalization.CultureInfo]::InvariantCulture, $styles)
    $age = [DateTime]::UtcNow - $stamp
} catch { $age = $null }
if ($null -eq $age -or $age -gt [TimeSpan]::FromMinutes(10)) {
    [Console]::Error.WriteLine("WARNING: codex did not refresh its model cache (not logged in, offline, or a CLI without 'debug models'); building from the copy it fetched $fetched. Models that appeared since are not in it - fix the cause and re-run to pick them up.")
}

$raised = New-Object System.Collections.Generic.List[object]
foreach ($m in $models) {
    $slug = [string](Get-Field $m "slug")
    $hit = $false
    foreach ($prefix in $raise) { if ($slug.StartsWith($prefix)) { $hit = $true; break } }
    if (-not $hit) { continue }
    $before = Get-Field $m "context_window"
    Set-Field $m "context_window" $targetWindow
    Set-Field $m "max_context_window" $targetWindow
    $raised.Add(@{ slug = $slug; before = $before })
}
$slugs = @($raised | ForEach-Object { $_.slug })
foreach ($prefix in $raise) {
    $hit = $false
    foreach ($s in $slugs) { if ($s.StartsWith($prefix)) { $hit = $true; break } }
    if (-not $hit) {
        [Console]::Error.WriteLine("WARNING: no ${prefix}* model in the catalog codex fetched, so it stays at the stock 258400 (a CLI too old to list it, or an account without access to it).")
    }
}
if ($raised.Count -eq 0) { Fail "no $($raise -join ' or ')* models in the catalog - nothing to raise" }
if ($isCore) {
    # Keep only "models": prune the cache's own bookkeeping keys from the parsed root
    # and serialize that (a fresh JsonObject needs a constructor PowerShell cannot bind).
    $extra = @()
    foreach ($kv in $root) { if ($kv.Key -ne "models") { $extra += $kv.Key } }
    foreach ($k in $extra) { $root.Remove($k) | Out-Null }
    $opts = New-Object System.Text.Json.JsonSerializerOptions
    $opts.WriteIndented = $true
    $json = $root.ToJsonString($opts)
} else {
    $wrap = New-Object 'System.Collections.Generic.Dictionary[string,object]'
    $wrap["models"] = $models
    $json = $ser.Serialize($wrap)
}
[System.IO.File]::WriteAllText($catalog, $json, $utf8NoBom)
# Verify against a model that was actually raised, preferring the one callers reach
# for by default. Hard-coding a family here is how the check goes on passing while
# the model people actually use stays capped.
$verifyModel = $null
foreach ($prefix in $raise) { if ($slugs -contains $prefix) { $verifyModel = $prefix; break } }
if (-not $verifyModel) { $verifyModel = $slugs[0] }
Write-Host "catalog: $catalog  ($($models.Count) models, $($raised.Count) raised)"
foreach ($r in $raised) { Write-Host ("  {0,-16} context_window {1} -> {2}" -f $r.slug, $r.before, $targetWindow) }

Set-CatalogConfig $catalog
$script:liftedConfig = $null
Write-Host "config: model_catalog_json set in $config"

# --- verify -----------------------------------------------------------------
# The catalog is parsed at startup, so a typo here is a hard failure on the next
# codex run. Prove it loads and that the window really moved, rather than
# assuming: a wrong answer here is silent until something important breaks.
if ($NoVerify) {
    Write-Host ""
    Write-Host "skipped verification. Restart Claude Desktop so the bridge spawns a fresh app-server."
    exit 0
}

Write-Host ""
Write-Host "verifying with a one-word codex run on $verifyModel..."
$tmp = New-TempDir
try {
    $run = Invoke-Codex @("exec", "--json", "--model", $verifyModel, "--skip-git-repo-check", "-C", ".", '"Reply only with OK."') $tmp
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
$out = $run.Out
if ($run.Exit -ne 0) {
    [Console]::Error.WriteLine("FAILED: codex could not start with the new catalog")
    foreach ($line in ($out | Select-Object -Last 3)) { [Console]::Error.WriteLine($line) }
    Fail "restore with: $PSCommandPath -Revert"
}

$tid = $null
foreach ($line in $out) {
    if ($line -like '*thread.started*') {
        try { $tid = [string](Get-Prop (ConvertFrom-Json $line) "thread_id") } catch { }
        if ($tid) { break }
    }
}
$window = $null
if ($tid) {
    $roll = Get-ChildItem -LiteralPath (Join-Path $codexHome "sessions") -Recurse -File -Filter "*$tid*" -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($roll) {
        $match = [regex]::Match([System.IO.File]::ReadAllText($roll.FullName), '"model_context_window":(\d+)')
        if ($match.Success) { $window = [int]$match.Groups[1].Value }
    }
}
$expected = [int]([long]$targetWindow * 95 / 100)

if ($window -eq $expected) {
    Write-Host "OK  effective window is now $window (= $targetWindow x 95% headroom), was 258400."
    Write-Host ""
    Write-Host "Restart Claude Desktop so the bridge spawns a fresh app-server - the catalog"
    Write-Host "is read at startup only."
} else {
    $shown = if ($null -ne $window) { $window } else { "unknown" }
    [Console]::Error.WriteLine("UNEXPECTED: effective window reported as $shown, expected $expected")
    Fail "the catalog may not have been picked up. Restore with: $PSCommandPath -Revert"
}
