Unicode True
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetDateSave on
SetDatablockOptimize on

!define APP_NAME "Smart Photo Triage"
!define APP_VERSION "1.2.1"
!define APP_EXE "Smart Photo Triage.exe"
!define UNINSTALL_KEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Smart Photo Triage"

!ifndef OUTPUT_FILE
!define OUTPUT_FILE "Smart-Photo-Triage-Setup-${APP_VERSION}-candidate.exe"
!endif

Name "${APP_NAME}"
OutFile "..\\release\\installer\\${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\\Programs\\Smart Photo Triage"
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
BrandingText "Smart Photo Triage"

Page directory
Page components
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

Section "Smart Photo Triage" Core
  SectionIn RO
  SetOutPath "$INSTDIR"
  File /r "..\\release\\dist\\Smart Photo Triage\\*"
  WriteUninstaller "$INSTDIR\\Uninstall Smart Photo Triage.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\\Uninstall Smart Photo Triage.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" '"$INSTDIR\\${APP_EXE}"'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  CreateDirectory "$SMPROGRAMS\\Smart Photo Triage"
  CreateShortCut "$SMPROGRAMS\\Smart Photo Triage\\Smart Photo Triage.lnk" "$INSTDIR\\${APP_EXE}"
  CreateShortCut "$SMPROGRAMS\\Smart Photo Triage\\Uninstall Smart Photo Triage.lnk" "$INSTDIR\\Uninstall Smart Photo Triage.exe"
SectionEnd

Section /o "创建桌面快捷方式" DesktopShortcut
  CreateShortCut "$DESKTOP\\Smart Photo Triage.lnk" "$INSTDIR\\${APP_EXE}"
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\\Smart Photo Triage.lnk"
  RMDir /r "$SMPROGRAMS\\Smart Photo Triage"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  RMDir /r "$INSTDIR"
SectionEnd
