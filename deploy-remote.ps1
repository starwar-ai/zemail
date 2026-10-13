#requires -Version 5.1
<#
.SYNOPSIS
zemail 远程一键部署（Windows PowerShell 版）：把本机当前代码部署到 172.16.52.11:8020

.DESCRIPTION
与 deploy-remote.sh 完全等价，用 Windows 10+ 自带的 OpenSSH / tar / curl，无需安装任何东西。
做了什么：
  1. tar 打包当前代码（排除 .git/venv/缓存/日志/运行数据），经 SSH 上传到服务器
  2. 在服务器上执行 deploy.sh：装依赖、建 venv、注册 systemd 服务 zemail
  3. 首次部署时若本机有 .env 则自动上传（仅当服务器还没有 .env），随后启动服务
  4. 健康检查 http://<服务器>:<端口>/
服务器上的运行数据（data/ 目录）与已有 .env 永远不会被本机内容覆盖。

.PARAMETER Server
服务器地址（默认 172.16.52.11）

.PARAMETER Port
监听端口（默认 8020）

.PARAMETER SshUser
SSH 登录用户（默认 root；非 root 需可 sudo）

.PARAMETER AppDir
服务器部署目录（默认 /opt/zemail）

.PARAMETER ServiceUser
服务运行用户（默认 zemail，不存在则自动创建）

.PARAMETER PushEnv
用本机 .env 覆盖服务器 .env（覆盖后自动重启）

.PARAMETER NoEnv
首次部署也不上传本机 .env（自行上服务器编辑）

.PARAMETER WithData
连同本机 data/ 测试数据一起同步（emails/sessions/contacts/monitor 增量合并；
users.json 双边合并，同名用户以本机为准，生产独有用户保留；服务器上的
settings.json 与 .secret_key 不动，同步前自动备份到 /root/zemail-data-backup-<时间戳>.tar.gz）

.PARAMETER SkipApt
传给 deploy.sh：跳过 apt 系统依赖安装（离线环境用）

.EXAMPLE
.\deploy-remote.ps1
首次部署 + 重复更新部署（等价于 Git Bash 下 ./deploy-remote.sh）

.EXAMPLE
.\deploy-remote.ps1 -PushEnv
用本机 .env 覆盖服务器上的 .env（等价于 ./deploy-remote.sh --push-env）

.EXAMPLE
.\deploy-remote.ps1 -WithData
部署并把本机测试数据同步到服务器（等价于 ./deploy-remote.sh --with-data）

.EXAMPLE
.\deploy-remote.ps1 -Server 172.16.52.12 -Port 8021 -SshUser ubuntu
部署到其他服务器/端口

.NOTES
若系统执行策略限制，运行方式：
  powershell -ExecutionPolicy Bypass -File .\deploy-remote.ps1
#>
param(
    [string]$Server = '172.16.52.11',
    [string]$Port = '8020',
    [string]$SshUser = 'root',
    [string]$AppDir = '/opt/zemail',
    [string]$ServiceUser = 'zemail',
    [switch]$PushEnv,
    [switch]$NoEnv,
    [switch]$WithData,
    [switch]$SkipApt
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'zemail'
$SrcDir = $PSScriptRoot
if (-not $SrcDir) { $SrcDir = (Get-Location).Path }   # 兼容直接粘贴到控制台执行
$SshDest = "$SshUser@$Server"
$StageDir = '/tmp/zemail-deploy-src'
$StageTarName = "zemail-deploy-$PID.tgz"
$StageTar = Join-Path $env:TEMP $StageTarName

function Invoke-Remote([string]$Command) {
    & ssh.exe -o ConnectTimeout=8 $SshDest $Command
    if ($LASTEXITCODE -ne 0) { throw "远程命令执行失败（退出码 $LASTEXITCODE）：$Command" }
}

Write-Host "==> 目标: http://$($Server):$Port  ($($SshDest):$AppDir)"

# ---------- 0. 依赖工具检查 ----------
foreach ($tool in 'ssh.exe', 'scp.exe', 'curl.exe') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "缺少 $tool（Windows 10 1803+ 自带，请检查系统）"
    }
}
# 必须用 Windows 自带的 bsdtar：Git 的 GNU tar 会把 C:\ 开头的输出路径当成「远程主机:路径」而失败
$TarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path $TarExe)) {
    if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) { throw '缺少 tar.exe（Windows 10 1803+ 自带，请检查系统）' }
    $TarExe = 'tar.exe'
}

# ---------- 1. 连通性 / sudo 检测 ----------
Write-Host '==> 检查 SSH 连接...'
$uid = & ssh.exe -o ConnectTimeout=8 $SshDest 'id -u'
if ($LASTEXITCODE -ne 0) { throw "无法通过 SSH 连接 $SshDest" }
if ("$($uid | Select-Object -First 1)".Trim() -eq '0') {
    $Sudo = ''
} else {
    $Sudo = 'sudo'
    Write-Host '    SSH 用户非 root，远程命令将使用 sudo（需免密 sudo 或允许交互输入密码）'
}
$SudoSpace = if ($Sudo) { "$Sudo " } else { '' }

# ---------- 2. 打包并上传代码 ----------
Write-Host "==> 打包本机代码: $StageTar"
if (Test-Path $StageTar) { Remove-Item $StageTar -Force }
& $TarExe -czf $StageTar `
    --exclude '.git' --exclude '.venv' --exclude 'venv' `
    --exclude 'data' --exclude '.env' `
    --exclude '.agents' --exclude '.claude' --exclude '.zcode' `
    --exclude 'wecom-auth-qr.png' `
    --exclude '__pycache__' --exclude '*.pyc' --exclude '*.log' `
    -C $SrcDir '.'
if ($LASTEXITCODE -ne 0) { throw "打包失败（退出码 $LASTEXITCODE）" }

Write-Host "==> 上传代码到 $($SshDest):$StageDir ..."
& scp.exe -q $StageTar "${SshDest}:/tmp/$StageTarName"
if ($LASTEXITCODE -ne 0) { throw '上传代码包失败' }
Invoke-Remote "rm -rf '$StageDir' && mkdir -p '$StageDir' && tar -xzf '/tmp/$StageTarName' -C '$StageDir'"
Invoke-Remote "rm -f '/tmp/$StageTarName'"
# 保险处理：万一 .sh 文件带了 Windows 行尾（CRLF），服务器上的 bash 会无法执行
Invoke-Remote "find '$StageDir' -name '*.sh' -exec sed -i 's/\r//' {} +"

# ---------- 3. 服务运行用户 ----------
if ($ServiceUser -ne 'root') {
    Write-Host "==> 确保服务用户 $ServiceUser 存在..."
    Invoke-Remote "id -u '$ServiceUser' >/dev/null 2>&1 || $SudoSpace useradd --system --home-dir '$AppDir' --create-home --shell /usr/sbin/nologin '$ServiceUser'"
}

# ---------- 4. 首次部署标记（以服务器上是否已有 .env 判断） ----------
& ssh.exe -o ConnectTimeout=8 $SshDest "test -f '$AppDir/.env'" | Out-Null
$FirstDeploy = ($LASTEXITCODE -ne 0)

# ---------- 5. 服务器端执行 deploy.sh ----------
Write-Host "==> 在服务器上执行 deploy.sh（端口 $Port）..."
$SkipAptArg = ''
if ($SkipApt) { $SkipAptArg = '--skip-apt' }
Invoke-Remote "cd '$StageDir' && $SudoSpace bash deploy.sh --dir '$AppDir' --port '$Port' --user '$ServiceUser' $SkipAptArg"

# ---------- 5.5 同步本机测试数据（-WithData） ----------
$PayloadDir = $null
if ($WithData) {
    Write-Host '==> 同步本机测试数据到服务器...'
    $PayloadDir = Join-Path $env:TEMP "zemail-data-$PID"
    if (Test-Path $PayloadDir) { Remove-Item $PayloadDir -Recurse -Force }
    New-Item -ItemType Directory -Path $PayloadDir | Out-Null

    # 收集数据目录（存在才带上）
    foreach ($d in 'emails', 'sessions', 'contacts', 'monitor') {
        $src = Join-Path $SrcDir "data\$d"
        if (Test-Path $src) { Copy-Item $src -Destination $PayloadDir -Recurse }
    }

    # users.json：拉取生产版本，与本机合并（同名用户以本机为准，生产独有用户保留）
    $LocalUsersFile = Join-Path $SrcDir 'data\users.json'
    $MergedUsersFile = $null
    if (Test-Path $LocalUsersFile) {
        & ssh.exe -o ConnectTimeout=8 $SshDest "test -f '$AppDir/data/users.json'"
        $ProdText = '{}'
        if ($LASTEXITCODE -eq 0) {
            $ProdText = (& ssh.exe -o ConnectTimeout=8 $SshDest "$SudoSpace cat '$AppDir/data/users.json'") -join "`n"
        }
        try { $ProdUsers = $ProdText | ConvertFrom-Json } catch { $ProdUsers = $null }
        if (-not $ProdUsers) { $ProdUsers = [pscustomobject]@{} }
        $LocalUsers = Get-Content -Raw -Encoding UTF8 $LocalUsersFile | ConvertFrom-Json
        foreach ($p in $LocalUsers.PSObject.Properties) {
            if ($ProdUsers.PSObject.Properties[$p.Name]) {
                $ProdUsers.PSObject.Properties[$p.Name].Value = $p.Value
            } else {
                $ProdUsers | Add-Member -MemberType NoteProperty -Name $p.Name -Value $p.Value
            }
        }
        $MergedUsersFile = Join-Path $PayloadDir 'users.json'
        # 必须无 BOM 写出：服务器端 Python 的 json.load 读到 BOM 会解析失败
        [System.IO.File]::WriteAllText($MergedUsersFile, ($ProdUsers | ConvertTo-Json -Depth 6),
            (New-Object System.Text.UTF8Encoding($false)))
        Write-Host ("    合并后用户: " + (($ProdUsers.PSObject.Properties.Name) -join ', '))
    }

    # 上传数据包，服务器端：备份 → 增量解包（不动 users.json）→ users.json 就位 → 归属修正
    $PayloadTar = Join-Path $env:TEMP "zemail-data-$PID.tgz"
    & $TarExe -czf $PayloadTar -C $PayloadDir '.'
    if ($LASTEXITCODE -ne 0) { throw "打包数据失败（退出码 $LASTEXITCODE）" }
    $PayloadTarName = Split-Path $PayloadTar -Leaf
    & scp.exe -q $PayloadTar "${SshDest}:/tmp/$PayloadTarName"
    if ($LASTEXITCODE -ne 0) { throw '上传数据包失败' }
    Invoke-Remote "mkdir -p '$StageDir/data-payload' && tar -xzf '/tmp/$PayloadTarName' -C '$StageDir/data-payload' && rm -f '/tmp/$PayloadTarName'"

    $BackupTag = Get-Date -Format 'yyyyMMdd-HHmmss'
    Invoke-Remote "$SudoSpace tar -czf /root/zemail-data-backup-$BackupTag.tar.gz -C '$AppDir' data 2>/dev/null || true"
    Invoke-Remote "$SudoSpace mkdir -p '$AppDir/data'"
    Invoke-Remote "$SudoSpace tar -C '$StageDir/data-payload' -c --exclude=./users.json . | $SudoSpace tar -C '$AppDir/data' -x"
    if ($MergedUsersFile) {
        Invoke-Remote "$SudoSpace cp '$StageDir/data-payload/users.json' '$AppDir/data/users.json' && $SudoSpace chmod 600 '$AppDir/data/users.json'"
    }
    Invoke-Remote "$SudoSpace chown -R '$($ServiceUser):$($ServiceUser)' '$AppDir/data'"
    Write-Host "    已备份服务器原数据: /root/zemail-data-backup-$BackupTag.tar.gz"

    # 服务已在运行则重启以加载新数据（首次部署由后续步骤统一启动）
    $SvcState = (& ssh.exe -o ConnectTimeout=8 $SshDest "$SudoSpace systemctl is-active $ServiceName") -join ''
    if ($SvcState.Trim() -eq 'active') {
        Write-Host '==> 重启服务加载新数据...'
        Invoke-Remote "$SudoSpace systemctl restart $ServiceName"
    }
}

# ---------- 6. .env 处理与启动 ----------
$LocalEnv = Join-Path $SrcDir '.env'
$NeedStart = $false
if ($FirstDeploy) {
    $NeedStart = $true   # 首次部署 deploy.sh 只生成模板不启动服务，由本脚本启动
    if (-not $NoEnv -and (Test-Path $LocalEnv)) {
        Write-Host '==> 服务器无 .env，上传本机配置...'
        & scp.exe -q $LocalEnv "${SshDest}:/tmp/zemail.env"
        if ($LASTEXITCODE -ne 0) { throw '上传 .env 失败' }
        Invoke-Remote "$SudoSpace mv /tmp/zemail.env '$AppDir/.env' && $SudoSpace chown '$($ServiceUser):$($ServiceUser)' '$AppDir/.env' && $SudoSpace chmod 600 '$AppDir/.env'"
    } else {
        $NeedStart = $false
        Write-Host ''
        Write-Host '=============================================='
        Write-Host ' 首次部署且未上传 .env：请先上服务器编辑配置'
        Write-Host "   $SudoSpace nano $AppDir/.env"
        Write-Host ' 然后启动：'
        Write-Host "   $SudoSpace systemctl start $ServiceName"
        Write-Host '=============================================='
    }
} elseif ($PushEnv -and (Test-Path $LocalEnv)) {
    Write-Host '==> -PushEnv：用本机 .env 覆盖服务器配置并重启...'
    & scp.exe -q $LocalEnv "${SshDest}:/tmp/zemail.env"
    if ($LASTEXITCODE -ne 0) { throw '上传 .env 失败' }
    Invoke-Remote "$SudoSpace mv /tmp/zemail.env '$AppDir/.env' && $SudoSpace chown '$($ServiceUser):$($ServiceUser)' '$AppDir/.env' && $SudoSpace chmod 600 '$AppDir/.env' && $SudoSpace systemctl restart $ServiceName"
}

if ($NeedStart) {
    Write-Host '==> 启动服务...'
    Invoke-Remote "$SudoSpace systemctl start $ServiceName"
}

# ---------- 7. 清理暂存目录 + 健康检查 ----------
Invoke-Remote "rm -rf '$StageDir'"
Remove-Item $StageTar -Force -ErrorAction SilentlyContinue
if ($PayloadDir) {
    Remove-Item $PayloadDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $env:TEMP "zemail-data-$PID.tgz") -Force -ErrorAction SilentlyContinue
}

Write-Host "==> 健康检查 http://$($Server):$Port/ ..."
$Code = ''
for ($i = 0; $i -lt 15; $i++) {
    $Code = "$(& curl.exe -s -o NUL -w '%{http_code}' --connect-timeout 5 "http://$($Server):$Port/")".Trim()
    if ($Code -eq '200') { break }
    Start-Sleep -Seconds 2
}

Write-Host ''
if ($Code -eq '200') {
    Write-Host "部署成功: http://$($Server):$Port"
    Write-Host '默认账号 admin / admin123，登录后请立即修改密码'
    Write-Host "查看日志: ssh $SshDest `"$SudoSpace journalctl -u $ServiceName -f`""
} else {
    Write-Host "健康检查未通过（HTTP $Code），请排查："
    Write-Host "  ssh $SshDest `"$SudoSpace systemctl status $ServiceName --no-pager`""
    Write-Host "  ssh $SshDest `"$SudoSpace journalctl -u $ServiceName -n 50 --no-pager`""
    exit 1
}
