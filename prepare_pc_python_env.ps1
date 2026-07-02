$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Toolchains = Join-Path $Root ".toolchains"
$Downloads = Join-Path $Toolchains "downloads"
$PythonInstaller = Join-Path $Downloads "python-3.12.10-amd64.exe"
$PythonDir = Join-Path $Toolchains "python312"
$VenvDir = Join-Path $Root ".venv-pc"
$PythonUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"

New-Item -ItemType Directory -Force -Path $Toolchains, $Downloads | Out-Null

if (-not (Test-Path $PythonInstaller)) {
    Write-Host "[download] $PythonUrl"
    Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonInstaller
} else {
    Write-Host "[skip] Already downloaded: $PythonInstaller"
}

if (-not (Test-Path (Join-Path $PythonDir "python.exe"))) {
    Write-Host "[install] Python 3.12.10 -> $PythonDir"
    $Args = "/quiet InstallAllUsers=0 TargetDir=`"$PythonDir`" Include_pip=1 Include_launcher=0 Include_test=0 PrependPath=0 Shortcuts=0 SimpleInstall=1"
    $Process = Start-Process -FilePath $PythonInstaller -ArgumentList $Args -Wait -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "Python installer failed with exit code $($Process.ExitCode)"
    }
}

$Python = Join-Path $PythonDir "python.exe"
if (-not (Test-Path $Python)) {
    $RegistryInstallPath = $null
    try {
        $RegistryInstallPath = (Get-Item "HKCU:\Software\Python\PythonCore\3.12\InstallPath").GetValue("")
    } catch {
        $RegistryInstallPath = $null
    }
    if ($RegistryInstallPath) {
        $RegistryPython = Join-Path $RegistryInstallPath "python.exe"
        if (Test-Path $RegistryPython) {
            $Python = $RegistryPython
            Write-Host "[info] Using registered Python install: $Python"
        }
    }
}
if (-not (Test-Path $Python)) {
    throw "Python 3.12 was installed, but python.exe could not be found."
}

Write-Host "[check] Python"
& $Python --version

if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
    Write-Host "[venv] Creating $VenvDir"
    & $Python -m venv $VenvDir
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
Write-Host "[pip] Upgrading pip"
& $VenvPython -m pip install --upgrade pip

Write-Host "[pip] Installing PC controller requirements"
& $VenvPython -m pip install -r (Join-Path $Root "requirements.txt")

Write-Host "[check] structured_light_pc_controller.py --help"
& $VenvPython (Join-Path $Root "structured_light_pc_controller.py") --help | Select-Object -First 20

Write-Host "[ok] PC Python environment is ready: $VenvDir"
