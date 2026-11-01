' Start Tejas — double-click to launch the dashboard (no console window).
' Starts the local web server hidden; it auto-opens your browser to the UI.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

' =============================================================================
' [MODIFIED 2026-11-01]: Virtualenv pythonw path resolution
' ORIGINAL INTENT: Ran pythonw / python from system PATH.
' REASON FOR CHANGE: System Python lacks required dependencies (joblib, lightgbm).
' SAFE ROLLBACK: Preserved original implementation below as comments.
' -----------------------------------------------------------------------------
' [PREVIOUS CODE - PRESERVED]:
' On Error Resume Next
' sh.Run "pythonw """ & dir & "\webui.py""", 0, False
' If Err.Number <> 0 Then
'     sh.Run "python """ & dir & "\webui.py""", 0, False
' End If
' -----------------------------------------------------------------------------
' [NEW CODE]:
Dim pywPath
pywPath = dir & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pywPath) Then
    pywPath = dir & "\.venv\Scripts\python.exe"
End If

If fso.FileExists(pywPath) Then
    sh.Run """" & pywPath & """ """ & dir & "\webui.py""", 0, False
Else
    On Error Resume Next
    sh.Run "pythonw """ & dir & "\webui.py""", 0, False
    If Err.Number <> 0 Then
        sh.Run "python """ & dir & "\webui.py""", 0, False
    End If
End If
' =============================================================================
