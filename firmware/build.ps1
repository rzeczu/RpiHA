# Buduje firmware dla Pico (RP2040) i Pico 2 (RP2350) narzedziami z C:\pico\tools.
# (plik celowo bez polskich znakow - Windows PowerShell 5.1 czyta .ps1 jako ANSI)
# Uzycie (zwykly PowerShell, z katalogu projektu):
#   powershell -ExecutionPolicy Bypass -File firmware\build.ps1
# Wynik: firmware\bin\rtgh_pico_rp2040.uf2, rtgh_pico2_rp2350.uf2 i rtgh_proxy_rp2040.uf2
$ErrorActionPreference = 'Stop'
$root  = 'C:\pico'
$src   = Split-Path -Parent $MyInvocation.MyCommand.Path
$work  = "$root\rtgh_pico"          # kopia zrodel w sciezce bez spacji i polskich znakow
$cmake = (Get-ChildItem "$root\tools\cmake" -Directory | Select-Object -First 1).FullName + '\bin'
$env:PATH = "$root\tools\armgcc\bin;$cmake;$root\tools\ninja;$env:PATH"

# Host TinyUSB zaklada, ze po naglowku konfiguracji jest od razu interfejs. Gitara ma tam
# deskryptor OTG (03 09 00) - latka pomija takie deskryptory (potrzebne dla proxy).
$usbh = "$root\pico-sdk\lib\tinyusb\src\host\usbh.c"
$txt = [IO.File]::ReadAllText($usbh)
if ($txt -notmatch 'RTGH patch') {
    $anchor = '    uint8_t assoc_itf_count = 1;'
    if ($txt.IndexOf($anchor) -lt 0) { throw 'usbh.c: nie znaleziono miejsca na latke OTG' }
    $patch = "    // RTGH patch: pomin deskryptory przed interfejsem (np. OTG)`n" +
             "    if (tu_desc_type(p_desc) != TUSB_DESC_INTERFACE && tu_desc_type(p_desc) != TUSB_DESC_INTERFACE_ASSOCIATION) {`n" +
             "      p_desc = tu_desc_next(p_desc);`n" +
             "      continue;`n" +
             "    }`n`n"
    $i = $txt.IndexOf($anchor)
    $txt = $txt.Substring(0, $i) + $patch + $txt.Substring($i)
    [IO.File]::WriteAllText($usbh, $txt)
    Write-Host 'usbh.c: latka OTG nalozona'
}

& py -3.13 (Join-Path (Split-Path $src) 'gen_firmware_header.py')
if ($LASTEXITCODE -ne 0) { throw 'gen_firmware_header.py failed' }

if (Test-Path $work) { Remove-Item $work -Recurse -Force }
Copy-Item $src $work -Recurse -Exclude 'bin'
New-Item -ItemType Directory -Force "$src\bin" | Out-Null

foreach ($b in @(@{board='pico'; out='rtgh_pico_rp2040.uf2'; proxy='rtgh_proxy_rp2040.uf2'},
                 @{board='pico2'; out='rtgh_pico2_rp2350.uf2'; proxy=$null})) {
    $bd = "$root\build\$($b.board)"
    & cmake -S $work -B $bd -G Ninja "-DPICO_SDK_PATH=$root/pico-sdk" "-DPICO_BOARD=$($b.board)" `
        "-DPICO_TOOLCHAIN_PATH=$root/tools/armgcc" "-Dpicotool_DIR=$root/tools/picotool/picotool" `
        "-Dpioasm_DIR=$root/tools/sdktools/pioasm" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cmake configure ($($b.board)) failed" }
    & cmake --build $bd
    if ($LASTEXITCODE -ne 0) { throw "build ($($b.board)) failed" }
    Copy-Item "$bd\rtgh_pico.uf2" "$src\bin\$($b.out)" -Force
    Write-Host "OK: firmware\bin\$($b.out)"
    if ($b.proxy) {
        Copy-Item "$bd\rtgh_proxy.uf2" "$src\bin\$($b.proxy)" -Force
        Write-Host "OK: firmware\bin\$($b.proxy)"
    }
}
