[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EventPath,
    [Parameter(Mandatory = $true)]
    [string]$ReadyPath,
    [Parameter(Mandatory = $true)]
    [string]$Title,
    [Parameter(Mandatory = $false)]
    [string]$RunId = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false

$readyParent = Split-Path -Parent $ReadyPath
New-Item -ItemType Directory -Force -Path $readyParent | Out-Null
$startingJson = '{"kind":"starting","title":"' + $Title.Replace('"', '\"') + '"}'
[System.IO.File]::WriteAllText($ReadyPath, $startingJson, $utf8NoBom)
$readyWritten = $false

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [object]$Value
    )
    $json = $Value | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($Path, $json, $utf8NoBom)
}

function Append-JsonLine {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [object]$Value
    )
    $json = $Value | ConvertTo-Json -Compress
    [System.IO.File]::AppendAllText(
        $Path,
        $json + "`n",
        $utf8NoBom
    )
}

$form = New-Object System.Windows.Forms.Form
$form.Name = "RelayComputerUseFixture"
$form.Text = $Title
$form.Width = 520
$form.Height = 260
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ShowInTaskbar = $true

$header = New-Object System.Windows.Forms.Label
$header.Name = "FixtureHeader"
$header.Text = "Agent Relay Computer Use Windows fixture"
$header.AccessibleName = "Fixture header"
$header.Location = New-Object System.Drawing.Point(24, 20)
$header.Size = New-Object System.Drawing.Size(450, 28)

$inputLabel = New-Object System.Windows.Forms.Label
$inputLabel.Name = "FixtureInputLabel"
$inputLabel.Text = "Name"
$inputLabel.AccessibleName = "Name label"
$inputLabel.Location = New-Object System.Drawing.Point(24, 70)
$inputLabel.Size = New-Object System.Drawing.Size(120, 24)

$input = New-Object System.Windows.Forms.TextBox
$input.Name = "FixtureInput"
$input.AccessibleName = "Name"
$input.Location = New-Object System.Drawing.Point(160, 66)
$input.Size = New-Object System.Drawing.Size(300, 28)
$input.TabIndex = 0

$submit = New-Object System.Windows.Forms.Button
$submit.Name = "SubmitButton"
$submit.Text = "Apply"
$submit.AccessibleName = "Apply"
$submit.Location = New-Object System.Drawing.Point(160, 112)
$submit.Size = New-Object System.Drawing.Size(120, 32)
$submit.TabIndex = 1
$submit.UseVisualStyleBackColor = $true

$status = New-Object System.Windows.Forms.Label
$status.Name = "FixtureStatus"
$status.Text = "Status: idle"
$status.AccessibleName = "Fixture status"
$status.Location = New-Object System.Drawing.Point(24, 170)
$status.Size = New-Object System.Drawing.Size(450, 28)

$form.Controls.Add($header)
$form.Controls.Add($inputLabel)
$form.Controls.Add($input)
$form.Controls.Add($submit)
$form.Controls.Add($status)
$form.AcceptButton = $submit

$submit.Add_Click({
    $value = $form.Controls["FixtureInput"].Text
    if ([string]::IsNullOrWhiteSpace($RunId)) {
        Append-JsonLine -Path $EventPath -Value @{
            kind = "submit"
            value = $value
        }
    }
    else {
        Append-JsonLine -Path $EventPath -Value ([ordered]@{
            run_id = $RunId
            event = "applied"
            value = $value
        })
    }
    $status.Text = "Status: submitted"
})

try {
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
    Write-JsonFile -Path $ReadyPath -Value @{
        kind = "ready"
        title = $Title
    }
    $readyWritten = $true
    [System.Windows.Forms.Application]::Run($form)
}
finally {
    if (-not $readyWritten) {
        Write-JsonFile -Path $ReadyPath -Value @{
            kind = "failed"
            title = $Title
        }
    }
}
