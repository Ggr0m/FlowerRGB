$wsh = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$startup = [Environment]::GetFolderPath('Startup')

$pythonw = (Get-Command python.exe).Source -replace 'python\.exe$', 'pythonw.exe'
$workDir = (Get-Item .).FullName
$script = Join-Path $workDir 'main.py'
$icon = Join-Path $workDir 'app_icon.ico'

# Remove old shortcuts
Remove-Item (Join-Path $desktop 'Smart Life RGB.lnk') -ErrorAction SilentlyContinue
Remove-Item (Join-Path $workDir 'Smart Life RGB.lnk') -ErrorAction SilentlyContinue
Remove-Item (Join-Path $startup 'Smart Life RGB.lnk') -ErrorAction SilentlyContinue

# 1. Desktop shortcut
$s1 = $wsh.CreateShortcut((Join-Path $desktop 'flowerRGB.lnk'))
$s1.TargetPath = $pythonw
$s1.Arguments = "`"$script`""
$s1.WorkingDirectory = $workDir
$s1.IconLocation = "$icon, 0"
$s1.Description = 'flowerRGB — Управление подсветкой'
$s1.Save()

# 2. Project folder shortcut (for pinning to taskbar)
$s2 = $wsh.CreateShortcut((Join-Path $workDir 'flowerRGB.lnk'))
$s2.TargetPath = $pythonw
$s2.Arguments = "`"$script`""
$s2.WorkingDirectory = $workDir
$s2.IconLocation = "$icon, 0"
$s2.Description = 'flowerRGB — Управление подсветкой'
$s2.Save()

# 3. Windows Startup folder shortcut (autorun with Windows immediately on boot)
$s3 = $wsh.CreateShortcut((Join-Path $startup 'flowerRGB.lnk'))
$s3.TargetPath = $pythonw
$s3.Arguments = "`"$script`""
$s3.WorkingDirectory = $workDir
$s3.IconLocation = "$icon, 0"
$s3.Description = 'flowerRGB — Управление подсветкой'
$s3.Save()

Write-Host "All shortcuts created successfully!"
