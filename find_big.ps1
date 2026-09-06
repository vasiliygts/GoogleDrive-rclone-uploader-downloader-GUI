# Пошук найбільших файлів у сховищі rclone.
#
#   powershell -ExecutionPolicy Bypass -File find_big.ps1 gdrivepc: 5000M
#   powershell -ExecutionPolicy Bypass -File find_big.ps1 gdrive: 500M big.txt
#
# Третій аргумент необов'язковий — файл, куди зберегти результат.

param(
    [string]$Remote  = "gdrive:",
    [string]$MinSize = "500M",
    [string]$OutFile = ""
)

$rclone = Join-Path $PSScriptRoot "rclone.exe"
if (-not (Test-Path $rclone)) {
    $rclone = "rclone.exe"        # якщо лежить не поруч, шукаємо в PATH
}

Write-Host "Шукаю у $Remote файли від $MinSize …" -ForegroundColor Cyan
Write-Host "Обхід усього дерева триває довго — це нормально." -ForegroundColor DarkGray

# --tpslimit тримає нас нижче межі запитів: спільний client_id rclone
#   легко впирається в 403. --drive-skip-shortcuts прибирає зайві запити
#   на розкриття ярликів, на яких обхід часто й обривався.
# @() обов'язкове: на одному знайденому файлі Sort-Object поверне не масив,
# і в результату не буде властивості Count
$rows = @(& $rclone lsl $Remote `
    --min-size $MinSize `
    --fast-list `
    --tpslimit 10 --tpslimit-burst 10 `
    --drive-skip-shortcuts `
    --low-level-retries 20 2>$null |
    ForEach-Object {
        if ($_ -match '^\s*(\d+)\s+\S+\s+\S+\s+(.+)$') {
            [PSCustomObject]@{ Bytes = [int64]$Matches[1]; Path = $Matches[2] }
        }
    } | Sort-Object Bytes -Descending)

if (-not $rows) {
    Write-Host "Нічого не знайдено (або обхід обірвався)." -ForegroundColor Yellow
    return
}

$lines = $rows | ForEach-Object {
    if ($_.Bytes -ge 1GB) { "{0,9:N2} ГБ   {1}" -f ($_.Bytes / 1GB), $_.Path }
    else                  { "{0,9:N1} МБ   {1}" -f ($_.Bytes / 1MB), $_.Path }
}

$lines | ForEach-Object { Write-Host $_ }

$total = ($rows | Measure-Object -Property Bytes -Sum).Sum
Write-Host ""
Write-Host ("Знайдено файлів: {0}   разом {1:N2} ГБ" -f $rows.Count, ($total / 1GB)) -ForegroundColor Green

if ($OutFile) {
    $lines | Set-Content -Path $OutFile -Encoding utf8
    Write-Host "Збережено у $OutFile" -ForegroundColor Cyan
}
