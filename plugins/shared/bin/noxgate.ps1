$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$architecture = $env:PROCESSOR_ARCHITEW6432
if (!$architecture) { $architecture = $env:PROCESSOR_ARCHITECTURE }
$platform = switch ($architecture) {
    'ARM64' { 'windows-arm64' }
    'AMD64' { 'windows-x64' }
    default { throw 'NoxGate: unsupported Windows CPU' }
}
$bundlePlatform = Join-Path $root 'runtime/platform.txt'
if (Test-Path -LiteralPath $bundlePlatform -PathType Leaf) {
    $bundled = (Get-Content -LiteralPath $bundlePlatform -Raw).Trim()
    if ($bundled -ne $platform) { throw "NoxGate: this package is for $bundled, expected $platform" }
    $env:NOXGATE_OFFLINE = '1'
}

$config = $env:NOXGATE_HOME
if (!$config) {
    $appData = $env:LOCALAPPDATA
    if (!$appData) { $appData = $env:APPDATA }
    $config = Join-Path $appData 'NoxGate'
} elseif ($config.StartsWith('~/') -or $config.StartsWith('~\')) {
    $config = Join-Path $env:USERPROFILE $config.Substring(2)
}
$cache = Join-Path $config 'runtime'
[void][IO.Directory]::CreateDirectory($cache)
$entries = @(Get-Content -LiteralPath (Join-Path $root 'lib/runtime.lock') | Where-Object { $_.StartsWith("$platform`tpython`t") })
if ($entries.Count -ne 1) { throw 'NoxGate: Python is missing from runtime.lock' }
$parts = $entries[0].Split("`t")
$target = Join-Path $cache $parts[3]
$python = Join-Path $target 'python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
    $stage = Join-Path $cache ('.python.' + [Guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($stage)
    try {
        $archive = Join-Path (Join-Path $root 'runtime') $parts[2]
        if (!(Test-Path -LiteralPath $archive -PathType Leaf)) {
            if ($env:NOXGATE_OFFLINE -eq '1') { throw "NoxGate: missing bundled runtime $($parts[2])" }
            $archive = Join-Path $stage $parts[2]
            [Console]::Error.WriteLine('NoxGate: preparing Python for the first launch')
            & "$env:SystemRoot\System32\curl.exe" --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 180 --retry 2 --output $archive $parts[4]
            if ($LASTEXITCODE) { throw 'NoxGate: Python download failed' }
        }
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $parts[3]) {
            throw 'NoxGate: Python archive SHA-256 mismatch'
        }
        & "$env:SystemRoot\System32\tar.exe" -xzf $archive -C $stage
        if ($LASTEXITCODE) { throw 'NoxGate: Python extraction failed' }
        try {
            [IO.Directory]::Move((Join-Path $stage 'python'), [IO.Path]::GetFullPath($target))
        } catch [IO.IOException] {
            if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw }
        }
    } finally {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}

& $python -I (Join-Path $root 'lib/noxgate_bootstrap.py') $platform @args
exit $LASTEXITCODE
