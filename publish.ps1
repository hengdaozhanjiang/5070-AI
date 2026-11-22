# 一键推送到 GitHub
#
# 前提：先登录一次（只需一次，之后凭据会被记住）
#     gh auth login
#
# 然后运行本脚本：
#     .\publish.ps1                    # 仓库名默认 5070-AI
#     .\publish.ps1 -RepoName my-name  # 自定义仓库名
#     .\publish.ps1 -Private           # 建成私有仓库
#
# 脚本会做的检查：
#   1. 确认 gh 已登录
#   2. 确认代理可用（本机 GitHub 需要走 127.0.0.1:65532）
#   3. 复查没有敏感文件混进提交
#   4. 建仓并推送

param(
    [string]$RepoName = "5070-AI",
    [switch]$Private
)

$ErrorActionPreference = "Stop"
$gh = "$env:LOCALAPPDATA\Programs\gh\gh.exe"
if (-not (Test-Path $gh)) {
    $found = Get-Command gh -ErrorAction SilentlyContinue
    if ($found) { $gh = $found.Source } else { throw "找不到 gh，请先安装：winget install GitHub.cli" }
}

Write-Host "[1/4] 检查登录状态" -ForegroundColor Cyan
# 本机访问 GitHub 必须走系统代理，gh 不会自动读取注册表设置
$proxy = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue).ProxyServer
if ($proxy) {
    $env:HTTPS_PROXY = "http://$proxy"
    $env:HTTP_PROXY = "http://$proxy"
    Write-Host "      使用代理 $proxy"
}
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $gh auth status 2>&1 | Out-String | Write-Host
$authCode = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($authCode -ne 0) {
    Write-Host ""
    Write-Host "还没登录。运行下面这条命令，按提示在浏览器里授权：" -ForegroundColor Yellow
    Write-Host "    gh auth login" -ForegroundColor White
    Write-Host "选 GitHub.com -> HTTPS -> Login with a web browser" -ForegroundColor Gray
    exit 1
}

Write-Host "[2/4] 复查提交内容" -ForegroundColor Cyan
$bad = git ls-files | Select-String -Pattern '(^|/)config\.json$|voices/|chats\.json|^data/images/|^data/speech/|^exports/|^outputs/|^logs/|edge-profile'
if ($bad) {
    Write-Host "      发现不该提交的文件：" -ForegroundColor Red
    $bad | ForEach-Object { Write-Host "        $_" -ForegroundColor Red }
    throw "请先清理后再推送"
}
$n = (git ls-files).Count
Write-Host "      $n 个文件，无敏感内容"

Write-Host "[3/4] 检查工作区" -ForegroundColor Cyan
$dirty = git status --porcelain
if ($dirty) {
    Write-Host "      有未提交的改动：" -ForegroundColor Yellow
    $dirty | Write-Host
    $ans = Read-Host "      要先提交吗？(y/n)"
    if ($ans -eq "y") {
        git add -A
        $msg = Read-Host "      提交说明"
        if (-not $msg) { $msg = "更新" }
        git commit -q -m $msg
    }
} else {
    Write-Host "      工作区干净"
}

Write-Host "[4/4] 建仓并推送" -ForegroundColor Cyan
$vis = if ($Private) { "--private" } else { "--public" }
$existing = git remote get-url origin 2>$null
if ($existing) {
    Write-Host "      已配置 remote: $existing"
    git push -u origin main
} else {
    & $gh repo create $RepoName $vis --source=. --remote=origin --push
}

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "完成！" -ForegroundColor Green
    $url = & $gh repo view --json url -q .url 2>$null
    if ($url) { Write-Host "仓库地址：$url" -ForegroundColor Green }
}
