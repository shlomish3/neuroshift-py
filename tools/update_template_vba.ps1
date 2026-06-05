param(
    [string]$TemplatePath = "$PSScriptRoot\..\templates\neuroshift_template.xlsm",
    [string]$ModulePath = "$PSScriptRoot\..\Module2.bas"
)

$ErrorActionPreference = "Stop"

$template = Resolve-Path -LiteralPath $TemplatePath
$module = Resolve-Path -LiteralPath $ModulePath

$excel = $null
$workbook = $null

try {
    $excel = New-Object -ComObject Excel.Application
    $excel.Visible = $false
    $excel.DisplayAlerts = $false

    $workbook = $excel.Workbooks.Open($template.Path)
    $components = $workbook.VBProject.VBComponents

    for ($i = $components.Count; $i -ge 1; $i--) {
        $component = $components.Item($i)
        if ($component.Name -eq "Module2") {
            $components.Remove($component)
        }
    }

    $components.Import($module.Path) | Out-Null
    $workbook.Save()
    Write-Host "Updated template VBA from Module2.bas."
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
