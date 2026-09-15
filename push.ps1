<#
    push.ps1 —— 把本地改动一键同步到 GitHub

    它按顺序做 5 件事，每一步都打印结果：

      [1/5] 检查待提交的文件（顺便拦住 .env 这类敏感文件）
      [2/5] 跑单元测试（改坏了就直接中止，不推上去）
      [3/5] fetch + rebase  ← 关键！机器人每周会自动提交 data/pushed_dois.json，
                              不先同步的话 push 会被拒绝
      [4/5] push（带重试，github.com 在国内时通时不通）
      [5/5] 校验远程真的收到了

    用法：
        .\push.ps1                          # 交互式，会问提交说明
        .\push.ps1 -Message "改关键词"       # 直接给提交说明
        .\push.ps1 -SkipTests               # 跳过测试（不推荐）
        .\push.ps1 -Retries 6               # 网络很差时多试几次

    也可以直接双击 push.cmd（免去执行策略的限制）。
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Message,
    [switch]$SkipTests,
    [int]$Retries = 4
)

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
Set-Location -LiteralPath $PSScriptRoot

$Branch = 'main'

# 必须挡住的文件：一旦推上去，密钥就泄露了（删掉也会留在 git 历史里）
$Forbidden = @('.env', '.venv/', 'data/logs/', 'data/topics_cache.json', 'data/outbox/', '__pycache__')

function Write-Step { param([string]$T) Write-Host "`n$T" -ForegroundColor Cyan }
function Write-Ok   { param([string]$T) Write-Host "  [OK]   $T" -ForegroundColor Green }
function Write-Warn { param([string]$T) Write-Host "  [警告] $T" -ForegroundColor Yellow }
function Write-Bad  { param([string]$T) Write-Host "  [错误] $T" -ForegroundColor Red }

function Find-GitPath {
    # ① PATH 里有就最省事
    $cmd = Get-Command git -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    # ② 常见安装位置（含本项目实测的 E:\Git）
    $fixed = @(
        'E:\Git\cmd\git.exe',
        "$env:ProgramFiles\Git\cmd\git.exe",
        "${env:ProgramFiles(x86)}\Git\cmd\git.exe",
        "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"
    )
    foreach ($p in $fixed) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }

    # ③ 兜底：查注册表的 InstallLocation（装在任意盘都能找到）
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    $loc = Get-ItemProperty $keys -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like 'Git*' -and $_.InstallLocation } |
        Select-Object -First 1 -ExpandProperty InstallLocation
    if ($loc) {
        foreach ($sub in @('cmd\git.exe', 'bin\git.exe')) {
            $p = Join-Path $loc $sub
            if (Test-Path -LiteralPath $p) { return $p }
        }
    }
    return $null
}

function Find-PythonPath {
    $venv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Invoke-Git {
    param([string[]]$Arguments, [switch]$AllowFail)
    # 注意：PowerShell 5.1 会把原生命令的 stderr 包装成 ErrorRecord，
    # 直接 Out-String 会得到一串 “+ FullyQualifiedErrorId : NativeCommandError” 噪音。
    # 这里把 ErrorRecord 拆回它的原始文本，才能看到 git 真正的报错。
    $output = & $script:GitPath @Arguments 2>&1
    $code = $LASTEXITCODE
    $text = ($output | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message }
        else { [string]$_ }
    }) -join "`n"
    $text = $text.Trim()
    if ($code -ne 0 -and -not $AllowFail) {
        throw "git $($Arguments -join ' ') 执行失败（退出码 $code）：`n$text"
    }
    return [pscustomobject]@{ Code = $code; Text = $text }
}

function Invoke-GitRetry {
    param([string[]]$Arguments, [int]$Attempts)
    if ($Attempts -lt 1) { $Attempts = 4 }
    $result = $null
    for ($i = 1; $i -le $Attempts; $i++) {
        $result = Invoke-Git -Arguments $Arguments -AllowFail
        if ($result.Code -eq 0) { return $result }
        $lastLine = ($result.Text -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        $lastLine = ($lastLine -replace '^fatal:\s*', '').Trim()
        if ($i -lt $Attempts) {
            Write-Warn "第 $i/$Attempts 次失败：$lastLine"
            Write-Warn "3 秒后重试……（github.com 在国内时通时不通，多试几次通常就成功）"
            Start-Sleep -Seconds 3
        }
        else {
            Write-Bad "第 $i/$Attempts 次失败：$lastLine"
        }
    }
    return $result
}

# ===========================================================================
Write-Host "===============================================" -ForegroundColor White
Write-Host " 同步本地改动到 GitHub" -ForegroundColor White
Write-Host "===============================================" -ForegroundColor White

# ---------- 0. 环境自检 ----------
$script:GitPath = Find-GitPath
if (-not $script:GitPath) {
    Write-Bad "找不到 git。可能还没装，或装在很特殊的位置。"
    Write-Host "       装法见 README「第 0 步：装 Git」，或去 https://git-scm.com/download/win" -ForegroundColor Gray
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.git'))) {
    Write-Bad "当前目录不是 git 仓库（没有 .git）。请把本脚本放在项目根目录。"
    exit 1
}
Write-Host "`n  git    : $script:GitPath" -ForegroundColor Gray
Write-Host "  仓库   : $PSScriptRoot" -ForegroundColor Gray

$current = (Invoke-Git -Arguments @('rev-parse', '--abbrev-ref', 'HEAD')).Text
if ($current -ne $Branch) {
    Write-Bad "当前分支是 '$current'，但脚本按 '$Branch' 工作。"
    Write-Warn "GitHub Actions 的定时任务只在默认分支 '$Branch' 上生效。"
    Write-Warn "切换分支：git checkout $Branch"
    exit 1
}

# ---------- 1. 检查工作区 ----------
Write-Step "[1/5] 检查待提交的文件……"

$porcelain = (Invoke-Git -Arguments @('status', '--porcelain')).Text
if ([string]::IsNullOrWhiteSpace($porcelain)) {
    Write-Ok "工作区是干净的，没有需要提交的改动（将直接尝试推送已有提交）"
}
else {
    Write-Host $porcelain -ForegroundColor Gray
    Invoke-Git -Arguments @('add', '-A') | Out-Null
}

$staged = (Invoke-Git -Arguments @('diff', '--cached', '--name-only')).Text
if (-not [string]::IsNullOrWhiteSpace($staged)) {
    $bad = @()
    foreach ($line in ($staged -split "`r?`n")) {
        $f = $line.Trim().Replace('\', '/')
        if (-not $f) { continue }
        foreach ($pat in $Forbidden) {
            if ($f -eq $pat.TrimEnd('/') -or $f -like "$pat*") { $bad += $f; break }
        }
    }
    if ($bad.Count -gt 0) {
        Write-Bad "检测到不该提交的文件，已中止："
        $bad | Sort-Object -Unique | ForEach-Object { Write-Host "         $_" -ForegroundColor Red }
        Write-Warn "这些文件一旦推上去，密钥就泄露了，而且删掉也仍留在 git 历史里。"
        Write-Warn "先取消暂存：git reset　　然后检查 .gitignore"
        exit 2
    }
    Write-Ok "待提交的文件没有敏感内容"
}

# ---------- 2. 单元测试 ----------
if ($SkipTests) {
    Write-Step "[2/5] 跳过单元测试（-SkipTests）"
}
else {
    Write-Step "[2/5] 跑单元测试……"
    $py = Find-PythonPath
    if (-not $py) {
        Write-Warn "找不到 python，跳过测试。建议手动跑一遍：python -m unittest discover -s tests"
    }
    else {
        # 测试脚本自己也会往 stderr 打印（比如故意构造损坏状态文件的用例），
        # 所以只在失败时才把输出显示出来，成功时不刷屏。
        $testOut = & $py -m unittest discover -s tests 2>&1
        $testCode = $LASTEXITCODE
        $testText = ($testOut | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message }
            else { [string]$_ }
        }) -join "`n"
        if ($testCode -ne 0) {
            Write-Host $testText -ForegroundColor Gray
            Write-Bad "测试没通过，已中止推送。修好之后再跑一次本脚本。"
            exit 3
        }
        $ran = ($testText -split "`r?`n" | Where-Object { $_ -match '^Ran\s+\d+\s+test' } | Select-Object -First 1)
        if ($ran) { Write-Ok "测试全部通过（$($ran.Trim())）" }
        else { Write-Ok "测试全部通过" }
    }
}

# ---------- 3. 提交 ----------
Write-Step "[3/5] 提交改动……"

$staged = (Invoke-Git -Arguments @('diff', '--cached', '--name-only')).Text
if ([string]::IsNullOrWhiteSpace($staged)) {
    Write-Ok "没有新改动需要提交"
}
else {
    Write-Host $staged -ForegroundColor Gray
    if ([string]::IsNullOrWhiteSpace($Message)) {
        $Message = Read-Host "  请输入提交说明（直接回车用默认值）"
    }
    if ([string]::IsNullOrWhiteSpace($Message)) {
        $Message = "chore: 更新配置 ($(Get-Date -Format 'yyyy-MM-dd HH:mm'))"
    }
    $r = Invoke-Git -Arguments @('commit', '-m', $Message) -AllowFail
    if ($r.Code -ne 0) {
        Write-Bad "提交失败：$($r.Text)"
        Write-Warn "如果提示 'Please tell me who you are'，说明没配邮箱："
        Write-Warn "  git config --global user.email `"你的邮箱`""
        exit 4
    }
    Write-Ok ($r.Text -split "`r?`n" | Select-Object -First 1)
}

# ---------- 4. 同步远程（关键步骤）----------
Write-Step "[4/5] 同步远程（fetch + rebase）……"
Write-Host "  说明：机器人每周会自动提交 data/pushed_dois.json，" -ForegroundColor Gray
Write-Host "        不先同步就直接 push 会被拒（non-fast-forward）。" -ForegroundColor Gray

$r = Invoke-GitRetry -Arguments @('fetch', 'origin') -Attempts $Retries
if ($r.Code -ne 0) {
    Write-Bad "连不上 GitHub，拉取失败。"
    Write-Warn "这通常不是你配错了，而是网络问题（github.com 在国内常被干扰）。"
    Write-Warn "挂上代理，或过一会儿再跑一次本脚本。"
    exit 5
}
Write-Ok "已拉取远程最新状态"

$r = Invoke-Git -Arguments @('rebase', "origin/$Branch") -AllowFail
if ($r.Code -ne 0) {
    Write-Bad "rebase 出现冲突："
    Write-Host $r.Text -ForegroundColor Red
    Invoke-Git -Arguments @('rebase', '--abort') -AllowFail | Out-Null
    Write-Warn "已自动回滚到 rebase 之前的状态，什么都没坏。"
    Write-Warn "多半是你本地跑过程序，data/pushed_dois.json 和机器人的版本打架了。"
    Write-Warn "常见解法：git checkout origin/$Branch -- data/pushed_dois.json  然后再跑本脚本。"
    exit 6
}
Write-Ok "已把你的改动叠到远程最新提交之上"

# ---------- 5. 推送 ----------
Write-Step "[5/5] 推送到 origin/$Branch ……"
$r = Invoke-GitRetry -Arguments @('push', 'origin', $Branch) -Attempts $Retries
if ($r.Code -ne 0) {
    Write-Bad "推送失败。"
    Write-Warn "若是认证弹窗，完成浏览器授权后重跑本脚本即可。"
    exit 7
}
Write-Ok "推送命令已成功返回"

# ---------- 校验 ----------
$local = (Invoke-Git -Arguments @('rev-parse', 'HEAD')).Text
$ls = Invoke-GitRetry -Arguments @('ls-remote', 'origin', "refs/heads/$Branch") -Attempts 2
if ($ls.Code -eq 0 -and $ls.Text) {
    $remote = ($ls.Text -split '\s+')[0]
    if ($local -eq $remote) {
        Write-Host "`n远程已是最新：$($local.Substring(0, 7))" -ForegroundColor Green
    }
    else {
        Write-Bad "远程是 $($remote.Substring(0, 7))，本地是 $($local.Substring(0, 7)) —— 没对上，请重跑一次。"
        exit 8
    }
}

Write-Host "`n===============================================" -ForegroundColor White
Write-Host " 完成！" -ForegroundColor Green
Write-Host "===============================================" -ForegroundColor White
Write-Host @"

 接下来会发生什么：
   * 下次定时任务（每周三 23:07 北京时间）会直接用新配置
   * 想立刻验证：GitHub → Actions → weekly-literature-push → Run workflow
     第一次务必勾上 dry_run（不发信，只产出 HTML 预览 artifact）

 改完 config 建议先在本地看一眼检索结果，免得白等一周：
   python -m src.main --dry-run --no-ai --verbose
   python -m src.main --find-topic          # 确认主题解析到了正确方向
"@ -ForegroundColor Gray
