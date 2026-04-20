<#
tools/push_windows_branch.ps1

Helper script to create a `windows-automation/*` branch, commit the Windows changes, push to origin
and optionally create a Draft PR using the GitHub CLI (`gh`). Designed to be run by an automation agent or a developer.

Usage examples:
  # Create and push a branch (interactive commit message)
  pwsh .\tools\push_windows_branch.ps1 -CreatePR

  # Provide a branch name and create draft PR
  pwsh .\tools\push_windows_branch.ps1 -BranchName "windows-automation/add-windows-ci" -CreatePR -Title "WIP: Windows support"

Requirements:
 - Git must be installed and the repo must have an "origin" remote.
 - If using -CreatePR, the GitHub CLI (`gh`) should be installed and authenticated.
 - Run this from the repository root.
#>

param(
    [string]$BranchName = '',
    [switch]$CreatePR,
    [string]$Title = 'WIP: Windows support automation',
    [string]$Body = "Automated branch containing Windows docs, CI workflows and helper scripts.",
    [string]$Remote = 'origin',
    [switch]$Force
)

function Abort([string]$msg){ Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# Ensure we are at repo root
if (-not (Test-Path .git)) { Abort "This script must be run from the repository root (where .git exists)." }

# Ensure branch name
if (-not $BranchName) {
    $now = Get-Date -Format 'yyyyMMdd-HHmmss'
    $BranchName = "windows-automation/$now"
}

Write-Host "Preparing branch: $BranchName"

# Check git status clean unless forced
$st = git status --porcelain
if ($st -and -not $Force) {
    Write-Host "Git working tree is not clean. Run with -Force to proceed anyway or commit your local changes first." -ForegroundColor Yellow
    git status --porcelain
    exit 2
}

# Create branch
Write-Host "Creating branch $BranchName..."
git checkout -b $BranchName
if ($LASTEXITCODE -ne 0) { Abort "Failed to create branch $BranchName" }

# Files to stage (keep list explicit to avoid accidental unrelated changes)
$files = @(
    'docs/windows.rst',
    'docs/windows.md',
    'start_synth.ps1',
    '.github/workflows/windows-ci.yml',
    '.github/workflows/pr-guard.yml',
    '.github/workflows/auto-draft-pr.yml',
    'tools/scan_platform_issues.py',
    'tools/push_windows_branch.ps1',
    'docs/index.rst'
)

# Stage files that exist
$toAdd = @()
foreach ($f in $files) {
    if (Test-Path $f) { $toAdd += $f }
}
if ($toAdd.Count -eq 0) { Abort "No files to add; ensure you run this from the repo with the prepared changes." }

Write-Host "Staging files:`n  $($toAdd -join "`n  ")"
git add -- $toAdd
if ($LASTEXITCODE -ne 0) { Abort "git add failed" }

# If docs/windows.md exists and we prefer to remove it to keep RST canonical, remove it
if (Test-Path 'docs/windows.md') {
    Write-Host "Removing docs/windows.md (RST canonicalization)"
    git rm --quiet docs/windows.md
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Warning: failed to git rm docs/windows.md" -ForegroundColor Yellow
    }
}

# Commit
$commitMsg = "WIP: Windows native support - docs, CI, helper scripts"
Write-Host "Committing with message: $commitMsg"
git commit -m "$commitMsg"
if ($LASTEXITCODE -ne 0) { Abort "git commit failed (nothing to commit or an error occurred)" }

# Push
Write-Host "Pushing branch to $Remote/$BranchName"
git push -u $Remote $BranchName
if ($LASTEXITCODE -ne 0) { Abort "git push failed" }

# Optionally create a draft PR using gh
if ($CreatePR) {
    Write-Host "Attempting to create a Draft PR using GitHub CLI (gh)..."
    $ghPath = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $ghPath) {
        Write-Host "gh CLI not found. Skipping PR creation. You can run: gh pr create --draft --title \"$Title\" --body \"$Body\" --base main" -ForegroundColor Yellow
    } else {
        & gh pr create --draft --title "$Title" --body "$Body" --base main
        if ($LASTEXITCODE -ne 0) {
            Write-Host "gh pr create returned non-zero exit code" -ForegroundColor Yellow
        }
    }
}

Write-Host "Branch pushed successfully. Draft PR created? $CreatePR"
Write-Host "Done. You can now open the branch in GitHub or run the auto-draft PR workflow if configured." -ForegroundColor Green
