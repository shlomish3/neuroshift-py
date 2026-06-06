param(
    [string]$TemplatePath = "$PSScriptRoot\..\templates\neuroshift_template.xlsm",
    [string]$ModulePath = "$PSScriptRoot\..\Module2.bas"
)

$ErrorActionPreference = "Stop"

$template = Resolve-Path -LiteralPath $TemplatePath
$module = Resolve-Path -LiteralPath $ModulePath

$templateItem = Get-Item -LiteralPath $template.Path
$moduleItem = Get-Item -LiteralPath $module.Path
$scriptItem = Get-Item -LiteralPath $PSCommandPath
$sourceStamp = $moduleItem.LastWriteTimeUtc
if ($scriptItem.LastWriteTimeUtc -gt $sourceStamp) {
    $sourceStamp = $scriptItem.LastWriteTimeUtc
}

if ($templateItem.LastWriteTimeUtc -ge $sourceStamp) {
    Write-Host "VBA template already current."
    exit 0
}

function Test-ExcelVbaProjectTrust {
    $officeRoot = "HKCU:\Software\Microsoft\Office"
    try {
        foreach ($versionKey in Get-ChildItem -LiteralPath $officeRoot -ErrorAction SilentlyContinue) {
            $securityKey = Join-Path $versionKey.PSPath "Excel\Security"
            $props = Get-ItemProperty -LiteralPath $securityKey -ErrorAction SilentlyContinue
            if ($null -ne $props -and $props.AccessVBOM -eq 1) {
                return $true
            }
        }
    }
    catch {
        return $false
    }
    return $false
}

function Set-MonthSheetEventCode {
    param(
        [object]$Workbook,
        [object]$Components
    )

    $monthSheet = $null
    foreach ($sheet in $Workbook.Worksheets) {
        if ($sheet.Name -match '^\d{4}-\d{2}$') {
            $monthSheet = $sheet
            break
        }
    }

    if ($null -eq $monthSheet) {
        throw "Could not find the template month sheet for worksheet event wiring."
    }

    $component = $Components.Item($monthSheet.CodeName)
    $codeModule = $component.CodeModule
    if ($codeModule.CountOfLines -gt 0) {
        $codeModule.DeleteLines(1, $codeModule.CountOfLines)
    }

    $eventCode = @'
Private Sub Worksheet_Change(ByVal Target As Range)
    If Intersect(Target, Me.Range("B:H")) Is Nothing Then Exit Sub
    RecolorDuplicateNamesAllForSheet Me
End Sub

Private Sub Worksheet_Calculate()
    Static busy As Boolean
    If busy Then Exit Sub
    busy = True
    RecolorDuplicateNamesAllForSheet Me
    busy = False
End Sub
'@

    $codeModule.AddFromString($eventCode)
}

if (-not (Test-ExcelVbaProjectTrust)) {
    Write-Host "Excel VBA project access registry flag was not found; trying Excel directly..." -ForegroundColor Yellow
}

$excel = $null
$workbook = $null

try {
    $excel = New-Object -ComObject Excel.Application
    $excel.Visible = $false
    $excel.DisplayAlerts = $false
    $excel.AutomationSecurity = 3

    $workbook = $excel.Workbooks.Open($template.Path)

    $vbProject = $workbook.VBProject
    if ($null -eq $vbProject) {
        throw "Excel did not expose the VBA project. In Excel, enable Trust Center > Macro Settings > Trust access to the VBA project object model, then rerun."
    }

    $components = $vbProject.VBComponents
    if ($null -eq $components) {
        throw "Excel did not expose VBA components. In Excel, enable Trust Center > Macro Settings > Trust access to the VBA project object model, then rerun."
    }

    for ($i = $components.Count; $i -ge 1; $i--) {
        $component = $components.Item($i)
        $removeComponent = $false

        # Type 1 is a standard VBA module. Do not remove sheet/workbook modules.
        if ($component.Type -eq 1) {
            if ($component.Name -like "Module2*") {
                $removeComponent = $true
            }
            else {
                $codeModule = $component.CodeModule
                if ($null -ne $codeModule -and $codeModule.CountOfLines -gt 0) {
                    $code = $codeModule.Lines(1, $codeModule.CountOfLines)
                    if (
                        $code -match "RecolorDuplicateNamesAllForSheet" -or
                        $code -match "ColorTokensInCell" -or
                        $code -match "ColorDuplicateTokensInCell" -or
                        $code -match "AnyDupToken"
                    ) {
                        $removeComponent = $true
                    }
                }
            }
        }

        if ($removeComponent) {
            $components.Remove($component)
        }
    }

    $components.Import($module.Path) | Out-Null
    Set-MonthSheetEventCode -Workbook $workbook -Components $components
    $workbook.Save()
    Write-Host "Updated template VBA from Module2.bas."
}
catch {
    Write-Host "VBA template update skipped." -ForegroundColor Yellow
    Write-Host "Reason: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "To update duplicate-name coloring macros automatically, enable in Excel:" -ForegroundColor Yellow
    Write-Host "Trust Center > Macro Settings > Trust access to the VBA project object model" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}
finally {
    if ($workbook -ne $null) {
        $workbook.Close($true) | Out-Null
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($workbook)
    }
    if ($excel -ne $null) {
        $excel.Quit() | Out-Null
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel)
    }
}
