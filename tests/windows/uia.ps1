# Use Windows' built-in UI Automation to inspect native WinUI controls cross-process.
param([long]$Window, [string]$Invoke, [string]$Screenshot)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Window)
if ($Screenshot) {
    Add-Type -AssemblyName System.Drawing
    $rect = $root.Current.BoundingRectangle
    $image = New-Object System.Drawing.Bitmap ([int]$rect.Width), ([int]$rect.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($image)
    $graphics.CopyFromScreen([int]$rect.X, [int]$rect.Y, 0, 0, $image.Size)
    $image.Save($Screenshot, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose()
    $image.Dispose()
    exit 0
}
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
