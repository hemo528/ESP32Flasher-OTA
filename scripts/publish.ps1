#Requires -Version 5.1
<#
.SYNOPSIS
    发布固件包到 ESP32Flasher-OTA 固件商店仓库。

.DESCRIPTION
    流程（幂等，可 -DryRun 预演）：
      1. 解析 .fwpack 的 manifest.json（id/name/version/desc/tag/chip/builtAt），
         并用 scripts/fwpack.py 做一次完整包校验（format / 文件 / 内部 sha256）；
      2. 拷贝到 firmware/<id>/<id>-v<version>.fwpack（已存在且 sha256 相同 → 幂等跳过）；
      3. 计算整包 size / sha256，写入 firmware.json（版本必须大于当前最新；
         -Force 允许覆盖同版本）；
      4. 版本按从新到旧排序，裁剪超过 -KeepVersions 的旧条目并删除旧文件；
      5. git add / commit / push（-DryRun 跳过）；
      6. 打印 raw 地址并提示 CDN 缓存约 5 分钟。

    在仓库根目录或任意位置执行均可（脚本按自身位置定位仓库根目录）。

.PARAMETER Pack
    一个或多个 .fwpack 文件路径。

.PARAMETER Changelog
    本次发布的更新说明（中文）。缺省时使用包内 manifest 的 desc。

.PARAMETER KeepVersions
    每个固件保留的历史版本数，默认 3。

.PARAMETER DryRun
    只打印将要执行的操作，不复制文件、不改索引、不提交、不推送。

.PARAMETER Force
    允许覆盖同版本（内容不同时覆盖文件；索引里同版本条目会被替换）。

.EXAMPLE
    .\scripts\publish.ps1 -Pack 'C:\build\rlcd-screen-v1.3.0.fwpack' -Changelog "1. 新增大模型额度页"

.EXAMPLE
    .\scripts\publish.ps1 -Pack .\firmware\eink-42bw\eink-42bw-v0.1.0.fwpack -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]]$Pack,

    [string]$Changelog = '',

    [ValidateRange(1, 100)]
    [int]$KeepVersions = 3,

    [switch]$DryRun,

    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$IndexPath = Join-Path $RepoRoot 'firmware.json'
$FirmwareDir = Join-Path $RepoRoot 'firmware'
$RawBase = 'https://raw.githubusercontent.com/hemo528/ESP32Flasher-OTA/main/'

# ---------------------------------------------------------------- 输出辅助

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Info([string]$Message) { Write-Host "    $Message" }
function Write-WarnMsg([string]$Message) { Write-Host "    [警告] $Message" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 版本号

function Test-VersionFormat([string]$Version) {
    return ($Version -match '^\d+\.\d+\.\d+$')
}

function Get-VersionKey([string]$Version) {
    if (-not (Test-VersionFormat $Version)) { return $Version }
    $p = $Version.Split('.')
    return ('{0:D8}.{1:D8}.{2:D8}' -f [int]$p[0], [int]$p[1], [int]$p[2])
}

function Compare-Version([string]$A, [string]$B) {
    return [string]::CompareOrdinal((Get-VersionKey $A), (Get-VersionKey $B))
}

# ---------------------------------------------------------------- 文件与包

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function New-FlashObject([object]$Manifest) {
    # 索引里的 flash 直接从包内 manifest 拷（缺省时按 fwpack 约定补齐）
    $mode = 'dio'
    $freq = '80m'
    $size = '16MB'
    $f = $Manifest.flash
    if ($f) {
        if ("$($f.mode)") { $mode = "$($f.mode)" }
        if ("$($f.freq)") { $freq = "$($f.freq)" }
        if ("$($f.size)") { $size = "$($f.size)" }
    }
    return [pscustomobject]([ordered]@{ mode = $mode; freq = $freq; size = $size })
}

function New-FilesArray([object]$Manifest) {
    # 索引里的 files 直接从包内 manifest 拷：offset 原样 hex 字符串，按 offset 升序
    $list = New-Object System.Collections.ArrayList
    foreach ($item in @($Manifest.files)) {
        if (-not $item) { continue }
        $role = "$($item.role)"
        if (-not $role) { $role = 'app' }
        $sz = 0
        if ($null -ne $item.size) { $sz = [int]$item.size }
        [void]$list.Add([pscustomobject]([ordered]@{
            offset = "$($item.offset)"
            path   = "$($item.path)"
            size   = $sz
            role   = $role
        }))
    }
    return @($list | Sort-Object -Property @{
        Expression = { [Convert]::ToInt64(("$($_.offset)" -replace '^0[xX]', ''), 16) }
    })
}

function Read-PackManifest([string]$Path) {
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    } catch {
        # 新版本 PowerShell 已自带该程序集，忽略加载失败
    }
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entry = $zip.Entries | Where-Object { $_.FullName -match '(^|/)manifest\.json$' } | Select-Object -First 1
        if (-not $entry) { throw "刷机包里没有 manifest.json：$Path" }
        $stream = $entry.Open()
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
        try {
            $raw = $reader.ReadToEnd()
        } finally {
            $reader.Dispose()
            $stream.Dispose()
        }
    } finally {
        $zip.Dispose()
    }
    try {
        return ($raw | ConvertFrom-Json)
    } catch {
        throw "manifest.json 不是合法 JSON：$Path（$($_.Exception.Message)）"
    }
}

function Get-PythonExe {
    $candidates = New-Object System.Collections.ArrayList
    if ($env:PYTHON) { [void]$candidates.Add($env:PYTHON) }
    foreach ($c in @('C:\Python314\python.exe', 'C:\Python313\python.exe', 'C:\Python312\python.exe')) {
        [void]$candidates.Add($c)
    }
    foreach ($name in @('python', 'python3', 'py')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { [void]$candidates.Add($cmd.Source) }
    }
    foreach ($c in $candidates) {
        if (-not $c) { continue }
        if (-not (Test-Path -LiteralPath $c)) { continue }
        try {
            $major = & $c -c "import sys;print(sys.version_info[0])" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$major".Trim() -eq '3') { return $c }
        } catch {
            continue
        }
    }
    return $null
}

function Test-PackWithFwpack([string]$Python, [string]$ScriptsDir, [string]$PackPath) {
    # 用仓库内的 scripts/fwpack.py 做完整包校验（format / 文件存在 / 内部 sha256）
    $code = 'import json,sys;sys.path.insert(0,sys.argv[1]);import fwpack;p=fwpack.load_pack(sys.argv[2]);probs=fwpack.verify_pack(p);print(json.dumps({"ok":not probs,"problems":probs},ensure_ascii=False))'
    $out = $code | & $Python - $ScriptsDir $PackPath
    $text = ($out | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "scripts/fwpack.py 校验失败（python 退出码 $LASTEXITCODE）：$PackPath`n$text"
    }
    try {
        $res = $text | ConvertFrom-Json
    } catch {
        throw "scripts/fwpack.py 输出无法解析：$text"
    }
    if (-not $res.ok) {
        throw ("包内部校验未通过：`n  " + (@($res.problems) -join "`n  "))
    }
}

function Write-IndexJson([object]$Index) {
    $json = $Index | ConvertTo-Json -Depth 20
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        # Windows PowerShell 5.1 的 ConvertTo-Json 会把中文转成 \uXXXX；
        # 逐字符还原成 UTF-8 中文（JSON 内容里不会出现字面 \u，替换是安全的）。
        $sb = New-Object System.Text.StringBuilder
        $last = 0
        foreach ($m in [regex]::Matches($json, '\\u([0-9a-fA-F]{4})')) {
            [void]$sb.Append($json.Substring($last, $m.Index - $last))
            [void]$sb.Append([char][Convert]::ToInt32($m.Groups[1].Value, 16))
            $last = $m.Index + $m.Length
        }
        [void]$sb.Append($json.Substring($last))
        $json = $sb.ToString()
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($IndexPath, $json + "`n", $utf8NoBom)
}

function Invoke-Git([string[]]$GitArgs) {
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & git -C $RepoRoot @GitArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldEap
    }
    if ($code -ne 0) {
        throw ("git " + ($GitArgs -join ' ') + " 失败（退出码 $code）：`n" + ($out | Out-String))
    }
    return ($out | Out-String)
}

# ---------------------------------------------------------------- 前置检查

if (-not (Test-Path -LiteralPath $IndexPath -PathType Leaf)) {
    throw "找不到索引文件：$IndexPath（请确认脚本位于仓库的 scripts/ 目录下）"
}
if (-not (Test-Path -LiteralPath $FirmwareDir -PathType Container)) {
    throw "找不到 firmware/ 目录：$FirmwareDir"
}

$Python = Get-PythonExe
if (-not $Python) {
    throw '找不到 Python 3（publish.ps1 需要调用 scripts/fwpack.py 做包校验；可用环境变量 PYTHON 指定解释器）'
}

$index = [System.IO.File]::ReadAllText($IndexPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
if ("$($index.schema)" -ne 'fwstore/1') {
    throw "索引 schema 不支持：$($index.schema)（期望 fwstore/1）"
}
if ($index.mirrors -and @($index.mirrors).Count -gt 0) {
    $RawBase = "$(@($index.mirrors)[0])"
}

Write-Step "仓库根目录：$RepoRoot"
Write-Info "Python：$Python"
if ($DryRun) { Write-WarnMsg 'DryRun 模式：不会修改任何文件、不会提交推送' }

# ---------------------------------------------------------------- 阶段 1：校验所有包

$plans = New-Object System.Collections.ArrayList
$seenKeys = @{}
foreach ($p in $Pack) {
    Write-Step "校验固件包：$p"
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
        throw "找不到固件包文件：$p"
    }
    $srcPath = (Resolve-Path -LiteralPath $p).ProviderPath
    $manifest = Read-PackManifest $srcPath

    $format = "$($manifest.format)"
    if (-not $format.StartsWith('fwpack/')) {
        throw "不是 fwpack 刷机包（format=$format）：$srcPath"
    }
    if (-not $manifest.files -or @($manifest.files).Count -eq 0) {
        throw "manifest.files 为空：$srcPath"
    }
    $id = "$($manifest.id)".Trim()
    $version = "$($manifest.version)".Trim()
    if (-not $id) { throw "manifest 缺少 id：$srcPath" }
    if (-not (Test-VersionFormat $version)) {
        throw "manifest.version=$version 不是 x.y.z 形式：$srcPath"
    }

    Test-PackWithFwpack -Python $Python -ScriptsDir $PSScriptRoot -PackPath $srcPath
    Write-Info ("id={0}  version={1}  name={2}" -f $id, $version, $manifest.name)

    $key = "$id@$version"
    if ($seenKeys.ContainsKey($key)) {
        throw "同一次调用里重复指定了 $id v$version"
    }
    $seenKeys[$key] = $true

    $sha = Get-Sha256 $srcPath
    $size = (Get-Item -LiteralPath $srcPath).Length
    $targetDir = Join-Path $FirmwareDir $id
    $target = Join-Path $targetDir ("{0}-v{1}.fwpack" -f $id, $version)

    [void]$plans.Add([pscustomobject]@{
        Id       = $id
        Version  = $version
        Manifest = $manifest
        Source   = $srcPath
        Sha256   = $sha
        Size     = $size
        Target   = $target
        TargetDir = $targetDir
    })
}

# ---------------------------------------------------------------- 阶段 2：更新索引（内存）

$ops = New-Object System.Collections.ArrayList
$changed = New-Object System.Collections.ArrayList

foreach ($plan in $plans) {
    $id = $plan.Id
    $version = $plan.Version
    $targetExists = Test-Path -LiteralPath $plan.Target -PathType Leaf
    $targetSha = $null
    if ($targetExists) { $targetSha = Get-Sha256 $plan.Target }

    $entry = @($index.firmwares | Where-Object { "$($_.id)" -eq $id }) | Select-Object -First 1
    $existingVersion = $null
    if ($entry) {
        $existingVersion = @($entry.versions | Where-Object { "$($_.version)" -eq $version }) | Select-Object -First 1
    }

    if ($targetExists -and $targetSha -eq $plan.Sha256) {
        if ($existingVersion -and "$($existingVersion.sha256)".ToLowerInvariant() -eq $plan.Sha256) {
            if ($existingVersion.flash -and $existingVersion.files -and @($existingVersion.files).Count -gt 0) {
                Write-Step "幂等：$id v$version 已存在且 sha256 相同，跳过"
                continue
            }
            # 已发布过、但索引缺 flash/files 元数据（schema 扩展前发布的包）：只补元数据
            Write-Info "已发布但索引缺少 flash/files 元数据，仅补写元数据"
            $oldMinFlasher = "$($existingVersion.minFlasher)"
            if (-not $oldMinFlasher) { $oldMinFlasher = '1.2.0' }
            $rebuilt = [pscustomobject]([ordered]@{
                version    = "$($existingVersion.version)"
                url        = "$($existingVersion.url)"
                size       = [long]$existingVersion.size
                sha256     = "$($existingVersion.sha256)"
                builtAt    = "$($existingVersion.builtAt)"
                changelog  = "$($existingVersion.changelog)"
                minFlasher = $oldMinFlasher
                flash      = (New-FlashObject $plan.Manifest)
                files      = (New-FilesArray $plan.Manifest)
            })
            $replaced = New-Object System.Collections.ArrayList
            foreach ($v in @($entry.versions)) {
                if ("$($v.version)" -eq $version) { [void]$replaced.Add($rebuilt) } else { [void]$replaced.Add($v) }
            }
            $entry.versions = @($replaced)
            if (-not ($changed -contains $id)) { [void]$changed.Add($id) }
            continue
        }
        Write-Info "目标文件已存在且内容相同，仅补写索引条目"
    } elseif ($targetExists -and $targetSha -ne $plan.Sha256) {
        if (-not $Force) {
            throw "目标文件已存在且内容不同：$($plan.Target)（如确需覆盖请加 -Force）"
        }
        Write-WarnMsg "将覆盖已存在的同版本文件（-Force）：$($plan.Target)"
    }

    $latest = $null
    if ($entry -and $entry.versions -and @($entry.versions).Count -gt 0) {
        $latest = "$(@($entry.versions)[0].version)"
    }
    if ($latest) {
        $cmp = Compare-Version $version $latest
        if ($cmp -lt 0) {
            throw "版本回退：$id 新版本 v$version 低于当前最新 v$latest，拒绝发布"
        }
        if ($cmp -eq 0) {
            if (-not $Force) {
                throw "$id v$version 已是当前最新版本；如确需覆盖同版本请加 -Force"
            }
            Write-WarnMsg "$id v$version 已存在，-Force 覆盖该版本条目"
        }
    }

    $builtAt = "$($plan.Manifest.builtAt)"
    if ([string]::IsNullOrWhiteSpace($builtAt)) {
        $builtAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    $changelogText = $Changelog
    if ([string]::IsNullOrWhiteSpace($changelogText)) {
        $changelogText = "$($plan.Manifest.desc)"
        if ([string]::IsNullOrWhiteSpace($changelogText)) { $changelogText = "发布 $id v$version" }
        Write-Info "未指定 -Changelog，使用包内 desc：$changelogText"
    }

    $newVer = [pscustomobject]([ordered]@{
        version    = $version
        url        = ("firmware/{0}/{0}-v{1}.fwpack" -f $id, $version)
        size       = [long]$plan.Size
        sha256     = $plan.Sha256
        builtAt    = $builtAt
        changelog  = $changelogText
        minFlasher = '1.2.0'
        flash      = (New-FlashObject $plan.Manifest)
        files      = (New-FilesArray $plan.Manifest)
    })

    if (-not $entry) {
        $entry = [pscustomobject]([ordered]@{
            id       = $id
            name     = "$($plan.Manifest.name)"
            tag      = "$($plan.Manifest.tag)"
            chip     = "$($plan.Manifest.chip)"
            desc     = "$($plan.Manifest.desc)"
            latest   = $version
            versions = @($newVer)
        })
        $index.firmwares = @(@($index.firmwares) + $entry)
        Write-Info "索引新增固件：$id v$version"
    } else {
        $entry.name = "$($plan.Manifest.name)"
        $entry.tag = "$($plan.Manifest.tag)"
        $entry.chip = "$($plan.Manifest.chip)"
        $entry.desc = "$($plan.Manifest.desc)"

        $kept = New-Object System.Collections.ArrayList
        foreach ($v in @($entry.versions)) {
            if ($Force -and "$($v.version)" -eq $version) { continue }  # 同版本覆盖：先移除旧条目
            [void]$kept.Add($v)
        }
        [void]$kept.Add($newVer)
        $sorted = @($kept | Sort-Object -Property @{ Expression = { Get-VersionKey "$($_.version)" } } -Descending)
        $entry.versions = $sorted
        $entry.latest = "$($sorted[0].version)"
        Write-Info "索引更新固件：$id → 最新 v$($entry.latest)"
    }

    # 裁剪超出 -KeepVersions 的旧版本
    $allVersions = @($entry.versions)
    if ($allVersions.Count -gt $KeepVersions) {
        $removed = @($allVersions[$KeepVersions..($allVersions.Count - 1)])
        $entry.versions = @($allVersions[0..($KeepVersions - 1)])
        $entry.latest = "$(@($entry.versions)[0].version)"
        foreach ($rv in $removed) {
            $rurl = "$($rv.url)"
            if ([string]::IsNullOrWhiteSpace($rurl)) {
                $rurl = ("firmware/{0}/{0}-v{1}.fwpack" -f $id, "$($rv.version)")
            }
            $rpath = Join-Path $RepoRoot ($rurl -replace '/', [System.IO.Path]::DirectorySeparatorChar)
            [void]$ops.Add([pscustomobject]@{ Kind = 'delete'; Path = $rpath; Label = "$id v$($rv.version)" })
            Write-Info "裁剪旧版本：$id v$($rv.version)（文件将删除）"
        }
    }

    [void]$ops.Add([pscustomobject]@{ Kind = 'copy'; Path = $plan.Target; Source = $plan.Source; Label = "$id v$version"; Size = $plan.Size })
    if (-not ($changed -contains $id)) { [void]$changed.Add($id) }
}

$index.generatedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

# ---------------------------------------------------------------- 阶段 3：落盘 / 预演

if ($DryRun) {
    Write-Step 'DryRun：以下操作不会执行'
    foreach ($op in $ops) {
        if ($op.Kind -eq 'copy') {
            $exists = Test-Path -LiteralPath $op.Path -PathType Leaf
            if ($exists -and (Get-Sha256 $op.Path) -eq (Get-Sha256 $op.Source)) {
                Write-Info "[跳过] 已存在且 sha256 相同：$($op.Path)"
            } else {
                Write-Info ("[复制] {0} → {1}（{2:N0} B）" -f $op.Source, $op.Path, $op.Size)
            }
        } else {
            Write-Info "[删除] $($op.Path)"
        }
    }
    Write-Info "[写入] firmware.json（generatedAt=$($index.generatedAt)）"
    Write-Info '[跳过] git add / commit / push'
} else {
    foreach ($op in $ops) {
        if ($op.Kind -eq 'copy') {
            $exists = Test-Path -LiteralPath $op.Path -PathType Leaf
            if ($exists -and (Get-Sha256 $op.Path) -eq (Get-Sha256 $op.Source)) {
                Write-Info "已存在且 sha256 相同，无需复制：$($op.Path)"
                continue
            }
            if (-not (Test-Path -LiteralPath (Split-Path -Parent $op.Path) -PathType Container)) {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $op.Path) | Out-Null
            }
            Copy-Item -LiteralPath $op.Source -Destination $op.Path -Force
            $copiedSha = Get-Sha256 $op.Path
            $srcSha = Get-Sha256 $op.Source
            if ($copiedSha -ne $srcSha) {
                throw "复制后 sha256 不一致：$($op.Path)"
            }
            Write-Info ("[复制] {0}（{1:N0} B）" -f $op.Path, (Get-Item -LiteralPath $op.Path).Length)
        } else {
            if (Test-Path -LiteralPath $op.Path -PathType Leaf) {
                Remove-Item -LiteralPath $op.Path -Force
                Write-Info "[删除] $($op.Path)"
            }
        }
    }

    Write-IndexJson $index
    Write-Info '[写入] firmware.json'

    # git 提交推送
    if (Test-Path -LiteralPath (Join-Path $RepoRoot '.git')) {
        Invoke-Git @('add', '-A') | Out-Null
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & git -C $RepoRoot diff --cached --quiet
        $hasChanges = ($LASTEXITCODE -ne 0)
        $ErrorActionPreference = $oldEap
        if ($hasChanges) {
            $ids = @($changed)
            if ($ids.Count -eq 1 -and $plans.Count -eq 1) {
                $commitMsg = "firmware($($ids[0])): v$($plans[0].Version)"
            } else {
                $commitMsg = "firmware: 发布 $(@($plans | ForEach-Object { "$($_.Id) v$($_.Version)" }) -join ', ')"
            }
            Invoke-Git @('commit', '-m', $commitMsg) | Out-Null
            Write-Info "已提交：$commitMsg"
            Invoke-Git @('push') | Out-Null
            Write-Info '已推送到 origin'
        } else {
            Write-Info '没有需要提交的改动，跳过 commit / push'
        }
    } else {
        Write-WarnMsg "不是 git 仓库（缺少 .git），跳过 git 提交推送：$RepoRoot"
    }
}

# ---------------------------------------------------------------- 阶段 4：输出地址

Write-Step '发布完成，固件下载地址'
Write-Info "索引：$($RawBase)firmware.json"
foreach ($plan in $plans) {
    $rel = "firmware/$($plan.Id)/$($plan.Id)-v$($plan.Version).fwpack"
    Write-Info ("包：  {0}{1}" -f $RawBase, $rel)
    Write-Info ("      size={0:N0} B  sha256={1}" -f $plan.Size, $plan.Sha256)
}
Write-Host ''
Write-Host '提示：raw.githubusercontent.com 的 CDN 缓存约 5 分钟；' -ForegroundColor Yellow
Write-Host '      jsDelivr 首次访问可能需要预热，发布后 5 分钟内不要重复发布同一版本。' -ForegroundColor Yellow
if ($DryRun) {
    Write-Host '（DryRun 模式：以上仅为预演结果）' -ForegroundColor Yellow
}
