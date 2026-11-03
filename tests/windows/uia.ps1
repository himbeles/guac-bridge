# Use Windows' built-in UI Automation to inspect native WinUI controls cross-process.
param([long]$Window, [string]$Invoke)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Window)
$items = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition)
if ($Invoke) {
    foreach ($item in $items) {
        if ($item.Current.Name -eq $Invoke -and $item.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button) {
            $pattern = $item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $pattern.Invoke()
            exit 0
        }
    }
    throw "Button not found: $Invoke"
}
$items | ForEach-Object { $_.Current.Name }
