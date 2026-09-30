Unicode true
!include "MUI2.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
Name "OceanRoute 0.12.1"
OutFile "../../outputs/releases/OceanRoute-0.12.1-Windows-x64-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\OceanRoute"
InstallDirRegKey HKCU "Software\OceanRoute" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
VIProductVersion "0.12.1.0"
VIAddVersionKey /LANG=2052 "ProductName" "OceanRoute"
VIAddVersionKey /LANG=2052 "FileDescription" "OceanRoute 0.12.1 离线安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "0.12.1"
VIAddVersionKey /LANG=2052 "LegalCopyright" "Independent OceanRoute implementation"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\OceanRoute.exe"
!define MUI_FINISHPAGE_RUN_TEXT "启动 OceanRoute"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "此安装包需要 64 位 Windows 10 / Windows 11（x64）。"
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "此版本需要 Windows 10 或 Windows 11。"
    Abort
  ${EndIf}
  ReadRegStr $0 HKCU "Software\OceanRoute" "InstallDir"
  IfFileExists "$0\OceanRoute.exe" 0 done
  ExecWait '"$0\OceanRoute.exe" --stop' $1
  IntCmp $1 0 done
  MessageBox MB_ICONSTOP "请先关闭正在运行的 OceanRoute，再重新安装。"
  Abort
  done:
FunctionEnd

Section "OceanRoute"
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  File /r /x "__pycache__" /x "*.pyc" "payload-0.12.1/*"
  ; .onInit has waited for the old owner to stop. Remove only audited old
  ; application-owned paths; empty-only RMDir preserves unrelated user files.
  !include "upgrade-owned-0.12.nsh"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\OceanRoute"
  CreateShortcut "$SMPROGRAMS\OceanRoute\OceanRoute.lnk" "$INSTDIR\OceanRoute.exe"
  CreateShortcut "$SMPROGRAMS\OceanRoute\关闭 OceanRoute.lnk" "$INSTDIR\OceanRoute.exe" "--stop"
  CreateShortcut "$SMPROGRAMS\OceanRoute\用户手册.lnk" "$INSTDIR\documents\OceanRoute_用户手册_0.12.1.pdf"
  CreateShortcut "$SMPROGRAMS\OceanRoute\卸载.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortcut "$DESKTOP\OceanRoute.lnk" "$INSTDIR\OceanRoute.exe"
  WriteRegStr HKCU "Software\OceanRoute" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OceanRoute" "DisplayName" "OceanRoute 0.12.1"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OceanRoute" "DisplayVersion" "0.12.1"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OceanRoute" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OceanRoute" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OceanRoute" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OceanRoute" "NoRepair" 1
SectionEnd

Section "Uninstall"
  ExecWait '"$INSTDIR\OceanRoute.exe" --stop' $0
  IntCmp $0 0 continue
  MessageBox MB_ICONSTOP "OceanRoute 未关闭。请关闭后重新卸载。"
  Abort
  continue:
  SetShellVarContext current
  Delete "$DESKTOP\OceanRoute.lnk"
  RMDir /r "$SMPROGRAMS\OceanRoute"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OceanRoute"
  DeleteRegKey HKCU "Software\OceanRoute"
  !include "uninstall-files-0.12.1.nsh"
  ; User projects/logs remain in LOCALAPPDATA\OceanRoute, outside the install directory.
SectionEnd
