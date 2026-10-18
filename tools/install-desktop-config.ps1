# Adds the dota-draft MCP server to Claude Desktop's config once Claude Desktop has quit.
# Claude Desktop rewrites its config from memory while it runs, so an edit made while it is
# open is lost. Start this script, quit Claude Desktop, and it edits the file after the app exits.

param(
    [string]$ConfigPath = "$env:LOCALAPPDATA\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json",
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$UvPath = "",
    [int]$MaxWaitMinutes = 120,
    [string]$LogPath = (Join-Path $PSScriptRoot 'install-desktop-config.log'),
    [switch]$NoWait  # for dry runs on a copy of the config only
)

$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

function Log([string]$Message) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    [System.IO.File]::AppendAllText($LogPath, $line + [Environment]::NewLine, $Utf8NoBom)
}

function Get-DesktopApp {
    # Only the Claude Desktop app itself (installed under WindowsApps), not Claude Code's claude.exe.
    Get-Process -Name 'Claude' -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*\WindowsApps\Claude_*' }
}

try {
    Log "Started. Config: $ConfigPath"

    if (-not $UvPath) {
        $found = Get-Command uv -ErrorAction SilentlyContinue
        if ($found) { $UvPath = $found.Source }
    }
    if (-not $UvPath -or -not (Test-Path $UvPath)) { throw "uv.exe not found (pass -UvPath)." }
    if (-not (Test-Path (Join-Path $ProjectDir 'pyproject.toml'))) { throw "Project not found at $ProjectDir." }
    if (-not (Test-Path $ConfigPath)) { throw "Config file not found." }
    Log "uv: $UvPath | project: $ProjectDir"

    # 1. Wait until Claude Desktop has quit.
    $deadline = (Get-Date).AddMinutes($MaxWaitMinutes)
    Log "Waiting for Claude Desktop to quit (up to $MaxWaitMinutes minutes)..."
    while (-not $NoWait -and (Get-DesktopApp)) {
        if ((Get-Date) -gt $deadline) { throw "Claude Desktop was still running after $MaxWaitMinutes minutes. Nothing changed." }
        Start-Sleep -Seconds 2
    }
    Start-Sleep -Seconds 3  # let the app finish its last write
    Log "Claude Desktop has quit."

    # 2. Back up the config next to the original.
    $BackupPath = '{0}.backup-{1}' -f $ConfigPath, (Get-Date -Format 'yyyyMMdd-HHmmss')
    Copy-Item -LiteralPath $ConfigPath -Destination $BackupPath
    Log "Backup: $BackupPath"

    # 3. Add mcpServers.dota-draft, keeping every other key.
    $originalText = [System.IO.File]::ReadAllText($ConfigPath)
    $config = $originalText | ConvertFrom-Json

    $server = [pscustomobject]@{
        command = $UvPath
        args    = @('run', '--directory', $ProjectDir, 'dota-draft-server')
    }
    if ($config.PSObject.Properties.Name -contains 'mcpServers') {
        $config.mcpServers | Add-Member -NotePropertyName 'dota-draft' -NotePropertyValue $server -Force
        Log "mcpServers existed; added or replaced dota-draft, other servers kept."
    } else {
        $config | Add-Member -NotePropertyName 'mcpServers' -NotePropertyValue ([pscustomobject]@{ 'dota-draft' = $server })
        Log "Added mcpServers with dota-draft."
    }
    [System.IO.File]::WriteAllText($ConfigPath, ($config | ConvertTo-Json -Depth 20), $Utf8NoBom)

    # 4. Check the result: valid JSON, dota-draft present, every other key unchanged.
    try {
        $check = [System.IO.File]::ReadAllText($ConfigPath) | ConvertFrom-Json
        if ($check.mcpServers.'dota-draft'.command -ne $UvPath) { throw "dota-draft entry missing after writing." }
        $rest = $check | Select-Object -Property * -ExcludeProperty mcpServers
        $restBefore = $originalText | ConvertFrom-Json | Select-Object -Property * -ExcludeProperty mcpServers
        if (($rest | ConvertTo-Json -Depth 20 -Compress) -ne ($restBefore | ConvertTo-Json -Depth 20 -Compress)) {
            throw "Other keys changed while writing."
        }
        Log "Check passed: valid JSON, dota-draft added, all other keys unchanged."
    } catch {
        Copy-Item -LiteralPath $BackupPath -Destination $ConfigPath -Force
        Log "Check FAILED ($($_.Exception.Message)). Restored the backup; config is unchanged."
        exit 1
    }

    Log "Done. Start Claude Desktop; dota-draft should appear in its MCP servers."
    exit 0
} catch {
    Log "ERROR: $($_.Exception.Message)"
    exit 1
}
