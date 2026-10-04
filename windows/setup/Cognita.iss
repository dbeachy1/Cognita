; Cognita Setup (Inno Setup 6.3 or later). Design: docs/DESIGN-WINDOWS-INSTALLER.md sections 4, 5.6, 7, 8, 9, 11.
;
; Build it with windows\build_setup.py, which passes these /D defines (all required):
;   Version, Revision                 the Cognita version and this Setup build's revision
;   ImagePath, ImageSha256, ImageSize the Linux image tarball (cognita-wsl-<v>.tar.gz)
;   SrcPath, SrcSha256, SrcSize       the source tarball for updates (cognita-src-<v>.tar.gz)
;   SizeCognitaCpu, SizeWorkspaceRuntime, SizeToolbox   compressed container image sizes (bytes)
;   SizeCognitaNvidia                 the NVIDIA Cognita image's compressed size (bytes); 0 = this build has no
;                                     NVIDIA image (design 22.7), so the Acceleration page offers no GPU choice
;   LauncherExe                      the compiled cognita.exe (windows\launcher\cognita.cs)
;   WindowsDir                        the folder holding CognitaWin.ps1, cognita.ps1, launch-keepalive.vbs
;   OutputDir                         where Cognita-Setup-<version>-r<Revision>.exe is written
;
; THE HELPER CALL CONTRACT THIS FILE ASSUMES (windows\CognitaWin.ps1, docs section 5.1 and 5.2).
; Section 5.2's verb table names the verbs and result keys but not every command-line option, and
; section 7.2 does not name the `state` keys that carry the saved values. This file therefore uses
; the following; each is built in ONE place (the Args* functions and State* readers below) so a
; rename in the helper is a one-line change here.
;   state                                     result keys (design 18.2): installed (1 iff the distro is ours AND
;                                             the Linux install completed), state (new|import-pending|installed|
;                                             uninstalled, design 19.4 item 23; `none` when there is no settings
;                                             file at all. The older list none|installing|installed is superseded),
;                                             distro (present|absent), owned (0 = a distro of
;                                             that name that is not ours), linux_version (the Setup version
;                                             that last finished cognita install/update), admin_user, resume,
;                                             running, plus, for the saved-value pages
;                                             and the uninstall form: root1, mcp_port, admin_port, workspace,
;                                             vhd_dir, vhd_bytes (all optional; a missing key falls back to a
;                                             default), and funnel_port (design 19.2 item 8: the public HTTPS
;                                             port of the Funnel Setup turned on; absent or empty when none is
;                                             recorded; ReadState reads it into StFunnelPort). Design 19.11 R7:
;                                             reported only while Tailscale still serves that Funnel (the
;                                             helper clears a stale record first). The install refusal
;                                             port-in-use-by-funnel reports the old MCP port as
;                                             funnel_target, never as funnel_port (R6). Design 22.4: acceleration
;                                             (cpu|amd|nvidia, or empty when unknown: an install from before 15.1;
;                                             ReadState reads it into StAcceleration).
;                                             Setup picks ONE mode from them (PickMode):
;                                               fresh   no owned distro
;                                               finish  owned distro, installed=0: every page as fresh, but the
;                                                       data location is fixed to vhd_dir; image AND source
;                                                       tarball are always unpacked and passed
;                                               repair  installed=1, state not `uninstalled`, linux_version =
;                                                       this Setup's version: folder, user and data location
;                                                       read-only, ONE password (Setup uses it to check Cognita
;                                                       afterwards); --admin-user is NOT passed
;                                               update  installed=1, state not `uninstalled`, another version:
;                                                       as repair, but the update verb with --src
;                                               reinstall  installed=1 AND state=uninstalled (after a keep-data
;                                                       uninstall; whatever the version): as repair (the Ready page
;                                                       says "Reinstall Cognita <v> (your data is kept)"), and the
;                                                       install verb with --src, NEVER update (the helper refuses
;                                                       it: the Linux side removed its releases)
;   Result values are percent-encoded by the helper (%25 for '%', %3B for ';'); ResultValue decodes
;   them, in that one place (design 18.5).
;   check --phase preflight|final [--data-dir P --mcp-port N --admin-port N --disk-bytes N]
;                                             The preflight result also carries (design 22.2 and 22.9; CheckPreflight
;                                             reads them): nvidia=none|ok|old (ok = a card with driver 580 or newer,
;                                             or a card whose driver could not be read; old = every card seen has a
;                                             readable older driver), nvidia_name=<text>, nvidia_driver=<major.minor
;                                             or empty>, wsl_reclaim=set|unset|unreadable (an autoMemoryReclaim key
;                                             in .wslconfig). They are not warnings and never fail the check.
;   wsl-install
;   restart-for-wsl --setup-exe <path to this Setup.exe> [--now --after-pid <this Setup's process id>]
;                                             (design 19.11 R1) without --now it only records RunOnce and the
;                                             resume marker (the install flow: Inno's own Finished page then
;                                             offers the restart, through NeedRestart). With --now --after-pid
;                                             the helper starts a detached hidden powershell that waits for
;                                             that process to exit and then runs `shutdown /r /t 0`: a running
;                                             Setup would refuse a non-forced restart. Only the WSL page's
;                                             "Restart now" uses it.
;   roots --validate PATH [--data-dir P]      an invalid folder answers result=ok;ok=0;reason=<text> (exit 0);
;                                             `failed` is an internal error only. Setup also runs it from
;                                             Advanced with the new data location (design 19.7 item 16)
;   password-broker --in <pipe A> --out <pipe B>   (section 5.6; started with ewNoWait)
;   install --projects-folder P [--admin-user U] --workspace on|off --mcp-port N --admin-port N
;           --data-dir P [--image P --image-sha256 H] [--src P --src-sha256 H]
;           --setup-version V --setup-revision R --password-pipe B
;           (every install run passes --src; fresh and finish also pass --image; repair and reinstall
;           pass no --admin-user and no --image)
;           [--acceleration cpu|nvidia] [--wsl-memory-reclaim]   (design 22.3, 22.7, 22.9, 22.12 item 1:
;           --acceleration is passed by AccelArg: always on fresh and finish (cpu when the Acceleration page is
;           hidden), on repair and reinstall only when the page was shown and the user changed the radio or the
;           install is known to run on cpu or nvidia, never on update; absent = the Linux side keeps what its env
;           file says. --wsl-memory-reclaim only when the Ready page's box was shown and ticked.)
;           The result carries acceleration=<cpu|amd|nvidia> (may be absent): the Finished page's line comes only
;           from it (design 22.12 item 11). A warning of stage `acceleration` is the Linux side dropping the GPU;
;           OnHelperLine adds "To try the GPU again, run Setup again and choose NVIDIA." when it has no fix.
;           Failure reasons Setup shows like any other failure (progress message and fix): folder-not-first
;           (a folder other than root 1 once root 1 exists: "Cognita already uses <root1> as its projects
;           folder."), distro-not-ready (update, start), port-in-use-by-funnel (a new MCP port while a Funnel
;           targets the old one; design 19.2 item 8), older-setup (update refuses a downgrade; design 19.2 item 6).
;   update  --src P --src-sha256 H --setup-version V --setup-revision R --disk-bytes N --password-pipe B
;           [--wsl-memory-reclaim]                (design 22.9; no --acceleration: an update never changes it);
;           the result carries acceleration=<cpu|amd|nvidia> (may be absent), as install's does
;   remote-access --yes [--funnel-port N] --password-pipe B
;                                             a failure may carry link=<url> (reason=funnel-not-enabled):
;                                             Setup shows the link and Retry, which hands the password over
;                                             again (new broker, new pipes) and reruns the verb (design 18.4).
;                                             Any http(s):// address in a progress `message` is clickable
;                                             while it is the latest message.
;   diagnostics --setup-log <Setup's own {log} path>
;   uninstall --keep-data | --delete-data     a delete-data result carries logs_copy=<folder in %TEMP%> when logs
;                                             were copied. A delete-data run whose `wsl --unregister` failed ends
;                                             result=failed with state ALREADY saved as uninstalled (design 19.4
;                                             item 9): the uninstaller then says "Cognita was removed, but its
;                                             data could not be deleted. Logs: <path>"
; Every verb prints JSON progress lines and ends with `result=ok|failed|restart-required;k=v;...`
; and exits 0, 1 or 3010 (section 5.1).
; Setup's own log ({log}) is copied to %LOCALAPPDATA%\Cognita\logs\setup-<yyyyMMdd-HHmmss>.log in
; DeinitializeSetup, and the uninstaller's to uninstall-<stamp>.log at the end of an uninstall
; (design 18.5; after a delete-data uninstall it goes next to the helper's %TEMP% copy instead).

#ifndef Version
  #error Version is not defined. Build with windows\build_setup.py.
#endif
#ifndef Revision
  #error Revision is not defined. Build with windows\build_setup.py.
#endif
#ifndef ImagePath
  #error ImagePath is not defined. Build with windows\build_setup.py.
#endif
#ifndef ImageSha256
  #error ImageSha256 is not defined. Build with windows\build_setup.py.
#endif
#ifndef SrcPath
  #error SrcPath is not defined. Build with windows\build_setup.py.
#endif
#ifndef SrcSha256
  #error SrcSha256 is not defined. Build with windows\build_setup.py.
#endif
#ifndef SizeCognitaCpu
  #error SizeCognitaCpu is not defined. Build with windows\build_setup.py.
#endif
#ifndef SizeWorkspaceRuntime
  #error SizeWorkspaceRuntime is not defined. Build with windows\build_setup.py.
#endif
#ifndef SizeToolbox
  #error SizeToolbox is not defined. Build with windows\build_setup.py.
#endif
#ifndef SizeCognitaNvidia
  #error SizeCognitaNvidia is not defined. Build with windows\build_setup.py.
#endif
#ifndef ImageSize
  #error ImageSize is not defined. Build with windows\build_setup.py.
#endif
#ifndef LauncherExe
  #error LauncherExe is not defined. Build with windows\build_setup.py.
#endif
#ifndef WindowsDir
  #error WindowsDir is not defined. Build with windows\build_setup.py.
#endif
#ifndef OutputDir
  #error OutputDir is not defined. Build with windows\build_setup.py.
#endif

; Where users can ask for help (2026-09-29): the repository's new-issue page, public by ship
; time. Setup never sends anything itself; every place that offers Save diagnostics also says where
; the user may attach the file, and that nothing was sent. windows\CognitaWin.ps1 has the same URL.
#define SupportUrl "https://github.com/dbeachy1/Cognita/issues/new"

#pragma message "Cognita Setup {#Version} revision {#Revision}"

[Setup]
; A fixed AppId: a newer Setup over an older install is an update (section 7.3).
AppId={{17D9746C-1795-4815-9300-7ECE429CEE13}
AppName=Cognita
AppVersion={#Version}
AppVerName=Cognita {#Version}
AppPublisher=Cognita
VersionInfoVersion={#Version}.{#Revision}
VersionInfoDescription=Cognita Setup {#Version} revision {#Revision}
; Per user, no admin prompt (W1). Only WSL's own installer asks for permission, once.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Cognita\app
DisableDirPage=yes
DisableProgramGroupPage=yes
DefaultGroupName=Cognita
; Our own Ready page (Advanced, final checks) replaces Inno's.
DisableReadyPage=yes
DisableWelcomePage=no
; The Linux image is x86_64: Windows on ARM is refused (section 4.1).
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.22000
SetupLogging=yes
; The uninstaller writes its log always too (design 18.5 copies it beside the helper logs).
UninstallLogging=yes
ChangesEnvironment=yes
OutputDir={#OutputDir}
OutputBaseFilename=Cognita-Setup-{#Version}-r{#Revision}
; The two payload tarballs are already compressed (nocompression below); nothing else is big.
Compression=lzma2
SolidCompression=no
WizardStyle=modern
; The source image is the same Cognita mark used by the web UI; Inno scales it into the wizard header.
WizardSmallImageFile=..\..\src\cognita\web\cognita-icon-512.png
; Tall, high-resolution Cognita art for the Welcome and other wizard pages.
WizardImageFile=assets\cognita-wizard.png
; Multi-size icon shown by Setup in the taskbar, title bar, and Explorer.
SetupIconFile=assets\cognita-setup.ico
UninstallDisplayName=Cognita
; Design 19.1 item 5: one Setup at a time. A second copy (the RunOnce resume plus a double-click, say)
; gets Inno's own "already running" message instead of two installs racing each other.
SetupMutex=CognitaSetup_17D9746C
; Design 19.1 item 22 (was CloseApplications=no): replacing bin\cognita.exe fails while a terminal still
; runs `cognita logs -f`. With the Restart Manager on, Inno offers to close that program instead of
; its Retry/Ignore/Abort. The filter keeps it to executables, so it never touches a document window.
CloseApplications=yes
CloseApplicationsFilter=*.exe

[Messages]
; Inno asks this AFTER the Remove Cognita dialog (AskUninstallChoice). Its stock text, "completely
; remove Cognita and all of its components", contradicted a "keep Cognita's data" choice made a
; moment earlier (P2, 2026-09-29).
english.ConfirmUninstall=Remove %1 from this PC now? Your documents are never touched, and your choice about Cognita's data applies.
spanish.ConfirmUninstall=¿Quitar %1 de este equipo ahora? Sus documentos nunca se tocan y se respeta su elección sobre los datos de Cognita.
french.ConfirmUninstall=Supprimer %1 de ce PC ? Vos documents ne sont jamais modifiés et votre choix concernant les données de Cognita est respecté.
german.ConfirmUninstall=%1 jetzt von diesem PC entfernen? Ihre Dokumente bleiben unberührt und Ihre Wahl zu den Cognita-Daten wird berücksichtigt.
italian.ConfirmUninstall=Rimuovere %1 da questo PC? I documenti non vengono mai modificati e viene rispettata la scelta sui dati di Cognita.
brazilianportuguese.ConfirmUninstall=Remover %1 deste computador agora? Seus documentos nunca são alterados e sua escolha sobre os dados do Cognita será respeitada.
; Design 19.4 item 21: ONE closing box. Inno shows one of these when the uninstall ends; they carry the
; documents sentence, so the usPostUninstall message box that used to say it is gone. (A delete-data run
; whose data could not be deleted shows its own text first, from CurUninstallStepChanged.)
english.UninstalledAll=Cognita was removed. Your documents were not touched.
spanish.UninstalledAll=Se quitó Cognita. Sus documentos no se modificaron.
french.UninstalledAll=Cognita a été supprimé. Vos documents n’ont pas été modifiés.
german.UninstalledAll=Cognita wurde entfernt. Ihre Dokumente blieben unberührt.
italian.UninstalledAll=Cognita è stato rimosso. I documenti non sono stati modificati.
brazilianportuguese.UninstalledAll=O Cognita foi removido. Seus documentos não foram alterados.
english.UninstalledMost=Cognita was removed. Your documents were not touched.
spanish.UninstalledMost=Se quitó Cognita. Sus documentos no se modificaron.
french.UninstalledMost=Cognita a été supprimé. Vos documents n’ont pas été modifiés.
german.UninstalledMost=Cognita wurde entfernt. Ihre Dokumente blieben unberührt.
italian.UninstalledMost=Cognita è stato rimosso. I documenti non sono stati modificati.
brazilianportuguese.UninstalledMost=O Cognita foi removido. Seus documentos não foram alterados.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[CustomMessages]
english.upgradePasswordCaption=Updating Cognita from %1 to %2. Enter your current Admin password once. To change it, run cognita password in a terminal.
spanish.upgradePasswordCaption=Actualización de Cognita de la versión %1 a la %2. Escriba su contraseña actual de administrador una sola vez. Para cambiarla, ejecute cognita password en una terminal.
french.upgradePasswordCaption=Mise à jour de Cognita de la version %1 vers la version %2. Saisissez une seule fois votre mot de passe Admin actuel. Pour le modifier, exécutez cognita password dans un terminal.
german.upgradePasswordCaption=Cognita wird von Version %1 auf %2 aktualisiert. Geben Sie Ihr aktuelles Admin-Kennwort einmal ein. Führen Sie zum Ändern cognita password in einem Terminal aus.
italian.upgradePasswordCaption=Aggiornamento di Cognita dalla versione %1 alla %2. Inserisci una sola volta la password Admin attuale. Per cambiarla, esegui cognita password in un terminale.
brazilianportuguese.upgradePasswordCaption=Atualizando o Cognita da versão %1 para %2. Digite sua senha Admin atual uma vez. Para alterá-la, execute cognita password em um terminal.
english.verifyPasswordCaption=Enter your current Admin password once. To change it, run cognita password in a terminal.
spanish.verifyPasswordCaption=Escriba su contraseña actual de administrador una sola vez. Para cambiarla, ejecute cognita password en una terminal.
french.verifyPasswordCaption=Saisissez une seule fois votre mot de passe Admin actuel. Pour le modifier, exécutez cognita password dans un terminal.
german.verifyPasswordCaption=Geben Sie Ihr aktuelles Admin-Kennwort einmal ein. Führen Sie zum Ändern cognita password in einem Terminal aus.
italian.verifyPasswordCaption=Inserisci una sola volta la password Admin attuale. Per cambiarla, esegui cognita password in un terminale.
brazilianportuguese.verifyPasswordCaption=Digite sua senha Admin atual uma vez. Para alterá-la, execute cognita password em um terminal.
english.downgradeText=Cognita %1 is installed; this Setup is older (%2). Use a newer Setup, or "cognita rollback" to go back a release.
spanish.downgradeText=Está instalada la versión %1 de Cognita; este programa de instalación es anterior (%2). Use uno más reciente o ejecute "cognita rollback" para volver a una versión anterior.
french.downgradeText=Cognita %1 est installée ; ce programme d’installation est plus ancien (%2). Utilisez un programme plus récent ou exécutez "cognita rollback" pour revenir à une version antérieure.
german.downgradeText=Cognita %1 ist installiert; dieses Setup ist älter (%2). Verwenden Sie ein neueres Setup oder führen Sie "cognita rollback" aus, um zu einer vorherigen Version zurückzukehren.
italian.downgradeText=È installato Cognita %1; questo programma di installazione è più vecchio (%2). Usa un programma più recente oppure esegui "cognita rollback" per tornare a una versione precedente.
brazilianportuguese.downgradeText=O Cognita %1 está instalado; esta instalação é mais antiga (%2). Use uma instalação mais recente ou execute "cognita rollback" para voltar a uma versão anterior.
english.wslTitle=Turn on WSL
spanish.wslTitle=Activar WSL
french.wslTitle=Activer WSL
german.wslTitle=WSL aktivieren
italian.wslTitle=Attiva WSL
brazilianportuguese.wslTitle=Ativar o WSL
english.wslRestartNow=Restart now
spanish.wslRestartNow=Reiniciar ahora
french.wslRestartNow=Redémarrer maintenant
german.wslRestartNow=Jetzt neu starten
italian.wslRestartNow=Riavvia ora
brazilianportuguese.wslRestartNow=Reiniciar agora
english.wslRestartLater=Later (Setup continues after your next restart)
spanish.wslRestartLater=Más tarde (la instalación continuará después del próximo reinicio)
french.wslRestartLater=Plus tard (l’installation continuera après le prochain redémarrage)
german.wslRestartLater=Später (das Setup wird nach dem nächsten Neustart fortgesetzt)
italian.wslRestartLater=Più tardi (l’installazione continuerà dopo il prossimo riavvio)
brazilianportuguese.wslRestartLater=Mais tarde (a instalação continuará após a próxima reinicialização)
english.folderTitle=Projects folder
spanish.folderTitle=Carpeta de proyectos
french.folderTitle=Dossier des projets
german.folderTitle=Projektordner
italian.folderTitle=Cartella dei progetti
brazilianportuguese.folderTitle=Pasta de projetos
english.folderQuestion=Where are the documents Cognita should search?
spanish.folderQuestion=¿Dónde están los documentos que Cognita debe buscar?
french.folderQuestion=Où se trouvent les documents que Cognita doit rechercher ?
german.folderQuestion=In welchem Ordner soll Cognita nach Dokumenten suchen?
italian.folderQuestion=Dove si trovano i documenti che Cognita deve cercare?
brazilianportuguese.folderQuestion=Onde estão os documentos que o Cognita deve pesquisar?
english.folderDescription=Cognita reads and writes documents in this folder and its subfolders, in place. Nothing outside it is visible to Cognita.
spanish.folderDescription=Cognita lee y escribe documentos en esta carpeta y sus subcarpetas. Cognita no puede ver nada fuera de ella.
french.folderDescription=Cognita lit et écrit les documents dans ce dossier et ses sous-dossiers. Cognita ne voit rien en dehors de ce dossier.
german.folderDescription=Cognita liest und schreibt Dokumente in diesem Ordner und seinen Unterordnern. Außerhalb davon kann Cognita nichts sehen.
italian.folderDescription=Cognita legge e scrive i documenti in questa cartella e nelle sue sottocartelle. Cognita non può vedere nulla al di fuori.
brazilianportuguese.folderDescription=O Cognita lê e grava documentos nesta pasta e nas subpastas. O Cognita não consegue ver nada fora dela.
english.folderOneDrive=If this folder is in OneDrive, right-click it and choose "Always keep on this device".
spanish.folderOneDrive=Si esta carpeta está en OneDrive, haga clic con el botón derecho y elija «Mantener siempre en este dispositivo».
french.folderOneDrive=Si ce dossier se trouve dans OneDrive, faites un clic droit et choisissez « Toujours conserver sur cet appareil ».
german.folderOneDrive=Wenn sich dieser Ordner in OneDrive befindet, klicken Sie mit der rechten Maustaste darauf und wählen Sie „Immer auf diesem Gerät behalten“.
italian.folderOneDrive=Se questa cartella è in OneDrive, fai clic con il pulsante destro e scegli "Mantieni sempre su questo dispositivo".
brazilianportuguese.folderOneDrive=Se esta pasta estiver no OneDrive, clique nela com o botão direito e escolha "Sempre manter neste dispositivo".
english.adminTitle=Admin sign-in
spanish.adminTitle=Inicio de sesión de administrador
french.adminTitle=Connexion Admin
german.adminTitle=Admin-Anmeldung
italian.adminTitle=Accesso Admin
brazilianportuguese.adminTitle=Login Admin
english.adminDescription=Choose the sign-in for Cognita Admin.
spanish.adminDescription=Elija los datos de inicio de sesión para Cognita Admin.
french.adminDescription=Choisissez les identifiants de connexion à Cognita Admin.
german.adminDescription=Legen Sie die Anmeldung für Cognita Admin fest.
italian.adminDescription=Scegli le credenziali per accedere a Cognita Admin.
brazilianportuguese.adminDescription=Escolha o login do Cognita Admin.
english.adminUser=User name:
spanish.adminUser=Nombre de usuario:
french.adminUser=Nom d’utilisateur :
german.adminUser=Benutzername:
italian.adminUser=Nome utente:
brazilianportuguese.adminUser=Nome de usuário:
english.newPassword=Password:
spanish.newPassword=Contraseña:
french.newPassword=Mot de passe :
german.newPassword=Kennwort:
italian.newPassword=Password:
brazilianportuguese.newPassword=Senha:
english.passwordAgain=Password again:
spanish.passwordAgain=Repita la contraseña:
french.passwordAgain=Confirmez le mot de passe :
german.passwordAgain=Kennwort wiederholen:
italian.passwordAgain=Ripeti la password:
brazilianportuguese.passwordAgain=Digite a senha novamente:
english.currentPassword=Current password:
spanish.currentPassword=Contraseña actual:
french.currentPassword=Mot de passe actuel :
german.currentPassword=Aktuelles Kennwort:
italian.currentPassword=Password attuale:
brazilianportuguese.currentPassword=Senha atual:
english.wslDescription=One-time Windows setup before Cognita can install.
spanish.wslDescription=Configuración única de Windows antes de instalar Cognita.
french.wslDescription=Configuration unique de Windows avant l’installation de Cognita.
german.wslDescription=Einmalige Windows-Einrichtung vor der Installation von Cognita.
italian.wslDescription=Configurazione iniziale di Windows prima di installare Cognita.
brazilianportuguese.wslDescription=Configuração única do Windows antes de instalar o Cognita.
english.wslDetails=Cognita runs inside WSL, Windows' built-in Linux support. Setup will turn it on. Windows asks your permission once, and a window shows WSL being installed. Then your PC must restart. Save your work in other programs first. Setup opens again by itself after you sign back in.
spanish.wslDetails=Cognita se ejecuta en WSL, la compatibilidad con Linux integrada en Windows. El programa de instalación lo activará. Windows le pedirá permiso una vez y mostrará una ventana mientras se instala WSL. Después, tendrá que reiniciar el equipo. Guarde antes el trabajo de otros programas. La instalación se abrirá de nuevo cuando inicie sesión.
french.wslDetails=Cognita s’exécute dans WSL, la prise en charge de Linux intégrée à Windows. Le programme d’installation l’activera. Windows vous demandera votre autorisation une fois et affichera une fenêtre pendant l’installation de WSL. Vous devrez ensuite redémarrer le PC. Enregistrez d’abord votre travail dans les autres programmes. Le programme d’installation se rouvrira après votre prochaine connexion.
german.wslDetails=Cognita läuft in WSL, der integrierten Linux-Unterstützung von Windows. Das Setup aktiviert WSL. Windows fragt einmal nach Ihrer Zustimmung und zeigt während der Installation von WSL ein Fenster an. Danach muss der PC neu gestartet werden. Speichern Sie zuerst Ihre Arbeit in anderen Programmen. Nach der nächsten Anmeldung öffnet sich das Setup automatisch wieder.
italian.wslDetails=Cognita viene eseguito in WSL, il supporto Linux integrato in Windows. Il programma di installazione lo attiverà. Windows chiederà il tuo consenso una volta e mostrerà una finestra durante l’installazione di WSL. Poi dovrai riavviare il PC. Prima salva il lavoro negli altri programmi. Il programma di installazione si riaprirà automaticamente dopo il prossimo accesso.
brazilianportuguese.wslDetails=O Cognita é executado no WSL, o suporte integrado do Windows para Linux. A instalação vai ativá-lo. O Windows pedirá sua permissão uma vez e mostrará uma janela enquanto o WSL é instalado. Depois, será necessário reiniciar o computador. Salve seu trabalho nos outros programas antes. A instalação será aberta novamente quando você entrar no Windows.
english.accelTitle=Acceleration
spanish.accelTitle=Aceleración
french.accelTitle=Accélération
german.accelTitle=Beschleunigung
italian.accelTitle=Accelerazione
brazilianportuguese.accelTitle=Aceleração
english.accelDescription=Choose what Cognita uses for its heavy work.
spanish.accelDescription=Elija qué recursos usará Cognita para las tareas más exigentes.
french.accelDescription=Choisissez les ressources utilisées par Cognita pour les tâches lourdes.
german.accelDescription=Wählen Sie aus, welche Hardware Cognita für rechenintensive Aufgaben verwendet.
italian.accelDescription=Scegli cosa userà Cognita per le attività più impegnative.
brazilianportuguese.accelDescription=Escolha o que o Cognita usará nas tarefas mais pesadas.
english.gpuChoice=Use the NVIDIA GPU (recommended).
spanish.gpuChoice=Usar la GPU NVIDIA (recomendado).
french.gpuChoice=Utiliser le GPU NVIDIA (recommandé).
german.gpuChoice=NVIDIA-GPU verwenden (empfohlen).
italian.gpuChoice=Usa la GPU NVIDIA (consigliato).
brazilianportuguese.gpuChoice=Usar a GPU NVIDIA (recomendado).
english.cpuChoice=Use the CPU only.
spanish.cpuChoice=Usar solo la CPU.
french.cpuChoice=Utiliser uniquement le processeur.
german.cpuChoice=Nur die CPU verwenden.
italian.cpuChoice=Usa solo la CPU.
brazilianportuguese.cpuChoice=Usar somente a CPU.
english.accelNote=Setup checks the card while it installs. If Cognita cannot use it, Setup installs for the CPU and tells you why.
spanish.accelNote=La instalación comprueba la tarjeta. Si Cognita no puede usarla, se instalará para la CPU y se explicará el motivo.
french.accelNote=Le programme d’installation vérifie la carte. Si Cognita ne peut pas l’utiliser, il sera configuré pour le processeur et la raison vous sera indiquée.
german.accelNote=Das Setup prüft die Grafikkarte während der Installation. Kann Cognita sie nicht verwenden, wird Cognita für die CPU eingerichtet und der Grund angezeigt.
italian.accelNote=Il programma di installazione controlla la scheda. Se Cognita non può usarla, verrà configurato per la CPU e ti verrà spiegato il motivo.
brazilianportuguese.accelNote=A instalação verifica a placa. Se o Cognita não puder usá-la, ele será configurado para a CPU e o motivo será informado.
english.readyTitle=Ready to install
spanish.readyTitle=Listo para instalar
french.readyTitle=Prêt à installer
german.readyTitle=Installationsbereit
italian.readyTitle=Pronto per l’installazione
brazilianportuguese.readyTitle=Pronto para instalar
english.readyDescription=Setup has what it needs.
spanish.readyDescription=La instalación tiene todo lo necesario.
french.readyDescription=Le programme d’installation dispose de tout ce dont il a besoin.
german.readyDescription=Das Setup hat alle erforderlichen Angaben.
italian.readyDescription=Il programma di installazione ha tutto ciò che serve.
brazilianportuguese.readyDescription=A instalação tem tudo o que precisa.
english.advancedButton=Advanced...
spanish.advancedButton=Avanzado...
french.advancedButton=Avancé...
german.advancedButton=Erweitert...
italian.advancedButton=Avanzate...
brazilianportuguese.advancedButton=Avançado...
english.copyLink=Copy link
spanish.copyLink=Copiar enlace
french.copyLink=Copier le lien
german.copyLink=Link kopieren
italian.copyLink=Copia link
brazilianportuguese.copyLink=Copiar link
english.signInWait=Setup waits up to 10 minutes for the sign-in.
spanish.signInWait=La instalación espera hasta 10 minutos para que inicie sesión.
french.signInWait=Le programme d’installation attend jusqu’à 10 minutes la connexion.
german.signInWait=Das Setup wartet bis zu 10 Minuten auf die Anmeldung.
italian.signInWait=Il programma di installazione attende fino a 10 minuti l’accesso.
brazilianportuguese.signInWait=A instalação aguardará o login por até 10 minutos.
english.skipProof=Skip self-tests
spanish.skipProof=Omitir autopruebas
french.skipProof=Ignorer les autotests
german.skipProof=Selbsttests überspringen
italian.skipProof=Salta i test automatici
brazilianportuguese.skipProof=Pular autotestes
english.retryButton=Retry
spanish.retryButton=Reintentar
french.retryButton=Réessayer
german.retryButton=Erneut versuchen
italian.retryButton=Riprova
brazilianportuguese.retryButton=Tentar novamente
english.saveDiagnostics=Save diagnostics
spanish.saveDiagnostics=Guardar diagnósticos
french.saveDiagnostics=Enregistrer les diagnostics
german.saveDiagnostics=Diagnosedaten speichern
italian.saveDiagnostics=Salva diagnostica
brazilianportuguese.saveDiagnostics=Salvar diagnósticos
english.openLogs=Open the log folder
spanish.openLogs=Abrir la carpeta de registros
french.openLogs=Ouvrir le dossier des journaux
german.openLogs=Protokollordner öffnen
italian.openLogs=Apri la cartella dei registri
brazilianportuguese.openLogs=Abrir a pasta de logs
english.reportProblem=Report a problem
spanish.reportProblem=Informar de un problema
french.reportProblem=Signaler un problème
german.reportProblem=Problem melden
italian.reportProblem=Segnala un problema
brazilianportuguese.reportProblem=Relatar um problema
english.pleaseNote=Please note:
spanish.pleaseNote=Nota:
french.pleaseNote=À noter :
german.pleaseNote=Hinweis:
italian.pleaseNote=Nota:
brazilianportuguese.pleaseNote=Observação:
english.wslRestartNeeded=WSL is turned on, but Windows must restart before Cognita can use it. Save your work in other programs first. Setup opens again by itself after you sign back in, and continues from there.
spanish.wslRestartNeeded=WSL está activado, pero Windows debe reiniciarse antes de que Cognita pueda usarlo. Guarde primero el trabajo de otros programas. La instalación se abrirá de nuevo después de iniciar sesión y continuará desde aquí.
french.wslRestartNeeded=WSL est activé, mais Windows doit redémarrer avant que Cognita puisse l’utiliser. Enregistrez d’abord votre travail dans les autres programmes. Le programme d’installation se rouvrira après votre connexion et reprendra ici.
german.wslRestartNeeded=WSL ist aktiviert, aber Windows muss neu gestartet werden, bevor Cognita es verwenden kann. Speichern Sie zuerst Ihre Arbeit in anderen Programmen. Nach der Anmeldung wird das Setup automatisch wieder geöffnet und fortgesetzt.
italian.wslRestartNeeded=WSL è attivo, ma Windows deve essere riavviato prima che Cognita possa usarlo. Prima salva il lavoro negli altri programmi. Dopo l’accesso il programma di installazione si riaprirà e continuerà da qui.
brazilianportuguese.wslRestartNeeded=O WSL está ativado, mas o Windows precisa ser reiniciado antes que o Cognita possa usá-lo. Salve seu trabalho nos outros programas antes. A instalação será aberta novamente depois do login e continuará daqui.
english.wslContinue=Turn on WSL
spanish.wslContinue=Activar WSL
french.wslContinue=Activer WSL
german.wslContinue=WSL aktivieren
italian.wslContinue=Attiva WSL
brazilianportuguese.wslContinue=Ativar o WSL
english.addFolderLater=To add another folder later: cognita add-folder
spanish.addFolderLater=Para agregar otra carpeta más adelante: cognita add-folder
french.addFolderLater=Pour ajouter un autre dossier plus tard : cognita add-folder
german.addFolderLater=Um später einen weiteren Ordner hinzuzufügen: cognita add-folder
italian.addFolderLater=Per aggiungere un’altra cartella in seguito: cognita add-folder
brazilianportuguese.addFolderLater=Para adicionar outra pasta depois: cognita add-folder
english.adminCreateGuidance=Use this user name and password to open Cognita Admin in your browser. Type the password twice.
spanish.adminCreateGuidance=Use este nombre de usuario y esta contraseña para abrir Cognita Admin en el navegador. Escriba la contraseña dos veces.
french.adminCreateGuidance=Utilisez ce nom d’utilisateur et ce mot de passe pour ouvrir Cognita Admin dans votre navigateur. Saisissez le mot de passe deux fois.
german.adminCreateGuidance=Mit diesem Benutzernamen und Kennwort öffnen Sie Cognita Admin im Browser. Geben Sie das Kennwort zweimal ein.
italian.adminCreateGuidance=Usa questo nome utente e questa password per aprire Cognita Admin nel browser. Inserisci la password due volte.
brazilianportuguese.adminCreateGuidance=Use este nome de usuário e esta senha para abrir o Cognita Admin no navegador. Digite a senha duas vezes.
english.unchanged=unchanged
spanish.unchanged=sin cambios
french.unchanged=inchangé
german.unchanged=unverändert
italian.unchanged=non modificato
brazilianportuguese.unchanged=inalterado
english.folderRequired=Choose the folder that holds your documents.
spanish.folderRequired=Elija la carpeta que contiene sus documentos.
french.folderRequired=Choisissez le dossier contenant vos documents.
german.folderRequired=Wählen Sie den Ordner mit Ihren Dokumenten aus.
italian.folderRequired=Scegli la cartella che contiene i tuoi documenti.
brazilianportuguese.folderRequired=Escolha a pasta que contém seus documentos.
english.quoteFolderError=That folder name cannot contain a double quote.
spanish.quoteFolderError=El nombre de la carpeta no puede contener comillas dobles.
french.quoteFolderError=Le nom du dossier ne peut pas contenir de guillemet double.
german.quoteFolderError=Der Ordnername darf kein doppeltes Anführungszeichen enthalten.
italian.quoteFolderError=Il nome della cartella non può contenere virgolette doppie.
brazilianportuguese.quoteFolderError=O nome da pasta não pode conter aspas duplas.
english.folderUnavailable=Cognita cannot use that folder.
spanish.folderUnavailable=Cognita no puede usar esa carpeta.
french.folderUnavailable=Cognita ne peut pas utiliser ce dossier.
german.folderUnavailable=Cognita kann diesen Ordner nicht verwenden.
italian.folderUnavailable=Cognita non può usare questa cartella.
brazilianportuguese.folderUnavailable=O Cognita não pode usar essa pasta.
english.typeUsername=Type a user name.
spanish.typeUsername=Escriba un nombre de usuario.
french.typeUsername=Saisissez un nom d’utilisateur.
german.typeUsername=Geben Sie einen Benutzernamen ein.
italian.typeUsername=Inserisci un nome utente.
brazilianportuguese.typeUsername=Digite um nome de usuário.
english.badUsername=The user name cannot contain a double quote or a control character.
spanish.badUsername=El nombre de usuario no puede contener comillas dobles ni caracteres de control.
french.badUsername=Le nom d’utilisateur ne peut pas contenir de guillemet double ni de caractère de contrôle.
german.badUsername=Der Benutzername darf kein doppeltes Anführungszeichen und kein Steuerzeichen enthalten.
italian.badUsername=Il nome utente non può contenere virgolette doppie o caratteri di controllo.
brazilianportuguese.badUsername=O nome de usuário não pode conter aspas duplas nem caracteres de controle.
english.typePassword=Type a password.
spanish.typePassword=Escriba una contraseña.
french.typePassword=Saisissez un mot de passe.
german.typePassword=Geben Sie ein Kennwort ein.
italian.typePassword=Inserisci una password.
brazilianportuguese.typePassword=Digite uma senha.
english.passwordMismatch=The two passwords are not the same.
spanish.passwordMismatch=Las dos contraseñas no coinciden.
french.passwordMismatch=Les deux mots de passe ne correspondent pas.
german.passwordMismatch=Die beiden Kennwörter stimmen nicht überein.
italian.passwordMismatch=Le due password non corrispondono.
brazilianportuguese.passwordMismatch=As duas senhas não são iguais.
english.summaryUpdate=Update Cognita %1 to %2.
spanish.summaryUpdate=Actualizar Cognita de %1 a %2.
french.summaryUpdate=Mettre à jour Cognita de %1 vers %2.
german.summaryUpdate=Cognita von %1 auf %2 aktualisieren.
italian.summaryUpdate=Aggiorna Cognita dalla versione %1 alla %2.
brazilianportuguese.summaryUpdate=Atualizar o Cognita de %1 para %2.
english.summaryUpdateNoVersion=Update Cognita to %1.
spanish.summaryUpdateNoVersion=Actualizar Cognita a la versión %1.
french.summaryUpdateNoVersion=Mettre Cognita à jour vers la version %1.
german.summaryUpdateNoVersion=Cognita auf Version %1 aktualisieren.
italian.summaryUpdateNoVersion=Aggiorna Cognita alla versione %1.
brazilianportuguese.summaryUpdateNoVersion=Atualizar o Cognita para a versão %1.
english.summaryRepair=Repair Cognita %1 (Setup runs the install again over what is there).
spanish.summaryRepair=Reparar Cognita %1 (se volverá a ejecutar la instalación sobre la existente).
french.summaryRepair=Réparer Cognita %1 (le programme relance l’installation sur la version existante).
german.summaryRepair=Cognita %1 reparieren (das Setup installiert erneut über die vorhandene Version).
italian.summaryRepair=Ripara Cognita %1 (il programma esegue di nuovo l’installazione su quella esistente).
brazilianportuguese.summaryRepair=Reparar o Cognita %1 (a instalação será executada novamente sobre a versão atual).
english.summaryReinstall=Reinstall Cognita %1 (your data is kept).
spanish.summaryReinstall=Volver a instalar Cognita %1 (se conservan sus datos).
french.summaryReinstall=Réinstaller Cognita %1 (vos données sont conservées).
german.summaryReinstall=Cognita %1 erneut installieren (Ihre Daten bleiben erhalten).
italian.summaryReinstall=Reinstalla Cognita %1 (i tuoi dati vengono conservati).
brazilianportuguese.summaryReinstall=Reinstalar o Cognita %1 (seus dados serão mantidos).
english.summaryFinish=Finish installing Cognita %1 (an earlier install did not complete; Setup continues it).
spanish.summaryFinish=Terminar de instalar Cognita %1 (una instalación anterior no terminó; esta instalación la continuará).
french.summaryFinish=Terminer l’installation de Cognita %1 (une installation précédente n’a pas abouti ; le programme la reprend).
german.summaryFinish=Cognita %1 fertig installieren (eine frühere Installation wurde nicht abgeschlossen; das Setup setzt sie fort).
italian.summaryFinish=Completa l’installazione di Cognita %1 (un’installazione precedente non è terminata; il programma la riprende).
brazilianportuguese.summaryFinish=Concluir a instalação do Cognita %1 (uma instalação anterior não terminou; esta instalação continuará).
english.summaryInstall=Install Cognita %1.
spanish.summaryInstall=Instalar Cognita %1.
french.summaryInstall=Installer Cognita %1.
german.summaryInstall=Cognita %1 installieren.
italian.summaryInstall=Installa Cognita %1.
brazilianportuguese.summaryInstall=Instalar o Cognita %1.
english.summaryFolder=Projects folder:  %1
spanish.summaryFolder=Carpeta de proyectos:  %1
french.summaryFolder=Dossier des projets :  %1
german.summaryFolder=Projektordner:  %1
italian.summaryFolder=Cartella dei progetti:  %1
brazilianportuguese.summaryFolder=Pasta de projetos:  %1
english.summaryAdmin=Admin sign-in:  %1
spanish.summaryAdmin=Inicio de sesión de administrador:  %1
french.summaryAdmin=Connexion Admin :  %1
german.summaryAdmin=Admin-Anmeldung:  %1
italian.summaryAdmin=Accesso Admin:  %1
brazilianportuguese.summaryAdmin=Login Admin:  %1
english.summaryPorts=Ports:  MCP %1, Admin %2
spanish.summaryPorts=Puertos:  MCP %1, Admin %2
french.summaryPorts=Ports :  MCP %1, Admin %2
german.summaryPorts=Ports:  MCP %1, Admin %2
italian.summaryPorts=Porte:  MCP %1, Admin %2
brazilianportuguese.summaryPorts=Portas:  MCP %1, Admin %2
english.summaryWorkspace=Workspace:  %1
spanish.summaryWorkspace=Workspace:  %1
french.summaryWorkspace=Workspace :  %1
german.summaryWorkspace=Workspace:  %1
italian.summaryWorkspace=Workspace:  %1
brazilianportuguese.summaryWorkspace=Workspace:  %1
english.summaryAcceleration=Acceleration:  %1
spanish.summaryAcceleration=Aceleración:  %1
french.summaryAcceleration=Accélération :  %1
german.summaryAcceleration=Beschleunigung:  %1
italian.summaryAcceleration=Accelerazione:  %1
brazilianportuguese.summaryAcceleration=Aceleração:  %1
english.summaryDataLocation=Cognita keeps its data in:  %1
spanish.summaryDataLocation=Cognita guarda sus datos en:  %1
french.summaryDataLocation=Cognita conserve ses données dans :  %1
german.summaryDataLocation=Cognita speichert seine Daten hier:  %1
italian.summaryDataLocation=Cognita conserva i dati in:  %1
brazilianportuguese.summaryDataLocation=O Cognita mantém os dados em:  %1
english.workspaceOn=on
spanish.workspaceOn=activado
french.workspaceOn=activé
german.workspaceOn=aktiviert
italian.workspaceOn=attivo
brazilianportuguese.workspaceOn=ativado
english.workspaceOff=off
spanish.workspaceOff=desactivado
french.workspaceOff=désactivé
german.workspaceOff=deaktiviert
italian.workspaceOff=disattivato
brazilianportuguese.workspaceOff=desativado
english.accelCpu=CPU
spanish.accelCpu=CPU
french.accelCpu=processeur
german.accelCpu=CPU
italian.accelCpu=CPU
brazilianportuguese.accelCpu=CPU
english.accelAmd=AMD GPU
spanish.accelAmd=GPU AMD
french.accelAmd=GPU AMD
german.accelAmd=AMD-GPU
italian.accelAmd=GPU AMD
brazilianportuguese.accelAmd=GPU AMD
english.accelNvidia=NVIDIA GPU
spanish.accelNvidia=GPU NVIDIA
french.accelNvidia=GPU NVIDIA
german.accelNvidia=NVIDIA-GPU
italian.accelNvidia=GPU NVIDIA
brazilianportuguese.accelNvidia=GPU NVIDIA
english.accelUnchanged=unchanged
spanish.accelUnchanged=sin cambios
french.accelUnchanged=inchangée
german.accelUnchanged=unverändert
italian.accelUnchanged=non modificata
brazilianportuguese.accelUnchanged=inalterada
english.accelReasonOld= (NVIDIA driver too old)
spanish.accelReasonOld= (el controlador NVIDIA es demasiado antiguo)
french.accelReasonOld= (pilote NVIDIA trop ancien)
german.accelReasonOld= (NVIDIA-Treiber zu alt)
italian.accelReasonOld= (driver NVIDIA troppo vecchio)
brazilianportuguese.accelReasonOld= (driver NVIDIA muito antigo)
english.accelReasonNoBuild= (no NVIDIA build in this version)
spanish.accelReasonNoBuild= (esta versión no incluye una compilación NVIDIA)
french.accelReasonNoBuild= (aucune version NVIDIA dans cette édition)
german.accelReasonNoBuild= (keine NVIDIA-Version in dieser Ausgabe)
italian.accelReasonNoBuild= (questa versione non include il pacchetto NVIDIA)
brazilianportuguese.accelReasonNoBuild= (esta versão não inclui uma compilação para NVIDIA)
english.accelReasonNoCard= (no NVIDIA graphics card found)
spanish.accelReasonNoCard= (no se encontró ninguna tarjeta gráfica NVIDIA)
french.accelReasonNoCard= (aucune carte graphique NVIDIA détectée)
german.accelReasonNoCard= (keine NVIDIA-Grafikkarte gefunden)
italian.accelReasonNoCard= (nessuna scheda grafica NVIDIA rilevata)
brazilianportuguese.accelReasonNoCard= (nenhuma placa de vídeo NVIDIA encontrada)
english.summaryDownloadsExisting=Setup downloads only what this PC does not already have (at most about %1) and needs about %2
spanish.summaryDownloadsExisting=La instalación descarga solo lo que aún no está en este equipo (como máximo, unos %1) y necesita unos %2
french.summaryDownloadsExisting=Le programme télécharge uniquement ce qui manque sur ce PC (environ %1 au maximum) et nécessite environ %2
german.summaryDownloadsExisting=Das Setup lädt nur herunter, was auf diesem PC noch fehlt (höchstens etwa %1), und benötigt etwa %2
italian.summaryDownloadsExisting=Il programma scarica solo ciò che manca sul PC (al massimo circa %1) e richiede circa %2
brazilianportuguese.summaryDownloadsExisting=A instalação baixa somente o que ainda falta neste computador (no máximo cerca de %1) e precisa de cerca de %2
english.summaryDownloadsFresh=Setup then downloads about %1 (Cognita and its search models) and needs about %2
spanish.summaryDownloadsFresh=Después, la instalación descarga unos %1 (Cognita y sus modelos de búsqueda) y necesita unos %2
french.summaryDownloadsFresh=Le programme télécharge ensuite environ %1 (Cognita et ses modèles de recherche) et nécessite environ %2
german.summaryDownloadsFresh=Das Setup lädt anschließend etwa %1 herunter (Cognita und seine Suchmodelle) und benötigt etwa %2
italian.summaryDownloadsFresh=Il programma scarica poi circa %1 (Cognita e i relativi modelli di ricerca) e richiede circa %2
brazilianportuguese.summaryDownloadsFresh=Em seguida, a instalação baixa cerca de %1 (Cognita e seus modelos de pesquisa) e precisa de cerca de %2
english.summaryFreeDisk=of free disk space. Pressing Install checks your ports and disk space again with these choices.
spanish.summaryFreeDisk=de espacio libre en disco. Al pulsar Instalar, se volverán a comprobar los puertos y el espacio con estas opciones.
french.summaryFreeDisk=d’espace disque libre. Le bouton Installer vérifie à nouveau les ports et l’espace avec ces choix.
german.summaryFreeDisk=freien Speicherplatz. Mit einem Klick auf Installieren werden Ports und Speicherplatz mit diesen Einstellungen erneut geprüft.
italian.summaryFreeDisk=di spazio libero. Premendo Installa verranno ricontrollati le porte e lo spazio disponibile.
brazilianportuguese.summaryFreeDisk=de espaço livre em disco. Ao clicar em Instalar, as portas e o espaço serão verificados novamente com estas opções.
english.summaryUpdateNote= An update keeps your ports and Workspace setting; to change them, run Setup again afterwards.
spanish.summaryUpdateNote= Una actualización conserva los puertos y Workspace; para cambiarlos, vuelva a ejecutar la instalación después.
french.summaryUpdateNote= Une mise à jour conserve les ports et le réglage Workspace ; pour les modifier, relancez ensuite le programme d’installation.
german.summaryUpdateNote= Bei einer Aktualisierung bleiben Ports und Workspace-Einstellung erhalten. Führen Sie das Setup danach erneut aus, um sie zu ändern.
italian.summaryUpdateNote= Un aggiornamento mantiene le porte e l’impostazione di Workspace; per modificarle, esegui di nuovo il programma in seguito.
brazilianportuguese.summaryUpdateNote= Uma atualização mantém as portas e a configuração do Workspace; para alterá-las, execute a instalação novamente depois.
english.summaryLockedNote= Use Advanced to change the ports or to turn Workspace off.
spanish.summaryLockedNote= Use Avanzado para cambiar los puertos o desactivar Workspace.
french.summaryLockedNote= Utilisez Avancé pour modifier les ports ou désactiver Workspace.
german.summaryLockedNote= Unter Erweitert können Sie die Ports ändern oder Workspace deaktivieren.
italian.summaryLockedNote= Usa Avanzate per modificare le porte o disattivare Workspace.
brazilianportuguese.summaryLockedNote= Use Avançado para alterar as portas ou desativar o Workspace.
english.summaryFreshNote= Use Advanced to change the ports, where Cognita keeps its data, or to turn Workspace off.
spanish.summaryFreshNote= Use Avanzado para cambiar los puertos, la ubicación de los datos de Cognita o desactivar Workspace.
french.summaryFreshNote= Utilisez Avancé pour modifier les ports, l’emplacement des données de Cognita ou désactiver Workspace.
german.summaryFreshNote= Unter Erweitert können Sie Ports, Cognitas Datenspeicherort oder Workspace ändern.
italian.summaryFreshNote= Usa Avanzate per modificare le porte, la posizione dei dati di Cognita o disattivare Workspace.
brazilianportuguese.summaryFreshNote= Use Avançado para alterar as portas, o local dos dados do Cognita ou desativar o Workspace.
english.welcomeResume=Setup is continuing after the restart. Press Next to check this PC again and finish installing Cognita.
spanish.welcomeResume=La instalación continúa después del reinicio. Pulse Siguiente para volver a comprobar este equipo y terminar de instalar Cognita.
french.welcomeResume=Le programme reprend après le redémarrage. Cliquez sur Suivant pour vérifier à nouveau ce PC et terminer l’installation de Cognita.
german.welcomeResume=Das Setup wird nach dem Neustart fortgesetzt. Klicken Sie auf Weiter, um diesen PC erneut zu prüfen und die Cognita-Installation abzuschließen.
italian.welcomeResume=L’installazione riprende dopo il riavvio. Premi Avanti per controllare di nuovo questo PC e completare l’installazione di Cognita.
brazilianportuguese.welcomeResume=A instalação continuará após a reinicialização. Clique em Avançar para verificar este computador novamente e concluir a instalação do Cognita.
english.welcomeUpdate=Cognita %1 is installed. This Setup updates it to %2. Press Next to check this PC.
spanish.welcomeUpdate=Está instalado Cognita %1. Esta instalación lo actualiza a %2. Pulse Siguiente para comprobar este equipo.
french.welcomeUpdate=Cognita %1 est installé. Ce programme le met à jour vers %2. Cliquez sur Suivant pour vérifier ce PC.
german.welcomeUpdate=Cognita %1 ist installiert. Dieses Setup aktualisiert es auf %2. Klicken Sie auf Weiter, um diesen PC zu prüfen.
italian.welcomeUpdate=Cognita %1 è installato. Questa installazione lo aggiorna alla versione %2. Premi Avanti per controllare questo PC.
brazilianportuguese.welcomeUpdate=O Cognita %1 está instalado. Esta instalação o atualiza para %2. Clique em Avançar para verificar este computador.
english.welcomeUpdateNoVersion=Cognita is installed. This Setup updates it to %1. Press Next to check this PC.
spanish.welcomeUpdateNoVersion=Está instalado Cognita. Esta instalación lo actualiza a %1. Pulse Siguiente para comprobar este equipo.
french.welcomeUpdateNoVersion=Cognita est installé. Ce programme le met à jour vers %1. Cliquez sur Suivant pour vérifier ce PC.
german.welcomeUpdateNoVersion=Cognita ist installiert. Dieses Setup aktualisiert es auf %1. Klicken Sie auf Weiter, um diesen PC zu prüfen.
italian.welcomeUpdateNoVersion=Cognita è installato. Questa installazione lo aggiorna alla versione %1. Premi Avanti per controllare questo PC.
brazilianportuguese.welcomeUpdateNoVersion=O Cognita está instalado. Esta instalação o atualiza para %1. Clique em Avançar para verificar este computador.
english.welcomeRepair=Cognita is already set up on this PC. Setup checks it and repairs problems. Press Next to check this PC.
spanish.welcomeRepair=Cognita ya está instalado en este equipo. La instalación lo comprobará y reparará los problemas. Pulse Siguiente para comprobar este equipo.
french.welcomeRepair=Cognita est déjà installé sur ce PC. Le programme le vérifie et répare les problèmes. Cliquez sur Suivant pour vérifier ce PC.
german.welcomeRepair=Cognita ist auf diesem PC bereits eingerichtet. Das Setup prüft es und behebt Probleme. Klicken Sie auf Weiter, um diesen PC zu prüfen.
italian.welcomeRepair=Cognita è già configurato su questo PC. L’installazione lo controlla e corregge i problemi. Premi Avanti per controllare questo PC.
brazilianportuguese.welcomeRepair=O Cognita já está configurado neste computador. A instalação verificará e corrigirá problemas. Clique em Avançar para verificar este computador.
english.welcomeReinstall=Cognita was removed from this PC and its data was kept. Setup installs Cognita %1 again over that data. Press Next to check this PC.
spanish.welcomeReinstall=Se quitó Cognita de este equipo y se conservaron sus datos. La instalación volverá a instalar Cognita %1 sobre esos datos. Pulse Siguiente para comprobar este equipo.
french.welcomeReinstall=Cognita a été supprimé de ce PC et ses données ont été conservées. Le programme réinstalle Cognita %1 sur ces données. Cliquez sur Suivant pour vérifier ce PC.
german.welcomeReinstall=Cognita wurde von diesem PC entfernt; die Daten blieben erhalten. Das Setup installiert Cognita %1 erneut mit diesen Daten. Klicken Sie auf Weiter, um diesen PC zu prüfen.
italian.welcomeReinstall=Cognita è stato rimosso da questo PC e i dati sono stati conservati. L’installazione reinstalla Cognita %1 usando quei dati. Premi Avanti per controllare questo PC.
brazilianportuguese.welcomeReinstall=O Cognita foi removido deste computador e os dados foram mantidos. A instalação instalará o Cognita %1 novamente usando esses dados. Clique em Avançar para verificar este computador.
english.welcomeFinish=An earlier Cognita install did not finish. Setup continues it, and nothing already set up is lost. Press Next to check this PC.
spanish.welcomeFinish=Una instalación anterior de Cognita no terminó. La instalación la continuará sin perder lo que ya está configurado. Pulse Siguiente para comprobar este equipo.
french.welcomeFinish=Une installation précédente de Cognita n’a pas abouti. Le programme la reprend sans perdre ce qui est déjà configuré. Cliquez sur Suivant pour vérifier ce PC.
german.welcomeFinish=Eine frühere Cognita-Installation wurde nicht abgeschlossen. Das Setup setzt sie fort, ohne bereits eingerichtete Daten zu verlieren. Klicken Sie auf Weiter, um diesen PC zu prüfen.
italian.welcomeFinish=Una precedente installazione di Cognita non è terminata. L’installazione la riprende senza perdere ciò che è già configurato. Premi Avanti per controllare questo PC.
brazilianportuguese.welcomeFinish=Uma instalação anterior do Cognita não foi concluída. A instalação continuará sem perder o que já está configurado. Clique em Avançar para verificar este computador.
english.welcomeFresh=This installs Cognita %1 on your PC and starts it whenever you sign in to Windows. Press Next to check that this PC can run it.
spanish.welcomeFresh=Esto instala Cognita %1 en su equipo y lo inicia cada vez que inicie sesión en Windows. Pulse Siguiente para comprobar que este equipo puede ejecutarlo.
french.welcomeFresh=Cette installation installe Cognita %1 sur ce PC et le démarre à chaque ouverture de session Windows. Cliquez sur Suivant pour vérifier que ce PC peut l’exécuter.
german.welcomeFresh=Dieses Setup installiert Cognita %1 auf Ihrem PC und startet es bei jeder Windows-Anmeldung. Klicken Sie auf Weiter, um zu prüfen, ob dieser PC dafür geeignet ist.
italian.welcomeFresh=Questa installazione installa Cognita %1 sul PC e lo avvia a ogni accesso a Windows. Premi Avanti per verificare che il PC possa eseguirlo.
brazilianportuguese.welcomeFresh=Esta instalação instala o Cognita %1 no computador e o inicia sempre que você entra no Windows. Clique em Avançar para verificar se este computador pode executá-lo.
english.accelFound=Setup found an NVIDIA graphics card (%1, driver %2). Cognita can use it to index documents and read text in images much faster than the CPU.
spanish.accelFound=La instalación encontró una tarjeta gráfica NVIDIA (%1, controlador %2). Cognita puede usarla para indexar documentos y leer texto en imágenes mucho más rápido que con la CPU.
french.accelFound=Le programme a détecté une carte graphique NVIDIA (%1, pilote %2). Cognita peut l’utiliser pour indexer des documents et lire le texte des images bien plus vite qu’avec le processeur.
german.accelFound=Das Setup hat eine NVIDIA-Grafikkarte gefunden (%1, Treiber %2). Cognita kann damit Dokumente indizieren und Bildtext viel schneller als mit der CPU lesen.
italian.accelFound=L’installazione ha rilevato una scheda grafica NVIDIA (%1, driver %2). Cognita può usarla per indicizzare documenti e leggere il testo nelle immagini molto più velocemente della CPU.
brazilianportuguese.accelFound=A instalação encontrou uma placa de vídeo NVIDIA (%1, driver %2). O Cognita pode usá-la para indexar documentos e ler textos em imagens muito mais rápido que a CPU.
english.accelOld=Your NVIDIA driver is %1. Cognita needs driver 580 or newer to use this card. Update the driver, then run Setup again and choose NVIDIA.
spanish.accelOld=El controlador NVIDIA es %1. Cognita necesita la versión 580 o posterior para usar esta tarjeta. Actualice el controlador, vuelva a ejecutar la instalación y elija NVIDIA.
french.accelOld=Votre pilote NVIDIA est %1. Cognita nécessite la version 580 ou ultérieure pour utiliser cette carte. Mettez le pilote à jour, relancez le programme et choisissez NVIDIA.
german.accelOld=Ihr NVIDIA-Treiber ist %1. Cognita benötigt Treiberversion 580 oder neuer, um diese Karte zu verwenden. Aktualisieren Sie den Treiber, starten Sie das Setup erneut und wählen Sie NVIDIA.
italian.accelOld=Il driver NVIDIA è %1. Cognita richiede la versione 580 o successiva per usare questa scheda. Aggiorna il driver, esegui di nuovo l’installazione e scegli NVIDIA.
brazilianportuguese.accelOld=O driver NVIDIA é %1. O Cognita precisa da versão 580 ou mais recente para usar esta placa. Atualize o driver, execute a instalação novamente e escolha NVIDIA.
english.accelNoBuild=This version of Cognita has no NVIDIA build, so it installs for the CPU.
spanish.accelNoBuild=Esta versión de Cognita no incluye una compilación para NVIDIA, por lo que se instalará para la CPU.
french.accelNoBuild=Cette version de Cognita ne propose pas de build NVIDIA ; l’installation utilisera donc le processeur.
german.accelNoBuild=Diese Cognita-Version enthält keinen NVIDIA-Build und wird daher für die CPU installiert.
italian.accelNoBuild=Questa versione di Cognita non include un pacchetto NVIDIA, quindi verrà installata per la CPU.
brazilianportuguese.accelNoBuild=Esta versão do Cognita não tem uma compilação para NVIDIA, então será instalada para uso com a CPU.
english.accelGpuChoice=Use the NVIDIA GPU (recommended).
spanish.accelGpuChoice=Usar la GPU NVIDIA (recomendado).
french.accelGpuChoice=Utiliser le GPU NVIDIA (recommandé).
german.accelGpuChoice=NVIDIA-GPU verwenden (empfohlen).
italian.accelGpuChoice=Usa la GPU NVIDIA (consigliato).
brazilianportuguese.accelGpuChoice=Usar a GPU NVIDIA (recomendado).
english.accelCpuChoice=Use the CPU only. Cognita downloads about %1.
spanish.accelCpuChoice=Usar solo la CPU. Cognita descargará unos %1.
french.accelCpuChoice=Utiliser uniquement le processeur. Cognita téléchargera environ %1.
german.accelCpuChoice=Nur die CPU verwenden. Cognita lädt etwa %1 herunter.
italian.accelCpuChoice=Usa solo la CPU. Cognita scaricherà circa %1.
brazilianportuguese.accelCpuChoice=Usar somente a CPU. O Cognita baixará cerca de %1.
english.accelGpuDownload= Cognita downloads about %1.
spanish.accelGpuDownload= Cognita descargará unos %1.
french.accelGpuDownload= Cognita téléchargera environ %1.
german.accelGpuDownload= Cognita lädt etwa %1 herunter.
italian.accelGpuDownload= Cognita scaricherà circa %1.
brazilianportuguese.accelGpuDownload= O Cognita baixará cerca de %1.
english.memoryReclaim=Let Windows reclaim memory that WSL holds as file cache. This adds one setting to %USERPROFILE%\.wslconfig, applying to all WSL distributions after WSL next starts.
spanish.memoryReclaim=Permitir que Windows recupere la memoria que WSL usa como caché de archivos. Añade una opción a %USERPROFILE%\.wslconfig y se aplica a todas las distribuciones WSL al próximo inicio de WSL.
french.memoryReclaim=Permettre à Windows de récupérer la mémoire que WSL utilise comme cache de fichiers. Cela ajoute un paramètre à %USERPROFILE%\.wslconfig, appliqué à toutes les distributions WSL au prochain démarrage de WSL.
german.memoryReclaim=Windows kann Speicher zurückgewinnen, den WSL als Dateicache belegt. Dafür wird eine Einstellung zu %USERPROFILE%\.wslconfig hinzugefügt, die nach dem nächsten WSL-Start für alle WSL-Distributionen gilt.
italian.memoryReclaim=Consenti a Windows di recuperare la memoria usata da WSL come cache dei file. Aggiunge un’impostazione a %USERPROFILE%\.wslconfig, applicata a tutte le distribuzioni WSL dal prossimo avvio di WSL.
brazilianportuguese.memoryReclaim=Permitir que o Windows recupere a memória que o WSL mantém como cache de arquivos. Isso adiciona uma configuração a %USERPROFILE%\.wslconfig, válida para todas as distribuições WSL após a próxima inicialização do WSL.
english.remoteTitle=Remote access (recommended)
spanish.remoteTitle=Acceso remoto (recomendado)
french.remoteTitle=Accès à distance (recommandé)
german.remoteTitle=Fernzugriff (empfohlen)
italian.remoteTitle=Accesso remoto (consigliato)
brazilianportuguese.remoteTitle=Acesso remoto (recomendado)
english.remoteSubtitle=Give your assistant a way to reach Cognita.
spanish.remoteSubtitle=Permita que su asistente acceda a Cognita.
french.remoteSubtitle=Permettez à votre assistant d’accéder à Cognita.
german.remoteSubtitle=Geben Sie Ihrem Assistenten Zugriff auf Cognita.
italian.remoteSubtitle=Consenti al tuo assistente di raggiungere Cognita.
brazilianportuguese.remoteSubtitle=Permita que seu assistente acesse o Cognita.
english.remoteBody=Assistants such as claude.ai, Claude Desktop, ChatGPT and Gemini connect to Cognita from their own servers, so they need a public address: a tunnel. Setup can create one now with Tailscale Funnel. It is free, and the address stays the same afterwards.%n%nYou can skip a tunnel only if you connect exclusively from a tool on this PC that can reach Cognita itself (Claude Code, Codex, Google Antigravity, Cursor, or a local LLM). Even then, a tunnel is recommended so any assistant can use Cognita.%n%nIf Tailscale is not installed, Setup downloads the official installer (about 40 MB) from pkgs.tailscale.com and runs it; Tailscale asks for permission itself. Cognita Admin is never made public.
spanish.remoteBody=Asistentes como claude.ai, Claude Desktop, ChatGPT y Gemini se conectan a Cognita desde sus propios servidores, por lo que necesitan una dirección pública: un túnel. La instalación puede crear uno ahora con Tailscale Funnel. Es gratuito y la dirección seguirá siendo la misma.%n%nPuede omitir el túnel solo si se conecta exclusivamente desde una herramienta de este equipo que pueda acceder directamente a Cognita (Claude Code, Codex, Google Antigravity, Cursor o un modelo local). Aun así, se recomienda un túnel para que cualquier asistente pueda usar Cognita.%n%nSi Tailscale no está instalado, la instalación descarga y ejecuta el instalador oficial (unos 40 MB) desde pkgs.tailscale.com; Tailscale solicitará permiso. La página Admin de Cognita nunca se hace pública.
french.remoteBody=Des assistants comme claude.ai, Claude Desktop, ChatGPT et Gemini se connectent à Cognita depuis leurs propres serveurs ; ils ont donc besoin d’une adresse publique, c’est-à-dire d’un tunnel. Le programme peut en créer un maintenant avec Tailscale Funnel. C’est gratuit et l’adresse restera la même.%n%nVous pouvez ignorer le tunnel uniquement si vous vous connectez exclusivement depuis un outil de ce PC qui accède directement à Cognita (Claude Code, Codex, Google Antigravity, Cursor ou un LLM local). Même dans ce cas, un tunnel est recommandé pour que tous les assistants puissent utiliser Cognita.%n%nSi Tailscale n’est pas installé, le programme télécharge et exécute l’installateur officiel (environ 40 Mo) depuis pkgs.tailscale.com ; Tailscale demande lui-même l’autorisation. L’interface Admin de Cognita n’est jamais rendue publique.
german.remoteBody=Assistenten wie claude.ai, Claude Desktop, ChatGPT und Gemini verbinden sich von ihren eigenen Servern mit Cognita und benötigen daher eine öffentliche Adresse, also einen Tunnel. Das Setup kann jetzt mit Tailscale Funnel einen einrichten. Er ist kostenlos und die Adresse bleibt dauerhaft gleich.%n%nSie können den Tunnel nur überspringen, wenn Sie ausschließlich ein Tool auf diesem PC verwenden, das direkt auf Cognita zugreifen kann (Claude Code, Codex, Google Antigravity, Cursor oder ein lokales Sprachmodell). Auch dann wird ein Tunnel empfohlen, damit jeder Assistent Cognita verwenden kann.%n%nFalls Tailscale nicht installiert ist, lädt das Setup den offiziellen Installer (etwa 40 MB) von pkgs.tailscale.com herunter und startet ihn. Tailscale fragt selbst nach der Berechtigung. Cognita Admin wird niemals öffentlich gemacht.
italian.remoteBody=Assistenti come claude.ai, Claude Desktop, ChatGPT e Gemini si connettono a Cognita dai propri server, quindi hanno bisogno di un indirizzo pubblico, cioè di un tunnel. L’installazione può crearne uno ora con Tailscale Funnel. È gratuito e l’indirizzo resterà lo stesso.%n%nPuoi evitare il tunnel solo se ti connetti esclusivamente da uno strumento su questo PC che raggiunge direttamente Cognita (Claude Code, Codex, Google Antigravity, Cursor o un modello locale). Anche in questo caso il tunnel è consigliato, così qualsiasi assistente può usare Cognita.%n%nSe Tailscale non è installato, l’installazione scarica ed esegue il programma ufficiale (circa 40 MB) da pkgs.tailscale.com; Tailscale chiede direttamente l’autorizzazione. La pagina Admin di Cognita non viene mai resa pubblica.
brazilianportuguese.remoteBody=Assistentes como claude.ai, Claude Desktop, ChatGPT e Gemini se conectam ao Cognita pelos próprios servidores e precisam de um endereço público: um túnel. A instalação pode criar um agora com o Tailscale Funnel. É gratuito e o endereço continuará o mesmo.%n%nVocê pode pular o túnel somente se usar exclusivamente uma ferramenta neste computador que acesse o Cognita diretamente (Claude Code, Codex, Google Antigravity, Cursor ou um modelo local). Mesmo assim, o túnel é recomendado para que qualquer assistente possa usar o Cognita.%n%nSe o Tailscale não estiver instalado, a instalação baixa e executa o instalador oficial (cerca de 40 MB) de pkgs.tailscale.com; o próprio Tailscale solicitará permissão. A página Admin do Cognita nunca é exposta publicamente.
english.remoteSkip=Skip for now. I can do it later with: cognita remote-access
spanish.remoteSkip=Omitir por ahora. Puedo hacerlo después con: cognita remote-access
french.remoteSkip=Ignorer pour l’instant. Je pourrai le faire plus tard avec : cognita remote-access
german.remoteSkip=Jetzt überspringen. Ich kann es später mit cognita remote-access einrichten.
italian.remoteSkip=Salta per ora. Potrò farlo più tardi con: cognita remote-access
brazilianportuguese.remoteSkip=Pular por enquanto. Posso fazer isso depois com: cognita remote-access
english.remoteSetup=Set up remote access with Tailscale Funnel now (recommended)
spanish.remoteSetup=Configurar ahora el acceso remoto con Tailscale Funnel (recomendado)
french.remoteSetup=Configurer maintenant l’accès à distance avec Tailscale Funnel (recommandé)
german.remoteSetup=Fernzugriff jetzt mit Tailscale Funnel einrichten (empfohlen)
italian.remoteSetup=Configura ora l’accesso remoto con Tailscale Funnel (consigliato)
brazilianportuguese.remoteSetup=Configurar o acesso remoto com o Tailscale Funnel agora (recomendado)
english.remoteNeedStep=Remote access needs one more step from you: open the link below and follow it, then press Retry, or press Next to continue without remote access.
spanish.remoteNeedStep=El acceso remoto requiere un paso más: abra el enlace siguiente y siga las instrucciones. Después, pulse Reintentar o Siguiente para continuar sin acceso remoto.
french.remoteNeedStep=L’accès à distance nécessite une étape supplémentaire : ouvrez le lien ci-dessous et suivez les instructions, puis cliquez sur Réessayer ou sur Suivant pour continuer sans accès à distance.
german.remoteNeedStep=Für den Fernzugriff ist noch ein Schritt erforderlich: Öffnen Sie den folgenden Link und folgen Sie den Anweisungen. Klicken Sie danach auf Wiederholen oder Weiter, um ohne Fernzugriff fortzufahren.
italian.remoteNeedStep=Per l’accesso remoto serve un altro passaggio: apri il link qui sotto e segui le istruzioni, poi premi Riprova oppure Avanti per continuare senza accesso remoto.
brazilianportuguese.remoteNeedStep=O acesso remoto precisa de mais uma etapa: abra o link abaixo e siga as instruções. Depois, clique em Tentar novamente ou Avançar para continuar sem acesso remoto.
english.remotePortConflict=Tailscale Funnel on this PC already uses public port 443 for something else, and Setup will not replace it.%n%nUse public port %1 for Cognita instead? Its address will then end in :%1.
spanish.remotePortConflict=Tailscale Funnel ya usa el puerto público 443 de este equipo para otra cosa y la instalación no lo reemplazará.%n%n¿Usar el puerto público %1 para Cognita? La dirección terminará en :%1.
french.remotePortConflict=Tailscale Funnel utilise déjà le port public 443 de ce PC pour autre chose ; le programme ne le remplacera pas.%n%nUtiliser plutôt le port public %1 pour Cognita ? Son adresse se terminera par :%1.
german.remotePortConflict=Tailscale Funnel verwendet auf diesem PC den öffentlichen Port 443 bereits für etwas anderes. Das Setup ersetzt diese Zuordnung nicht.%n%nSoll Cognita stattdessen den öffentlichen Port %1 verwenden? Die Adresse endet dann auf :%1.
italian.remotePortConflict=Tailscale Funnel usa già la porta pubblica 443 di questo PC per altro e l’installazione non la sostituirà.%n%nVuoi usare la porta pubblica %1 per Cognita? L’indirizzo terminerà con :%1.
brazilianportuguese.remotePortConflict=O Tailscale Funnel já usa a porta pública 443 deste computador para outra coisa, e a instalação não substituirá essa configuração.%n%nUsar a porta pública %1 para o Cognita? O endereço terminará em :%1.
english.remoteSkipped=Remote access was skipped: port 443 of your Funnel is in use. Run cognita remote-access to choose another port.
spanish.remoteSkipped=Se omitió el acceso remoto porque el puerto 443 de Funnel está en uso. Ejecute cognita remote-access para elegir otro puerto.
french.remoteSkipped=L’accès à distance a été ignoré car le port 443 de votre Funnel est utilisé. Exécutez cognita remote-access pour choisir un autre port.
german.remoteSkipped=Der Fernzugriff wurde übersprungen, weil Port 443 Ihres Funnels belegt ist. Führen Sie cognita remote-access aus, um einen anderen Port auszuwählen.
italian.remoteSkipped=Accesso remoto saltato: la porta 443 del Funnel è in uso. Esegui cognita remote-access per scegliere un’altra porta.
brazilianportuguese.remoteSkipped=O acesso remoto foi ignorado porque a porta 443 do Funnel está em uso. Execute cognita remote-access para escolher outra porta.
english.remoteOnNext=Remote access is on. Press Next to finish.
spanish.remoteOnNext=El acceso remoto está activado. Pulse Siguiente para terminar.
french.remoteOnNext=L’accès à distance est activé. Cliquez sur Suivant pour terminer.
german.remoteOnNext=Der Fernzugriff ist aktiviert. Klicken Sie auf Weiter, um den Vorgang abzuschließen.
italian.remoteOnNext=L’accesso remoto è attivo. Premi Avanti per terminare.
brazilianportuguese.remoteOnNext=O acesso remoto está ativado. Clique em Avançar para concluir.
english.remoteOnNote=Remote access is on.
spanish.remoteOnNote=El acceso remoto está activado.
french.remoteOnNote=L’accès à distance est activé.
german.remoteOnNote=Der Fernzugriff ist aktiviert.
italian.remoteOnNote=L’accesso remoto è attivo.
brazilianportuguese.remoteOnNote=O acesso remoto está ativado.
english.remoteSkippedNext=Remote access was skipped. Press Next to finish.
spanish.remoteSkippedNext=Se omitió el acceso remoto. Pulse Siguiente para terminar.
french.remoteSkippedNext=L’accès à distance a été ignoré. Cliquez sur Suivant pour terminer.
german.remoteSkippedNext=Der Fernzugriff wurde übersprungen. Klicken Sie auf Weiter, um den Vorgang abzuschließen.
italian.remoteSkippedNext=Accesso remoto saltato. Premi Avanti per terminare.
brazilianportuguese.remoteSkippedNext=O acesso remoto foi ignorado. Clique em Avançar para concluir.
english.remoteFailed=Remote access was not set up.%n%n%1%nCognita itself is installed and working. You can try again later with: cognita remote-access
spanish.remoteFailed=No se configuró el acceso remoto.%n%n%1%nCognita está instalado y funciona. Puede volver a intentarlo más tarde con: cognita remote-access
french.remoteFailed=L’accès à distance n’a pas été configuré.%n%n%1%nCognita est installé et fonctionne. Vous pourrez réessayer plus tard avec : cognita remote-access
german.remoteFailed=Der Fernzugriff wurde nicht eingerichtet.%n%n%1%nCognita ist installiert und funktioniert. Sie können es später mit cognita remote-access erneut versuchen.
italian.remoteFailed=Accesso remoto non configurato.%n%n%1%nCognita è installato e funziona. Puoi riprovare più tardi con: cognita remote-access
brazilianportuguese.remoteFailed=O acesso remoto não foi configurado.%n%n%1%nO Cognita está instalado e funcionando. Você pode tentar novamente depois com: cognita remote-access
english.remoteFailurePrefix=Remote access was not set up:
spanish.remoteFailurePrefix=No se configuró el acceso remoto:
french.remoteFailurePrefix=L’accès à distance n’a pas été configuré :
german.remoteFailurePrefix=Der Fernzugriff wurde nicht eingerichtet:
italian.remoteFailurePrefix=Accesso remoto non configurato:
brazilianportuguese.remoteFailurePrefix=O acesso remoto não foi configurado:
english.remoteTryLater=You can try again any time with: cognita remote-access
spanish.remoteTryLater=Puede volver a intentarlo cuando quiera con: cognita remote-access
french.remoteTryLater=Vous pourrez réessayer quand vous le souhaitez avec : cognita remote-access
german.remoteTryLater=Sie können es jederzeit mit cognita remote-access erneut versuchen.
italian.remoteTryLater=Puoi riprovare in qualsiasi momento con: cognita remote-access
brazilianportuguese.remoteTryLater=Você pode tentar novamente a qualquer momento com: cognita remote-access
english.remoteUnavailable=Remote access was not set up because Setup could not start its helper in time. Run cognita remote-access later.
spanish.remoteUnavailable=No se configuró el acceso remoto porque la instalación no pudo iniciar su asistente a tiempo. Ejecute cognita remote-access más tarde.
french.remoteUnavailable=L’accès à distance n’a pas été configuré, car le programme n’a pas pu démarrer son assistant à temps. Exécutez cognita remote-access plus tard.
german.remoteUnavailable=Der Fernzugriff wurde nicht eingerichtet, weil das Setup seinen Helfer nicht rechtzeitig starten konnte. Führen Sie später cognita remote-access aus.
italian.remoteUnavailable=Accesso remoto non configurato perché l’installazione non è riuscita ad avviare l’assistente in tempo. Esegui cognita remote-access più tardi.
brazilianportuguese.remoteUnavailable=O acesso remoto não foi configurado porque a instalação não conseguiu iniciar o auxiliar a tempo. Execute cognita remote-access mais tarde.
english.restartHeading=A restart is needed
spanish.restartHeading=Es necesario reiniciar
french.restartHeading=Un redémarrage est nécessaire
german.restartHeading=Ein Neustart ist erforderlich
italian.restartHeading=È necessario riavviare
brazilianportuguese.restartHeading=É necessário reiniciar
english.restartResume=Windows must restart to finish turning on WSL. Save your work in other programs first. Setup opens again by itself after you sign back in and continues.
spanish.restartResume=Windows debe reiniciarse para terminar de activar WSL. Guarde primero su trabajo en otros programas. La instalación se abrirá de nuevo automáticamente después de iniciar sesión y continuará.
french.restartResume=Windows doit redémarrer pour terminer l’activation de WSL. Enregistrez d’abord votre travail dans les autres programmes. Le programme se rouvrira automatiquement après votre reconnexion et reprendra.
german.restartResume=Windows muss neu gestartet werden, um die Aktivierung von WSL abzuschließen. Speichern Sie zuerst Ihre Arbeit in anderen Programmen. Nach der Anmeldung öffnet sich das Setup automatisch wieder und wird fortgesetzt.
italian.restartResume=Windows deve riavviarsi per completare l’attivazione di WSL. Salva prima il lavoro negli altri programmi. Dopo aver effettuato di nuovo l’accesso, l’installazione si riaprirà automaticamente e continuerà.
brazilianportuguese.restartResume=O Windows precisa reiniciar para concluir a ativação do WSL. Salve o trabalho em outros programas primeiro. A instalação abrirá novamente após você entrar no Windows e continuará automaticamente.
english.restartNoResume=Windows must restart to finish turning on WSL. Save your work in other programs first. Setup could not save where it stopped, so run Setup again after the restart to continue.
spanish.restartNoResume=Windows debe reiniciarse para terminar de activar WSL. Guarde primero su trabajo en otros programas. No se pudo guardar el punto de continuación; vuelva a ejecutar la instalación después del reinicio.
french.restartNoResume=Windows doit redémarrer pour terminer l’activation de WSL. Enregistrez d’abord votre travail dans les autres programmes. Le point de reprise n’a pas pu être enregistré ; relancez le programme après le redémarrage.
german.restartNoResume=Windows muss neu gestartet werden, um WSL zu aktivieren. Speichern Sie zuerst Ihre Arbeit. Das Setup konnte den Fortsetzungspunkt nicht speichern; starten Sie es nach dem Neustart erneut.
italian.restartNoResume=Windows deve riavviarsi per completare l’attivazione di WSL. Salva prima il lavoro negli altri programmi. Non è stato possibile salvare il punto di ripresa; esegui di nuovo l’installazione dopo il riavvio.
brazilianportuguese.restartNoResume=O Windows precisa reiniciar para concluir a ativação do WSL. Salve o trabalho em outros programas primeiro. Não foi possível salvar o ponto de retomada; execute a instalação novamente após reiniciar.
english.finishedHeading=Cognita is installed
spanish.finishedHeading=Cognita está instalado
french.finishedHeading=Cognita est installé
german.finishedHeading=Cognita ist installiert
italian.finishedHeading=Cognita è installato
brazilianportuguese.finishedHeading=O Cognita está instalado
english.finishedRunning=Cognita %1 is running.
spanish.finishedRunning=Cognita %1 está en ejecución.
french.finishedRunning=Cognita %1 est en cours d’exécution.
german.finishedRunning=Cognita %1 läuft.
italian.finishedRunning=Cognita %1 è in esecuzione.
brazilianportuguese.finishedRunning=O Cognita %1 está em execução.
english.finishedStarts= Cognita starts when you sign in to Windows.
spanish.finishedStarts= Cognita se inicia cuando inicia sesión en Windows.
french.finishedStarts= Cognita démarre lorsque vous ouvrez une session Windows.
german.finishedStarts= Cognita startet, wenn Sie sich bei Windows anmelden.
italian.finishedStarts= Cognita si avvia quando accedi a Windows.
brazilianportuguese.finishedStarts= O Cognita inicia quando você entra no Windows.
english.finishedWorkspace=Workspace:  %1
spanish.finishedWorkspace=Workspace:  %1
french.finishedWorkspace=Workspace :  %1
german.finishedWorkspace=Workspace:  %1
italian.finishedWorkspace=Workspace:  %1
brazilianportuguese.finishedWorkspace=Workspace:  %1
english.finishedUser= (user: %1)
spanish.finishedUser= (usuario: %1)
french.finishedUser= (utilisateur : %1)
german.finishedUser= (Benutzer: %1)
italian.finishedUser= (utente: %1)
brazilianportuguese.finishedUser= (usuário: %1)
english.finishedConnect=To connect claude.ai or ChatGPT: open Cognita Admin, go to Connectors, create a connector, and use its address.
spanish.finishedConnect=Para conectar claude.ai o ChatGPT: abra Cognita Admin, vaya a Conectores, cree un conector y use su dirección.
french.finishedConnect=Pour connecter claude.ai ou ChatGPT : ouvrez Cognita Admin, accédez à Connecteurs, créez un connecteur et utilisez son adresse.
german.finishedConnect=So verbinden Sie claude.ai oder ChatGPT: Öffnen Sie Cognita Admin, gehen Sie zu Connectors, erstellen Sie einen Connector und verwenden Sie dessen Adresse.
italian.finishedConnect=Per collegare claude.ai o ChatGPT: apri Cognita Admin, vai a Connectors, crea un connector e usa il relativo indirizzo.
brazilianportuguese.finishedConnect=Para conectar o claude.ai ou ChatGPT: abra o Cognita Admin, acesse Conectores, crie um conector e use o endereço dele.
english.failedStep=Step that failed:  %1
spanish.failedStep=Paso que falló:  %1
french.failedStep=Étape en échec :  %1
german.failedStep=Fehlgeschlagener Schritt:  %1
italian.failedStep=Passaggio non riuscito:  %1
brazilianportuguese.failedStep=Etapa que falhou:  %1
english.failureStageStarting=Starting
spanish.failureStageStarting=Inicio
french.failureStageStarting=Démarrage
german.failureStageStarting=Start
italian.failureStageStarting=Avvio
brazilianportuguese.failureStageStarting=Início
english.failureStagePassword=Handing the Admin password to the installer
spanish.failureStagePassword=Entrega de la contraseña de Admin al instalador
french.failureStagePassword=Transmission du mot de passe Admin au programme d’installation
german.failureStagePassword=Übergabe des Admin-Kennworts an das Setup
italian.failureStagePassword=Consegna della password Admin al programma di installazione
brazilianportuguese.failureStagePassword=Entrega da senha Admin ao instalador
english.failureStageUnknown=Current step
spanish.failureStageUnknown=Paso actual
french.failureStageUnknown=Étape en cours
german.failureStageUnknown=Aktueller Schritt
italian.failureStageUnknown=Passaggio corrente
brazilianportuguese.failureStageUnknown=Etapa atual
english.technicalDetail=Technical detail:
spanish.technicalDetail=Detalle técnico:
french.technicalDetail=Détail technique :
german.technicalDetail=Technische Details:
italian.technicalDetail=Dettagli tecnici:
brazilianportuguese.technicalDetail=Detalhe técnico:
english.whatToDo=What to do:
spanish.whatToDo=Qué hacer:
french.whatToDo=Que faire :
german.whatToDo=Was zu tun ist:
italian.whatToDo=Cosa fare:
brazilianportuguese.whatToDo=O que fazer:
english.showFile=Show the file
spanish.showFile=Mostrar el archivo
french.showFile=Afficher le fichier
german.showFile=Datei anzeigen
italian.showFile=Mostra il file
brazilianportuguese.showFile=Mostrar o arquivo
english.advancedTitle=Advanced
spanish.advancedTitle=Avanzado
french.advancedTitle=Avancé
german.advancedTitle=Erweitert
italian.advancedTitle=Avanzate
brazilianportuguese.advancedTitle=Avançado
english.advancedMcpPort=MCP port (connectors):
spanish.advancedMcpPort=Puerto MCP (conectores):
french.advancedMcpPort=Port MCP (connecteurs) :
german.advancedMcpPort=MCP-Port (Connectors):
italian.advancedMcpPort=Porta MCP (connector):
brazilianportuguese.advancedMcpPort=Porta MCP (conectores):
english.advancedFunnelPort=Remote access uses this port. Turn remote access off first to change it.
spanish.advancedFunnelPort=El acceso remoto usa este puerto. Desactive primero el acceso remoto para cambiarlo.
french.advancedFunnelPort=L’accès à distance utilise ce port. Désactivez-le d’abord pour le modifier.
german.advancedFunnelPort=Der Fernzugriff verwendet diesen Port. Deaktivieren Sie ihn zuerst, um den Port zu ändern.
italian.advancedFunnelPort=L’accesso remoto usa questa porta. Disattivalo prima per modificarla.
brazilianportuguese.advancedFunnelPort=O acesso remoto usa esta porta. Desative-o primeiro para alterá-la.
english.advancedAdminPort=Admin port:
spanish.advancedAdminPort=Puerto Admin:
french.advancedAdminPort=Port Admin :
german.advancedAdminPort=Admin-Port:
italian.advancedAdminPort=Porta Admin:
brazilianportuguese.advancedAdminPort=Porta Admin:
english.advancedUpdateData=An update keeps your ports, your Workspace setting and where Cognita keeps its data. To change the ports or Workspace, run Setup again after the update.
spanish.advancedUpdateData=Una actualización conserva los puertos, la configuración de Workspace y la ubicación de los datos de Cognita. Para cambiarlos, vuelva a ejecutar la instalación después de actualizar.
french.advancedUpdateData=Une mise à jour conserve les ports, le réglage Workspace et l’emplacement des données de Cognita. Pour les modifier, relancez le programme après la mise à jour.
german.advancedUpdateData=Bei einer Aktualisierung bleiben Ports, Workspace-Einstellung und Cognitas Datenspeicherort erhalten. Führen Sie das Setup danach erneut aus, um Ports oder Workspace zu ändern.
italian.advancedUpdateData=Un aggiornamento mantiene le porte, l’impostazione di Workspace e la posizione dei dati di Cognita. Per modificarli, esegui di nuovo il programma dopo l’aggiornamento.
brazilianportuguese.advancedUpdateData=Uma atualização mantém as portas, a configuração do Workspace e o local dos dados do Cognita. Para alterá-los, execute novamente a instalação após a atualização.
english.advancedDataLocked=Where Cognita keeps its data (its index, settings and Workspace). Cognita's Linux system is already there, so this cannot change. Your documents stay where they are.
spanish.advancedDataLocked=Ubicación de los datos de Cognita (índice, configuración y Workspace). El sistema Linux de Cognita ya está aquí, por lo que no se puede cambiar. Sus documentos permanecen donde están.
french.advancedDataLocked=Emplacement des données de Cognita (index, paramètres et Workspace). Le système Linux de Cognita se trouve déjà ici et ne peut pas être déplacé. Vos documents restent à leur emplacement.
german.advancedDataLocked=Speicherort der Cognita-Daten (Index, Einstellungen und Workspace). Cognitas Linux-System befindet sich bereits hier und kann nicht verschoben werden. Ihre Dokumente bleiben am bisherigen Speicherort.
italian.advancedDataLocked=Dove Cognita conserva i dati (indice, impostazioni e Workspace). Il sistema Linux di Cognita è già qui e non può essere spostato. I documenti restano dove sono.
brazilianportuguese.advancedDataLocked=Local dos dados do Cognita (índice, configurações e Workspace). O sistema Linux do Cognita já está aqui e não pode ser movido. Seus documentos permanecem onde estão.
english.advancedDataFresh=Where Cognita keeps its data (its index, settings and Workspace). Choose a new or empty folder on a drive with enough free space if C: is small. Your documents stay where they are.
spanish.advancedDataFresh=Ubicación de los datos de Cognita (índice, configuración y Workspace). Si la unidad C: tiene poco espacio, elija una carpeta nueva o vacía en otra unidad con espacio suficiente. Sus documentos permanecen donde están.
french.advancedDataFresh=Emplacement des données de Cognita (index, paramètres et Workspace). Si le lecteur C: manque d’espace, choisissez un dossier nouveau ou vide sur un lecteur disposant d’assez d’espace. Vos documents restent à leur emplacement.
german.advancedDataFresh=Speicherort der Cognita-Daten (Index, Einstellungen und Workspace). Wenn auf C: wenig Platz ist, wählen Sie einen neuen oder leeren Ordner auf einem Laufwerk mit genügend freiem Speicherplatz. Ihre Dokumente bleiben am bisherigen Speicherort.
italian.advancedDataFresh=Dove Cognita conserva i dati (indice, impostazioni e Workspace). Se l’unità C: ha poco spazio, scegli una cartella nuova o vuota su un’unità con spazio sufficiente. I documenti restano dove sono.
brazilianportuguese.advancedDataFresh=Local dos dados do Cognita (índice, configurações e Workspace). Se a unidade C: tiver pouco espaço, escolha uma pasta nova ou vazia em outra unidade com espaço suficiente. Seus documentos permanecem onde estão.
english.advancedBrowse=Browse...
spanish.advancedBrowse=Examinar...
french.advancedBrowse=Parcourir...
german.advancedBrowse=Durchsuchen...
italian.advancedBrowse=Sfoglia...
brazilianportuguese.advancedBrowse=Procurar...
english.browseDataTitle=Choose where Cognita keeps its data
spanish.browseDataTitle=Elija dónde guarda Cognita sus datos
french.browseDataTitle=Choisissez l’emplacement des données de Cognita
german.browseDataTitle=Speicherort der Cognita-Daten auswählen
italian.browseDataTitle=Scegli dove Cognita conserva i dati
brazilianportuguese.browseDataTitle=Escolher onde o Cognita mantém os dados
english.dataDriveRoot=Choose a folder on that drive, not the whole drive (for example %1Cognita).
spanish.dataDriveRoot=Elija una carpeta de esa unidad, no la unidad completa (por ejemplo, %1Cognita).
french.dataDriveRoot=Choisissez un dossier sur ce lecteur, pas le lecteur entier (par exemple %1Cognita).
german.dataDriveRoot=Wählen Sie einen Ordner auf diesem Laufwerk statt des gesamten Laufwerks (zum Beispiel %1Cognita).
italian.dataDriveRoot=Scegli una cartella sull’unità, non l’intera unità (ad esempio %1Cognita).
brazilianportuguese.dataDriveRoot=Escolha uma pasta nessa unidade, não a unidade inteira (por exemplo, %1Cognita).
english.dataOwnDrive=Choose a data folder on one of this PC's own drives (a full path that starts with a drive letter).
spanish.dataOwnDrive=Elija una carpeta de datos en una unidad local de este equipo (una ruta completa que empiece con una letra de unidad).
french.dataOwnDrive=Choisissez un dossier de données sur l’un des lecteurs de ce PC (un chemin complet commençant par une lettre de lecteur).
german.dataOwnDrive=Wählen Sie einen Datenordner auf einem lokalen Laufwerk dieses PCs (einen vollständigen Pfad, der mit einem Laufwerksbuchstaben beginnt).
italian.dataOwnDrive=Scegli una cartella dati su una delle unità locali di questo PC (un percorso completo che inizia con una lettera di unità).
brazilianportuguese.dataOwnDrive=Escolha uma pasta de dados em uma das unidades deste computador (um caminho completo que comece com uma letra de unidade).
english.dataOneDrive=Cognita's data cannot live in a OneDrive folder.
spanish.dataOneDrive=Los datos de Cognita no pueden estar en una carpeta de OneDrive.
french.dataOneDrive=Les données de Cognita ne peuvent pas se trouver dans un dossier OneDrive.
german.dataOneDrive=Cognita-Daten dürfen nicht in einem OneDrive-Ordner liegen.
italian.dataOneDrive=I dati di Cognita non possono trovarsi in una cartella OneDrive.
brazilianportuguese.dataOneDrive=Os dados do Cognita não podem ficar em uma pasta do OneDrive.
english.dataFolderNotEmpty=Choose a new or empty folder. That folder already holds files, and Cognita's data must not be mixed with them.
spanish.dataFolderNotEmpty=Elija una carpeta nueva o vacía. Esa carpeta ya contiene archivos y los datos de Cognita no deben mezclarse con ellos.
french.dataFolderNotEmpty=Choisissez un dossier nouveau ou vide. Ce dossier contient déjà des fichiers ; les données de Cognita ne doivent pas y être mélangées.
german.dataFolderNotEmpty=Wählen Sie einen neuen oder leeren Ordner. Dieser Ordner enthält bereits Dateien; Cognita-Daten dürfen nicht damit vermischt werden.
italian.dataFolderNotEmpty=Scegli una cartella nuova o vuota. Questa cartella contiene già file e i dati di Cognita non devono essere mescolati con essi.
brazilianportuguese.dataFolderNotEmpty=Escolha uma pasta nova ou vazia. Ela já contém arquivos, e os dados do Cognita não devem ser misturados a eles.
english.dataCheckFailed=Setup could not check that folder.
spanish.dataCheckFailed=La instalación no pudo comprobar esa carpeta.
french.dataCheckFailed=Le programme n’a pas pu vérifier ce dossier.
german.dataCheckFailed=Das Setup konnte diesen Ordner nicht prüfen.
italian.dataCheckFailed=L’installazione non è riuscita a controllare questa cartella.
brazilianportuguese.dataCheckFailed=A instalação não conseguiu verificar essa pasta.
english.dataFolderInvalid=The data folder cannot contain a double quote.
spanish.dataFolderInvalid=La carpeta de datos no puede contener comillas dobles.
french.dataFolderInvalid=Le dossier de données ne peut pas contenir de guillemet double.
german.dataFolderInvalid=Der Datenordner darf kein doppeltes Anführungszeichen enthalten.
italian.dataFolderInvalid=La cartella dei dati non può contenere virgolette doppie.
brazilianportuguese.dataFolderInvalid=A pasta de dados não pode conter aspas duplas.
english.dataFolderUnsupported=Cognita cannot use that folder.
spanish.dataFolderUnsupported=Cognita no puede usar esa carpeta.
french.dataFolderUnsupported=Cognita ne peut pas utiliser ce dossier.
german.dataFolderUnsupported=Cognita kann diesen Ordner nicht verwenden.
italian.dataFolderUnsupported=Cognita non può usare questa cartella.
brazilianportuguese.dataFolderUnsupported=O Cognita não pode usar essa pasta.
english.advancedWorkspace=Turn on Workspace (Cognita can run code for you in a sandbox)
spanish.advancedWorkspace=Activar Workspace (Cognita puede ejecutar código en un entorno aislado)
french.advancedWorkspace=Activer Workspace (Cognita peut exécuter du code dans un environnement isolé)
german.advancedWorkspace=Workspace aktivieren (Cognita kann Code in einer Sandbox ausführen)
italian.advancedWorkspace=Attiva Workspace (Cognita può eseguire codice in un ambiente isolato)
brazilianportuguese.advancedWorkspace=Ativar o Workspace (o Cognita pode executar código em uma área isolada)
english.invalidMcpPort=The MCP port must be a number from 1024 to 65535.
spanish.invalidMcpPort=El puerto MCP debe ser un número entre 1024 y 65535.
french.invalidMcpPort=Le port MCP doit être un nombre compris entre 1024 et 65535.
german.invalidMcpPort=Der MCP-Port muss eine Zahl zwischen 1024 und 65535 sein.
italian.invalidMcpPort=La porta MCP deve essere un numero da 1024 a 65535.
brazilianportuguese.invalidMcpPort=A porta MCP deve ser um número entre 1024 e 65535.
english.invalidAdminPort=The Admin port must be a number from 1024 to 65535.
spanish.invalidAdminPort=El puerto Admin debe ser un número entre 1024 y 65535.
french.invalidAdminPort=Le port Admin doit être un nombre compris entre 1024 et 65535.
german.invalidAdminPort=Der Admin-Port muss eine Zahl zwischen 1024 und 65535 sein.
italian.invalidAdminPort=La porta Admin deve essere un numero da 1024 a 65535.
brazilianportuguese.invalidAdminPort=A porta Admin deve ser um número entre 1024 e 65535.
english.portsDifferent=The MCP port and the Admin port must be different.
spanish.portsDifferent=El puerto MCP y el puerto Admin deben ser distintos.
french.portsDifferent=Le port MCP et le port Admin doivent être différents.
german.portsDifferent=MCP-Port und Admin-Port müssen unterschiedlich sein.
italian.portsDifferent=La porta MCP e la porta Admin devono essere diverse.
brazilianportuguese.portsDifferent=As portas MCP e Admin devem ser diferentes.
english.diagnosticsSaved=Diagnostics were saved to:%n%1%n%nThey contain logs and settings but no passwords, tokens or documents. Nothing was sent anywhere.%n%nTo get help, attach this file to a new issue at%n%2%n%nOpen that page now?
spanish.diagnosticsSaved=Los diagnósticos se guardaron en:%n%1%n%nIncluyen registros y configuración, pero no contraseñas, tokens ni documentos. No se envió nada.%n%nPara obtener ayuda, adjunte este archivo a un nuevo informe en%n%2%n%n¿Abrir esa página ahora?
french.diagnosticsSaved=Les diagnostics ont été enregistrés dans :%n%1%n%nIls contiennent des journaux et des paramètres, mais aucun mot de passe, jeton ni document. Rien n’a été envoyé.%n%nPour obtenir de l’aide, joignez ce fichier à un nouveau ticket sur%n%2%n%nOuvrir cette page maintenant ?
german.diagnosticsSaved=Diagnosedaten wurden gespeichert unter:%n%1%n%nSie enthalten Protokolle und Einstellungen, aber keine Kennwörter, Tokens oder Dokumente. Es wurde nichts gesendet.%n%nWenn Sie Hilfe benötigen, hängen Sie diese Datei an ein neues Problem an unter%n%2%n%nDiese Seite jetzt öffnen?
italian.diagnosticsSaved=La diagnostica è stata salvata in:%n%1%n%nContiene registri e impostazioni, ma non password, token o documenti. Non è stato inviato nulla.%n%nPer ricevere assistenza, allega questo file a una nuova segnalazione su%n%2%n%nAprire la pagina ora?
brazilianportuguese.diagnosticsSaved=Os diagnósticos foram salvos em:%n%1%n%nEles contêm registros e configurações, mas não senhas, tokens ou documentos. Nada foi enviado.%n%nPara obter ajuda, anexe este arquivo a um novo problema em%n%2%n%nAbrir essa página agora?
english.diagnosticsFailure=Saving diagnostics failed.%n%n%1%n%2
spanish.diagnosticsFailure=No se pudieron guardar los diagnósticos.%n%n%1%n%2
french.diagnosticsFailure=L’enregistrement des diagnostics a échoué.%n%n%1%n%2
german.diagnosticsFailure=Diagnosedaten konnten nicht gespeichert werden.%n%n%1%n%2
italian.diagnosticsFailure=Salvataggio della diagnostica non riuscito.%n%n%1%n%2
brazilianportuguese.diagnosticsFailure=Falha ao salvar os diagnósticos.%n%n%1%n%2
english.diagnosticsSavedSentence=Diagnostics were saved to:%n%1%nNothing was sent anywhere. To get help, attach that file to a new issue at %2 (Report a problem).
spanish.diagnosticsSavedSentence=Los diagnósticos se guardaron en:%n%1%nNo se envió nada. Para obtener ayuda, adjunte el archivo a un nuevo informe en %2 (Informar de un problema).
french.diagnosticsSavedSentence=Les diagnostics ont été enregistrés dans :%n%1%nRien n’a été envoyé. Pour obtenir de l’aide, joignez ce fichier à un nouveau ticket sur %2 (Signaler un problème).
german.diagnosticsSavedSentence=Diagnosedaten wurden gespeichert unter:%n%1%nEs wurde nichts gesendet. Wenn Sie Hilfe benötigen, hängen Sie die Datei unter %2 an ein neues Problem an (Problem melden).
italian.diagnosticsSavedSentence=La diagnostica è stata salvata in:%n%1%nNon è stato inviato nulla. Per ricevere assistenza, allega il file a una nuova segnalazione su %2 (Segnala un problema).
brazilianportuguese.diagnosticsSavedSentence=Os diagnósticos foram salvos em:%n%1%nNada foi enviado. Para obter ajuda, anexe o arquivo a um novo problema em %2 (Relatar um problema).
english.diagnosticsFailedSentence=Nothing was sent anywhere. Saving diagnostics did not work; the logs are in %1. To get help, attach them to a new issue at %2 (Report a problem).
spanish.diagnosticsFailedSentence=No se envió nada. No se pudieron guardar los diagnósticos; los registros están en %1. Para obtener ayuda, adjúntelos a un nuevo informe en %2 (Informar de un problema).
french.diagnosticsFailedSentence=Rien n’a été envoyé. L’enregistrement des diagnostics a échoué ; les journaux se trouvent dans %1. Pour obtenir de l’aide, joignez-les à un nouveau ticket sur %2 (Signaler un problème).
german.diagnosticsFailedSentence=Es wurde nichts gesendet. Diagnosedaten konnten nicht gespeichert werden; die Protokolle befinden sich unter %1. Wenn Sie Hilfe benötigen, hängen Sie sie unter %2 an ein neues Problem an (Problem melden).
italian.diagnosticsFailedSentence=Non è stato inviato nulla. Non è stato possibile salvare la diagnostica; i registri si trovano in %1. Per ricevere assistenza, allegali a una nuova segnalazione su %2 (Segnala un problema).
brazilianportuguese.diagnosticsFailedSentence=Nada foi enviado. Não foi possível salvar os diagnósticos; os registros estão em %1. Para obter ajuda, anexe-os a um novo problema em %2 (Relatar um problema).
english.wslUnavailable=WSL is still not available. Run Setup again after restarting Windows.
spanish.wslUnavailable=WSL aún no está disponible. Vuelva a ejecutar la instalación después de reiniciar Windows.
french.wslUnavailable=WSL n’est toujours pas disponible. Relancez le programme après avoir redémarré Windows.
german.wslUnavailable=WSL ist weiterhin nicht verfügbar. Starten Sie Windows neu und führen Sie das Setup erneut aus.
italian.wslUnavailable=WSL non è ancora disponibile. Esegui di nuovo l’installazione dopo aver riavviato Windows.
brazilianportuguese.wslUnavailable=O WSL ainda não está disponível. Execute a instalação novamente após reiniciar o Windows.
english.restartContinue=Setup will continue after your next restart.
spanish.restartContinue=La instalación continuará después del próximo reinicio.
french.restartContinue=Le programme reprendra après votre prochain redémarrage.
german.restartContinue=Das Setup wird nach dem nächsten Neustart fortgesetzt.
italian.restartContinue=L’installazione continuerà dopo il prossimo riavvio.
brazilianportuguese.restartContinue=A instalação continuará após a próxima reinicialização.
english.powerShellMissing=Windows PowerShell 5.1 was not found at %1. Cognita Setup needs it.
spanish.powerShellMissing=No se encontró Windows PowerShell 5.1 en %1. La instalación de Cognita lo necesita.
french.powerShellMissing=Windows PowerShell 5.1 est introuvable à l’emplacement %1. Le programme d’installation de Cognita en a besoin.
german.powerShellMissing=Windows PowerShell 5.1 wurde unter %1 nicht gefunden. Das Cognita-Setup benötigt es.
italian.powerShellMissing=Windows PowerShell 5.1 non è stato trovato in %1. L’installazione di Cognita ne ha bisogno.
brazilianportuguese.powerShellMissing=O Windows PowerShell 5.1 não foi encontrado em %1. A instalação do Cognita precisa dele.
english.helperUnpackFailed=Setup could not unpack its helper script:%n%1
spanish.helperUnpackFailed=La instalación no pudo extraer su script auxiliar:%n%1
french.helperUnpackFailed=Le programme n’a pas pu extraire son script auxiliaire :%n%1
german.helperUnpackFailed=Das Setup konnte sein Hilfsskript nicht entpacken:%n%1
italian.helperUnpackFailed=L’installazione non è riuscita a estrarre lo script ausiliario:%n%1
brazilianportuguese.helperUnpackFailed=A instalação não conseguiu extrair o script auxiliar:%n%1
english.stateReadFailed=Setup could not read the current state of this PC.%n%n%1%nSetup log: %2
spanish.stateReadFailed=La instalación no pudo leer el estado actual de este equipo.%n%n%1%nRegistro de instalación: %2
french.stateReadFailed=Le programme n’a pas pu lire l’état actuel de ce PC.%n%n%1%nJournal d’installation : %2
german.stateReadFailed=Das Setup konnte den aktuellen Zustand dieses PCs nicht lesen.%n%n%1%nSetup-Protokoll: %2
italian.stateReadFailed=L’installazione non è riuscita a leggere lo stato attuale del PC.%n%n%1%nRegistro dell’installazione: %2
brazilianportuguese.stateReadFailed=A instalação não conseguiu ler o estado atual deste computador.%n%n%1%nRegistro da instalação: %2
english.uninstallCleanupFailed=Cognita's own files are being removed, but part of the cleanup did not finish.%n%n%1%nLogs: %2
spanish.uninstallCleanupFailed=Se están quitando los archivos propios de Cognita, pero no terminó parte de la limpieza.%n%n%1%nRegistros: %2
french.uninstallCleanupFailed=Les fichiers de Cognita sont en cours de suppression, mais une partie du nettoyage n’a pas abouti.%n%n%1%nJournaux : %2
german.uninstallCleanupFailed=Die Cognita-Dateien werden entfernt, aber ein Teil der Bereinigung wurde nicht abgeschlossen.%n%n%1%nProtokolle: %2
italian.uninstallCleanupFailed=I file di Cognita vengono rimossi, ma una parte della pulizia non è stata completata.%n%n%1%nRegistri: %2
brazilianportuguese.uninstallCleanupFailed=Os arquivos próprios do Cognita estão sendo removidos, mas parte da limpeza não foi concluída.%n%n%1%nRegistros: %2
english.uninstallDataFailed=Cognita was removed, but its data could not be deleted. Logs: %1
spanish.uninstallDataFailed=Se quitó Cognita, pero no se pudieron eliminar sus datos. Registros: %1
french.uninstallDataFailed=Cognita a été supprimé, mais ses données n’ont pas pu l’être. Journaux : %1
german.uninstallDataFailed=Cognita wurde entfernt, seine Daten konnten jedoch nicht gelöscht werden. Protokolle: %1
italian.uninstallDataFailed=Cognita è stato rimosso, ma non è stato possibile eliminare i dati. Registri: %1
brazilianportuguese.uninstallDataFailed=O Cognita foi removido, mas não foi possível excluir os dados. Registros: %1
english.failurePreflight=Cognita cannot be installed on this PC yet.
spanish.failurePreflight=Todavía no se puede instalar Cognita en este equipo.
french.failurePreflight=Cognita ne peut pas encore être installé sur ce PC.
german.failurePreflight=Cognita kann auf diesem PC noch nicht installiert werden.
italian.failurePreflight=Cognita non può ancora essere installato su questo PC.
brazilianportuguese.failurePreflight=O Cognita ainda não pode ser instalado neste computador.
english.failureWsl=Setup could not turn on WSL.
spanish.failureWsl=La instalación no pudo activar WSL.
french.failureWsl=Le programme n’a pas pu activer WSL.
german.failureWsl=Das Setup konnte WSL nicht aktivieren.
italian.failureWsl=L’installazione non è riuscita ad attivare WSL.
brazilianportuguese.failureWsl=A instalação não conseguiu ativar o WSL.
english.failureRestart=Setup could not restart Windows.
spanish.failureRestart=La instalación no pudo reiniciar Windows.
french.failureRestart=Le programme n’a pas pu redémarrer Windows.
german.failureRestart=Das Setup konnte Windows nicht neu starten.
italian.failureRestart=L’installazione non è riuscita a riavviare Windows.
brazilianportuguese.failureRestart=A instalação não conseguiu reiniciar o Windows.
english.failureResume=Setup could not save where it stopped.
spanish.failureResume=La instalación no pudo guardar el punto donde se detuvo.
french.failureResume=Le programme n’a pas pu enregistrer son point d’arrêt.
german.failureResume=Das Setup konnte seine letzte Position nicht speichern.
italian.failureResume=L’installazione non è riuscita a salvare il punto in cui si è fermata.
brazilianportuguese.failureResume=A instalação não conseguiu salvar o ponto em que parou.
english.failureFolder=Setup could not check that folder.
spanish.failureFolder=La instalación no pudo comprobar esa carpeta.
french.failureFolder=Le programme n’a pas pu vérifier ce dossier.
german.failureFolder=Das Setup konnte diesen Ordner nicht prüfen.
italian.failureFolder=L’installazione non è riuscita a controllare questa cartella.
brazilianportuguese.failureFolder=A instalação não conseguiu verificar essa pasta.
english.failureUpdateChoices=Setup cannot update with the current state of this PC. Fix the problem and try again.
spanish.failureUpdateChoices=La instalación no puede actualizar con el estado actual de este equipo. Corrija el problema y vuelva a intentarlo.
french.failureUpdateChoices=Le programme ne peut pas effectuer la mise à jour dans l’état actuel de ce PC. Corrigez le problème et réessayez.
german.failureUpdateChoices=Das Setup kann mit dem aktuellen Zustand dieses PCs nicht aktualisieren. Beheben Sie das Problem und versuchen Sie es erneut.
italian.failureUpdateChoices=L’installazione non può aggiornare con lo stato attuale del PC. Correggi il problema e riprova.
brazilianportuguese.failureUpdateChoices=A instalação não pode atualizar com o estado atual deste computador. Corrija o problema e tente novamente.
english.failureInstallChoices=Setup cannot install with these choices. Change them under Advanced, or fix the problem, and try again.
spanish.failureInstallChoices=La instalación no puede continuar con estas opciones. Cámbielas en Avanzado o corrija el problema y vuelva a intentarlo.
french.failureInstallChoices=Le programme ne peut pas installer avec ces choix. Modifiez-les dans Avancé ou corrigez le problème, puis réessayez.
german.failureInstallChoices=Das Setup kann mit diesen Einstellungen nicht installieren. Ändern Sie sie unter Erweitert oder beheben Sie das Problem und versuchen Sie es erneut.
italian.failureInstallChoices=L’installazione non può procedere con queste scelte. Modificale in Avanzate o correggi il problema, poi riprova.
brazilianportuguese.failureInstallChoices=A instalação não pode continuar com estas opções. Altere-as em Avançado ou corrija o problema e tente novamente.
english.selfTestsSkipped=The self-tests were skipped. To run them later, run this Setup again.
spanish.selfTestsSkipped=Se omitieron las pruebas automáticas. Para ejecutarlas más adelante, vuelva a iniciar esta instalación.
french.selfTestsSkipped=Les autotests ont été ignorés. Pour les exécuter plus tard, relancez ce programme d’installation.
german.selfTestsSkipped=Die Selbsttests wurden übersprungen. Führen Sie dieses Setup später erneut aus, um sie zu starten.
italian.selfTestsSkipped=I test automatici sono stati saltati. Per eseguirli più tardi, avvia di nuovo questa installazione.
brazilianportuguese.selfTestsSkipped=Os autotestes foram ignorados. Para executá-los depois, inicie esta instalação novamente.
english.finishedDailyIntro=Every day: use the Start menu (Cognita Admin, Cognita Status, Cognita Diagnostics), or type these in a terminal window:
spanish.finishedDailyIntro=Para el uso diario: use el menú Inicio (Cognita Admin, Cognita Status, Cognita Diagnostics) o escriba estos comandos en una terminal:
french.finishedDailyIntro=Au quotidien : utilisez le menu Démarrer (Cognita Admin, Cognita Status, Cognita Diagnostics) ou saisissez ces commandes dans un terminal :
german.finishedDailyIntro=Für den täglichen Gebrauch: Verwenden Sie das Startmenü (Cognita Admin, Cognita Status, Cognita Diagnostics) oder geben Sie diese Befehle in einem Terminal ein:
italian.finishedDailyIntro=Per l’uso quotidiano: usa il menu Start (Cognita Admin, Cognita Status, Cognita Diagnostics) oppure digita questi comandi in un terminale:
brazilianportuguese.finishedDailyIntro=No dia a dia: use o menu Iniciar (Cognita Admin, Cognita Status, Cognita Diagnostics) ou digite estes comandos em um terminal:
english.finishedUpdate=Update: run a newer Cognita Setup.
spanish.finishedUpdate=Actualizar: ejecute una instalación más reciente de Cognita.
french.finishedUpdate=Mise à jour : exécutez une version plus récente du programme d’installation de Cognita.
german.finishedUpdate=Aktualisieren: Führen Sie ein neueres Cognita-Setup aus.
italian.finishedUpdate=Aggiornamento: esegui una versione più recente dell’installazione di Cognita.
brazilianportuguese.finishedUpdate=Atualização: execute uma instalação mais recente do Cognita.
english.finishedUninstall=Uninstall: Settings > Apps > Installed apps.
spanish.finishedUninstall=Desinstalar: Configuración > Aplicaciones > Aplicaciones instaladas.
french.finishedUninstall=Désinstallation : Paramètres > Applications > Applications installées.
german.finishedUninstall=Deinstallieren: Einstellungen > Apps > Installierte Apps.
italian.finishedUninstall=Disinstallazione: Impostazioni > App > App installate.
brazilianportuguese.finishedUninstall=Desinstalar: Configurações > Aplicativos > Aplicativos instalados.
english.publicLabel=Public:
spanish.publicLabel=Público:
french.publicLabel=Public :
german.publicLabel=Öffentlich:
italian.publicLabel=Pubblico:
brazilianportuguese.publicLabel=Público:
english.logFolderLabel=Log folder:
spanish.logFolderLabel=Carpeta de registros:
french.logFolderLabel=Dossier des journaux :
german.logFolderLabel=Protokollordner:
italian.logFolderLabel=Cartella dei registri:
brazilianportuguese.logFolderLabel=Pasta de registros:
english.setupLogLabel=Setup log:
spanish.setupLogLabel=Registro de instalación:
french.setupLogLabel=Journal d’installation :
german.setupLogLabel=Setup-Protokoll:
italian.setupLogLabel=Registro dell’installazione:
brazilianportuguese.setupLogLabel=Registro da instalação:
english.failureContinue=Nothing is lost: run Setup again to continue.
spanish.failureContinue=No se pierde nada: vuelva a ejecutar la instalación para continuar.
french.failureContinue=Rien n’est perdu : relancez le programme pour continuer.
german.failureContinue=Es geht nichts verloren: Führen Sie das Setup erneut aus, um fortzufahren.
italian.failureContinue=Non si perde nulla: esegui di nuovo l’installazione per continuare.
brazilianportuguese.failureContinue=Nada será perdido: execute a instalação novamente para continuar.
english.removeTitle=Remove Cognita
spanish.removeTitle=Quitar Cognita
french.removeTitle=Supprimer Cognita
german.removeTitle=Cognita entfernen
italian.removeTitle=Rimuovi Cognita
brazilianportuguese.removeTitle=Remover o Cognita
english.removeHead=Remove Cognita. Your documents are never touched.
spanish.removeHead=Se quitará Cognita. Sus documentos nunca se modifican.
french.removeHead=Cognita sera supprimé. Vos documents ne sont jamais modifiés.
german.removeHead=Cognita wird entfernt. Ihre Dokumente bleiben unberührt.
italian.removeHead=Cognita verrà rimosso. I documenti non vengono mai modificati.
brazilianportuguese.removeHead=O Cognita será removido. Seus documentos nunca são alterados.
english.keepData=Keep Cognita's data (recommended)
spanish.keepData=Conservar los datos de Cognita (recomendado)
french.keepData=Conserver les données de Cognita (recommandé)
german.keepData=Cognita-Daten behalten (empfohlen)
italian.keepData=Conserva i dati di Cognita (consigliato)
brazilianportuguese.keepData=Manter os dados do Cognita (recomendado)
english.keepDataNote=Installing Cognita again reuses your index, settings and credentials.
spanish.keepDataNote=Al volver a instalar Cognita, se reutilizarán el índice, la configuración y las credenciales.
french.keepDataNote=La réinstallation de Cognita réutilisera votre index, vos paramètres et vos identifiants.
german.keepDataNote=Bei einer erneuten Installation verwendet Cognita Ihren Index, Ihre Einstellungen und Ihre Zugangsdaten weiter.
italian.keepDataNote=Reinstallando Cognita verranno riutilizzati l’indice, le impostazioni e le credenziali.
brazilianportuguese.keepDataNote=Ao instalar o Cognita novamente, seu índice, suas configurações e credenciais serão reutilizados.
english.deleteData=Also delete Cognita's data: index, settings, credentials, Workspace
spanish.deleteData=Eliminar también los datos de Cognita: índice, configuración, credenciales y Workspace
french.deleteData=Supprimer aussi les données de Cognita : index, paramètres, identifiants et Workspace
german.deleteData=Cognita-Daten ebenfalls löschen: Index, Einstellungen, Zugangsdaten und Workspace
italian.deleteData=Elimina anche i dati di Cognita: indice, impostazioni, credenziali e Workspace
brazilianportuguese.deleteData=Excluir também os dados do Cognita: índice, configurações, credenciais e Workspace
english.deleteDataNoteSize=That is %1 in %2. This cannot be undone. Setup asks you to confirm.
spanish.deleteDataNoteSize=Son %1 en %2. Esta acción no se puede deshacer. La instalación le pedirá confirmación.
french.deleteDataNoteSize=Cela représente %1 dans %2. Cette action est irréversible. Le programme vous demandera confirmation.
german.deleteDataNoteSize=Das sind %1 unter %2. Dieser Vorgang kann nicht rückgängig gemacht werden. Das Setup fragt nach einer Bestätigung.
italian.deleteDataNoteSize=Si tratta di %1 in %2. L’operazione è irreversibile. Il programma chiederà una conferma.
brazilianportuguese.deleteDataNoteSize=São %1 em %2. Esta ação não pode ser desfeita. A instalação pedirá sua confirmação.
english.deleteDataNotePath=It is in %1. This cannot be undone. Setup asks you to confirm.
spanish.deleteDataNotePath=Está en %1. Esta acción no se puede deshacer. La instalación le pedirá confirmación.
french.deleteDataNotePath=Ces données se trouvent dans %1. Cette action est irréversible. Le programme vous demandera confirmation.
german.deleteDataNotePath=Die Daten liegen unter %1. Dieser Vorgang kann nicht rückgängig gemacht werden. Das Setup fragt nach einer Bestätigung.
italian.deleteDataNotePath=Si trova in %1. L’operazione è irreversibile. Il programma chiederà una conferma.
brazilianportuguese.deleteDataNotePath=Está em %1. Esta ação não pode ser desfeita. A instalação pedirá sua confirmação.
english.deleteDataNoteNoPath=This cannot be undone. Setup asks you to confirm.
spanish.deleteDataNoteNoPath=Esta acción no se puede deshacer. La instalación le pedirá confirmación.
french.deleteDataNoteNoPath=Cette action est irréversible. Le programme vous demandera confirmation.
german.deleteDataNoteNoPath=Dieser Vorgang kann nicht rückgängig gemacht werden. Das Setup fragt nach einer Bestätigung.
italian.deleteDataNoteNoPath=L’operazione è irreversibile. Il programma chiederà una conferma.
brazilianportuguese.deleteDataNoteNoPath=Esta ação não pode ser desfeita. A instalação pedirá sua confirmação.
english.continueButton=Continue
spanish.continueButton=Continuar
french.continueButton=Continuer
german.continueButton=Weiter
italian.continueButton=Continua
brazilianportuguese.continueButton=Continuar
english.cancelButton=Cancel
spanish.cancelButton=Cancelar
french.cancelButton=Annuler
german.cancelButton=Abbrechen
italian.cancelButton=Annulla
brazilianportuguese.cancelButton=Cancelar
english.deleteConfirmTitle=Delete Cognita's data
spanish.deleteConfirmTitle=Eliminar los datos de Cognita
french.deleteConfirmTitle=Supprimer les données de Cognita
german.deleteConfirmTitle=Cognita-Daten löschen
italian.deleteConfirmTitle=Elimina i dati di Cognita
brazilianportuguese.deleteConfirmTitle=Excluir os dados do Cognita
english.deleteConfirmStart=This permanently deletes:
spanish.deleteConfirmStart=Se eliminarán de forma permanente:
french.deleteConfirmStart=Cette action supprimera définitivement :
german.deleteConfirmStart=Dies wird dauerhaft gelöscht:
italian.deleteConfirmStart=Verranno eliminati definitivamente:
brazilianportuguese.deleteConfirmStart=Os itens a seguir serão excluídos permanentemente:
english.deleteConfirmLinux=- The Cognita Linux system and everything in it: search index, settings, connector keys, credentials and Workspace files (at %1)
spanish.deleteConfirmLinux=- El sistema Linux de Cognita y todo su contenido: índice de búsqueda, configuración, claves de conectores, credenciales y archivos de Workspace (en %1)
french.deleteConfirmLinux=- Le système Linux de Cognita et tout son contenu : index de recherche, paramètres, clés des connecteurs, identifiants et fichiers Workspace (dans %1)
german.deleteConfirmLinux=- Das Cognita-Linux-System und alle darin enthaltenen Daten: Suchindex, Einstellungen, Connectorschlüssel, Zugangsdaten und Workspace-Dateien (unter %1)
italian.deleteConfirmLinux=- Il sistema Linux di Cognita e tutto ciò che contiene: indice di ricerca, impostazioni, chiavi dei connettori, credenziali e file Workspace (in %1)
brazilianportuguese.deleteConfirmLinux=- O sistema Linux do Cognita e tudo o que contém: índice de pesquisa, configurações, chaves de conectores, credenciais e arquivos do Workspace (em %1)
english.deleteConfirmWindows=- Cognita's Windows settings and logs (under %1)
spanish.deleteConfirmWindows=- La configuración y los registros de Cognita para Windows (en %1)
french.deleteConfirmWindows=- Les paramètres Windows et les journaux de Cognita (dans %1)
german.deleteConfirmWindows=- Cognitas Windows-Einstellungen und Protokolle (unter %1)
italian.deleteConfirmWindows=- Le impostazioni e i registri di Cognita per Windows (in %1)
brazilianportuguese.deleteConfirmWindows=- As configurações e os logs do Cognita no Windows (em %1)
english.deleteConfirmDocuments=Your documents are never touched. Setup first copies the logs to %1.
spanish.deleteConfirmDocuments=Sus documentos nunca se modifican. La instalación copia primero los registros en %1.
french.deleteConfirmDocuments=Vos documents ne sont jamais modifiés. Le programme copie d’abord les journaux dans %1.
german.deleteConfirmDocuments=Ihre Dokumente bleiben unberührt. Das Setup kopiert die Protokolle zuerst nach %1.
italian.deleteConfirmDocuments=I documenti non vengono mai modificati. Il programma copia prima i registri in %1.
brazilianportuguese.deleteConfirmDocuments=Seus documentos nunca são alterados. A instalação copia os logs primeiro para %1.
english.deleteConfirmInstruction=To go ahead, type the word DELETE below.
spanish.deleteConfirmInstruction=Para continuar, escriba la palabra DELETE abajo.
french.deleteConfirmInstruction=Pour continuer, saisissez le mot DELETE ci-dessous.
german.deleteConfirmInstruction=Geben Sie unten DELETE ein, um fortzufahren.
italian.deleteConfirmInstruction=Per continuare, digita la parola DELETE qui sotto.
brazilianportuguese.deleteConfirmInstruction=Para continuar, digite a palavra DELETE abaixo.
english.deleteButton=Delete
spanish.deleteButton=Eliminar
french.deleteButton=Supprimer
german.deleteButton=Löschen
italian.deleteButton=Elimina
brazilianportuguese.deleteButton=Excluir
english.progressBytes=%1 transferred
spanish.progressBytes=%1 transferidos
french.progressBytes=%1 transférés
german.progressBytes=%1 übertragen
italian.progressBytes=%1 trasferiti
brazilianportuguese.progressBytes=%1 transferidos
english.failureNoHelperResult=The Cognita helper stopped without a result (exit code %1).
spanish.failureNoHelperResult=El asistente de Cognita se detuvo sin devolver un resultado (código de salida %1).
french.failureNoHelperResult=L’assistant Cognita s’est arrêté sans résultat (code de sortie %1).
german.failureNoHelperResult=Der Cognita-Helfer wurde ohne Ergebnis beendet (Exitcode %1).
italian.failureNoHelperResult=L’assistente di Cognita si è fermato senza un risultato (codice di uscita %1).
brazilianportuguese.failureNoHelperResult=O auxiliar do Cognita parou sem apresentar um resultado (código de saída %1).
english.failureNoHelperReason=The Cognita helper reported a failure without saying what went wrong (exit code %1).
spanish.failureNoHelperReason=El asistente de Cognita indicó un error, pero no explicó qué ocurrió (código de salida %1).
french.failureNoHelperReason=L’assistant Cognita a signalé un échec sans en préciser la cause (code de sortie %1).
german.failureNoHelperReason=Der Cognita-Helfer meldete einen Fehler, ohne die Ursache anzugeben (Exitcode %1).
italian.failureNoHelperReason=L’assistente di Cognita ha segnalato un errore senza indicarne la causa (codice di uscita %1).
brazilianportuguese.failureNoHelperReason=O auxiliar do Cognita informou uma falha sem explicar o que aconteceu (código de saída %1).
english.failurePowerShellStart=Windows PowerShell could not be started (%1).
spanish.failurePowerShellStart=No se pudo iniciar Windows PowerShell (%1).
french.failurePowerShellStart=Impossible de démarrer Windows PowerShell (%1).
german.failurePowerShellStart=Windows PowerShell konnte nicht gestartet werden (%1).
italian.failurePowerShellStart=Impossibile avviare Windows PowerShell (%1).
brazilianportuguese.failurePowerShellStart=Não foi possível iniciar o Windows PowerShell (%1).
english.failureHelperException=Running the Cognita helper failed: %1
spanish.failureHelperException=Se produjo un error al ejecutar el asistente de Cognita: %1
french.failureHelperException=Une erreur s’est produite lors de l’exécution de l’assistant Cognita : %1
german.failureHelperException=Beim Ausführen des Cognita-Helfers ist ein Fehler aufgetreten: %1
italian.failureHelperException=Errore durante l’esecuzione dell’assistente di Cognita: %1
brazilianportuguese.failureHelperException=Ocorreu um erro ao executar o auxiliar do Cognita: %1

; Windows Setup progress and failure text. Helper protocol values remain English.
english.busySavingDiagnostics=Saving diagnostics
spanish.busySavingDiagnostics=Guardando los diagnósticos
french.busySavingDiagnostics=Enregistrement des diagnostics
german.busySavingDiagnostics=Diagnosedaten werden gespeichert
italian.busySavingDiagnostics=Salvataggio dei dati diagnostici
brazilianportuguese.busySavingDiagnostics=Salvando os diagnósticos
english.busyCheckingFolder=Checking the data folder
spanish.busyCheckingFolder=Comprobando la carpeta de datos
french.busyCheckingFolder=Vérification du dossier de données
german.busyCheckingFolder=Datenordner wird überprüft
italian.busyCheckingFolder=Verifica della cartella dei dati
brazilianportuguese.busyCheckingFolder=Verificando a pasta de dados
english.busyCheckingThisPc=Checking this PC
spanish.busyCheckingThisPc=Comprobando este equipo
french.busyCheckingThisPc=Vérification de ce PC
german.busyCheckingThisPc=Dieser PC wird überprüft
italian.busyCheckingThisPc=Verifica di questo PC
brazilianportuguese.busyCheckingThisPc=Verificando este computador
english.busyTurningOnWsl=Turning on WSL
spanish.busyTurningOnWsl=Activando WSL
french.busyTurningOnWsl=Activation de WSL
german.busyTurningOnWsl=WSL wird aktiviert
italian.busyTurningOnWsl=Attivazione di WSL
brazilianportuguese.busyTurningOnWsl=Ativando o WSL
english.busyRestartingWindows=Restarting Windows
spanish.busyRestartingWindows=Reiniciando Windows
french.busyRestartingWindows=Redémarrage de Windows
german.busyRestartingWindows=Windows wird neu gestartet
italian.busyRestartingWindows=Riavvio di Windows
brazilianportuguese.busyRestartingWindows=Reiniciando o Windows
english.busySavingPlace=Saving your place
spanish.busySavingPlace=Guardando el punto de continuación
french.busySavingPlace=Enregistrement du point de reprise
german.busySavingPlace=Fortsetzungspunkt wird gespeichert
italian.busySavingPlace=Salvataggio del punto di ripresa
brazilianportuguese.busySavingPlace=Salvando o ponto de retomada
english.busyCheckingPorts=Checking ports and disk space
spanish.busyCheckingPorts=Comprobando los puertos y el espacio en disco
french.busyCheckingPorts=Vérification des ports et de l’espace disque
german.busyCheckingPorts=Ports und Speicherplatz werden überprüft
italian.busyCheckingPorts=Verifica delle porte e dello spazio su disco
brazilianportuguese.busyCheckingPorts=Verificando as portas e o espaço em disco
english.wslPageDescription=One-time Windows setup before Cognita can install.
spanish.wslPageDescription=Configuración única de Windows antes de instalar Cognita.
french.wslPageDescription=Configuration unique de Windows avant l’installation de Cognita.
german.wslPageDescription=Einmalige Windows-Einrichtung, bevor Cognita installiert werden kann.
italian.wslPageDescription=Configurazione iniziale di Windows prima di installare Cognita.
brazilianportuguese.wslPageDescription=Configuração única do Windows antes de instalar o Cognita.
english.progressStoppingTests=Stopping the self-tests...
spanish.progressStoppingTests=Deteniendo las pruebas automáticas...
french.progressStoppingTests=Arrêt des autotests...
german.progressStoppingTests=Selbsttests werden angehalten...
italian.progressStoppingTests=Interruzione dei test automatici...
brazilianportuguese.progressStoppingTests=Interrompendo os autotestes...
english.progressBytesOf=%1 of %2
spanish.progressBytesOf=%1 de %2
french.progressBytesOf=%1 sur %2
german.progressBytesOf=%1 von %2
italian.progressBytesOf=%1 di %2
brazilianportuguese.progressBytesOf=%1 de %2
english.progressElapsed=Elapsed %1
spanish.progressElapsed=Tiempo transcurrido: %1
french.progressElapsed=Temps écoulé : %1
german.progressElapsed=Verstrichene Zeit: %1
italian.progressElapsed=Tempo trascorso: %1
brazilianportuguese.progressElapsed=Tempo decorrido: %1
english.remoteProgressCaption=Setting up remote access
spanish.remoteProgressCaption=Configurando el acceso remoto
french.remoteProgressCaption=Configuration de l’accès à distance
german.remoteProgressCaption=Fernzugriff wird eingerichtet
italian.remoteProgressCaption=Configurazione dell’accesso remoto
brazilianportuguese.remoteProgressCaption=Configurando o acesso remoto
english.remoteProgressDescription=Setup is setting up remote access. Keep this window open.
spanish.remoteProgressDescription=La instalación está configurando el acceso remoto. Mantenga esta ventana abierta.
french.remoteProgressDescription=Le programme configure l’accès à distance. Gardez cette fenêtre ouverte.
german.remoteProgressDescription=Das Setup richtet den Fernzugriff ein. Lassen Sie dieses Fenster geöffnet.
italian.remoteProgressDescription=L’installazione sta configurando l’accesso remoto. Lascia aperta questa finestra.
brazilianportuguese.remoteProgressDescription=A instalação está configurando o acesso remoto. Mantenha esta janela aberta.
english.remoteProgressPort=Setting up remote access on port %1
spanish.remoteProgressPort=Configurando el acceso remoto en el puerto %1
french.remoteProgressPort=Configuration de l’accès à distance sur le port %1
german.remoteProgressPort=Fernzugriff über Port %1 wird eingerichtet
italian.remoteProgressPort=Configurazione dell’accesso remoto sulla porta %1
brazilianportuguese.remoteProgressPort=Configurando o acesso remoto na porta %1
english.installDescription=This can take several minutes; keep this window open.
spanish.installDescription=Esto puede tardar varios minutos; mantenga esta ventana abierta.
french.installDescription=Cela peut prendre plusieurs minutes ; gardez cette fenêtre ouverte.
german.installDescription=Dies kann einige Minuten dauern. Lassen Sie dieses Fenster geöffnet.
italian.installDescription=Potrebbero volerci alcuni minuti; lascia aperta questa finestra.
brazilianportuguese.installDescription=Isso pode levar alguns minutos; mantenha esta janela aberta.
english.progressPreparing=Preparing
spanish.progressPreparing=Preparando
french.progressPreparing=Préparation
german.progressPreparing=Vorbereitung
italian.progressPreparing=Preparazione
brazilianportuguese.progressPreparing=Preparando
english.progressUnpacking=Unpacking. This takes a moment.
spanish.progressUnpacking=Descomprimiendo. Esto tardará un momento.
french.progressUnpacking=Décompression en cours. Cela prend un instant.
german.progressUnpacking=Dateien werden entpackt. Dies dauert einen Moment.
italian.progressUnpacking=Estrazione in corso. Ci vorrà un momento.
brazilianportuguese.progressUnpacking=Descompactando. Isso levará um instante.
english.progressPreparingUpdate=Preparing the update
spanish.progressPreparingUpdate=Preparando la actualización
french.progressPreparingUpdate=Préparation de la mise à jour
german.progressPreparingUpdate=Update wird vorbereitet
italian.progressPreparingUpdate=Preparazione dell’aggiornamento
brazilianportuguese.progressPreparingUpdate=Preparando a atualização
english.progressPreparingLinux=Preparing the Linux image
spanish.progressPreparingLinux=Preparando la imagen de Linux
french.progressPreparingLinux=Préparation de l’image Linux
german.progressPreparingLinux=Linux-Image wird vorbereitet
italian.progressPreparingLinux=Preparazione dell’immagine Linux
brazilianportuguese.progressPreparingLinux=Preparando a imagem do Linux
english.progressPreparingFiles=Preparing the Cognita files
spanish.progressPreparingFiles=Preparando los archivos de Cognita
french.progressPreparingFiles=Préparation des fichiers Cognita
german.progressPreparingFiles=Cognita-Dateien werden vorbereitet
italian.progressPreparingFiles=Preparazione dei file di Cognita
brazilianportuguese.progressPreparingFiles=Preparando os arquivos do Cognita
english.failurePasswordHandoff=Setup could not hand the Admin password to its helper: the Setup helper did not start in time.
spanish.failurePasswordHandoff=La instalación no pudo entregar la contraseña de administrador a su asistente: el asistente no se inició a tiempo.
french.failurePasswordHandoff=Le programme n’a pas pu transmettre le mot de passe Admin à son assistant, qui n’a pas démarré à temps.
german.failurePasswordHandoff=Das Setup konnte das Admin-Kennwort nicht an den Helfer übergeben: Der Setup-Helfer startete nicht rechtzeitig.
italian.failurePasswordHandoff=L’installazione non è riuscita a passare la password Admin all’assistente: l’assistente non si è avviato in tempo.
brazilianportuguese.failurePasswordHandoff=A instalação não conseguiu passar a senha Admin ao auxiliar: ele não foi iniciado a tempo.
english.failurePasswordFix=Run Setup again. If it keeps happening, choose Save diagnostics and attach the file to a new issue at %1
spanish.failurePasswordFix=Vuelva a ejecutar la instalación. Si vuelve a ocurrir, elija Guardar diagnósticos y adjunte el archivo a un nuevo informe en %1
french.failurePasswordFix=Relancez le programme. Si le problème persiste, choisissez Enregistrer les diagnostics et joignez le fichier à un nouveau signalement sur %1
german.failurePasswordFix=Führen Sie das Setup erneut aus. Falls das Problem weiterhin auftritt, wählen Sie Diagnosedaten speichern und fügen Sie die Datei einem neuen Bericht unter %1 bei.
italian.failurePasswordFix=Esegui di nuovo l’installazione. Se il problema continua, scegli Salva dati diagnostici e allega il file a una nuova segnalazione su %1
brazilianportuguese.failurePasswordFix=Execute a instalação novamente. Se o problema continuar, escolha Salvar diagnósticos e anexe o arquivo a um novo relato em %1
english.failureUnexpected=Setup stopped unexpectedly: %1
spanish.failureUnexpected=La instalación se detuvo inesperadamente: %1
french.failureUnexpected=Le programme s’est arrêté de manière inattendue : %1
german.failureUnexpected=Das Setup wurde unerwartet beendet: %1
italian.failureUnexpected=L’installazione si è interrotta inaspettatamente: %1
brazilianportuguese.failureUnexpected=A instalação parou inesperadamente: %1
english.failureUnexpectedFix=Choose Save diagnostics. To get help, attach the file to a new issue at %1
spanish.failureUnexpectedFix=Elija Guardar diagnósticos. Para obtener ayuda, adjunte el archivo a un nuevo informe en %1
french.failureUnexpectedFix=Choisissez Enregistrer les diagnostics. Pour obtenir de l’aide, joignez le fichier à un nouveau signalement sur %1
german.failureUnexpectedFix=Wählen Sie Diagnosedaten speichern. Wenn Sie Hilfe benötigen, fügen Sie die Datei einem neuen Bericht unter %1 bei.
italian.failureUnexpectedFix=Scegli Salva dati diagnostici. Per ricevere assistenza, allega il file a una nuova segnalazione su %1
brazilianportuguese.failureUnexpectedFix=Escolha Salvar diagnósticos. Para obter ajuda, anexe o arquivo a um novo relato em %1
english.failedUpdateHeading=Cognita was not updated
spanish.failedUpdateHeading=Cognita no se actualizó
french.failedUpdateHeading=Cognita n’a pas été mis à jour
german.failedUpdateHeading=Cognita wurde nicht aktualisiert
italian.failedUpdateHeading=Cognita non è stato aggiornato
brazilianportuguese.failedUpdateHeading=O Cognita não foi atualizado
english.failedRepairHeading=Cognita was not repaired
spanish.failedRepairHeading=Cognita no se reparó
french.failedRepairHeading=Cognita n’a pas été réparé
german.failedRepairHeading=Cognita wurde nicht repariert
italian.failedRepairHeading=Cognita non è stato riparato
brazilianportuguese.failedRepairHeading=O Cognita não foi reparado
english.failedReinstallHeading=Cognita was not reinstalled
spanish.failedReinstallHeading=Cognita no se reinstaló
french.failedReinstallHeading=Cognita n’a pas été réinstallé
german.failedReinstallHeading=Cognita wurde nicht neu installiert
italian.failedReinstallHeading=Cognita non è stato reinstallato
brazilianportuguese.failedReinstallHeading=O Cognita não foi reinstalado
english.failedInstallHeading=Cognita was not installed
spanish.failedInstallHeading=Cognita no se instaló
french.failedInstallHeading=Cognita n’a pas été installé
german.failedInstallHeading=Cognita wurde nicht installiert
italian.failedInstallHeading=Cognita non è stato installato
brazilianportuguese.failedInstallHeading=O Cognita não foi instalado
english.failedExistingInstallRemains=The Cognita you had is still there.
spanish.failedExistingInstallRemains=La instalación de Cognita que ya tenía sigue ahí.
french.failedExistingInstallRemains=Votre installation précédente de Cognita est toujours présente.
german.failedExistingInstallRemains=Die bisherige Cognita-Installation ist weiterhin vorhanden.
italian.failedExistingInstallRemains=L’installazione di Cognita esistente è ancora presente.
brazilianportuguese.failedExistingInstallRemains=A instalação anterior do Cognita continua disponível.
english.progressInstalling=Installing Cognita
spanish.progressInstalling=Instalando Cognita
french.progressInstalling=Installation de Cognita
german.progressInstalling=Cognita wird installiert
italian.progressInstalling=Installazione di Cognita
brazilianportuguese.progressInstalling=Instalando o Cognita
english.progressUpdating=Updating Cognita
spanish.progressUpdating=Actualizando Cognita
french.progressUpdating=Mise à jour de Cognita
german.progressUpdating=Cognita wird aktualisiert
italian.progressUpdating=Aggiornamento di Cognita
brazilianportuguese.progressUpdating=Atualizando o Cognita
english.progressRepairing=Repairing Cognita
spanish.progressRepairing=Reparando Cognita
french.progressRepairing=Réparation de Cognita
german.progressRepairing=Cognita wird repariert
italian.progressRepairing=Riparazione di Cognita
brazilianportuguese.progressRepairing=Reparando o Cognita
english.progressReinstalling=Reinstalling Cognita
spanish.progressReinstalling=Reinstalando Cognita
french.progressReinstalling=Réinstallation de Cognita
german.progressReinstalling=Cognita wird neu installiert
italian.progressReinstalling=Reinstallazione di Cognita
brazilianportuguese.progressReinstalling=Reinstalando o Cognita
english.finishedAcceleration=Acceleration:  %1
spanish.finishedAcceleration=Aceleración:  %1
french.finishedAcceleration=Accélération :  %1
german.finishedAcceleration=Beschleunigung:  %1
italian.finishedAcceleration=Accelerazione:  %1
brazilianportuguese.finishedAcceleration=Aceleração:  %1
english.warningAccelerationFix=To try the GPU again, run Setup again and choose NVIDIA.
spanish.warningAccelerationFix=Para volver a probar la GPU, ejecute la instalación de nuevo y elija NVIDIA.
french.warningAccelerationFix=Pour réessayer le GPU, relancez le programme et choisissez NVIDIA.
german.warningAccelerationFix=Um die GPU erneut zu testen, führen Sie das Setup erneut aus und wählen Sie NVIDIA.
italian.warningAccelerationFix=Per provare di nuovo la GPU, esegui l’installazione e scegli NVIDIA.
brazilianportuguese.warningAccelerationFix=Para testar a GPU novamente, execute a instalação outra vez e escolha NVIDIA.

[Files]
; Order matters little here (SolidCompression=no). Temporary copies first: the helper must run
; from {tmp} before Inno installs anything (section 4.2). The same source is installed below.
Source: "{#WindowsDir}\CognitaWin.ps1"; Flags: dontcopy noencryption
Source: "{#WindowsDir}\locales\windows-setup.en-US.json"; DestName: "windows-setup.en-US.json"; Flags: dontcopy noencryption
Source: "{#WindowsDir}\locales\windows-setup.es-ES.json"; DestName: "windows-setup.es-ES.json"; Flags: dontcopy noencryption
Source: "{#WindowsDir}\locales\windows-setup.fr-FR.json"; DestName: "windows-setup.fr-FR.json"; Flags: dontcopy noencryption
Source: "{#WindowsDir}\locales\windows-setup.de-DE.json"; DestName: "windows-setup.de-DE.json"; Flags: dontcopy noencryption
Source: "{#WindowsDir}\locales\windows-setup.it-IT.json"; DestName: "windows-setup.it-IT.json"; Flags: dontcopy noencryption
Source: "{#WindowsDir}\locales\windows-setup.pt-BR.json"; DestName: "windows-setup.pt-BR.json"; Flags: dontcopy noencryption
; The payload. DestName is the name ExtractTemporaryFile takes (verified on 6.7.3: it matches
; DestName, not the source file's own name). Extracted only when a step needs them, deleted after.
Source: "{#ImagePath}"; DestName: "cognita-wsl.tar.gz"; Flags: dontcopy nocompression noencryption
Source: "{#SrcPath}"; DestName: "cognita-src.tar.gz"; Flags: dontcopy nocompression noencryption
; Installed files: app\ (Inno owns and removes it) and bin\ (the launcher).
Source: "{#WindowsDir}\CognitaWin.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WindowsDir}\locales\windows-setup.en-US.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WindowsDir}\locales\windows-setup.es-ES.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WindowsDir}\locales\windows-setup.fr-FR.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WindowsDir}\locales\windows-setup.de-DE.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WindowsDir}\locales\windows-setup.it-IT.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WindowsDir}\locales\windows-setup.pt-BR.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WindowsDir}\cognita.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WindowsDir}\launch-keepalive.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#LauncherExe}"; DestDir: "{localappdata}\Cognita\bin"; DestName: "cognita.exe"; Flags: ignoreversion

[INI]
; Cognita Admin opens the Admin URL. The port is whatever Setup was given (Advanced can change it).
Filename: "{app}\CognitaAdmin.url"; Section: "InternetShortcut"; Key: "URL"; String: "{code:AdminUrl}"

[UninstallDelete]
Type: files; Name: "{app}\CognitaAdmin.url"

[Icons]
Name: "{group}\Cognita Admin"; Filename: "{app}\CognitaAdmin.url"
; -ExecutionPolicy Bypass: Windows 11's default policy blocks a local .ps1; -NoExit keeps the window open.
Name: "{group}\Cognita Status"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoExit -NoProfile -ExecutionPolicy Bypass -File ""{app}\cognita.ps1"" status"
; Diagnostics (design 19.4 item 23): the window is SHOWN, with its progress, and stays open (-NoExit) so
; the path of the saved zip can be read. It used to run hidden, which left the user clicking an entry
; that appeared to do nothing for as long as the collection took.
; Design 19.11 R4: it runs the HUMAN launcher (cognita.ps1 -> the helper's cli verb), the same form as the
; Status entry above, not CognitaWin.ps1 directly, whose output is the machine progress lines (JSON).
Name: "{group}\Cognita Diagnostics"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoExit -NoProfile -ExecutionPolicy Bypass -File ""{app}\cognita.ps1"" diagnostics"

[Registry]
; PATH (section 4.4): stays REG_EXPAND_SZ. Two entries so an empty PATH does not gain a leading ';'.
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{localappdata}\Cognita\bin"; Check: PathIsEmpty
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{localappdata}\Cognita\bin"; Check: PathNeedsAppend

[Run]
; The Finished page's "Open Cognita Admin". Offered only after a successful install.
Filename: "{code:AdminUrl}"; Description: "Open Cognita Admin"; Flags: postinstall shellexec skipifsilent; Check: OpenAdminOffered

[Code]
{ ---------------------------------------------------------------------------------------------
  Windows API calls: a named pipe for the Admin password (section 5.6, proven in spike S1),
  UuidCreate for the random pipe names, GetTickCount for the elapsed clock.
  --------------------------------------------------------------------------------------------- }
type
  TUuid = record
    D1: Cardinal;
    D2: Cardinal;
    D3: Cardinal;
    D4: Cardinal;
  end;

function GetTickCount: DWORD; external 'GetTickCount@kernel32.dll stdcall';
{ Setup's own process id (design 19.11 R1): passed to `restart-for-wsl --now --after-pid` so the helper's
  detached waiter restarts Windows only after THIS process has exited. }
function GetCurrentProcessId: Cardinal; external 'GetCurrentProcessId@kernel32.dll stdcall';
{ Design 21.4 (VM proof, 2026-09-29): while ExecAndLogOutput waits for the helper, Inno disables the
  wizard window, so nothing on the Installing page could be clicked (the Skip self-tests and Copy
  buttons showed as disabled). The helper's line callback re-enables the window; Inno still pumps
  messages while it waits, so the click then reaches the button. }
{ Plain Integer/Cardinal signatures: a BOOL/HWND-typed import raised "Could not call proc" at run time. }
function EnableWindow(hWnd: Cardinal; bEnable: Integer): Integer; external 'EnableWindow@user32.dll stdcall';
function IsWindowEnabled(hWnd: Cardinal): Integer; external 'IsWindowEnabled@user32.dll stdcall';
function CreateFileW(lpFileName: String; dwDesiredAccess, dwShareMode, lpSecurityAttributes,
  dwCreationDisposition, dwFlagsAndAttributes, hTemplateFile: Cardinal): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function WriteFile(hFile: THandle; const lpBuffer: String; nNumberOfBytesToWrite: Cardinal;
  var lpNumberOfBytesWritten: Cardinal; lpOverlapped: Cardinal): BOOL;
  external 'WriteFile@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): BOOL; external 'CloseHandle@kernel32.dll stdcall';
function UuidCreate(var Uuid: TUuid): Longint; external 'UuidCreate@rpcrt4.dll stdcall';
{ lpValue = 0 (a null pointer) deletes the variable from Setup's own environment. }
function SetEnvironmentVariableW(lpName: String; lpValue: Cardinal): BOOL;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';
function SetEnvironmentVariableTextW(lpName, lpValue: String): BOOL;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

const
  DefaultMcpPort = 8675;
  DefaultAdminPort = 8676;
  NvidiaDriverUrl = 'https://www.nvidia.com/drivers';   { design 22.1: shown, clickable, when the NVIDIA driver is older than 580 }

var
  { What `state` told us }
  StInstalled: Boolean;
  StResumeAfterWsl: Boolean;
  StDistroPresent: Boolean;       { distro=present AND ours (owned<>0): the distro Setup may work in }
  StLinuxVersion: String;         { linux_version: the Setup version that last finished cognita install/update }
  StAdminUser: String;            { admin_user: the Admin user recorded with linux_version }
  StRoot1: String;
  StVhdDir: String;
  StVhdBytes: String;
  StWorkspace: String;
  StFunnelPort: String;           { funnel_port: the public HTTPS port of a Funnel Setup turned on; '' when none is recorded (design 19.2 item 8) }
  StAcceleration: String;         { acceleration: cpu | amd | nvidia, '' when unknown (an install from before 15.1; design 22.4) }
  { What the user chose (Advanced can change ports, data location and Workspace) }
  ChosenMcpPort: Integer;
  ChosenAdminPort: Integer;
  ChosenDataDir: String;
  ChosenWorkspaceOn: Boolean;
  AdminUser: String;
  AdminPassword: String;          { memory only, cleared in DeinitializeSetup (section 5.6) }
  { What the checks told us }
  WslState: String;               { present | missing | old | '' before the first check }
  WslPageStage: Integer;          { 0 = asking to turn WSL on, 1 = asking Restart now / Later }
  PreflightWarned: Boolean;
  { What `check --phase preflight` told us about NVIDIA and WSL's file cache (design 22.2, 22.9) }
  NvState: String;                { nvidia: none | ok | old, '' before the first check }
  NvName: String;                 { nvidia_name }
  NvDriver: String;               { nvidia_driver: <major.minor> or '' }
  WslReclaim: String;             { wsl_reclaim: set | unset | unreadable, '' before the first check }
  { The Acceleration page's pick (design 22.7, 22.12 item 1) }
  ChosenAccel: String;            { cpu | nvidia }
  AccelDefaultPick: String;       { the radio the page opened with (AccelDefault), to tell a user's change from the default }
  AccelInited: Boolean;           { ChosenAccel has been set once from AccelDefault; Back and Next then keep the user's pick }
  AccelTouched: Boolean;          { the pick differs from the default the page opened with (set in the page's Next) }
  { Flow state }
  ResumeSwitch: Boolean;
  CloseQuietly: Boolean;
  InstallMode: Integer;           { ModeFresh, ModeRepair, ModeUpdate, ModeFinish or ModeReinstall (design 18.2) }
  RemoteStage: Integer;           { Remote access page: 0 = choose, 1 = failed with a link (Retry shown), 2 = turned on }
  RemoteFunnelPort: String;       { the public port the user accepted after funnel-port-busy; kept for Retry }
  RemoteLinkUrl: String;          { the link= of a failed remote-access result (design 18.4) }
  UninstLogsCopy: String;         { logs_copy of a delete-data uninstall: where the helper copied the logs }
  InstallAttempted: Boolean;
  InstallOk: Boolean;
  InstallRestart: Boolean;        { the install run needs a Windows restart (helper exit 3010): NeedRestart returns this (design 19.11 R1) }
  InstallResumeSaved: Boolean;    { restart-for-wsl recorded RunOnce and the resume marker for that restart; False = Setup will not reopen by itself }
  InstallWarnText: String;        { WarnText of the install/update run, kept for the Finished page (design 19.4 item 2): remote access runs its own helper verb, which resets WarnText }
  InstallSignInWarned: Boolean;   { the install/update run warned that the sign-in task could not be registered }
  ResultVersion: String;
  ResultWorkspace: String;        { the workspace value the install/update result (`status`) reported; '' when it reported none }
  ResultAccel: String;            { the acceleration the install/update result reported (cpu|amd|nvidia); '' when it reported none: the Finished page's only source (design 22.12 item 11) }
  PublicUrl: String;
  RemoteNote: String;
  HelperFolder: String;           { where the helper runs from: the temp folder first, the app folder after install }
  { One helper run's outcome, filled by OnHelperLine }
  LastResult: String;
  HelperExit: Integer;
  FailCount: Integer;
  FailText: String;
  FailStage: String;
  FailStageDisplay: String;
  FailMessage: String;
  FailFix: String;
  FailTechnicalFix: String;
  WarnText: String;
  SignInWarned: Boolean;          { a warning line of stage `keepalive` arrived: the sign-in task is not in place }
  LastStatus: String;             { ok | restart-required | failed, from JudgeResult }
  RunMode: Integer;               { 0 = log only, 1 = busy marquee page, 2 = progress page }
  RunStartTick: DWORD;
  CurTitle: String;
  CurStage: String;               { stage of the latest progress line that carried one (remote.login = the Tailscale sign-in wait) }
  LastDone: Int64;                { the latest byte counts, kept while the title does not change (design 19.6 item 13) }
  LastTotal: Int64;
  LinkUrl: String;
  { Wizard pieces }
  BusyPage: TOutputMarqueeProgressWizardPage;
  ProgressPage: TOutputProgressWizardPage;
  { The Tailscale sign-in link, also as text that can be selected, plus a Copy button: the tailnet's
    owner often opens the link on another device (design 19.5 item 12). Shown while the link is. }
  ProgressLinkEdit: TNewEdit;
  ProgressCopyButton: TNewButton;
  ProgressWaitLabel: TNewStaticText;
  { Design 21.4: "Skip self-tests", shown under the bar while the Linux side is on its `proof` stage. }
  ProgressSkipButton: TNewButton;
  SkipPressed: Boolean;           { the user pressed it in this install run (the request flag was written) }
  WizardUp: Boolean;              { InitializeWizard has run: WizardForm exists (the first helper call, `state`, runs before it) }
  FinProofSkipped: Boolean;       { the install/update result carried proof=skipped: the Finished page says so }
  PageWsl: TWizardPage;
  WslLabel: TNewStaticText;
  WslRestartNow: TNewRadioButton;
  WslRestartLater: TNewRadioButton;
  PageFolder: TInputDirWizardPage;
  FolderNote: TNewStaticText;
  FolderReason: TNewStaticText;   { why Cognita cannot use the folder just typed (roots --validate ok=0) }
  PageAdmin: TInputQueryWizardPage;
  PageAccel: TWizardPage;           { Acceleration (design 22.7): after Admin sign-in, before Ready }
  AccelLabelText: TNewStaticText;
  AccelUseGpu: TNewRadioButton;
  AccelUseCpu: TNewRadioButton;
  AccelNote: TNewStaticText;        { "Setup checks the card while it installs ...", only when the GPU is offered }
  AccelLink: TNewStaticText;        { the NVIDIA driver address, only for an old driver }
  PageReady: TWizardPage;
  ReadyText: TNewStaticText;
  ReclaimBox: TNewCheckBox;         { design 22.9: let Windows take back WSL's file cache; shown only when wsl_reclaim=unset }
  ReclaimText: TNewStaticText;      { the box's caption, wrapped, beside it (a check box does not wrap its caption) }
  AdvancedButton: TNewButton;
  PageRemote: TWizardPage;
  RemoteLabel: TNewStaticText;
  RemoteSkip: TNewRadioButton;
  RemoteSetUp: TNewRadioButton;
  RemoteLinkLabel: TNewStaticText;  { the link from a failed remote-access result }
  RemoteLinkEdit: TNewEdit;         { the same link as selectable text, with a Copy button (design 19.5 item 12) }
  RemoteCopyButton: TNewButton;
  RemoteRetryButton: TNewButton;
  DiagButton: TNewButton;
  LogLink: TNewStaticText;
  ReportLink: TNewStaticText;       { "Report a problem": opens the support page, sends nothing }
  FinDiagZip: String;               { the diagnostics zip saved automatically after an install failure }
  { Finished page, success: clickable Admin / MCP / public addresses, the connector hint and the commands }
  FinAdminPre, FinAdminLink, FinAdminPost: TNewStaticText;
  FinMcpPre, FinMcpLink: TNewStaticText;
  FinPubPre, FinPubLink: TNewStaticText;
  FinConnectLabel: TNewStaticText;
  FinMemo: TNewMemo;
  { Uninstall }
  UninstallDeleteData: Boolean;
  UninstallDeleteFailed: Boolean; { a delete-data uninstall whose helper run failed: its data could not be deleted (design 19.4 item 9) }

{ The PURE block (no wizard objects, no globals of this file) lives in pure.iss so a test harness can
  compile it alone (design 19.10: tests\pure_tests.iss, run by tests\test_setup_pure.py). It defines
  the Mode* constants, PickMode, ResultValue, JsonField, RedactToken, the version compare and the other
  helpers that need no wizard. It is #included here, in [Code], where the block used to be inline. }
#include "pure.iss"

function HasSwitch(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then
      Result := True;
end;

function PsExe: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function HelperPath: String;
begin
  Result := HelperFolder + '\CognitaWin.ps1';
end;

function SetupLocaleTag: String;
begin
  if ActiveLanguage = 'spanish' then Result := 'es-ES'
  else if ActiveLanguage = 'french' then Result := 'fr-FR'
  else if ActiveLanguage = 'german' then Result := 'de-DE'
  else if ActiveLanguage = 'italian' then Result := 'it-IT'
  else if ActiveLanguage = 'brazilianportuguese' then Result := 'pt-BR'
  else Result := 'en-US';
end;

function FailureStageDisplayFor(const RawStage: String): String;
begin
  if RawStage = 'Starting' then
    Result := CustomMessage('failureStageStarting')
  else if RawStage = 'Handing the Admin password to the installer' then
    Result := CustomMessage('failureStagePassword')
  else
    Result := CustomMessage('failureStageUnknown');
end;

function AdminUrl(Param: String): String;
begin
  Result := 'http://localhost:' + IntToStr(ChosenAdminPort) + '/';
end;

function LogsDir: String;
begin
  Result := ExpandConstant('{localappdata}\Cognita\logs');
end;

function OnOff(On: Boolean): String;
begin
  if On then
    Result := 'on'
  else
    Result := 'off';
end;

function CustomMessageWithLines(const Id: String): String;
begin
  Result := CustomMessage(Id);
  StringChangeEx(Result, '%n', #13#10, True);
end;

{ Design 18.2: what each mode fixes. finish, repair and update all work in the owned distro, so the
  data location is fixed to its vhd_dir; repair, update and reinstall also fix the Admin user, which
  only makes sense once Cognita has been installed, and (design 19.2 item 7) every non-fresh mode fixes
  the folder (root 1) once it exists. A fixed field with no saved value to show stays editable, so
  Setup can never be stuck on an empty read-only box (the Admin user is the exception: its box says
  "(unchanged)", because Setup does not pass the name at all there). }
function DataLocationLocked: Boolean;
begin
  Result := (InstallMode <> ModeFresh) and (StVhdDir <> '');
end;

{ Repair, update and reinstall-after-uninstall all run over an installed Cognita. }
function RunsOverInstalled: Boolean;
begin
  Result := (InstallMode = ModeRepair) or (InstallMode = ModeUpdate) or (InstallMode = ModeReinstall);
end;

{ Design 19.2 item 7 (was: only repair, update and reinstall): the folder is locked whenever root 1
  exists and the run is not fresh. Finish is included: the helper refuses a projects folder other
  than root 1 once root 1 exists, so offering an editable box there only led to a refusal later. }
function FolderLocked: Boolean;
begin
  Result := (InstallMode <> ModeFresh) and (StRoot1 <> '');
end;

{ Design 19.2 item 14 (was: locked only when admin_user was known): over an installed Cognita the user
  field is always read-only. Setup does not pass --admin-user there, so an editable box would accept a
  name and then drop it; when the name is not known the box says "(unchanged)". }
function UserLocked: Boolean;
begin
  Result := RunsOverInstalled;
end;

{ Design 19.2 item 8: a recorded Funnel points at the MCP port, so the helper's install refuses a
  different one (reason=port-in-use-by-funnel). Advanced shows the port read-only instead. An update
  already locks both ports, with its own note. }
function McpPortLockedByFunnel: Boolean;
begin
  Result := (StFunnelPort <> '') and (InstallMode <> ModeUpdate);
end;

{ A fresh random name for a pipe: 128 bits from the system's UUID generator. }
function RandomPipeName: String;
var
  U: TUuid;
begin
  U.D1 := 0;
  U.D2 := 0;
  U.D3 := 0;
  U.D4 := 0;
  UuidCreate(U);
  Result := 'cognita-' + HexOf(U.D1) + HexOf(U.D2) + HexOf(U.D3) + HexOf(U.D4);
end;

{ Writes S to the named pipe as UTF-16LE (the String's own bytes), no terminator (section 5.6). }
function WritePipe(const PipeName, S: String): Boolean;
var
  H: THandle;
  Written: Cardinal;
begin
  Result := False;
  H := CreateFileW(PipeName, $40000000 { GENERIC_WRITE }, 0, 0, 3 { OPEN_EXISTING }, 0, 0);
  if H = THandle(-1) then
    Exit;
  try
    Result := WriteFile(H, S, Length(S) * 2, Written, 0);
    Result := Result and (Integer(Written) = Length(S) * 2);
  finally
    CloseHandle(H);
  end;
end;

{ ---------------------------------------------------------------------------------------------
  Running the helper
  --------------------------------------------------------------------------------------------- }

procedure ResetRun;
begin
  LastResult := '';
  HelperExit := -1;
  FailCount := 0;
  FailText := '';
  FailStage := '';
  FailStageDisplay := '';
  FailMessage := '';
  FailFix := '';
  FailTechnicalFix := '';
  WarnText := '';
  SignInWarned := False;
  CurTitle := '';
  CurStage := '';
  LastDone := 0;
  LastTotal := 0;
  LinkUrl := '';
end;

{ Puts Text on the clipboard. Inno's script has no clipboard call, so this hands the text to Windows'
  own clip.exe through a temp file (a file, not the command line: the text never meets cmd's quoting).
  The text itself is never logged, only its length: a sign-in link is one-time but still a credential
  in spirit. A failure is logged and otherwise ignored; the box the text came from can be selected and
  copied by hand. }
procedure CopyTextToClipboard(const Text: String);
var
  Tmp: String;
  Code: Integer;
begin
  if Text = '' then
  begin
    Log('clipboard: nothing to copy');
    Exit;
  end;
  Tmp := ExpandConstant('{tmp}\cognita-clip.txt');
  try
    if not SaveStringToFile(Tmp, Text, False) then
    begin
      Log('clipboard: could not write ' + Tmp);
      Exit;
    end;
    if Exec(ExpandConstant('{cmd}'), '/C clip < "' + Tmp + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Log('clipboard: copied ' + IntToStr(Length(Text)) + ' characters, clip.exe exit ' + IntToStr(Code))
    else
      Log('clipboard: clip.exe could not be started: ' + SysErrorMessage(Code));
  except
    Log('clipboard: exception: ' + GetExceptionMessage);
  end;
  DeleteFile(Tmp);
end;

procedure ProgressCopyClick(Sender: TObject);
begin
  CopyTextToClipboard(ProgressLinkEdit.Text);
end;

{ Design 21.4: the request flag the helper watches (design 21.3). The helper's data folder is
  %LOCALAPPDATA%\Cognita, the same place its settings and logs live. }
function SkipRequestFile: String;
begin
  Result := ExpandConstant('{localappdata}\Cognita\skip-self-test.request');
end;

{ Removes the request flag if it is there; Why says which moment this is (start, end) for the log. }
procedure DeleteSkipRequest(const Why: String);
var
  F: String;
begin
  F := SkipRequestFile;
  if FileExists(F) then
  begin
    if DeleteFile(F) then
      Log('self-test skip: removed the request flag ' + F + ' (' + Why + ')')
    else
      Log('self-test skip: could not remove the request flag ' + F + ' (' + Why + ')');
  end
  else
    Log('self-test skip: no request flag to remove (' + Why + ')');
end;

{ Design 21.4: the button's click. Writes the request flag (an empty file), remembers the press, disables
  the button and says what is happening; the helper turns the flag into the skip file the Linux side
  watches, and the next progress line replaces this text. }
procedure ProgressSkipClick(Sender: TObject);
begin
  ProgressSkipButton.Enabled := False;
  if SaveStringToFile(SkipRequestFile, '', False) and FileExists(SkipRequestFile) then
  begin
    SkipPressed := True;
    Log('self-test skip requested: wrote ' + SkipRequestFile);
    ProgressPage.SetText(CurTitle, CustomMessage('progressStoppingTests'));
  end
  else
  begin
    { Nothing was requested, so the button stays usable: pressing it again tries again. }
    ProgressSkipButton.Enabled := True;
    Log('self-test skip requested but the request flag could not be written: ' + SkipRequestFile);
  end;
end;

procedure ShowProgressLine(const Title, Msg: String; const Done, Total: Int64);
var
  Line2: String;
  SkipWant: Boolean;
begin
  Line2 := Msg;
  if Total > 0 then
  begin
    if Line2 <> '' then
      Line2 := FmtMessage(CustomMessage('progressBytesOf'), [FormatBytes(Done), FormatBytes(Total)]) + '  -  ' + Line2
    else
      Line2 := FmtMessage(CustomMessage('progressBytes'), [FormatBytes(Done)]);
  end;
  { A link in the message (Tailscale sign-in, section 9 step 3) becomes clickable while it is the
    latest message; the helper repeats it in every heartbeat (design 18.4), so it does not vanish. }
  LinkUrl := UrlInText(Msg);
  ProgressPage.SetText(Title, Line2 + '     ' +
    FmtMessage(CustomMessage('progressElapsed'), [FormatElapsed(Int64(GetTickCount - RunStartTick))]));
  { A stage with a byte count fills the bar; any other stage (start, self-test, the Linux side's own
    steps) shows a moving marquee, never a still empty bar: a bar that does not move for three minutes
    makes the user think Setup is stuck (2026-09-29). }
  if Total > 0 then
  begin
    ProgressPage.ProgressBar.Style := npbstNormal;
    ProgressPage.SetProgress(Integer((Done * 1000) div Total), 1000);
  end
  else
  begin
    ProgressPage.ProgressBar.Style := npbstMarquee;
    ProgressPage.SetProgress(0, 0);
  end;
  if LinkUrl <> '' then
  begin
    ProgressPage.Msg2Label.Cursor := crHand;
    ProgressPage.Msg2Label.Font.Color := clBlue;
    ProgressPage.Msg2Label.Font.Style := [fsUnderline];
  end
  else
  begin
    ProgressPage.Msg2Label.Cursor := crDefault;
    ProgressPage.Msg2Label.Font.Color := clWindowText;
    ProgressPage.Msg2Label.Font.Style := [];
  end;
  { The same link as selectable text with a Copy button (design 19.5 item 12). The text is only
    assigned when it changed, so a selection the user has made survives the 5-second heartbeat. }
  if LinkUrl <> '' then
  begin
    if ProgressLinkEdit.Text <> LinkUrl then
      ProgressLinkEdit.Text := LinkUrl;
    ProgressLinkEdit.Visible := True;
    ProgressCopyButton.Visible := True;
  end
  else
  begin
    ProgressLinkEdit.Visible := False;
    ProgressCopyButton.Visible := False;
  end;
  { Design 19.9: Setup runs the helper synchronously, so the sign-in wait cannot be cancelled; the page
    says how long it lasts instead. Only the Tailscale sign-in (stage remote.login) is that wait. }
  ProgressWaitLabel.Visible := (LinkUrl <> '') and (CurStage = 'remote.login');
  { Design 21.4: Skip self-tests while the Linux side is on its proof stage and it has not been pressed;
    re-evaluated on every progress line. Never together with the Copy button (a sign-in link on this
    stage would be a helper bug, but the two must not overlap in any case). }
  SkipWant := ProofSkipVisible(CurStage, SkipPressed) and (not ProgressCopyButton.Visible);
  if SkipWant <> ProgressSkipButton.Visible then
    Log('self-test skip: button visible=' + IntToStr(Ord(SkipWant)) + ' stage=[' + CurStage + '] pressed=' + IntToStr(Ord(SkipPressed)));
  ProgressSkipButton.Visible := SkipWant;
end;

procedure OnHelperLine(const S: String; const Error, FirstLine: Boolean);
var
  L, St, Stage, Title, DisplayTitle, Msg, Fix, WarnFix: String;
  Done, Total: Int64;
  TitleNew: Boolean;
begin
  L := Trim(S);
  if L = '' then
    Exit;
  { The helper never prints the password; connector tokens are redacted anyway. }
  Log('helper: ' + RedactToken(L));
  { WizardUp: `state` runs from InitializeSetup, before the wizard exists; touching WizardForm there is
    "Could not call proc" (VM, 2026-09-29). }
  if WizardUp and (IsWindowEnabled(WizardForm.Handle) = 0) then
  begin
    EnableWindow(WizardForm.Handle, 1);
    Log('helper: the wizard window was disabled while waiting; enabled it so the page''s buttons work');
  end;
  if Copy(L, 1, 7) = 'result=' then
  begin
    LastResult := L;
    Exit;
  end;
  if L[1] <> '{' then
    Exit;
  St := JsonField(L, 'state');
  Stage := JsonField(L, 'stage');
  Title := JsonField(L, 'title');
  DisplayTitle := JsonField(L, 'title_display');
  Msg := JsonField(L, 'message');
  Fix := JsonField(L, 'fix');
  Done := StrToInt64Def(JsonField(L, 'bytes_done'), 0);
  Total := StrToInt64Def(JsonField(L, 'bytes_total'), 0);
  { Design 19.6 item 13: byte counts belong to a title. A line that reports them replaces the remembered
    ones; a line WITHOUT them (a heartbeat, a warning or a message on the same step) keeps them while the
    title has not changed, so the bar does not fall back to empty in the middle of a download. A NEW
    title starts from zero. (The helper clears its own remembered counts the same way.) }
  TitleNew := (Title <> '') and (Title <> CurTitle);
  if Stage <> '' then
    CurStage := Stage;
  if Title <> '' then
    CurTitle := Title
  else if (CurTitle = '') and (Stage <> '') then
    CurTitle := Stage;
  if JsonField(L, 'bytes_total') <> '' then
  begin
    LastDone := Done;
    LastTotal := Total;
  end
  else if TitleNew then
  begin
    LastDone := 0;
    LastTotal := 0;
  end
  else if LastTotal > 0 then
    Log('progress: a line without byte counts on the same step keeps ' + IntToStr(LastDone) + ' of ' + IntToStr(LastTotal));
  if DisplayTitle = '' then
    DisplayTitle := Title;
  if JsonField(L, 'message_display') <> '' then
    Msg := JsonField(L, 'message_display');
  if JsonField(L, 'fix_display') <> '' then
    Fix := JsonField(L, 'fix_display');
  if St = 'failed' then
  begin
    FailCount := FailCount + 1;
    FailStage := CurTitle;
    FailStageDisplay := JsonField(L, 'title_display');
    if FailStageDisplay = '' then
      FailStageDisplay := FailureStageDisplayFor(FailStage);
    FailMessage := Msg;
    FailFix := Fix;
    FailTechnicalFix := JsonField(L, 'fix_technical');
    FailText := FailText + Msg;
    if Fix <> '' then
      FailText := FailText + #13#10 + CustomMessage('whatToDo') + ' ' + Fix;
    FailText := FailText + AppendTechnicalDetail(FailFix, FailTechnicalFix, CustomMessage('technicalDetail'));
    FailText := FailText + #13#10#13#10;
  end
  else if St = 'warning' then
  begin
    if Msg <> '' then
    begin
      WarnText := WarnText + Msg;
      { Design 22.7 and 22.12 item 2: a warning of the `acceleration` stage (the Linux side dropped the GPU)
        that carries no fix of its own gets "run Setup again and choose NVIDIA". Rerunning Setup is what
        works: the install is then on the CPU image, which ignores Admin's GPU switch. }
      WarnFix := WarningFix(Stage, Fix);
      if (Fix = '') and (WarnFix <> '') then
        Log('helper: warning of stage [' + Stage + '] has no fix of its own; added the line "' + WarnFix + '"');
      if WarnFix <> '' then
        WarnText := WarnText + #13#10 + WarnFix;
      WarnText := WarnText + #13#10#13#10;
    end;
    { The helper's `keepalive` stage is the sign-in task (design 19.4 item 2): when it warns, "Cognita
      starts when you sign in to Windows" would be untrue, so the Finished page drops that sentence. }
    if Stage = 'keepalive' then
    begin
      SignInWarned := True;
      Log('helper: the sign-in task warning was seen (stage keepalive)');
    end;
  end;
  if RunMode = 2 then
    ShowProgressLine(DisplayTitle, Msg, LastDone, LastTotal)
  else if RunMode = 1 then
  begin
    if DisplayTitle <> '' then
      BusyPage.SetText(DisplayTitle, '');
    BusyPage.Animate;
  end;
end;

{ The helper's last result line, judged together with its exit code (section 5.1): 'ok' needs
  exit 0, 'restart-required' needs exit 3010, everything else (including no result line at all)
  is 'failed'. Sets LastStatus. }
procedure JudgeResult;
var
  R: String;
begin
  R := ResultValue(LastResult, 'result');
  LastStatus := 'failed';
  if (R = 'ok') and (HelperExit = 0) then
    LastStatus := 'ok'
  else if (R = 'restart-required') and (HelperExit = 3010) then
    LastStatus := 'restart-required'
  else if R = '' then
    Log('helper: no result line was printed (exit ' + IntToStr(HelperExit) + ')')
  else if (R = 'ok') or (R = 'restart-required') then
    Log('helper: result=' + R + ' does not match exit code ' + IntToStr(HelperExit));
end;

{ Guarantees a readable failure when the helper failed without saying why. }
procedure EnsureFailureText;
begin
  if FailCount = 0 then
  begin
    FailCount := 1;
    if FailStage = '' then
      FailStage := CurTitle;
    if FailStageDisplay = '' then
      FailStageDisplay := FailureStageDisplayFor(FailStage);
    if FailMessage = '' then
    begin
      if LastResult = '' then
        FailMessage := FmtMessage(CustomMessage('failureNoHelperResult'), [IntToStr(HelperExit)])
      else
        FailMessage := FmtMessage(CustomMessage('failureNoHelperReason'), [IntToStr(HelperExit)]);
    end;
    if FailFix = '' then
      FailFix := FmtMessage(CustomMessage('failureUnexpectedFix'), ['{#SupportUrl}']);
    FailText := FailMessage + #13#10 + CustomMessage('whatToDo') + ' ' + FailFix + #13#10#13#10;
  end;
end;

{ Runs `powershell CognitaWin.ps1 <Verb> <Args>` and waits. Output goes through OnHelperLine, so
  the last result line, failures and warnings are filled in. Returns True when the verb ended
  ok; LastStatus tells ok from restart-required. Never raises. }
function RunHelper(const Verb, Args: String): Boolean;
var
  Params: String;
  Code: Integer;
  Tick: DWORD;
begin
  ResetRun;
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + QuoteArg(HelperPath) + ' ' + Verb;
  if Args <> '' then
    Params := Params + ' ' + Args;
  Log('helper start: verb=' + Verb + ' args=' + RedactToken(Args) + ' helper=' + HelperPath);
  Tick := GetTickCount;
  Code := -1;
  try
    if not ExecAndLogOutput(PsExe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code, @OnHelperLine) then
    begin
      Log('helper: powershell could not be started: ' + SysErrorMessage(Code));
      FailMessage := FmtMessage(CustomMessage('failurePowerShellStart'), [SysErrorMessage(Code)]);
    end;
  except
    Log('helper: exception while running ' + Verb + ': ' + GetExceptionMessage);
    FailMessage := FmtMessage(CustomMessage('failureHelperException'), [GetExceptionMessage]);
  end;
  HelperExit := Code;
  JudgeResult;
  if LastStatus = 'failed' then
    EnsureFailureText;
  Result := LastStatus = 'ok';
  Log('helper done: verb=' + Verb + ' exit=' + IntToStr(Code) + ' status=' + LastStatus +
    ' elapsed_ms=' + IntToStr(Integer(GetTickCount - Tick)) + ' failures=' + IntToStr(FailCount));
end;

{ A short verb behind the marquee page, so the wizard shows something while it runs. }
function RunHelperBusy(const Title, Verb, Args: String): Boolean;
begin
  BusyPage.SetText(Title, '');
  BusyPage.Show;
  RunMode := 1;
  try
    Result := RunHelper(Verb, Args);
  finally
    RunMode := 0;
    BusyPage.Hide;
  end;
end;

{ ---------------------------------------------------------------------------------------------
  The Admin password: two pipes and a broker (section 5.6 steps 1 and 2)
  --------------------------------------------------------------------------------------------- }

{ Starts the broker, hands it the password, and returns the name of the pipe the verb reads
  from. False when the broker could not be started or never opened its pipe within 60 seconds
  (design 19.5 item 15; it was 10). A cold Windows PowerShell 5.1 start behind antivirus scanning or a
  busy disk has taken longer than 10 seconds, and the pipe only exists once the broker is running. The
  limit is measured on the tick clock, not counted in attempts, so a slow connect attempt cannot
  stretch it. }
function HandOverPassword(var PipeB: String): Boolean;
var
  PipeA: String;
  Code, Attempts: Integer;
  Started: DWORD;
begin
  Result := False;
  PipeA := RandomPipeName;
  PipeB := RandomPipeName;
  Log('password broker: starting (two random pipe names; the password itself is never logged)');
  if not Exec(PsExe, '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + QuoteArg(HelperPath) +
    ' password-broker --in ' + PipeA + ' --out ' + PipeB, '', SW_HIDE, ewNoWait, Code) then
  begin
    Log('password broker: could not start: ' + SysErrorMessage(Code));
    Exit;
  end;
  Started := GetTickCount;
  Attempts := 0;
  while Integer(GetTickCount - Started) < 60000 do
  begin
    Attempts := Attempts + 1;
    if WritePipe('\\.\pipe\' + PipeA, AdminPassword) then
    begin
      Result := True;
      Log('password broker: password written after ' + IntToStr(Attempts) + ' attempt(s), ' +
        IntToStr(Integer(GetTickCount - Started)) + ' ms');
      Exit;
    end;
    Sleep(100);
  end;
  Log('password broker: gave up opening the pipe after ' + IntToStr(Attempts) + ' attempts (60 s): the Setup helper did not start in time');
end;

{ ---------------------------------------------------------------------------------------------
  Arguments for the helper (one place; see the contract at the top of this file)
  --------------------------------------------------------------------------------------------- }

{ The NVIDIA Cognita image's size in this build; 0 = this build has no NVIDIA image (design 22.7). }
function NvidiaBuildBytes: Int64;
begin
  Result := StrToInt64('{#SizeCognitaNvidia}');
end;

{ Which flavor of the Acceleration page this run has (AccelHidden, AccelOffer, AccelOldDriver, AccelNoBuild). }
function AccelMode: Integer;
begin
  Result := AccelPageMode(NvState, NvidiaBuildBytes, InstallMode);
end;

{ The profile the install will have as far as Setup knows (cpu, amd, nvidia) or '' when it keeps a profile
  Setup does not know (design 22.12 items 1(b), 1(c) and 10). }
function EffectiveAccel: String;
begin
  Result := AccelEffective(AccelMode, InstallMode, ChosenAccel, AccelTouched, StAcceleration);
end;

{ The WSL memory option for the install and update verbs: the box is shown (wsl_reclaim=unset) and ticked.
  Only the Ready page's check box changes it; before the Ready page exists nothing is passed. }
function ReclaimArgText: String;
begin
  Result := WslReclaimArg(WslReclaim, ReclaimBox.Checked);
end;

{ Design 22.7 and 22.12 items 1(c) and 10: the Cognita image sized is the one the install will have (the
  NVIDIA image for nvidia, the larger of the CPU and NVIDIA images when the profile is unknown, so the disk
  check never under-sizes). }
function ImagesBytes: Int64;
begin
  Result := AccelCognitaBytes(EffectiveAccel, NvState, StrToInt64('{#SizeCognitaCpu}'), NvidiaBuildBytes);
  if ChosenWorkspaceOn then
    Result := Result + StrToInt64('{#SizeWorkspaceRuntime}') + StrToInt64('{#SizeToolbox}');
end;

{ Search models Cognita downloads at install (Linux design 6.3: "2.3 GB today"). Only used for the
  size figures shown on the Ready page and the disk estimate passed to `check`. }
function ModelsBytes: Int64;
begin
  Result := StrToInt64('2300000000');
end;

function DownloadBytes: Int64;
begin
  Result := ImagesBytes + ModelsBytes;
end;

{ The Windows design 5.3 disk formula (Linux design 6.3's, with its sizes compiled in): image
  sizes x 4 + models + 3.5 GB, plus 1 GB for the imported Linux image. }
function DiskNeededBytes: Int64;
begin
  Result := ImagesBytes * 4 + ModelsBytes + StrToInt64('4500000000');
end;

function ArgsCheckFinal: String;
begin
  Result := '--phase final --data-dir ' + QuoteArg(ChosenDataDir) + ' --mcp-port ' + IntToStr(ChosenMcpPort) +
    ' --admin-port ' + IntToStr(ChosenAdminPort) + ' --disk-bytes ' + IntToStr(DiskNeededBytes);
end;

{ The `install` verb. --admin-user is not passed on a repair or a reinstall (the user is read-only
  there, design 18.2). --image only when the Linux image was unpacked (fresh and finish); --src on
  every install run, so the helper can swap an older tree in the distro before cognita install. }
function ArgsInstall(const PipeB, ImageFile, SrcFile: String): String;
begin
  Result := '--projects-folder ' + QuoteArg(PageFolder.Values[0]);
  if not RunsOverInstalled then
    Result := Result + ' --admin-user ' + QuoteArg(AdminUser);
  Result := Result + ' --workspace ' + OnOff(ChosenWorkspaceOn) + ' --mcp-port ' + IntToStr(ChosenMcpPort) +
    ' --admin-port ' + IntToStr(ChosenAdminPort) + ' --data-dir ' + QuoteArg(ChosenDataDir);
  if ImageFile <> '' then
    Result := Result + ' --image ' + QuoteArg(ImageFile) + ' --image-sha256 {#ImageSha256}';
  if SrcFile <> '' then
    Result := Result + ' --src ' + QuoteArg(SrcFile) + ' --src-sha256 {#SrcSha256}';
  { Design 22.7 and 22.12 item 1: --acceleration after --workspace (AccelArg decides whether and what),
    --wsl-memory-reclaim when the Ready page's box was shown and ticked (design 22.9). }
  Result := Result + AccelArg(AccelMode, InstallMode, ChosenAccel, AccelTouched, StAcceleration);
  Result := Result + ' --setup-version {#Version} --setup-revision {#Revision} --password-pipe ' + PipeB;
  Result := Result + ReclaimArgText;
end;

function ArgsUpdate(const PipeB, SrcFile: String): String;
begin
  Result := '--src ' + QuoteArg(SrcFile) + ' --src-sha256 {#SrcSha256} --setup-version {#Version}' +
    ' --setup-revision {#Revision} --disk-bytes ' + IntToStr(DiskNeededBytes) + ' --password-pipe ' + PipeB +
    ReclaimArgText;
end;

{ Logs what Setup decided about acceleration and WSL's memory option for this run, with the values it
  decided from; called once, right before the helper is started (design 22.8). }
procedure LogAccelDecision(const Verb: String);
begin
  Log('acceleration: verb=' + Verb + ' mode=' + IntToStr(InstallMode) + ' page_mode=' + IntToStr(AccelMode) +
    ' nvidia=[' + NvState + '] build_bytes=' + IntToStr(NvidiaBuildBytes) + ' default=[' + AccelDefaultPick +
    '] chosen=[' + ChosenAccel + '] touched=' + IntToStr(Ord(AccelTouched)) + ' installed_profile=[' + StAcceleration +
    '] effective=[' + EffectiveAccel + '] argument=[' + Trim(AccelArg(AccelMode, InstallMode, ChosenAccel, AccelTouched,
    StAcceleration)) + '] images_bytes=' + IntToStr(ImagesBytes));
  Log('wsl memory: wsl_reclaim=[' + WslReclaim + '] box_visible=' + IntToStr(Ord(WslReclaimBoxVisible(WslReclaim))) +
    ' box_ticked=' + IntToStr(Ord(ReclaimBox.Checked)) + ' argument=[' + Trim(ReclaimArgText) + ']');
end;

{ ---------------------------------------------------------------------------------------------
  `state` (sections 5.2 and 7.2)
  --------------------------------------------------------------------------------------------- }

procedure ReadState;
var
  P: Integer;
begin
  StInstalled := IsTruthy(ResultValue(LastResult, 'installed'));
  StResumeAfterWsl := ResultValue(LastResult, 'resume') = 'after-wsl';
  StDistroPresent := StateDistroOwned(LastResult);
  StLinuxVersion := ResultValue(LastResult, 'linux_version');
  StAdminUser := ResultValue(LastResult, 'admin_user');
  StRoot1 := ResultValue(LastResult, 'root1');
  StVhdDir := ResultValue(LastResult, 'vhd_dir');
  StVhdBytes := ResultValue(LastResult, 'vhd_bytes');
  StWorkspace := Lowercase(ResultValue(LastResult, 'workspace'));
  { Design 19.2 item 8 and 19.11 R6/R7. `state` reports funnel_port = the recorded Funnel's PUBLIC https
    port while Tailscale still serves it (or cannot be asked); empty when none is recorded or it was
    turned off by hand. Non-empty locks the MCP port in Advanced. }
  StFunnelPort := ResultValue(LastResult, 'funnel_port');
  { Design 22.4: the acceleration the install runs on; '' when unknown (before 15.1) or a word Setup does not know. }
  StAcceleration := AccelKnown(ResultValue(LastResult, 'acceleration'));
  P := StrToIntDef(ResultValue(LastResult, 'mcp_port'), 0);
  if (P >= 1024) and (P <= 65535) then
    ChosenMcpPort := P;
  P := StrToIntDef(ResultValue(LastResult, 'admin_port'), 0);
  if (P >= 1024) and (P <= 65535) then
    ChosenAdminPort := P;
  { The saved data location only matters when the owned distro exists: it is where that distro's
    virtual disk lives. With no distro the location is a free choice (the default, or Advanced). }
  if (StVhdDir <> '') and StDistroPresent then
    ChosenDataDir := StVhdDir;
  if StWorkspace = 'off' then
    ChosenWorkspaceOn := False
  else if StWorkspace = 'on' then
    ChosenWorkspaceOn := True;
  Log('state: installed=' + IntToStr(Ord(StInstalled)) + ' resume_after_wsl=' + IntToStr(Ord(StResumeAfterWsl)) +
    ' distro_present=' + IntToStr(Ord(StDistroPresent)) + ' linux_version=' + StLinuxVersion +
    ' admin_user=' + StAdminUser + ' root1=' + StRoot1 +
    ' mcp=' + IntToStr(ChosenMcpPort) + ' admin=' + IntToStr(ChosenAdminPort) + ' data_dir=' + ChosenDataDir +
    ' workspace=' + OnOff(ChosenWorkspaceOn) + ' funnel_port=' + StFunnelPort + ' acceleration=[' + StAcceleration +
    '] (reported [' + ResultValue(LastResult, 'acceleration') + '])');
end;

{ ---------------------------------------------------------------------------------------------
  PATH (section 4.4)
  --------------------------------------------------------------------------------------------- }

function BinDir: String;
begin
  Result := ExpandConstant('{localappdata}\Cognita\bin');
end;

function PathIsEmpty: Boolean;
var
  Old: String;
begin
  Result := (not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Old)) or (Trim(Old) = '');
end;

function PathNeedsAppend: Boolean;
var
  Old: String;
begin
  Result := False;
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Old) then
    Exit;
  if Trim(Old) = '' then
    Exit;
  Result := not PathHasPart(Old, BinDir);
end;

procedure RemoveBinFromPath;
var
  Old, New: String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Old) then
  begin
    Log('path: no user Path value; nothing to remove');
    Exit;
  end;
  if not PathHasPart(Old, BinDir) then
  begin
    Log('path: ' + BinDir + ' is not in the user Path; nothing to remove');
    Exit;
  end;
  New := PathRemovePart(Old, BinDir);
  { RegQueryStringValue returns the raw (unexpanded) text, so other entries keep their %VARS%. }
  if RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', New) then
    Log('path: removed ' + BinDir + ' from the user Path (' + IntToStr(Length(Old)) + ' -> ' + IntToStr(Length(New)) + ' characters)')
  else
    Log('path: could not rewrite the user Path value');
end;

{ ---------------------------------------------------------------------------------------------
  Wizard
  --------------------------------------------------------------------------------------------- }

{ A plain label on the Finished page; hidden until the success layout places it. }
function NewFinishedText(const Cap: String): TNewStaticText;
begin
  Result := TNewStaticText.Create(WizardForm);
  Result.Parent := WizardForm.FinishedPage;
  Result.Caption := Cap;
  Result.Visible := False;
end;

{ Opens one web address in the default browser (every clickable link goes through here). }
procedure OpenUrl(const Url: String);
var
  Code: Integer;
begin
  if Url = '' then
    Exit;
  Log('link: opening ' + RedactToken(Url));
  ShellExec('open', Url, '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure LinkClick(Sender: TObject);
begin
  OpenUrl(LinkUrl);
end;

procedure RemoteLinkClick(Sender: TObject);
begin
  OpenUrl(RemoteLinkUrl);
end;

procedure AccelLinkClick(Sender: TObject);
begin
  OpenUrl(NvidiaDriverUrl);
end;

{ A click on the check box's caption toggles the box, as a click on a real check box's caption would. }
procedure ReclaimTextClick(Sender: TObject);
begin
  ReclaimBox.Checked := not ReclaimBox.Checked;
  Log('ready page: the WSL memory caption was clicked; box_ticked=' + IntToStr(Ord(ReclaimBox.Checked)));
end;

procedure FinAdminClick(Sender: TObject);
begin
  OpenUrl(AdminUrl(''));
end;

procedure FinMcpClick(Sender: TObject);
begin
  OpenUrl('http://localhost:' + IntToStr(ChosenMcpPort) + '/');
end;

procedure FinPubClick(Sender: TObject);
begin
  OpenUrl(PublicUrl);
end;

{ A link-styled label: the same control the failure page uses for "Open the log folder". }
procedure StyleAsLink(const L: TNewStaticText);
begin
  L.Cursor := crHand;
  L.Font.Color := clBlue;
  L.Font.Style := [fsUnderline];
end;

procedure OpenLogsClick(Sender: TObject);
var
  Code: Integer;
begin
  ForceDirectories(LogsDir);
  ShellExec('open', LogsDir, '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

{ The support page opens in the user's browser only when they click it; nothing is put in the
  address (no log text, no machine details). }
procedure ReportProblemClick(Sender: TObject);
var
  Code: Integer;
begin
  Log('support: the user opened the issue page');
  ShellExec('open', '{#SupportUrl}', '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure SaveDiagnosticsClick(Sender: TObject);
var
  Zip: String;
begin
  Log('diagnostics: requested by the user (Finished page or a failure dialog)');
  if RunHelperBusy(CustomMessage('busySavingDiagnostics'), 'diagnostics', '--setup-log ' + QuoteArg(ExpandConstant('{log}'))) then
  begin
    { The helper has already opened Explorer with the file selected. Nothing is sent: the user
      decides whether to attach it anywhere, so the box says where they can and offers the page. }
    Zip := ResultValue(LastResult, 'zip');
    if MsgBox(FmtMessage(CustomMessageWithLines('diagnosticsSaved'), [Zip, '{#SupportUrl}']),
      mbInformation, MB_YESNO) = IDYES then
      ReportProblemClick(nil);
  end
  else
    MsgBox(FmtMessage(CustomMessageWithLines('diagnosticsFailure'), [FailText,
      FmtMessage(CustomMessage('diagnosticsFailedSentence'), [LogsDir, '{#SupportUrl}'])]), mbError, MB_OK);
end;

{ On any failure Setup saves the diagnostics zip to the Desktop by itself (Doug, 2026-09-29: the user
  should not have to find a hidden log folder). It is a LOCAL file only: nothing is sent, and the
  failure text names the file and where the user may attach it. Running the helper resets the failure
  fields, so they are kept and restored around the call. Returns the zip path, or '' when saving
  failed (the text then points at the log folder instead). }
function AutoSaveDiagnostics(const Why: String): String;
var
  KText, KStage, KStageDisplay, KMsg, KFix, KTechnicalFix, KResult: String;
  KCount: Integer;
begin
  KText := FailText; KStage := FailStage; KStageDisplay := FailStageDisplay;
  KMsg := FailMessage; KFix := FailFix; KTechnicalFix := FailTechnicalFix; KResult := LastResult;
  KCount := FailCount;
  Result := '';
  Log('diagnostics: saving automatically after a failure (' + Why + ')');
  if RunHelperBusy(CustomMessage('busySavingDiagnostics'), 'diagnostics', '--no-open --setup-log ' + QuoteArg(ExpandConstant('{log}'))) then
    Result := ResultValue(LastResult, 'zip');
  Log('diagnostics: automatic save zip=[' + Result + ']');
  FailText := KText; FailStage := KStage; FailStageDisplay := KStageDisplay;
  FailMessage := KMsg; FailFix := KFix; FailTechnicalFix := KTechnicalFix; LastResult := KResult;
  FailCount := KCount;
end;

{ The closing text of every failure: where the diagnostics are, that nothing was sent, where help is. }
function DiagnosticsSentence(const Zip: String): String;
begin
  if Zip <> '' then
    Result := FmtMessage(CustomMessageWithLines('diagnosticsSavedSentence'), [Zip, '{#SupportUrl}'])
  else
    Result := FmtMessage(CustomMessage('diagnosticsFailedSentence'), [LogsDir, '{#SupportUrl}']);
end;

procedure ShowFileInExplorer(const Path: String);
var
  Code: Integer;
begin
  Log('diagnostics: showing the file in Explorer');
  ShellExec('open', 'explorer.exe', '/select,"' + Path + '"', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure ShowDiagFileClick(Sender: TObject);
begin
  if FinDiagZip <> '' then
    ShowFileInExplorer(FinDiagZip)
  else
    SaveDiagnosticsClick(Sender);
end;

{ Design 19.5 item 12: a radio button in a custom form moves AND selects with the arrow keys, as in a
  standard dialog. Focus alone moved between the buttons here without checking one, so the choice on
  screen and the choice Setup acted on could disagree. Every radio button of a custom form or page
  (the uninstall form, the WSL restart choice, the Remote page) gets this handler as its OnEnter: the
  button that receives the focus is the one that is checked. Tab from another control lands on the
  checked button of the group, so tabbing in changes nothing. }
procedure RadioEnter(Sender: TObject);
begin
  if not TNewRadioButton(Sender).Checked then
  begin
    TNewRadioButton(Sender).Checked := True;
    Log('keyboard: radio "' + TNewRadioButton(Sender).Caption + '" checked by focus');
  end;
end;

{ A failure from the preflight check, wsl-install, the folder check, the final check or the
  restart-for-wsl step (design 18.5, extended by 19 to the last three): the same text, and the same
  two ways out the Installing page's failure gives: Save diagnostics and the log folder. Save
  diagnostics ends the modal call with mrYes and the loop shows the dialog again afterwards, the
  same pattern as the Advanced form. The text is captured first: saving diagnostics runs the
  helper, which resets the failure fields. }
procedure ShowFailuresWithHelp(const Heading: String);
var
  Form: TSetupForm;
  Body: TNewMemo;
  OpenLogs, Report: TNewStaticText;
  DiagB, OkB: TNewButton;
  R: Integer;
  Zip: String;
begin
  Log('failure dialog: ' + Heading + ' failures=' + IntToStr(FailCount));
  Zip := AutoSaveDiagnostics(Heading);
  Form := CreateCustomForm(ScaleX(480), ScaleY(310), False, False);
  try
    Form.Caption := 'Cognita Setup';
    Body := TNewMemo.Create(Form);
    Body.Parent := Form;
    Body.Left := ScaleX(14);
    Body.Top := ScaleY(14);
    Body.Width := ScaleX(452);
    Body.Height := ScaleY(200);
    Body.ReadOnly := True;
    Body.ScrollBars := ssVertical;
    Body.WordWrap := True;
    Body.Text := Heading + #13#10#13#10 + FailText + DiagnosticsSentence(Zip);
    OpenLogs := TNewStaticText.Create(Form);
    OpenLogs.Parent := Form;
    OpenLogs.Left := ScaleX(14);
    OpenLogs.Top := ScaleY(224);
    OpenLogs.Caption := CustomMessage('openLogs');
    StyleAsLink(OpenLogs);
    OpenLogs.OnClick := @OpenLogsClick;
    Report := TNewStaticText.Create(Form);
    Report.Parent := Form;
    Report.Left := OpenLogs.Left + OpenLogs.Width + ScaleX(24);
    Report.Top := OpenLogs.Top;
    Report.Caption := CustomMessage('reportProblem');
    StyleAsLink(Report);
    Report.OnClick := @ReportProblemClick;
    DiagB := TNewButton.Create(Form);
    DiagB.Parent := Form;
    { The zip is already on the Desktop: the button shows it. When saving failed it tries again. }
    if Zip <> '' then
      DiagB.Caption := CustomMessage('showFile')
    else
      DiagB.Caption := CustomMessage('saveDiagnostics');
    DiagB.Left := ScaleX(14);
    DiagB.Top := Form.ClientHeight - ScaleY(25 + 14);
    DiagB.Width := ScaleX(120);
    DiagB.Height := ScaleY(25);
    DiagB.ModalResult := mrYes;
    OkB := TNewButton.Create(Form);
    OkB.Parent := Form;
    OkB.Caption := SetupMessage(msgButtonOK);
    OkB.Left := Form.ClientWidth - ScaleX(80 + 14);
    OkB.Top := Form.ClientHeight - ScaleY(25 + 14);
    OkB.Width := ScaleX(80);
    OkB.Height := ScaleY(25);
    OkB.ModalResult := mrOk;
    OkB.Default := True;
    OkB.Cancel := True;
    { Design 19.5 item 12: the memo is the first control, so it took the focus and swallowed Enter (a
      multi-line edit treats Enter as a new line). The default button holds the focus instead. }
    Form.ActiveControl := OkB;
    Form.FlipAndCenterIfNeeded(True, WizardForm, False);
    repeat
      R := Form.ShowModal;
      if R = mrYes then
      begin
        if Zip <> '' then
          ShowFileInExplorer(Zip)
        else
          SaveDiagnosticsClick(nil);
      end;
    until R <> mrYes;
  finally
    Form.Free;
  end;
end;

{ True when Dir holds nothing at all (no files, no folders). Used by Advanced: the data location must be
  a new folder or an empty one (design 19.7 item 16). }
function DirIsEmpty(const Dir: String): Boolean;
var
  F: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Dir) + '*', F) then
  begin
    try
      repeat
        if (F.Name <> '.') and (F.Name <> '..') then
          Result := False;
      until (not Result) or (not FindNext(F));
    finally
      FindClose(F);
    end;
  end;
end;

{ The data location Advanced was given, checked on OK (design 19.7 item 16). Returns '' when it is fine,
  otherwise the sentence to show. Only a location the user CAN change and did change is checked: an
  unchanged default is the one Setup has always used, and a locked one is where the distro already is.
  The order is cheapest first; the helper's own check (roots --validate, which knows the projects
  folder and the folders Cognita already uses) runs last, because it starts PowerShell. }
function DataFolderProblem(const Dir: String): String;
var
  Reason, DisplayReason: String;
  HelperRejected: Boolean;
begin
  Result := '';
  HelperRejected := False;
  if HasBadArgChar(Dir) then
    Result := CustomMessage('dataFolderInvalid')
  else if IsDriveRoot(Dir) then
    Result := FmtMessage(CustomMessage('dataDriveRoot'), [AddBackslash(Dir)])
  else if (Length(Dir) < 3) or (Copy(Dir, 2, 2) <> ':\') then
    Result := CustomMessage('dataOwnDrive')
  else if IsOneDrivePath(Dir, GetEnv('OneDrive'), GetEnv('OneDriveConsumer'), GetEnv('OneDriveCommercial')) then
    Result := CustomMessage('dataOneDrive')
  else if DirExists(Dir) and (not DirIsEmpty(Dir)) then
    Result := CustomMessage('dataFolderNotEmpty')
  else if not RunHelperBusy(CustomMessage('busyCheckingFolder'), 'roots', '--validate ' + QuoteArg(PageFolder.Values[0]) +
    ' --data-dir ' + QuoteArg(Dir)) then
  begin
    Log('advanced: roots --validate failed internally for data_dir=' + Dir);
    Result := CustomMessage('dataCheckFailed') + #13#10#13#10 + FailText;
  end
  else if ResultValue(LastResult, 'ok') = '0' then
  begin
    Reason := ResultValue(LastResult, 'reason');
    if Reason = '' then
      Reason := CustomMessage('dataFolderUnsupported');
    DisplayReason := ResultValue(LastResult, 'reason_display');
    if DisplayReason = '' then
      DisplayReason := Reason;
    Result := DisplayReason;
    HelperRejected := True;
    Log('advanced: data_dir=' + Dir + ' refused: ' + Reason);
  end;
  if not HelperRejected then
  begin
    if Result <> '' then
      Log('advanced: data_dir=' + Dir + ' refused: ' + Result)
    else
      Log('advanced: data_dir=' + Dir + ' accepted');
  end;
end;

{ Defined below, with the Ready page's other code; AdvancedClick rebuilds the summary with it. }
procedure ShowReadyPage; forward;

procedure AdvancedClick(Sender: TObject);
var
  Form: TSetupForm;
  McpEdit, AdminEdit, DataEdit: TNewEdit;
  WorkspaceBox: TNewCheckBox;
  Lbl, FunnelNote: TNewStaticText;
  OkButton, CancelButton, BrowseButton: TNewButton;
  Mcp, Adm, NoteH: Integer;
  Dir, Problem: String;
  Done: Boolean;
begin
  { A recorded Funnel points at the MCP port: the port shows read-only with the reason under it, and
    every row below moves down by the note's height (design 19.2 item 8). }
  NoteH := 0;
  if McpPortLockedByFunnel then
    NoteH := ScaleY(32);
  Form := CreateCustomForm(ScaleX(440), ScaleY(250) + NoteH, False, False);
  try
    Form.Caption := CustomMessage('advancedTitle');

    Lbl := TNewStaticText.Create(Form);
    Lbl.Parent := Form;
    Lbl.Left := ScaleX(12);
    Lbl.Top := ScaleY(14);
    Lbl.Caption := CustomMessage('advancedMcpPort');
    McpEdit := TNewEdit.Create(Form);
    McpEdit.Parent := Form;
    McpEdit.Left := ScaleX(200);
    McpEdit.Top := ScaleY(10);
    McpEdit.Width := ScaleX(80);
    McpEdit.Text := IntToStr(ChosenMcpPort);
    if NoteH > 0 then
    begin
      McpEdit.ReadOnly := True;
      FunnelNote := TNewStaticText.Create(Form);
      FunnelNote.Parent := Form;
      FunnelNote.Left := ScaleX(12);
      FunnelNote.Top := ScaleY(36);
      FunnelNote.Width := ScaleX(416);
      FunnelNote.AutoSize := False;
      FunnelNote.WordWrap := True;
      FunnelNote.Height := NoteH - ScaleY(4);
      FunnelNote.Caption := CustomMessage('advancedFunnelPort');
      Log('advanced: the MCP port is read-only; a Funnel is recorded on public port ' + StFunnelPort);
    end;

    Lbl := TNewStaticText.Create(Form);
    Lbl.Parent := Form;
    Lbl.Left := ScaleX(12);
    Lbl.Top := ScaleY(46) + NoteH;
    Lbl.Caption := CustomMessage('advancedAdminPort');
    AdminEdit := TNewEdit.Create(Form);
    AdminEdit.Parent := Form;
    AdminEdit.Left := ScaleX(200);
    AdminEdit.Top := ScaleY(42) + NoteH;
    AdminEdit.Width := ScaleX(80);
    AdminEdit.Text := IntToStr(ChosenAdminPort);

    Lbl := TNewStaticText.Create(Form);
    Lbl.Parent := Form;
    Lbl.Left := ScaleX(12);
    Lbl.Top := ScaleY(82) + NoteH;
    Lbl.Width := ScaleX(416);
    Lbl.AutoSize := False;
    Lbl.WordWrap := True;
    Lbl.Height := ScaleY(48);
    { The helper's update verb carries no ports and no Workspace switch, so an update keeps what is
      installed; changing them is a repair (a rerun of Setup), design 7.2. Showing editable fields
      here would accept a change and then drop it. }
    if InstallMode = ModeUpdate then
      Lbl.Caption := CustomMessage('advancedUpdateData')
    else if DataLocationLocked then
      Lbl.Caption := CustomMessage('advancedDataLocked')
    else
      Lbl.Caption := CustomMessage('advancedDataFresh');
    DataEdit := TNewEdit.Create(Form);
    DataEdit.Parent := Form;
    DataEdit.Left := ScaleX(12);
    DataEdit.Top := ScaleY(134) + NoteH;
    DataEdit.Width := ScaleX(330);
    DataEdit.Text := ChosenDataDir;
    { With an owned distro (finish, repair, update) the data lives where its virtual disk already is:
      the location is shown but cannot be changed (design 18.2). }
    if DataLocationLocked then
      DataEdit.ReadOnly := True;
    BrowseButton := TNewButton.Create(Form);
    BrowseButton.Parent := Form;
    BrowseButton.Left := ScaleX(350);
    BrowseButton.Top := ScaleY(132) + NoteH;
    BrowseButton.Width := ScaleX(78);
    BrowseButton.Height := ScaleY(25);
    BrowseButton.Caption := CustomMessage('advancedBrowse');
    BrowseButton.ModalResult := mrYes;
    BrowseButton.Enabled := not DataLocationLocked;

    WorkspaceBox := TNewCheckBox.Create(Form);
    WorkspaceBox.Parent := Form;
    WorkspaceBox.Left := ScaleX(12);
    WorkspaceBox.Top := ScaleY(172) + NoteH;
    WorkspaceBox.Width := ScaleX(416);
    WorkspaceBox.Caption := CustomMessage('advancedWorkspace');
    WorkspaceBox.Checked := ChosenWorkspaceOn;
    if InstallMode = ModeUpdate then
    begin
      McpEdit.ReadOnly := True;
      AdminEdit.ReadOnly := True;
      WorkspaceBox.Enabled := False;
    end;

    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := SetupMessage(msgButtonOK);
    OkButton.Left := Form.ClientWidth - ScaleX(75 + 6 + 75 + 12);
    OkButton.Top := Form.ClientHeight - ScaleY(25 + 12);
    OkButton.Width := ScaleX(75);
    OkButton.Height := ScaleY(25);
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := SetupMessage(msgButtonCancel);
    CancelButton.Left := Form.ClientWidth - ScaleX(75 + 12);
    CancelButton.Top := Form.ClientHeight - ScaleY(25 + 12);
    CancelButton.Width := ScaleX(75);
    CancelButton.Height := ScaleY(25);
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    { Design 19.5 item 12: Enter means OK here. Without this the first edit box held the focus. }
    Form.ActiveControl := OkButton;

    Form.FlipAndCenterIfNeeded(True, WizardForm, False);
    Done := False;
    while not Done do
    begin
      { A small loop instead of event handlers: Browse (mrYes) and OK (mrOk) both end the modal
        call and come back here; a bad value shows its message and the form is shown again. }
      case Form.ShowModal of
        mrYes:
          begin
            Dir := DataEdit.Text;
            if BrowseForFolder(CustomMessage('browseDataTitle'), Dir, True) then
              DataEdit.Text := Dir;
          end;
        mrOk:
          begin
            Dir := RemoveBackslash(Trim(DataEdit.Text));
            if not PortOk(McpEdit.Text, Mcp) then
              MsgBox(CustomMessage('invalidMcpPort'), mbError, MB_OK)
            else if not PortOk(AdminEdit.Text, Adm) then
              MsgBox(CustomMessage('invalidAdminPort'), mbError, MB_OK)
            else if Mcp = Adm then
              MsgBox(CustomMessage('portsDifferent'), mbError, MB_OK)
            else
            begin
              { Design 19.7 item 16: a changed, changeable data location must pass DataFolderProblem
                (no drive root, a new or empty folder, not OneDrive, and the helper's roots --validate). }
              Problem := '';
              if (not DataLocationLocked) and (CompareText(Dir, ChosenDataDir) <> 0) then
                Problem := DataFolderProblem(Dir)
              else
                Log('advanced: data_dir not checked (locked=' + IntToStr(Ord(DataLocationLocked)) +
                  ' unchanged=' + IntToStr(Ord(CompareText(Dir, ChosenDataDir) = 0)) + ')');
              if Problem <> '' then
                MsgBox(Problem, mbError, MB_OK)
              else
              begin
                ChosenMcpPort := Mcp;
                ChosenAdminPort := Adm;
                if not DataLocationLocked then
                  ChosenDataDir := Dir;
                ChosenWorkspaceOn := WorkspaceBox.Checked;
                Log('advanced: mcp=' + IntToStr(Mcp) + ' admin=' + IntToStr(Adm) + ' data_dir=' + ChosenDataDir +
                  ' workspace=' + OnOff(ChosenWorkspaceOn));
                { 15.1.0 review: the Ready summary was built only when the page was entered, so after OK here it
                  still showed the old ports, Workspace and sizes while the install used the new ones. }
                ShowReadyPage;
                { As when the page is entered: Enter means Install, not Advanced again. }
                WizardForm.ActiveControl := WizardForm.NextButton;
                Done := True;
              end;
            end;
          end;
      else
        Done := True;
      end;
    end;
  finally
    Form.Free;
  end;
end;

{ ---------------------------------------------------------------------------------------------
  The remote access step (section 9, design 18.4). It runs from the Remote page's Next button and
  from that page's Retry button, so it lives above InitializeWizard, which wires the button.
  --------------------------------------------------------------------------------------------- }

{ One try at `remote-access`: a NEW broker and new pipes (a broker serves the password once), then
  the verb behind the progress page. HandedOver is False when the password could not be handed to
  the broker, in which case the verb was not run. }
function RemoteAttempt(const Extra, Title: String; var HandedOver: Boolean): Boolean;
var
  PipeB: String;
begin
  Result := False;
  HandedOver := HandOverPassword(PipeB);
  if not HandedOver then
    Exit;
  { Design 19.4 item 23: the caption of this step is its own (the page was still saying "Installing
    Cognita" while Tailscale was being set up). }
  ProgressPage.Caption := CustomMessage('remoteProgressCaption');
  ProgressPage.Description := CustomMessage('remoteProgressDescription');
  ProgressPage.SetText(Title, '');
  ProgressLinkEdit.Visible := False;
  ProgressCopyButton.Visible := False;
  ProgressWaitLabel.Visible := False;
  ProgressSkipButton.Visible := False;
  ProgressPage.Show;
  WizardForm.BackButton.Visible := False;
  RunMode := 2;
  RunStartTick := GetTickCount;
  try
    Result := RunHelper('remote-access', '--yes' + Extra + ' --password-pipe ' + PipeB);
  finally
    RunMode := 0;
    ProgressPage.Hide;
  end;
end;

procedure RemoteCopyClick(Sender: TObject);
begin
  CopyTextToClipboard(RemoteLinkEdit.Text);
end;

{ A failure that carries a link (design 18.4, e.g. reason=funnel-not-enabled): the outcome stays on
  the Remote page as ONE fixed sentence (design 19.4 item 17: the helper's own message, which already
  holds the link, used to be pasted in as well, so the link showed twice), then the link below it as
  a clickable link AND as selectable text with a Copy button (design 19.5 item 12: the tailnet's owner
  often opens it on another device), then Retry. Next then carries on to the Finished page without
  remote access. The controls are moved up under the short sentence: the page was laid out for the
  long intro text. }
procedure ShowRemoteLinkStage(const Link: String);
begin
  RemoteStage := 1;
  RemoteLinkUrl := Link;
  RemoteLabel.Height := ScaleY(48);
  RemoteLabel.Caption := CustomMessage('remoteNeedStep');
  RemoteSkip.Visible := False;
  RemoteSetUp.Visible := False;
  RemoteLinkLabel.Top := ScaleY(56);
  RemoteLinkLabel.Caption := Link;
  RemoteLinkLabel.Visible := True;
  RemoteLinkEdit.Top := ScaleY(82);
  RemoteLinkEdit.Text := Link;
  RemoteLinkEdit.Visible := True;
  RemoteCopyButton.Top := RemoteLinkEdit.Top - ScaleY(2);
  RemoteCopyButton.Left := RemoteLinkEdit.Left + RemoteLinkEdit.Width + ScaleX(8);
  RemoteCopyButton.Visible := True;
  RemoteRetryButton.Top := ScaleY(118);
  RemoteRetryButton.Visible := True;
  Log('remote access: outcome shown on the Remote page with a link, a copy box and Retry; link=' + RedactToken(Link));
end;

{ Leaves the link stage: the link, its copy box and Retry go away (a Retry that worked, or one that
  ended skipped). }
procedure HideRemoteLinkStage;
begin
  RemoteLinkLabel.Visible := False;
  RemoteLinkEdit.Visible := False;
  RemoteCopyButton.Visible := False;
  RemoteRetryButton.Visible := False;
end;

{ Runs the verb and turns its outcome into the Finished page's note (RemoteNote), the public address
  (PublicUrl) and, for a failure with a link, the Remote page's link and Retry (RemoteStage = 1). }
procedure RunRemoteAccess;
var
  AltPort, Link, Extra: String;
  Ok, Handed, PortBusy: Boolean;
begin
  RemoteNote := '';
  Extra := '';
  if RemoteFunnelPort <> '' then
    Extra := ' --funnel-port ' + RemoteFunnelPort;
  Log('remote access: starting; funnel_port=' + RemoteFunnelPort + ' stage=' + IntToStr(RemoteStage));
  Ok := RemoteAttempt(Extra, CustomMessage('remoteProgressCaption'), Handed);
  if not Handed then
  begin
    RemoteNote := CustomMessage('remoteUnavailable');
    Exit;
  end;
  { Section 9 step 4: the helper never replaces a Funnel that already serves something else on
    port 443. It answers reason=funnel-port-busy with the free ports it can use instead, and the
    rerun names one with --funnel-port. A new broker serves the rerun (the first served its one use). }
  PortBusy := (not Ok) and (ResultValue(LastResult, 'reason') = 'funnel-port-busy');
  if PortBusy then
  begin
    AltPort := ResultValue(LastResult, 'offer');
    Log('remote access: public port 443 is taken; offered ' + AltPort);
    if Pos(',', AltPort) > 0 then
      AltPort := Copy(AltPort, 1, Pos(',', AltPort) - 1);
    if (AltPort <> '') and (MsgBox(FmtMessage(CustomMessageWithLines('remotePortConflict'), [AltPort, AltPort]),
      mbConfirmation, MB_YESNO) = IDYES) then
    begin
      RemoteFunnelPort := AltPort;
      Ok := RemoteAttempt(' --funnel-port ' + AltPort,
        FmtMessage(CustomMessage('remoteProgressPort'), [AltPort]), Handed);
      if not Handed then
      begin
        RemoteNote := CustomMessage('remoteUnavailable');
        Exit;
      end;
      PortBusy := (not Ok) and (ResultValue(LastResult, 'reason') = 'funnel-port-busy');
    end
    else
      Log('remote access: the alternate port was declined or none was offered (offer=[' + AltPort + '])');
  end;
  { Design 19.4 item 11: no port was accepted, so remote access is SKIPPED, which is the user's own
    decision and not a failure: a note on the Finished page, no error box. The helper answered with a
    warning line and no `failed` line, so the generic "reported a failure without saying" text that
    EnsureFailureText builds must not be shown either. }
  if PortBusy then
  begin
    RemoteNote := CustomMessage('remoteSkipped');
    Log('remote access: skipped; ' + RemoteNote);
    if RemoteStage = 1 then
    begin
      HideRemoteLinkStage;
      RemoteLabel.Caption := RemoteNote + #13#10 + CustomMessage('remoteSkippedNext');
    end;
    RemoteStage := 2;
    Exit;
  end;
  if Ok then
  begin
    PublicUrl := ResultValue(LastResult, 'public_url');
    RemoteNote := CustomMessage('remoteOnNote');
    Log('remote access: on; public_url_set=' + IntToStr(Ord(PublicUrl <> '')));
    if RemoteStage = 1 then
    begin
      { A Retry that worked: say so on the page and let Next carry on. }
      HideRemoteLinkStage;
      RemoteLabel.Caption := CustomMessage('remoteOnNext');
    end;
    RemoteStage := 2;
  end
  else
  begin
    Link := ResultValue(LastResult, 'link');
    RemoteNote := CustomMessage('remoteFailurePrefix') + ' ' + FailMessage;
    if FailFix <> '' then
    RemoteNote := RemoteNote + ' ' + FailFix;
    RemoteNote := RemoteNote + AppendTechnicalDetail(FailFix, FailTechnicalFix, CustomMessage('technicalDetail'));
    RemoteNote := RemoteNote + ' ' + CustomMessage('remoteTryLater');
    Log('remote access: failed; reason=' + ResultValue(LastResult, 'reason') + ' link_set=' + IntToStr(Ord(Link <> '')));
    if Link <> '' then
      ShowRemoteLinkStage(Link)
    else
      MsgBox(FmtMessage(CustomMessageWithLines('remoteFailed'), [FailText]), mbInformation, MB_OK);
  end;
end;

procedure RemoteRetryClick(Sender: TObject);
begin
  Log('remote access: Retry pressed');
  RunRemoteAccess;
end;

procedure InitializeWizard;
var
  Idx: Integer;
begin
  WizardUp := True;
  CloseQuietly := False;
  BusyPage := CreateOutputMarqueeProgressPage('Working', 'One moment...');
  ProgressPage := CreateOutputProgressPage('Installing Cognita',
    'Setup is installing Cognita. This can take several minutes; keep this window open.');
  ProgressPage.Msg2Label.OnClick := @LinkClick;
  { The Tailscale sign-in link as selectable text with a Copy button, and the "how long" sentence
    (design 19.5 item 12 and 19.9). Hidden until ShowProgressLine sees a sign-in link. They sit under
    the progress bar, on the page's own surface. }
  ProgressLinkEdit := TNewEdit.Create(ProgressPage);
  ProgressLinkEdit.Parent := ProgressPage.Surface;
  ProgressLinkEdit.Left := 0;
  ProgressLinkEdit.Top := ProgressPage.ProgressBar.Top + ProgressPage.ProgressBar.Height + ScaleY(24);
  ProgressLinkEdit.Width := ProgressPage.SurfaceWidth - ScaleX(92);
  ProgressLinkEdit.ReadOnly := True;
  ProgressLinkEdit.Visible := False;
  ProgressCopyButton := TNewButton.Create(ProgressPage);
  ProgressCopyButton.Parent := ProgressPage.Surface;
  ProgressCopyButton.Left := ProgressLinkEdit.Left + ProgressLinkEdit.Width + ScaleX(8);
  ProgressCopyButton.Top := ProgressLinkEdit.Top - ScaleY(2);
  ProgressCopyButton.Width := ScaleX(84);
  ProgressCopyButton.Height := ScaleY(25);
  ProgressCopyButton.Caption := CustomMessage('copyLink');
  ProgressCopyButton.OnClick := @ProgressCopyClick;
  ProgressCopyButton.Visible := False;
  ProgressWaitLabel := TNewStaticText.Create(ProgressPage);
  ProgressWaitLabel.Parent := ProgressPage.Surface;
  ProgressWaitLabel.Left := 0;
  ProgressWaitLabel.Top := ProgressLinkEdit.Top + ScaleY(32);
  ProgressWaitLabel.Width := ProgressPage.SurfaceWidth;
  ProgressWaitLabel.AutoSize := False;
  ProgressWaitLabel.WordWrap := True;
  ProgressWaitLabel.Height := ScaleY(34);
  ProgressWaitLabel.Caption := CustomMessage('signInWait');
  ProgressWaitLabel.Visible := False;
  { Design 21.4: Skip self-tests, under the bar on the right (the row the Copy button uses; the two are
    never visible together). Shown by ShowProgressLine while the stage is `proof`. }
  ProgressSkipButton := TNewButton.Create(ProgressPage);
  ProgressSkipButton.Parent := ProgressPage.Surface;
  ProgressSkipButton.Width := ScaleX(120);
  ProgressSkipButton.Height := ScaleY(25);
  ProgressSkipButton.Left := ProgressPage.SurfaceWidth - ProgressSkipButton.Width;
  ProgressSkipButton.Top := ProgressPage.ProgressBar.Top + ProgressPage.ProgressBar.Height + ScaleY(22);
  ProgressSkipButton.Caption := CustomMessage('skipProof');
  ProgressSkipButton.OnClick := @ProgressSkipClick;
  ProgressSkipButton.Visible := False;

  { WSL (shown only when the preflight check says WSL is missing or too old) }
  PageWsl := CreateCustomPage(wpWelcome, CustomMessage('wslTitle'),
    CustomMessage('wslPageDescription'));
  WslLabel := TNewStaticText.Create(PageWsl);
  WslLabel.Parent := PageWsl.Surface;
  WslLabel.Left := 0;
  WslLabel.Top := 0;
  WslLabel.Width := PageWsl.SurfaceWidth;
  WslLabel.AutoSize := False;
  WslLabel.WordWrap := True;
  WslLabel.Height := ScaleY(110);
  WslLabel.Caption := CustomMessage('wslDetails');
  WslRestartNow := TNewRadioButton.Create(PageWsl);
  WslRestartNow.Parent := PageWsl.Surface;
  WslRestartNow.Left := 0;
  WslRestartNow.Top := ScaleY(120);
  WslRestartNow.Width := PageWsl.SurfaceWidth;
  WslRestartNow.Caption := CustomMessage('wslRestartNow');
  WslRestartNow.Checked := True;
  WslRestartNow.OnEnter := @RadioEnter;
  WslRestartNow.Visible := False;
  WslRestartLater := TNewRadioButton.Create(PageWsl);
  WslRestartLater.Parent := PageWsl.Surface;
  WslRestartLater.Left := 0;
  WslRestartLater.Top := ScaleY(146);
  WslRestartLater.Width := PageWsl.SurfaceWidth;
  WslRestartLater.Caption := CustomMessage('wslRestartLater');
  WslRestartLater.OnEnter := @RadioEnter;
  WslRestartLater.Visible := False;
  WslPageStage := 0;

  { Projects folder }
  PageFolder := CreateInputDirPage(PageWsl.ID, CustomMessage('folderTitle'),
    CustomMessage('folderQuestion'), CustomMessage('folderDescription'), False, '');
  PageFolder.Add('');
  PageFolder.Values[0] := '';
  FolderNote := TNewStaticText.Create(PageFolder);
  FolderNote.Parent := PageFolder.Surface;
  FolderNote.Left := 0;
  FolderNote.Top := PageFolder.Buttons[0].Top + PageFolder.Buttons[0].Height + ScaleY(24);
  FolderNote.Width := PageFolder.SurfaceWidth;
  FolderNote.AutoSize := False;
  FolderNote.WordWrap := True;
  FolderNote.Height := ScaleY(60);
  FolderNote.Caption := CustomMessage('folderOneDrive');
  { The reason `roots --validate` gave for a folder Cognita cannot use (design 18.5), shown on the page. }
  FolderReason := TNewStaticText.Create(PageFolder);
  FolderReason.Parent := PageFolder.Surface;
  FolderReason.Left := 0;
  FolderReason.Top := FolderNote.Top + FolderNote.Height;
  FolderReason.Width := PageFolder.SurfaceWidth;
  FolderReason.AutoSize := False;
  FolderReason.WordWrap := True;
  FolderReason.Height := ScaleY(60);
  FolderReason.Font.Color := clRed;
  FolderReason.Caption := '';

  { Admin sign-in. The sub-caption is set per mode in CurPageChanged. The page is created with
    a caption long enough for the update guidance:
    Inno places the fields below the caption's height at creation and never moves them, so a caption
    that later grows to two lines runs under the first field. }
  PageAdmin := CreateInputQueryPage(PageFolder.ID, CustomMessage('adminTitle'),
    CustomMessage('adminDescription'), CustomMessage('verifyPasswordCaption'));
  Idx := PageAdmin.Add(CustomMessage('adminUser'), False);
  PageAdmin.Values[Idx] := 'admin';
  Idx := PageAdmin.Add(CustomMessage('newPassword'), True);
  Idx := PageAdmin.Add(CustomMessage('passwordAgain'), True);

  { Acceleration (design 22.7): shown only when preflight saw an NVIDIA card (ShouldSkipPage). The texts, the
    enabled radio, the link and the note are set per run in CurPageChanged; the controls are placed there too,
    because the label's height depends on its text. }
  PageAccel := CreateCustomPage(PageAdmin.ID, CustomMessage('accelTitle'),
    CustomMessage('accelDescription'));
  AccelLabelText := TNewStaticText.Create(PageAccel);
  AccelLabelText.Parent := PageAccel.Surface;
  AccelLabelText.Left := 0;
  AccelLabelText.Top := 0;
  AccelLabelText.Width := PageAccel.SurfaceWidth;
  AccelLabelText.AutoSize := False;
  AccelLabelText.WordWrap := True;
  AccelLabelText.Height := ScaleY(48);
  AccelLink := TNewStaticText.Create(PageAccel);
  AccelLink.Parent := PageAccel.Surface;
  AccelLink.Left := 0;
  AccelLink.Top := ScaleY(52);
  AccelLink.Caption := NvidiaDriverUrl;
  StyleAsLink(AccelLink);
  AccelLink.OnClick := @AccelLinkClick;
  AccelLink.Visible := False;
  AccelUseGpu := TNewRadioButton.Create(PageAccel);
  AccelUseGpu.Parent := PageAccel.Surface;
  AccelUseGpu.Left := 0;
  AccelUseGpu.Top := ScaleY(76);
  AccelUseGpu.Width := PageAccel.SurfaceWidth;
  AccelUseGpu.Caption := CustomMessage('gpuChoice');
  AccelUseGpu.OnEnter := @RadioEnter;
  AccelUseCpu := TNewRadioButton.Create(PageAccel);
  AccelUseCpu.Parent := PageAccel.Surface;
  AccelUseCpu.Left := 0;
  AccelUseCpu.Top := ScaleY(100);
  AccelUseCpu.Width := PageAccel.SurfaceWidth;
  AccelUseCpu.Caption := CustomMessage('cpuChoice');
  AccelUseCpu.Checked := True;
  AccelUseCpu.OnEnter := @RadioEnter;
  AccelNote := TNewStaticText.Create(PageAccel);
  AccelNote.Parent := PageAccel.Surface;
  AccelNote.Left := 0;
  AccelNote.Top := ScaleY(132);
  AccelNote.Width := PageAccel.SurfaceWidth;
  AccelNote.AutoSize := False;
  AccelNote.WordWrap := True;
  AccelNote.Height := ScaleY(32);
  AccelNote.Caption := CustomMessage('accelNote');
  AccelNote.Visible := False;

  { Ready }
  PageReady := CreateCustomPage(PageAccel.ID, CustomMessage('readyTitle'), CustomMessage('readyDescription'));
  ReadyText := TNewStaticText.Create(PageReady);
  ReadyText.Parent := PageReady.Surface;
  ReadyText.Left := 0;
  ReadyText.Top := 0;
  ReadyText.Width := PageReady.SurfaceWidth;
  ReadyText.AutoSize := False;
  ReadyText.WordWrap := True;
  ReadyText.Height := PageReady.SurfaceHeight - ScaleY(40);
  { Design 22.9: the WSL file-cache check box, under the summary and above Advanced. Hidden (and the summary at
    full height) unless wsl_reclaim=unset; ShowReadyPage sets that each time the page is shown. Ticked by
    default. Neither TNewCheckBox nor TNewRadioButton wraps its caption (verified: ISCC rejects WordWrap on both),
    and this caption is three lines, so the box is a bare check square and the design's caption, word for word,
    is a wrapped label beside it that toggles the box when clicked (ReclaimTextClick). }
  ReclaimBox := TNewCheckBox.Create(PageReady);
  ReclaimBox.Parent := PageReady.Surface;
  ReclaimBox.Left := 0;
  ReclaimBox.Width := ScaleX(18);
  ReclaimBox.Height := ScaleY(17);
  ReclaimBox.Top := PageReady.SurfaceHeight - ScaleY(28) - ScaleY(48) - ScaleY(6);
  ReclaimBox.Checked := True;
  ReclaimBox.Visible := False;
  ReclaimText := TNewStaticText.Create(PageReady);
  ReclaimText.Parent := PageReady.Surface;
  ReclaimText.Left := ScaleX(20);
  ReclaimText.Top := ReclaimBox.Top + ScaleY(1);
  ReclaimText.Width := PageReady.SurfaceWidth - ScaleX(20);
  ReclaimText.AutoSize := False;
  ReclaimText.WordWrap := True;
  ReclaimText.Height := ScaleY(48);
  ReclaimText.Cursor := crHand;
  ReclaimText.Caption := CustomMessage('memoryReclaim');
  ReclaimText.OnClick := @ReclaimTextClick;
  ReclaimText.Visible := False;
  AdvancedButton := TNewButton.Create(PageReady);
  AdvancedButton.Parent := PageReady.Surface;
  AdvancedButton.Left := 0;
  AdvancedButton.Top := PageReady.SurfaceHeight - ScaleY(28);
  AdvancedButton.Width := ScaleX(100);
  AdvancedButton.Height := ScaleY(25);
  AdvancedButton.Caption := CustomMessage('advancedButton');
  AdvancedButton.OnClick := @AdvancedClick;

  { Remote access (after the install step) }
  PageRemote := CreateCustomPage(wpInstalling, CustomMessage('remoteTitle'),
    CustomMessage('remoteSubtitle'));
  RemoteLabel := TNewStaticText.Create(PageRemote);
  RemoteLabel.Parent := PageRemote.Surface;
  RemoteLabel.Left := 0;
  RemoteLabel.Top := 0;
  RemoteLabel.Width := PageRemote.SurfaceWidth;
  RemoteLabel.AutoSize := False;
  RemoteLabel.WordWrap := True;
  RemoteLabel.Height := ScaleY(176);
  { 2026-09-29 (Doug): recommended, not optional. What decides it is WHO makes the connection to Cognita:
    an assistant whose own servers connect (claude.ai, Claude Desktop, ChatGPT, Gemini) needs a public
    address; only a tool on this PC that reaches Cognita itself (Claude Code, Codex, Google Antigravity,
    Cursor, a local LLM) can do without, and even then a tunnel is recommended. Cognita listens on this PC
    only (127.0.0.1), so "on this PC" is exact. The old text ("optional", "use Cognita from claude.ai over
    the internet") read as if this were a niche extra. }
  RemoteLabel.Caption := CustomMessageWithLines('remoteBody');
  RemoteSkip := TNewRadioButton.Create(PageRemote);
  RemoteSkip.Parent := PageRemote.Surface;
  RemoteSkip.Left := 0;
  RemoteSkip.Top := ScaleY(212);
  RemoteSkip.Width := PageRemote.SurfaceWidth;
  RemoteSkip.Caption := CustomMessage('remoteSkip');
  RemoteSkip.OnEnter := @RadioEnter;
  RemoteSetUp := TNewRadioButton.Create(PageRemote);
  RemoteSetUp.Parent := PageRemote.Surface;
  RemoteSetUp.Left := 0;
  RemoteSetUp.Top := ScaleY(186);
  RemoteSetUp.Width := PageRemote.SurfaceWidth;
  RemoteSetUp.Caption := CustomMessage('remoteSetup');
  RemoteSetUp.Checked := True;
  RemoteSetUp.OnEnter := @RadioEnter;
  { After a failure that carries a link (design 18.4: Funnel not enabled on the tailnet): the link, and
    Retry, which hands the password over again and reruns the verb. Hidden until then. }
  RemoteLinkLabel := TNewStaticText.Create(PageRemote);
  RemoteLinkLabel.Parent := PageRemote.Surface;
  RemoteLinkLabel.Left := 0;
  RemoteLinkLabel.Top := ScaleY(130);
  RemoteLinkLabel.Width := PageRemote.SurfaceWidth;
  RemoteLinkLabel.AutoSize := False;
  RemoteLinkLabel.Height := ScaleY(18);
  StyleAsLink(RemoteLinkLabel);
  RemoteLinkLabel.OnClick := @RemoteLinkClick;
  RemoteLinkLabel.Visible := False;
  { The same link as selectable text with a Copy button (design 19.5 item 12); ShowRemoteLinkStage
    places them under the link. }
  RemoteLinkEdit := TNewEdit.Create(PageRemote);
  RemoteLinkEdit.Parent := PageRemote.Surface;
  RemoteLinkEdit.Left := 0;
  RemoteLinkEdit.Top := ScaleY(150);
  RemoteLinkEdit.Width := PageRemote.SurfaceWidth - ScaleX(92);
  RemoteLinkEdit.ReadOnly := True;
  RemoteLinkEdit.Visible := False;
  RemoteCopyButton := TNewButton.Create(PageRemote);
  RemoteCopyButton.Parent := PageRemote.Surface;
  RemoteCopyButton.Left := RemoteLinkEdit.Left + RemoteLinkEdit.Width + ScaleX(8);
  RemoteCopyButton.Top := RemoteLinkEdit.Top - ScaleY(2);
  RemoteCopyButton.Width := ScaleX(84);
  RemoteCopyButton.Height := ScaleY(25);
  RemoteCopyButton.Caption := CustomMessage('copyLink');
  RemoteCopyButton.OnClick := @RemoteCopyClick;
  RemoteCopyButton.Visible := False;
  RemoteRetryButton := TNewButton.Create(PageRemote);
  RemoteRetryButton.Parent := PageRemote.Surface;
  RemoteRetryButton.Left := 0;
  RemoteRetryButton.Top := ScaleY(160);
  RemoteRetryButton.Width := ScaleX(100);
  RemoteRetryButton.Height := ScaleY(25);
  RemoteRetryButton.Caption := CustomMessage('retryButton');
  RemoteRetryButton.OnClick := @RemoteRetryClick;
  RemoteRetryButton.Visible := False;
  RemoteStage := 0;

  { Finished page extras (visible only after a failure) }
  DiagButton := TNewButton.Create(WizardForm);
  DiagButton.Parent := WizardForm.FinishedPage;
  DiagButton.Caption := CustomMessage('saveDiagnostics');
  DiagButton.Width := ScaleX(120);
  DiagButton.Height := ScaleY(25);
  DiagButton.OnClick := @SaveDiagnosticsClick;
  DiagButton.Visible := False;
  LogLink := TNewStaticText.Create(WizardForm);
  LogLink.Parent := WizardForm.FinishedPage;
  LogLink.Caption := CustomMessage('openLogs');
  LogLink.Cursor := crHand;
  LogLink.Font.Color := clBlue;
  LogLink.Font.Style := [fsUnderline];
  LogLink.OnClick := @OpenLogsClick;
  LogLink.Visible := False;
  ReportLink := TNewStaticText.Create(WizardForm);
  ReportLink.Parent := WizardForm.FinishedPage;
  ReportLink.Caption := CustomMessage('reportProblem');
  StyleAsLink(ReportLink);
  ReportLink.OnClick := @ReportProblemClick;
  ReportLink.Visible := False;

  { Finished page after a successful install: the addresses are links (the same link control as
    "Open the log folder"), then how to connect a client and the everyday commands (design C12, P1). }
  FinAdminPre := NewFinishedText('Admin:');
  FinAdminLink := NewFinishedText('');
  StyleAsLink(FinAdminLink);
  FinAdminLink.OnClick := @FinAdminClick;
  FinAdminPost := NewFinishedText('');
  FinMcpPre := NewFinishedText('MCP:');
  FinMcpLink := NewFinishedText('');
  StyleAsLink(FinMcpLink);
  FinMcpLink.OnClick := @FinMcpClick;
  FinPubPre := NewFinishedText(CustomMessage('publicLabel'));
  FinPubLink := NewFinishedText('');
  StyleAsLink(FinPubLink);
  FinPubLink.OnClick := @FinPubClick;
  FinConnectLabel := NewFinishedText('');
  FinConnectLabel.AutoSize := False;
  FinConnectLabel.WordWrap := True;
  FinMemo := TNewMemo.Create(WizardForm);
  FinMemo.Parent := WizardForm.FinishedPage;
  FinMemo.ReadOnly := True;
  FinMemo.ScrollBars := ssVertical;
  FinMemo.WordWrap := True;
  FinMemo.Visible := False;
end;

function InitializeSetup: Boolean;
begin
  Result := False;
  ChosenMcpPort := DefaultMcpPort;
  ChosenAdminPort := DefaultAdminPort;
  ChosenDataDir := ExpandConstant('{localappdata}\Cognita\wsl');
  ChosenWorkspaceOn := True;
  RunMode := 0;
  ResumeSwitch := HasSwitch('/resume');
  { Setup started from a PowerShell 7 window inherits 7's PSModulePath, and every Windows
    PowerShell 5.1 helper it runs would then fail to load 5.1's own built-in modules (seen with
    Get-AuthenticodeSignature). Without the variable, 5.1 builds its default module path. The
    cognita.exe launcher does the same. }
  SetEnvironmentVariableW('PSModulePath', 0);
  Log('setup start: version={#Version} revision={#Revision} resume_switch=' + IntToStr(Ord(ResumeSwitch)) +
    ' srcexe=' + ExpandConstant('{srcexe}'));
  if WizardSilent then
  begin
    Log('setup: a silent install is not supported; Setup asks questions (folder, password). Refusing.');
    Exit;
  end;
  if not FileExists(PsExe) then
  begin
    MsgBox(FmtMessage(CustomMessage('powerShellMissing'), [PsExe]), mbError, MB_OK);
    Exit;
  end;
  try
    ExtractTemporaryFile('CognitaWin.ps1');
    ExtractTemporaryFile('windows-setup.en-US.json');
    ExtractTemporaryFile('windows-setup.es-ES.json');
    ExtractTemporaryFile('windows-setup.fr-FR.json');
    ExtractTemporaryFile('windows-setup.de-DE.json');
    ExtractTemporaryFile('windows-setup.it-IT.json');
    ExtractTemporaryFile('windows-setup.pt-BR.json');
  except
    Log('setup: could not extract the helper: ' + GetExceptionMessage);
    MsgBox(FmtMessage(CustomMessageWithLines('helperUnpackFailed'), [GetExceptionMessage]), mbError, MB_OK);
    Exit;
  end;
  HelperFolder := ExpandConstant('{tmp}');
  if not SetEnvironmentVariableTextW('COGNITA_LANG', SetupLocaleTag) then
    Log('setup: could not set COGNITA_LANG for helper processes');
  if not RunHelper('state', '') then
  begin
    MsgBox(FmtMessage(CustomMessageWithLines('stateReadFailed'), [FailText, ExpandConstant('{log}')]), mbError, MB_OK);
    Exit;
  end;
  ReadState;
  InstallMode := ModeFromState(LastResult, '{#Version}');
  Log('setup: install_mode=' + IntToStr(InstallMode) + ' (0 fresh, 1 repair, 2 update, 3 finish, 4 reinstall) from distro_owned=' +
    IntToStr(Ord(StDistroPresent)) + ' installed=' + IntToStr(Ord(StInstalled)) + ' linux_version=' +
    StLinuxVersion + ' setup_version={#Version} admin_user=' + StAdminUser);
  { Design 19.2 item 6: no downgrade. The version compare is numeric, so 14.10 is newer than 14.9. Only an
    installed Cognita counts (installed=1 means an owned distro AND a recorded linux_version): a stale
    version with no distro is a fresh install, and the helper resets those records. The helper's `update`
    verb refuses the same case (reason=older-setup), so this is the friendly front door, not the only lock. }
  if StInstalled and SetupIsOlder(StLinuxVersion, '{#Version}') then
  begin
    Log('setup: refusing a downgrade; installed ' + StLinuxVersion + ' is newer than this Setup {#Version}');
    MsgBox(FmtMessage(CustomMessage('downgradeText'), [StLinuxVersion, '{#Version}']), mbError, MB_OK);
    Exit;
  end;
  Log('setup: no downgrade (installed=[' + StLinuxVersion + '] setup={#Version} compare=' +
    IntToStr(CompareVersions(StLinuxVersion, '{#Version}')) + ')');
  Result := True;
end;

{ ---- Copying a Setup or uninstall log into %LOCALAPPDATA%\Cognita\logs (design 18.5) ---- }

{ The local date and time as yyyyMMdd-HHmmss (the machine's local time, like every Cognita log). }
function StampNow: String;
begin
  Result := GetDateTimeString('yyyymmdd-hhnnss', #0, #0);
end;

{ Copies the log Inno is writing (the log constant) to DestFile. The log is open for writing while
  Setup runs, so CopyFile can be refused; the fallback reads the bytes with LoadStringFromFile.
  A missing or unreadable log is logged and skipped; it never stops Setup or the uninstaller. }
function CopyOwnLog(const DestFile: String): Boolean;
var
  Src: String;
  Data: AnsiString;
begin
  Result := False;
  Src := '';
  try
    Src := ExpandConstant('{log}');
  except
    Log('log copy: no log file is being written; nothing to copy (' + GetExceptionMessage + ')');
    Exit;
  end;
  if (Src = '') or (not FileExists(Src)) then
  begin
    Log('log copy: the log file ' + Src + ' does not exist; nothing to copy');
    Exit;
  end;
  try
    ForceDirectories(ExtractFileDir(DestFile));
    if CopyFile(Src, DestFile, False) then
    begin
      Result := True;
      Log('log copy: ' + Src + ' -> ' + DestFile + ' (CopyFile)');
      Exit;
    end;
    Log('log copy: CopyFile of ' + Src + ' was refused; reading the bytes instead');
    if LoadStringFromFile(Src, Data) and SaveStringToFile(DestFile, Data, False) then
    begin
      Result := True;
      Log('log copy: ' + Src + ' -> ' + DestFile + ' (' + IntToStr(Length(Data)) + ' bytes read and written)');
    end
    else
      Log('log copy: could not copy ' + Src + ' to ' + DestFile);
  except
    Log('log copy: exception copying ' + Src + ' to ' + DestFile + ': ' + GetExceptionMessage);
  end;
end;

procedure DeinitializeSetup;
begin
  AdminPassword := '';
  { Inno writes Setup's own log under %TEMP%, where nobody looks for it: copy it beside the helper
    logs, so support has both in one folder (design 18.5). }
  CopyOwnLog(LogsDir + '\setup-' + StampNow + '.log');
end;

{ Tight: the WSL check box takes room from the summary (design 22.9), so the two blank lines between its blocks
  become one line break; every word stays. }
function ReadySummary(const Tight: Boolean): String;
var
  Head, Gap, AccelText, Reason, WorkspaceText, DownloadsText: String;
begin
  Gap := #13#10#13#10;
  if Tight then
    Gap := #13#10;
  if InstallMode = ModeUpdate then
  begin
    if StLinuxVersion <> '' then
      Head := FmtMessage(CustomMessage('summaryUpdate'), [StLinuxVersion, '{#Version}'])
    else
      Head := FmtMessage(CustomMessage('summaryUpdateNoVersion'), ['{#Version}']);
  end
  else if InstallMode = ModeRepair then
    Head := FmtMessage(CustomMessage('summaryRepair'), ['{#Version}'])
  else if InstallMode = ModeReinstall then
    Head := FmtMessage(CustomMessage('summaryReinstall'), ['{#Version}'])
  else if InstallMode = ModeFinish then
    Head := FmtMessage(CustomMessage('summaryFinish'), ['{#Version}'])
  else
    Head := FmtMessage(CustomMessage('summaryInstall'), ['{#Version}']);
  Result := Head + Gap + FmtMessage(CustomMessage('summaryFolder'), [PageFolder.Values[0]]) + #13#10;
  { Design 19.2 item 14: a user name only when it is known (recorded by the helper) or really passed
    (fresh and finish). Over an installed Cognita that never told us the name, Setup does not know it and
    passes none, so the line is left out rather than showing a name that means nothing. }
  if AdminUser <> '' then
    Result := Result + FmtMessage(CustomMessage('summaryAdmin'), [AdminUser]) + #13#10;
  { Design 22.1, 22.12 items 1(b), 1(c) and 10: the acceleration the install will have, in every mode; the
    reason in parentheses when the GPU was not offered; "unchanged" when the install keeps a profile Setup
    does not know. }
  if EffectiveAccel = 'nvidia' then
    AccelText := CustomMessage('accelNvidia')
  else if EffectiveAccel = 'amd' then
    AccelText := CustomMessage('accelAmd')
  else if EffectiveAccel = 'cpu' then
    AccelText := CustomMessage('accelCpu')
  else
    AccelText := CustomMessage('accelUnchanged');
  Reason := '';
  if (InstallMode <> ModeUpdate) and (EffectiveAccel = 'cpu') then
  begin
    if AccelMode = AccelOldDriver then
      Reason := CustomMessage('accelReasonOld')
    else if AccelMode = AccelNoBuild then
      Reason := CustomMessage('accelReasonNoBuild')
    else if (AccelMode = AccelHidden) and (Lowercase(Trim(NvState)) = 'none') then
      Reason := CustomMessage('accelReasonNoCard');
  end;
  if Reason <> '' then
    AccelText := AccelText + ' ' + Reason;
  WorkspaceText := CustomMessage('workspaceOff');
  if ChosenWorkspaceOn then
    WorkspaceText := CustomMessage('workspaceOn');
  Result := Result +
    FmtMessage(CustomMessage('summaryPorts'), [IntToStr(ChosenMcpPort), IntToStr(ChosenAdminPort)]) + #13#10 +
    FmtMessage(CustomMessage('summaryWorkspace'), [WorkspaceText]) + #13#10 +
    FmtMessage(CustomMessage('summaryAcceleration'), [AccelText]) + #13#10 +
    FmtMessage(CustomMessage('summaryDataLocation'), [ChosenDataDir]) + Gap;
  Log('ready page: acceleration line=[' + AccelText + '] effective=[' + EffectiveAccel + '] page_mode=' + IntToStr(AccelMode) +
    ' mode=' + IntToStr(InstallMode) + ' touched=' + IntToStr(Ord(AccelTouched)) + ' images_bytes=' + IntToStr(ImagesBytes));
  { Over an installed Cognita the images and models are already here, so only what changed is
    fetched; the compiled-in total would overstate it. }
  if RunsOverInstalled then
    DownloadsText := FmtMessage(CustomMessage('summaryDownloadsExisting'), [FormatBytes(DownloadBytes), FormatBytes(DiskNeededBytes)])
  else
    DownloadsText := FmtMessage(CustomMessage('summaryDownloadsFresh'), [FormatBytes(DownloadBytes), FormatBytes(DiskNeededBytes)]);
  Result := Result + DownloadsText + ' ' + CustomMessage('summaryFreeDisk');
  if InstallMode = ModeUpdate then
    Result := Result + CustomMessage('summaryUpdateNote')
  else if DataLocationLocked then
    Result := Result + ' ' + CustomMessage('summaryLockedNote')
  else
    Result := Result + ' ' + CustomMessage('summaryFreshNote');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = PageWsl.ID then
    Result := not ((WslState = 'missing') or (WslState = 'old'))
  else if PageID = PageAccel.ID then
    Result := AccelMode = AccelHidden
  else if PageID = PageRemote.ID then
    Result := not InstallOk;
end;

{ One row of the Finished page's address list: a caption, the address as a link, and optionally a
  trailing plain text (the sign-in user). Y moves down one row. }
procedure PlaceLinkRow(const Pre, Link: TNewStaticText; const LinkText: String;
  const Post: TNewStaticText; const PostText: String; const X: Integer; var Y: Integer);
begin
  Pre.Left := X;
  Pre.Top := Y;
  Pre.Visible := True;
  Link.Caption := LinkText;
  Link.Left := X + ScaleX(62);
  Link.Top := Y;
  Link.Visible := True;
  if Post <> nil then
  begin
    Post.Caption := PostText;
    Post.Left := Link.Left + Link.Width + ScaleX(8);
    Post.Top := Y;
    Post.Visible := True;
  end;
  Y := Y + ScaleY(18);
end;

{ The Finished page after a successful install (design 0 step 9, C12, P1): that Cognita starts at
  sign-in, the Admin / MCP / public addresses as links, how to connect a client (the same sentence
  the Linux installer's finish screen gives), and a small scrolling memo with the everyday commands.
  The wizard's own text label is sized to its text; the memo takes whatever height is left above the
  "Open Cognita Admin" check box. }
procedure LayoutFinishedSuccess;
var
  X, Y, W, Bottom: Integer;
  Cap, WsText, UserText, Notes, AccelText, AccelProfile: String;
begin
  X := WizardForm.FinishedLabel.Left;
  W := WizardForm.FinishedLabel.Width;
  WizardForm.FinishedHeadingLabel.Caption := CustomMessage('finishedHeading');
  { Design 19.4 item 2: "Cognita starts when you sign in to Windows" is dropped when the helper warned
    that the sign-in task could not be registered, because then it is not true. }
  Cap := FmtMessage(CustomMessage('finishedRunning'), ['{#Version}']);
  if not InstallSignInWarned then
    Cap := Cap + CustomMessage('finishedStarts');
  { Workspace shows what the install/update result (`status`) reported, which can differ from the
    Ready choice (for example Workspace turned off because this PC's WSL has no /dev/kvm). Only when
    the result said nothing does the choice stand in for it. }
  if ResultWorkspace <> '' then
  begin
    if Lowercase(ResultWorkspace) = 'on' then
      WsText := CustomMessage('workspaceOn')
    else if Lowercase(ResultWorkspace) = 'off' then
      WsText := CustomMessage('workspaceOff')
    else
      WsText := ResultWorkspace;
  end
  else
  begin
    WsText := CustomMessage('workspaceOff');
    if ChosenWorkspaceOn then
      WsText := CustomMessage('workspaceOn');
  end;
  Cap := Cap + #13#10 + FmtMessage(CustomMessage('finishedWorkspace'), [WsText]);
  { Design 22.7 and 22.12 item 11: the Acceleration line comes only from the result; no line when it did not say. }
  AccelProfile := '';
  if AccelKnown(ResultAccel) = 'cpu' then
    AccelProfile := CustomMessage('accelCpu')
  else if AccelKnown(ResultAccel) = 'amd' then
    AccelProfile := CustomMessage('accelAmd')
  else if AccelKnown(ResultAccel) = 'nvidia' then
    AccelProfile := CustomMessage('accelNvidia');
  AccelText := '';
  if AccelProfile <> '' then
    AccelText := FmtMessage(CustomMessage('summaryAcceleration'), [AccelProfile]);
  if AccelText <> '' then
    Cap := Cap + #13#10 + AccelText;
  Log('finished page: acceleration=[' + AccelText + '] from_result=' + IntToStr(Ord(ResultAccel <> '')) +
    ' result_value=[' + ResultAccel + ']');
  if RemoteNote <> '' then
    Cap := Cap + #13#10 + RemoteNote;
  WizardForm.FinishedLabel.Caption := Cap;
  WizardForm.FinishedLabel.AdjustHeight;
  Y := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(10);
  { The user name only when it is known or really passed (design 19.2 item 14). }
  UserText := '';
  if AdminUser <> '' then
    UserText := FmtMessage(CustomMessage('finishedUser'), [AdminUser]);
  PlaceLinkRow(FinAdminPre, FinAdminLink, AdminUrl(''), FinAdminPost, UserText, X, Y);
  PlaceLinkRow(FinMcpPre, FinMcpLink, 'http://localhost:' + IntToStr(ChosenMcpPort) + '/', nil, '', X, Y);
  if PublicUrl <> '' then
    PlaceLinkRow(FinPubPre, FinPubLink, PublicUrl, nil, '', X, Y);
  FinConnectLabel.Left := X;
  FinConnectLabel.Top := Y + ScaleY(4);
  FinConnectLabel.Width := W;
  FinConnectLabel.Caption := CustomMessage('finishedConnect');
  { Design 21.4: one line after the addresses when the self-tests were skipped; nothing else here changes. }
  if FinProofSkipped then
    FinConnectLabel.Caption := FinConnectLabel.Caption + #13#10 + CustomMessage('selfTestsSkipped');
  Log('finished page: skipped-self-tests note shown=' + IntToStr(Ord(FinProofSkipped)));
  FinConnectLabel.AdjustHeight;
  FinConnectLabel.Visible := True;
  Y := FinConnectLabel.Top + FinConnectLabel.Height + ScaleY(8);
  { The "Open Cognita Admin" check box sits at the bottom; the memo fills the space above it. }
  Bottom := WizardForm.FinishedPage.ClientHeight;
  if WizardForm.RunList.Visible then
  begin
    WizardForm.RunList.Height := ScaleY(26);
    WizardForm.RunList.Top := Bottom - WizardForm.RunList.Height - ScaleY(2);
    Bottom := WizardForm.RunList.Top - ScaleY(6);
  end;
  FinMemo.Left := X;
  FinMemo.Top := Y;
  FinMemo.Width := W;
  FinMemo.Height := Bottom - Y;
  if FinMemo.Height < ScaleY(48) then
    FinMemo.Height := ScaleY(48);
  { The memo starts with what the install run warned about (design 19.4 item 2): those lines used to be
    shown once in a box during the run, if at all, and were gone from the Finished page. }
  Notes := '';
  if Trim(InstallWarnText) <> '' then
    Notes := CustomMessage('pleaseNote') + #13#10#13#10 + InstallWarnText;
  FinMemo.Text := Notes + CustomMessage('finishedDailyIntro') + #13#10 +
    '  cognita status' + #13#10 +
    '  cognita logs app -f' + #13#10 +
    '  cognita start | stop | restart' + #13#10 +
    '  cognita password' + #13#10 +
    '  cognita add-folder' + #13#10 +
    '  cognita remote-access' + #13#10 +
    '  cognita diagnostics' + #13#10#13#10 +
    CustomMessage('finishedUpdate') + #13#10 +
    CustomMessage('finishedUninstall');
  FinMemo.Visible := True;
  Log('finished page: success layout label_h=' + IntToStr(WizardForm.FinishedLabel.Height) + ' memo_top=' + IntToStr(Y) +
    ' memo_h=' + IntToStr(FinMemo.Height) + ' page_h=' + IntToStr(WizardForm.FinishedPage.ClientHeight) +
    ' public_link=' + IntToStr(Ord(PublicUrl <> '')) + ' warnings_chars=' + IntToStr(Length(InstallWarnText)) +
    ' sign_in_warned=' + IntToStr(Ord(InstallSignInWarned)) + ' workspace=' + WsText +
    ' workspace_from_status=' + IntToStr(Ord(ResultWorkspace <> '')) + ' user_shown=' + IntToStr(Ord(AdminUser <> '')));
end;

{ Fills and lays out the Acceleration page for this run (design 22.1 and 22.7): the text for the page's flavor,
  the GPU radio enabled only when it is offered, the pick restored from ChosenAccel (Back and Next keep it), the
  note only when offered, the driver link only for an old driver. }
procedure ShowAccelPage;
var
  Mode: Integer;
  Card, Drv, GpuCap, CpuCap: String;
  Y: Integer;
begin
  Mode := AccelMode;
  Card := NvName;
  if Card = '' then
    Card := 'NVIDIA graphics card';
  Drv := NvDriver;
  if Drv = '' then
    Drv := 'version unknown';
  if Mode = AccelOffer then
    AccelLabelText.Caption := FmtMessage(CustomMessage('accelFound'), [Card, Drv])
  else if Mode = AccelOldDriver then
    AccelLabelText.Caption := FmtMessage(CustomMessage('accelOld'), [Drv])
  else
    AccelLabelText.Caption := CustomMessage('accelNoBuild');
  AccelLabelText.Width := PageAccel.SurfaceWidth;
  AccelLabelText.AdjustHeight;
  Y := AccelLabelText.Top + AccelLabelText.Height + ScaleY(8);
  AccelLink.Visible := Mode = AccelOldDriver;
  if AccelLink.Visible then
  begin
    AccelLink.Top := Y;
    Y := Y + AccelLink.Height + ScaleY(10);
  end;
  { The sizes are the compiled-in image sizes, formatted like every other size on Setup's pages. }
  GpuCap := CustomMessage('accelGpuChoice');
  if NvidiaBuildBytes > 0 then
    GpuCap := GpuCap + FmtMessage(CustomMessage('accelGpuDownload'), [FormatBytes(NvidiaBuildBytes)]);
  CpuCap := FmtMessage(CustomMessage('accelCpuChoice'), [FormatBytes(StrToInt64('{#SizeCognitaCpu}'))]);
  AccelUseGpu.Caption := GpuCap;
  AccelUseCpu.Caption := CpuCap;
  AccelUseGpu.Top := Y;
  AccelUseCpu.Top := Y + ScaleY(24);
  AccelNote.Top := AccelUseCpu.Top + ScaleY(32);
  AccelUseGpu.Enabled := Mode = AccelOffer;
  AccelNote.Visible := Mode = AccelOffer;
  { CPU first, then GPU: checking a radio unchecks its sibling, and a disabled GPU radio must never stay checked. }
  AccelUseCpu.Checked := True;
  if (Mode = AccelOffer) and (ChosenAccel = 'nvidia') then
    AccelUseGpu.Checked := True;
  Log('accel page shown: mode=' + IntToStr(Mode) + ' card=[' + NvName + '] driver=[' + NvDriver + '] gpu_enabled=' +
    IntToStr(Ord(AccelUseGpu.Enabled)) + ' chosen=[' + ChosenAccel + '] gpu_checked=' + IntToStr(Ord(AccelUseGpu.Checked)) +
    ' link=' + IntToStr(Ord(AccelLink.Visible)) + ' note=' + IntToStr(Ord(AccelNote.Visible)) +
    ' label_h=' + IntToStr(AccelLabelText.Height));
end;

{ The Ready page (design 22.9): the check box when wsl_reclaim=unset, the summary above it at the height that is
  left. Logs how much room the summary needs and has, so an overflow is in the log and not only on a screen. }
procedure ShowReadyPage;
var
  Avail, Needed: Integer;
  BoxShown: Boolean;
begin
  BoxShown := WslReclaimBoxVisible(WslReclaim);
  ReclaimBox.Visible := BoxShown;
  ReclaimText.Visible := BoxShown;
  if BoxShown then
    Avail := ReclaimBox.Top - ScaleY(4)
  else
    Avail := PageReady.SurfaceHeight - ScaleY(40);
  ReadyText.Caption := ReadySummary(BoxShown);
  ReadyText.Height := Avail;
  ReadyText.AdjustHeight;
  Needed := ReadyText.Height;
  ReadyText.Height := Avail;
  Log('ready page: wsl_reclaim=[' + WslReclaim + '] box_visible=' + IntToStr(Ord(BoxShown)) + ' box_ticked=' +
    IntToStr(Ord(ReclaimBox.Checked)) + ' summary_height_available=' + IntToStr(Avail) + ' needed=' + IntToStr(Needed) +
    ' overflow=' + IntToStr(Ord(Needed > Avail)));
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Top: Integer;
  Cap: String;
begin
  WizardForm.NextButton.Caption := SetupMessage(msgButtonNext);
  WizardForm.BackButton.Visible := True;
  { P1 showed a Back button on the Installing page while the install ran (design 18.5). That page is
    Inno's own wpInstalling here and, from ssPostInstall on, our progress page: RunInstallFlow and
    RemoteAttempt hide the button right after they Show it, because Show does not come through
    here. Nothing
    after the install step can be gone back to either, so the Remote access and Finished pages have
    none, and the Welcome page has nothing to go back to (the line above had been showing a Back
    button on the Welcome and Finished pages too). }
  if (CurPageID = wpWelcome) or (CurPageID = wpInstalling) or (CurPageID = PageRemote.ID) or (CurPageID = wpFinished) then
    WizardForm.BackButton.Visible := False;
  if CurPageID = wpWelcome then
  begin
    if ResumeSwitch or StResumeAfterWsl then
      WizardForm.WelcomeLabel2.Caption := CustomMessage('welcomeResume')
    else if InstallMode = ModeUpdate then
    begin
      if StLinuxVersion <> '' then
        WizardForm.WelcomeLabel2.Caption := FmtMessage(CustomMessage('welcomeUpdate'), [StLinuxVersion, '{#Version}'])
      else
        WizardForm.WelcomeLabel2.Caption := FmtMessage(CustomMessage('welcomeUpdateNoVersion'), ['{#Version}']);
    end
    else if InstallMode = ModeRepair then
      WizardForm.WelcomeLabel2.Caption := CustomMessage('welcomeRepair')
    else if InstallMode = ModeReinstall then
      WizardForm.WelcomeLabel2.Caption := FmtMessage(CustomMessage('welcomeReinstall'), ['{#Version}'])
    else if InstallMode = ModeFinish then
      WizardForm.WelcomeLabel2.Caption := CustomMessage('welcomeFinish')
    else
      WizardForm.WelcomeLabel2.Caption := FmtMessage(CustomMessage('welcomeFresh'), ['{#Version}']);
  end
  else if CurPageID = PageWsl.ID then
  begin
    if WslPageStage = 0 then
      WizardForm.NextButton.Caption := CustomMessage('wslContinue')
    else
    begin
      WizardForm.NextButton.Caption := CustomMessage('continueButton');
      WizardForm.BackButton.Visible := False;
    end;
  end
  else if CurPageID = PageFolder.ID then
  begin
    if FolderLocked then
    begin
      { Every non-fresh mode once root 1 exists: the folder is the one Cognita already uses (design
        18.2, 19.2 item 7). The helper refuses another one anyway. }
      PageFolder.Values[0] := StRoot1;
      PageFolder.Edits[0].ReadOnly := True;
      PageFolder.Buttons[0].Enabled := False;
      FolderNote.Caption := CustomMessage('addFolderLater');
    end
    else
    begin
      { Fresh (and a finish that has no root 1 yet): editable. A leftover record of a folder is offered. }
      if (PageFolder.Values[0] = '') and (StRoot1 <> '') then
        PageFolder.Values[0] := StRoot1;
      PageFolder.Edits[0].ReadOnly := False;
      PageFolder.Buttons[0].Enabled := True;
      FolderNote.Caption := CustomMessage('folderOneDrive');
    end;
  end
  else if CurPageID = PageAdmin.ID then
  begin
    if UserLocked then
    begin
      { Design 19.2 item 14: read-only over an installed Cognita; "(unchanged)" when the helper never
        recorded a name (AdminPageNext then passes and remembers no name). }
      if StAdminUser <> '' then
        PageAdmin.Values[0] := StAdminUser
      else
        PageAdmin.Values[0] := '(' + CustomMessage('unchanged') + ')';
      PageAdmin.Edits[0].ReadOnly := True;
    end
    else
      PageAdmin.Edits[0].ReadOnly := False;
    if not RunsOverInstalled then
    begin
      PageAdmin.SubCaptionLabel.Caption := CustomMessage('adminCreateGuidance');
      PageAdmin.PromptLabels[1].Caption := CustomMessage('newPassword');
    end
    else
    begin
      { Repair, update and reinstall use the existing password only to check Cognita afterwards. }
      PageAdmin.PromptLabels[1].Caption := CustomMessage('currentPassword');
      if InstallMode = ModeUpdate then
      begin
        if StLinuxVersion <> '' then
          PageAdmin.SubCaptionLabel.Caption := FmtMessage(CustomMessage('upgradePasswordCaption'), [StLinuxVersion, '{#Version}'])
        else
          PageAdmin.SubCaptionLabel.Caption := FmtMessage(CustomMessage('upgradePasswordCaption'), ['an earlier version', '{#Version}']);
      end
      else
        PageAdmin.SubCaptionLabel.Caption := CustomMessage('verifyPasswordCaption');
    end;
    PageAdmin.PromptLabels[2].Visible := not RunsOverInstalled;
    PageAdmin.Edits[2].Visible := not RunsOverInstalled;
  end
  else if CurPageID = PageAccel.ID then
    ShowAccelPage
  else if CurPageID = PageReady.ID then
  begin
    WizardForm.NextButton.Caption := SetupMessage(msgButtonInstall);
    ShowReadyPage;
    { Advanced is the page's only control, so Inno focuses it and Enter opened Advanced instead of
      installing. Install is what Enter means here. }
    WizardForm.ActiveControl := WizardForm.NextButton;
  end
  else if CurPageID = PageRemote.ID then
    WizardForm.NextButton.Caption := SetupMessage(msgButtonNext)
  else if CurPageID = wpFinished then
  begin
    { The line at the top of this procedure sets every page's button to "Next"; the last page's is "Finish". }
    WizardForm.NextButton.Caption := SetupMessage(msgButtonFinish);
    DiagButton.Visible := False;
    LogLink.Visible := False;
    FinAdminPre.Visible := False;
    FinAdminLink.Visible := False;
    FinAdminPost.Visible := False;
    FinMcpPre.Visible := False;
    FinMcpLink.Visible := False;
    FinPubPre.Visible := False;
    FinPubLink.Visible := False;
    FinConnectLabel.Visible := False;
    FinMemo.Visible := False;
    if InstallOk then
      LayoutFinishedSuccess
    else if InstallRestart then
    begin
      { Design 19.11 R1: NeedRestart returned True, so Inno shows its own "Yes, restart now / No, I will
        restart later" radio buttons here and restarts Windows after Setup has exited. Our text replaces
        Inno's label; the radio buttons are kept visible and placed under it explicitly, so a label of a
        different height than Inno's cannot cover them. }
      WizardForm.FinishedHeadingLabel.Caption := CustomMessage('restartHeading');
      if InstallResumeSaved then
        WizardForm.FinishedLabel.Caption := CustomMessage('restartResume')
      else
        WizardForm.FinishedLabel.Caption := CustomMessage('restartNoResume');
      WizardForm.FinishedLabel.AdjustHeight;
      WizardForm.YesRadio.Left := WizardForm.FinishedLabel.Left;
      WizardForm.YesRadio.Width := WizardForm.FinishedLabel.Width;
      WizardForm.YesRadio.Top := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(16);
      WizardForm.NoRadio.Left := WizardForm.YesRadio.Left;
      WizardForm.NoRadio.Width := WizardForm.YesRadio.Width;
      WizardForm.NoRadio.Top := WizardForm.YesRadio.Top + WizardForm.YesRadio.Height + ScaleY(6);
      WizardForm.YesRadio.Visible := True;
      WizardForm.NoRadio.Visible := True;
      Log('finished page: restart layout resume_saved=' + IntToStr(Ord(InstallResumeSaved)) + ' label_h=' +
        IntToStr(WizardForm.FinishedLabel.Height) + ' yes_top=' + IntToStr(WizardForm.YesRadio.Top) +
        ' no_top=' + IntToStr(WizardForm.NoRadio.Top));
    end
    else if InstallAttempted then
    begin
      { Design 19.4 item 10: the same layout as the success page. A short label at the top (what
        failed, and that the earlier Cognita is untouched), the failure text in a scrolling read-only
        memo (a long failure used to push the buttons off the page), and a fixed row at the bottom
        (Save diagnostics, Open the log folder). The heading says which run it was. }
      WizardForm.FinishedHeadingLabel.Caption := FailHeading(InstallMode);
      Cap := FmtMessage(CustomMessage('failedStep'), [FailStageDisplay]);
      if StillThereText(InstallMode) <> '' then
        Cap := Cap + #13#10#13#10 + StillThereText(InstallMode);
      WizardForm.FinishedLabel.Caption := Cap;
      WizardForm.FinishedLabel.AdjustHeight;
      Top := WizardForm.FinishedPage.ClientHeight - DiagButton.Height - ScaleY(2);
      DiagButton.Left := WizardForm.FinishedLabel.Left;
      DiagButton.Top := Top;
      if FinDiagZip <> '' then
        DiagButton.Caption := CustomMessage('showFile')
      else
        DiagButton.Caption := CustomMessage('saveDiagnostics');
      DiagButton.OnClick := @ShowDiagFileClick;
      DiagButton.Visible := True;
      LogLink.Left := DiagButton.Left + DiagButton.Width + ScaleX(16);
      LogLink.Top := Top + ScaleY(4);
      LogLink.Visible := True;
      ReportLink.Left := LogLink.Left + LogLink.Width + ScaleX(24);
      ReportLink.Top := LogLink.Top;
      ReportLink.Visible := True;
      FinMemo.Left := WizardForm.FinishedLabel.Left;
      FinMemo.Top := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(10);
      FinMemo.Width := WizardForm.FinishedLabel.Width;
      FinMemo.Height := Top - ScaleY(8) - FinMemo.Top;
      if FinMemo.Height < ScaleY(48) then
        FinMemo.Height := ScaleY(48);
      { (A source line may not START with #13#10: ISPP reads a leading # as a preprocessor directive.) }
      FinMemo.Text := FailText + CustomMessage('logFolderLabel') + '  ' + LogsDir + #13#10 +
        CustomMessage('setupLogLabel') + '  ' + ExpandConstant('{log}') + #13#10#13#10 +
        CustomMessage('failureContinue') + #13#10#13#10 + DiagnosticsSentence(FinDiagZip);
      FinMemo.Visible := True;
      Log('finished page: failure layout mode=' + IntToStr(InstallMode) + ' heading=[' + FailHeading(InstallMode) +
        '] still_there=' + IntToStr(Ord(StillThereText(InstallMode) <> '')) + ' memo_h=' + IntToStr(FinMemo.Height) +
        ' fail_chars=' + IntToStr(Length(FailText)) + ' stage=[' + FailStage + ']');
    end;
  end;
end;

function OpenAdminOffered: Boolean;
begin
  Result := InstallOk;
end;

{ ---------------------------------------------------------------------------------------------
  Next button: each page runs its step (section 4.2)
  --------------------------------------------------------------------------------------------- }

function CheckPreflight: Boolean;
begin
  Result := RunHelperBusy(CustomMessage('busyCheckingThisPc'), 'check', '--phase preflight');
  if not Result then
  begin
    ShowFailuresWithHelp(CustomMessage('failurePreflight'));
    Exit;
  end;
  WslState := Lowercase(ResultValue(LastResult, 'wsl'));
  Log('preflight: wsl=' + WslState);
  { Design 22.2 and 22.9: what the helper saw of NVIDIA and of .wslconfig. Not check results: no warning, no failure. }
  NvState := Lowercase(Trim(ResultValue(LastResult, 'nvidia')));
  NvName := ResultValue(LastResult, 'nvidia_name');
  NvDriver := ResultValue(LastResult, 'nvidia_driver');
  WslReclaim := Lowercase(Trim(ResultValue(LastResult, 'wsl_reclaim')));
  { The Acceleration page's default, and the pick: set ONCE (a second preflight, after turning WSL on or after
    Back, keeps what the user picked), unless the card or build no longer offers NVIDIA. }
  AccelDefaultPick := AccelDefault(AccelMode, InstallMode, StAcceleration);
  if not AccelInited then
  begin
    ChosenAccel := AccelDefaultPick;
    AccelInited := True;
  end
  else if (ChosenAccel = 'nvidia') and (AccelMode <> AccelOffer) then
    ChosenAccel := 'cpu';
  Log('preflight: nvidia=' + NvState + ' name=[' + NvName + '] driver=[' + NvDriver + '] wsl_reclaim=' + WslReclaim +
    ' | acceleration: mode=' + IntToStr(InstallMode) + ' page_mode=' + IntToStr(AccelMode) + ' build_bytes=' +
    IntToStr(NvidiaBuildBytes) + ' installed_profile=[' + StAcceleration + '] default=[' + AccelDefaultPick +
    '] chosen=[' + ChosenAccel + '] page_shown=' + IntToStr(Ord(AccelMode <> AccelHidden)));
  if (WarnText <> '') and (not PreflightWarned) then
  begin
    PreflightWarned := True;
    MsgBox(CustomMessage('pleaseNote') + #13#10#13#10 + WarnText, mbInformation, MB_OK);
  end;
end;

{ The WSL page: turn WSL on, then ask about the restart (section 5.4, section 4.3) }
function WslPageNext: Boolean;
begin
  Result := False;
  if WslPageStage = 0 then
  begin
    Log('wsl page: running wsl-install');
    if RunHelperBusy(CustomMessage('busyTurningOnWsl'), 'wsl-install', '') then
    begin
      { Turned on and no restart needed: check again and carry on. }
      if CheckPreflight then
      begin
        if (WslState = 'missing') or (WslState = 'old') then
          MsgBox(CustomMessage('wslUnavailable'), mbError, MB_OK)
        else
          Result := True;
      end;
      Exit;
    end;
    if LastStatus = 'restart-required' then
    begin
      WslPageStage := 1;
      { Design 19.1 item 1: the helper's restart never force-closes programs (no /f), so a program with
        unsaved work would hold the restart up; the user is told to save first. }
      WslLabel.Caption := CustomMessage('wslRestartNeeded');
      WslRestartNow.Visible := True;
      WslRestartLater.Visible := True;
      WizardForm.NextButton.Caption := CustomMessage('continueButton');
      WizardForm.BackButton.Visible := False;
      Log('wsl page: restart required; asking Restart now or Later');
      Exit;
    end;
    ShowFailuresWithHelp(CustomMessage('failureWsl'));
    Exit;
  end;
  { Stage 1: Restart now or Later. Both write the RunOnce value and the resume record through
    the helper; Setup then ends without the "Exit Setup?" question (proven in spike S1). }
  if WslRestartNow.Checked then
  begin
    Log('wsl page: restart now chosen; setup pid=' + IntToStr(Integer(GetCurrentProcessId)));
    { Design 19.11 R1: the helper starts a detached waiter that restarts Windows once THIS process has
      exited (a running Setup would answer Windows' "may we shut down?" with No). Setup closes right below. }
    if not RunHelperBusy(CustomMessage('busyRestartingWindows'), 'restart-for-wsl', '--setup-exe ' + QuoteArg(ExpandConstant('{srcexe}')) +
      ' --now --after-pid ' + IntToStr(Integer(GetCurrentProcessId))) then
    begin
      ShowFailuresWithHelp(CustomMessage('failureRestart'));
      Exit;
    end;
  end
  else
  begin
    Log('wsl page: later chosen');
    if not RunHelperBusy(CustomMessage('busySavingPlace'), 'restart-for-wsl', '--setup-exe ' + QuoteArg(ExpandConstant('{srcexe}'))) then
    begin
      ShowFailuresWithHelp(CustomMessage('failureResume'));
      Exit;
    end;
    MsgBox(CustomMessage('restartContinue'), mbInformation, MB_OK);
  end;
  CloseQuietly := True;
  WizardForm.Close;
end;

function FolderPageNext: Boolean;
var
  Path, Reason, DisplayReason: String;
begin
  Result := False;
  FolderReason.Caption := '';
  if FolderLocked then
  begin
    { Every non-fresh mode keeps the folder Cognita already uses; the helper checks it again on install. }
    Log('folder page: locked to ' + StRoot1 + ' (mode ' + IntToStr(InstallMode) + '); not validated again');
    Result := True;
    Exit;
  end;
  Path := Trim(PageFolder.Values[0]);
  if Path = '' then
  begin
    MsgBox(CustomMessage('folderRequired'), mbError, MB_OK);
    Exit;
  end;
  if HasBadArgChar(Path) then
  begin
    MsgBox(CustomMessage('quoteFolderError'), mbError, MB_OK);
    Exit;
  end;
  Path := RemoveBackslash(Path);
  PageFolder.Values[0] := Path;
  { An invalid folder is a normal answer, result=ok;ok=0;reason=<text> (design 18.5); `failed` is only
    an internal error. }
  if not RunHelperBusy(CustomMessage('busyCheckingFolder'), 'roots', '--validate ' + QuoteArg(Path)) then
  begin
    Log('folder page: roots --validate failed internally for ' + Path);
    { Design 19 (18.5 extended): an internal error here offers Save diagnostics like the other failures. }
    ShowFailuresWithHelp(CustomMessage('failureFolder'));
    Exit;
  end;
  if ResultValue(LastResult, 'ok') = '0' then
  begin
    Reason := ResultValue(LastResult, 'reason');
    if Reason = '' then
      Reason := CustomMessage('dataFolderUnsupported');
    Log('folder page: ' + Path + ' refused: ' + Reason);
    DisplayReason := ResultValue(LastResult, 'reason_display');
    if DisplayReason = '' then
      DisplayReason := Reason;
    FolderReason.Caption := CustomMessage('folderUnavailable') + ': ' + DisplayReason;
    MsgBox(CustomMessage('folderUnavailable') + #13#10#13#10 + DisplayReason, mbError, MB_OK);
    Exit;
  end;
  Log('folder page: ' + Path + ' accepted');
  Result := True;
end;

function AdminPageNext: Boolean;
var
  Pw, Pw2: String;
begin
  Result := False;
  Pw := PageAdmin.Values[1];
  Pw2 := PageAdmin.Values[2];
  if UserLocked then
  begin
    { Design 19.2 item 14: the box is read-only, and its "(unchanged)" text is not a name. AdminUser is
      only what the helper recorded ('' when it never did), so Ready and Finished show a name only when
      it is known; --admin-user is not passed in these modes (ArgsInstall). }
    AdminUser := StAdminUser;
    Log('admin page: user locked; recorded user=[' + AdminUser + ']');
  end
  else
  begin
    AdminUser := Trim(PageAdmin.Values[0]);
    if AdminUser = '' then
    begin
      MsgBox(CustomMessage('typeUsername'), mbError, MB_OK);
      Exit;
    end;
    if HasBadArgChar(AdminUser) then
    begin
      MsgBox(CustomMessage('badUsername'), mbError, MB_OK);
      Exit;
    end;
  end;
  if Pw = '' then
  begin
    MsgBox(CustomMessage('typePassword'), mbError, MB_OK);
    Exit;
  end;
  if (not RunsOverInstalled) and (Pw <> Pw2) then
  begin
    MsgBox(CustomMessage('passwordMismatch'), mbError, MB_OK);
    Exit;
  end;
  AdminPassword := Pw;
  Log('admin page: user=[' + AdminUser + '] locked=' + IntToStr(Ord(UserLocked)) + ' password accepted (the password itself is never logged)');
  Result := True;
end;

{ The Acceleration page's Next (design 22.7 and 22.12 item 1(b)): records the pick, and whether it differs from
  the default the page opened with (AccelTouched). A GPU radio that is not enabled can never be the pick. }
function AccelPageNext: Boolean;
begin
  if AccelUseGpu.Enabled and AccelUseGpu.Checked then
    ChosenAccel := 'nvidia'
  else
    ChosenAccel := 'cpu';
  AccelTouched := ChosenAccel <> AccelDefaultPick;
  Log('accel page: mode=' + IntToStr(AccelMode) + ' chosen=' + ChosenAccel + ' default=' + AccelDefaultPick +
    ' touched=' + IntToStr(Ord(AccelTouched)) + ' installed_profile=[' + StAcceleration + '] argument=[' +
    Trim(AccelArg(AccelMode, InstallMode, ChosenAccel, AccelTouched, StAcceleration)) + ']');
  Result := True;
end;

{ The final check. The helper's fixes say "choose other ports under Advanced" and "choose another data
  location under Advanced", but Advanced cannot change a value the mode locks (an update locks the
  ports; every non-fresh mode locks the data location), so the text is rewritten for those cases
  (design 19.4 item 18; RewriteAdvancedHints, tested in the harness). The failure dialog is the one with
  Save diagnostics (design 18.5, extended by 19 to this check). }
function ReadyPageNext: Boolean;
var
  Drive, Fixed, Heading: String;
begin
  { Design 22.9: the check box state is logged when Install is pressed, with what the other new choices were. }
  LogAccelDecision('ready');
  Result := RunHelperBusy(CustomMessage('busyCheckingPorts'), 'check', ArgsCheckFinal);
  if not Result then
  begin
    Drive := Copy(ChosenDataDir, 1, 2);
    Fixed := RewriteAdvancedHints(FailText, InstallMode, DataLocationLocked, Drive);
    if Fixed <> FailText then
      Log('ready check: rewrote the "under Advanced" fixes for mode ' + IntToStr(InstallMode) +
        ' (data_locked=' + IntToStr(Ord(DataLocationLocked)) + ' drive=' + Drive + ')');
    FailText := Fixed;
    if InstallMode = ModeUpdate then
      Heading := CustomMessage('failureUpdateChoices')
    else
      Heading := CustomMessage('failureInstallChoices');
    ShowFailuresWithHelp(Heading);
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpWelcome then
    Result := CheckPreflight
  else if CurPageID = PageWsl.ID then
    Result := WslPageNext
  else if CurPageID = PageFolder.ID then
    Result := FolderPageNext
  else if CurPageID = PageAdmin.ID then
    Result := AdminPageNext
  else if CurPageID = PageAccel.ID then
    Result := AccelPageNext
  else if CurPageID = PageReady.ID then
    Result := ReadyPageNext
  else if CurPageID = PageRemote.ID then
  begin
    if RemoteStage <> 0 then
      Log('remote access: carrying on to the Finished page; stage=' + IntToStr(RemoteStage))
    else if RemoteSetUp.Checked then
    begin
      RunRemoteAccess;
      { A failure with a link keeps the user here to open it and press Retry (design 18.4). }
      if RemoteStage = 1 then
        Result := False;
    end
    else
      Log('remote access: skipped by the user');
  end;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  if CloseQuietly then
  begin
    Confirm := False;
    Log('closing Setup quietly from a page (restart flow)');
  end;
end;

{ ---------------------------------------------------------------------------------------------
  The Installing page (section 4.2 step 6, sections 7.1 to 7.3)
  --------------------------------------------------------------------------------------------- }

procedure ExtractPayload(const TempName: String; const Title: String);
begin
  ProgressPage.SetText(Title, CustomMessage('progressUnpacking'));
  ProgressPage.SetProgress(0, 0);
  Log('payload: extracting ' + TempName + ' to {tmp}');
  ExtractTemporaryFile(TempName);
end;

procedure RunInstallFlow;
var
  PipeB, ImageFile, SrcFile, Verb, Args: String;
  Status: String;
begin
  InstallAttempted := True;
  InstallOk := False;
  InstallRestart := False;
  InstallResumeSaved := False;
  ImageFile := '';
  SrcFile := '';
  FailCount := 0;
  FailText := '';
  FailStage := 'Starting';
  FailStageDisplay := FailureStageDisplayFor(FailStage);
  InstallWarnText := '';
  InstallSignInWarned := False;
  ResultWorkspace := '';
  ResultAccel := '';
  { Design 21.4: a new run starts with the button unpressed and enabled, and no request flag left over
    from an earlier run (the helper removes a stale one too). }
  SkipPressed := False;
  FinProofSkipped := False;
  ProgressSkipButton.Enabled := True;
  DeleteSkipRequest('start of the install run');
  { Design 19.4 item 23: the page's caption names the run ("Updating Cognita", "Repairing Cognita" ...),
    not always "Installing". The text under it says the same; the remote-access step sets its own. }
  ProgressPage.Caption := ProgressCaption(InstallMode);
  ProgressPage.Description := CustomMessage('installDescription');
  Log('install flow: progress caption=[' + ProgressPage.Caption + ']');
  ProgressPage.SetText(CustomMessage('progressPreparing'), '');
  ProgressLinkEdit.Visible := False;
  ProgressCopyButton.Visible := False;
  ProgressWaitLabel.Visible := False;
  ProgressSkipButton.Visible := False;
  ProgressPage.Show;
  { The Installing page: a Back button here lets the user walk away from a running install (P1, design 18.5). }
  WizardForm.BackButton.Visible := False;
  RunMode := 2;
  RunStartTick := GetTickCount;
  try
    try
      { The helper now runs from the app folder, where Inno has just installed it. }
      HelperFolder := ExpandConstant('{app}');
      if InstallMode = ModeUpdate then
      begin
        { Update: the source tree only; the helper swaps it in and runs cognita update (section 7.3). }
        ExtractPayload('cognita-src.tar.gz', CustomMessage('progressPreparingUpdate'));
        SrcFile := ExpandConstant('{tmp}\cognita-src.tar.gz');
        Verb := 'update';
      end
      else
      begin
        { Every install run (fresh, finish, repair, reinstall after an uninstall) carries the source
          tarball, so the helper can swap an older tree in the distro before cognita install. Fresh
          and finish also unpack the image: the helper may need to import it again (section 5.7 step
          4) even when an unfinished distro exists. Repair and reinstall never need the image: the
          distro is there. }
        Verb := 'install';
        if (InstallMode = ModeFresh) or (InstallMode = ModeFinish) then
        begin
          ExtractPayload('cognita-wsl.tar.gz', CustomMessage('progressPreparingLinux'));
          ImageFile := ExpandConstant('{tmp}\cognita-wsl.tar.gz');
        end
        else
          Log('install: mode ' + IntToStr(InstallMode) + ' runs over the existing Cognita distro; the Linux image is not needed');
        ExtractPayload('cognita-src.tar.gz', CustomMessage('progressPreparingFiles'));
        SrcFile := ExpandConstant('{tmp}\cognita-src.tar.gz');
      end;
      Log('install flow: mode=' + IntToStr(InstallMode) + ' verb=' + Verb + ' image=' + IntToStr(Ord(ImageFile <> '')) +
        ' src=' + IntToStr(Ord(SrcFile <> '')) + ' admin_user_passed=' + IntToStr(Ord((Verb = 'install') and (not RunsOverInstalled))));
      FailStage := 'Handing the Admin password to the installer';
      FailStageDisplay := FailureStageDisplayFor(FailStage);
      if not HandOverPassword(PipeB) then
      begin
        FailCount := 1;
        { Design 19.5 item 15: the broker gets 60 seconds to open its pipe; if it never did, the helper
          itself did not start (a blocked or very slow PowerShell), which is what the text says. }
        FailMessage := CustomMessage('failurePasswordHandoff');
        FailFix := FmtMessage(CustomMessage('failurePasswordFix'), ['{#SupportUrl}']);
        FailText := FailMessage + #13#10 + CustomMessage('whatToDo') + ' ' + FailFix + #13#10#13#10;
        Exit;
      end;
      { Design 22.8: the acceleration and WSL-memory decisions, with the values they came from, right before
        the arguments are built. (The helper start line below logs the arguments themselves.) }
      LogAccelDecision(Verb);
      if Verb = 'update' then
        Args := ArgsUpdate(PipeB, SrcFile)
      else
        Args := ArgsInstall(PipeB, ImageFile, SrcFile);
      ProgressPage.SetText(ProgressCaption(InstallMode), '');
      RunStartTick := GetTickCount;
      RunHelper(Verb, Args);
      { Design 19.4 item 2: remote access runs its own helper verb next, and every RunHelper resets
        WarnText, so the warnings of THIS run are kept for the Finished page right here. }
      InstallWarnText := WarnText;
      InstallSignInWarned := SignInWarned;
      Log('install flow: kept ' + IntToStr(Length(InstallWarnText)) + ' characters of warnings for the Finished page; sign_in_warned=' +
        IntToStr(Ord(InstallSignInWarned)));
      Status := LastStatus;
      if Status = 'ok' then
      begin
        InstallOk := True;
        ResultVersion := ResultValue(LastResult, 'version');
        PublicUrl := ResultValue(LastResult, 'public_url');
        { Design 19.4 item 2: the Finished page shows the workspace value the result (`status`) reported;
          the Ready choice (ChosenWorkspaceOn) is left as it was. }
        ResultWorkspace := Lowercase(ResultValue(LastResult, 'workspace'));
        Log('install flow: result workspace=[' + ResultWorkspace + '] chosen=' + OnOff(ChosenWorkspaceOn));
        { Design 22.1, 22.7 and 22.12 item 11: the Finished page's Acceleration line comes only from the result
          (`status --json`'s acceleration), never from the choice. An unknown word counts as "did not say". }
        ResultAccel := AccelKnown(ResultValue(LastResult, 'acceleration'));
        Log('install flow: result acceleration=[' + ResultAccel + '] (reported [' + ResultValue(LastResult, 'acceleration') +
          ']) requested=[' + Trim(AccelArg(AccelMode, InstallMode, ChosenAccel, AccelTouched, StAcceleration)) + ']');
        { Design 21.4: the self-tests were skipped; the Finished page says so (and how to run them). }
        FinProofSkipped := ResultValue(LastResult, 'proof') = 'skipped';
        Log('install flow: result proof=[' + ResultValue(LastResult, 'proof') + '] skip_pressed=' + IntToStr(Ord(SkipPressed)) +
          ' finished_page_note=' + IntToStr(Ord(FinProofSkipped)));
      end
      else if Status = 'restart-required' then
        InstallRestart := True;
      Log('install flow: verb=' + Verb + ' status=' + Status + ' version=' + ResultVersion + ' public_url_set=' +
        IntToStr(Ord(PublicUrl <> '')));
    except
      Log('install flow: exception: ' + GetExceptionMessage);
      FailCount := 1;
      FailMessage := FmtMessage(CustomMessage('failureUnexpected'), [GetExceptionMessage]);
      FailFix := FmtMessage(CustomMessage('failureUnexpectedFix'), ['{#SupportUrl}']);
      FailText := FailMessage + #13#10 + CustomMessage('whatToDo') + ' ' + FailFix + #13#10#13#10;
    end;
  finally
    RunMode := 0;
    ProgressSkipButton.Visible := False;
    ProgressPage.Hide;
    { Design 21.4: the request flag belongs to this run; it does not outlive it. }
    DeleteSkipRequest('end of the install run');
    { The big files are removed as soon as a step is done with them (section 4.1). }
    DeleteFile(ExpandConstant('{tmp}\cognita-wsl.tar.gz'));
    DeleteFile(ExpandConstant('{tmp}\cognita-src.tar.gz'));
  end;
  if InstallRestart then
  begin
    { Design 19.11 R1: Setup NEVER restarts Windows from inside itself here (a running Setup answers
      Windows' "may we shut down?" with No, and the restart stalls on "Cognita Setup is preventing
      restart"). The helper only records RunOnce and the resume marker (no --now); NeedRestart below
      returns True, so Inno's own Finished page offers "Restart now / later" and Inno restarts after
      Setup has exited. The Finished page (CurPageChanged) says to save work first. }
    Log('install flow: the helper needs a restart (exit 3010); recording where Setup stopped, no restart from here');
    InstallResumeSaved := RunHelperBusy(CustomMessage('busySavingPlace'), 'restart-for-wsl', '--setup-exe ' + QuoteArg(ExpandConstant('{srcexe}')));
    Log('install flow: resume recorded=' + IntToStr(Ord(InstallResumeSaved)) + '; NeedRestart will return True');
    { Design 19.11 R3: a failed record step is not silent. }
    if not InstallResumeSaved then
      ShowFailuresWithHelp(CustomMessage('failureResume'));
  end
  else if not InstallOk then
    { The failure Finished page names this file (and its button shows it). }
    FinDiagZip := AutoSaveDiagnostics('the install flow failed');
end;

{ Inno calls this after the install steps (ssPostInstall included) finish. True only for the install-flow
  case that needs a restart (design 19.11 R1); Inno's Finished page then offers Restart now / later. }
function NeedRestart: Boolean;
begin
  Result := InstallRestart;
  Log('NeedRestart: ' + IntToStr(Ord(Result)) + ' (resume recorded=' + IntToStr(Ord(InstallResumeSaved)) + ')');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RunInstallFlow;
end;

{ ---------------------------------------------------------------------------------------------
  Uninstall (sections 4.5 and 7.5)
  --------------------------------------------------------------------------------------------- }

function AskUninstallChoice(var DeleteData: Boolean): Boolean;
var
  Form: TSetupForm;
  Head, KeepNote, DeleteNote: TNewStaticText;
  RadioPanel: TPanel;
  KeepRadio, DeleteRadio: TNewRadioButton;
  OkButton, CancelButton: TNewButton;
  Size: String;
begin
  Result := False;
  DeleteData := False;
  Form := CreateCustomForm(ScaleX(460), ScaleY(230), False, False);
  try
    Form.Caption := CustomMessage('removeTitle');
    Head := TNewStaticText.Create(Form);
    Head.Parent := Form;
    Head.Left := ScaleX(14);
    Head.Top := ScaleY(14);
    Head.Width := ScaleX(432);
    Head.AutoSize := False;
    Head.WordWrap := True;
    Head.Height := ScaleY(36);
    Head.Caption := CustomMessage('removeHead');
    { Design 19.11 R2: the two radio buttons (and their notes) sit in their OWN borderless panel. Directly
      on the form they were siblings of Continue and Cancel, so the arrow keys walked from a radio button
      to the buttons (and selected "Also delete" through the OnEnter handler). In a panel the arrows move
      only between the two radios. The panel is not a tab stop; Tab reaches the checked radio. It spans
      from the old radio top (58) to the bottom of the delete note (188), above the buttons. }
    RadioPanel := TPanel.Create(Form);
    RadioPanel.Parent := Form;
    RadioPanel.Left := ScaleX(14);
    RadioPanel.Top := ScaleY(58);
    RadioPanel.Width := ScaleX(432);
    RadioPanel.Height := ScaleY(130);
    RadioPanel.BevelOuter := bvNone;
    RadioPanel.Caption := '';
    KeepRadio := TNewRadioButton.Create(Form);
    KeepRadio.Parent := RadioPanel;
    KeepRadio.Left := 0;
    KeepRadio.Top := 0;
    KeepRadio.Width := ScaleX(432);
    KeepRadio.Caption := CustomMessage('keepData');
    KeepRadio.Checked := True;
    KeepRadio.OnEnter := @RadioEnter;
    KeepNote := TNewStaticText.Create(Form);
    KeepNote.Parent := RadioPanel;
    KeepNote.Left := ScaleX(20);
    KeepNote.Top := ScaleY(22);
    KeepNote.Width := ScaleX(412);
    KeepNote.AutoSize := False;
    KeepNote.WordWrap := True;
    KeepNote.Height := ScaleY(32);
    KeepNote.Caption := CustomMessage('keepDataNote');
    DeleteRadio := TNewRadioButton.Create(Form);
    DeleteRadio.Parent := RadioPanel;
    DeleteRadio.Left := 0;
    DeleteRadio.Top := ScaleY(60);
    DeleteRadio.Width := ScaleX(432);
    Size := '';
    if StVhdBytes <> '' then
      Size := FormatBytes(StrToInt64Def(StVhdBytes, 0));
    { A radio button's caption does not wrap, so the size and the path go in the note below it,
      which does; with them in the caption the path ran off the dialog (P2, 2026-09-29). }
    DeleteRadio.Caption := CustomMessage('deleteData');
    DeleteRadio.OnEnter := @RadioEnter;
    DeleteNote := TNewStaticText.Create(Form);
    DeleteNote.Parent := RadioPanel;
    DeleteNote.Left := ScaleX(20);
    DeleteNote.Top := ScaleY(82);
    DeleteNote.Width := ScaleX(412);
    DeleteNote.AutoSize := False;
    DeleteNote.WordWrap := True;
    DeleteNote.Height := ScaleY(48);
    if StVhdDir <> '' then
    begin
      if Size <> '' then
        DeleteNote.Caption := FmtMessage(CustomMessage('deleteDataNoteSize'), [Size, StVhdDir])
      else
        DeleteNote.Caption := FmtMessage(CustomMessage('deleteDataNotePath'), [StVhdDir]);
    end
    else
      DeleteNote.Caption := CustomMessage('deleteDataNoteNoPath');
    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := CustomMessage('continueButton');
    OkButton.Left := Form.ClientWidth - ScaleX(80 + 6 + 80 + 14);
    OkButton.Top := Form.ClientHeight - ScaleY(25 + 14);
    OkButton.Width := ScaleX(80);
    OkButton.Height := ScaleY(25);
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := CustomMessage('cancelButton');
    CancelButton.Left := Form.ClientWidth - ScaleX(80 + 14);
    CancelButton.Top := Form.ClientHeight - ScaleY(25 + 14);
    CancelButton.Width := ScaleX(80);
    CancelButton.Height := ScaleY(25);
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    { Design 19.5 item 12: Enter means Continue. (The arrow keys move and select between the two radio
      buttons through their OnEnter handlers; Tab reaches them from here.) }
    Form.ActiveControl := OkButton;
    Form.FlipAndCenterIfNeeded(True, nil, False);
    if Form.ShowModal = mrOk then
    begin
      Result := True;
      DeleteData := DeleteRadio.Checked;
    end;
    Log('uninstall: first form closed ok=' + IntToStr(Ord(Result)) + ' delete_data=' + IntToStr(Ord(DeleteData)) +
      ' (radios in their own panel, design 19.11 R2)');
  finally
    Form.Free;
  end;
end;

{ The second form for "delete": lists exactly what goes and needs the word DELETE typed. }
var
  DeleteConfirmEdit: TNewEdit;
  DeleteConfirmOk: TNewButton;

procedure DeleteConfirmChange(Sender: TObject);
begin
  DeleteConfirmOk.Enabled := DeleteConfirmEdit.Text = 'DELETE';
end;

function ConfirmDelete: Boolean;
var
  Form: TSetupForm;
  Head: TNewStaticText;
  CancelButton: TNewButton;
  Where: String;
begin
  Result := False;
  Form := CreateCustomForm(ScaleX(480), ScaleY(300), False, False);
  try
    Form.Caption := CustomMessage('deleteConfirmTitle');
    Where := StVhdDir;
    if Where = '' then
      Where := ExpandConstant('{localappdata}\Cognita\wsl');
    Head := TNewStaticText.Create(Form);
    Head.Parent := Form;
    Head.Left := ScaleX(14);
    Head.Top := ScaleY(14);
    Head.Width := ScaleX(452);
    Head.AutoSize := False;
    Head.WordWrap := True;
    Head.Height := ScaleY(190);
    Head.Caption := CustomMessage('deleteConfirmStart') + #13#10#13#10 +
      FmtMessage(CustomMessage('deleteConfirmLinux'), [Where]) + #13#10 +
      FmtMessage(CustomMessage('deleteConfirmWindows'), [ExpandConstant('{localappdata}\Cognita')]) + #13#10#13#10 +
      FmtMessage(CustomMessage('deleteConfirmDocuments'), [ExpandConstant('{tmp}')]) + #13#10#13#10 +
      CustomMessage('deleteConfirmInstruction');
    DeleteConfirmEdit := TNewEdit.Create(Form);
    DeleteConfirmEdit.Parent := Form;
    DeleteConfirmEdit.Left := ScaleX(14);
    DeleteConfirmEdit.Top := ScaleY(212);
    DeleteConfirmEdit.Width := ScaleX(200);
    DeleteConfirmEdit.OnChange := @DeleteConfirmChange;
    DeleteConfirmOk := TNewButton.Create(Form);
    DeleteConfirmOk.Parent := Form;
    DeleteConfirmOk.Caption := CustomMessage('deleteButton');
    DeleteConfirmOk.Left := Form.ClientWidth - ScaleX(80 + 6 + 80 + 14);
    DeleteConfirmOk.Top := Form.ClientHeight - ScaleY(25 + 14);
    DeleteConfirmOk.Width := ScaleX(80);
    DeleteConfirmOk.Height := ScaleY(25);
    DeleteConfirmOk.ModalResult := mrOk;
    DeleteConfirmOk.Enabled := False;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := CustomMessage('cancelButton');
    CancelButton.Left := Form.ClientWidth - ScaleX(80 + 14);
    CancelButton.Top := Form.ClientHeight - ScaleY(25 + 14);
    CancelButton.Width := ScaleX(80);
    CancelButton.Height := ScaleY(25);
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    { The one form whose focus is not its default button: Delete is disabled until the word is typed
      (and is deliberately not the default), so the edit box where the word goes holds the focus. }
    Form.ActiveControl := DeleteConfirmEdit;
    Form.FlipAndCenterIfNeeded(True, nil, False);
    Result := (Form.ShowModal = mrOk) and (DeleteConfirmEdit.Text = 'DELETE');
  finally
    Form.Free;
  end;
end;

function InitializeUninstall: Boolean;
var
  Wanted: Boolean;
begin
  Result := True;
  if not SetEnvironmentVariableTextW('COGNITA_LANG', SetupLocaleTag) then
    Log('uninstall: could not set COGNITA_LANG for helper processes');
  UninstallDeleteData := False;
  UninstallDeleteFailed := False;
  HelperFolder := ExpandConstant('{app}');
  RunMode := 0;
  if UninstallSilent then
  begin
    Log('uninstall: silent uninstall always keeps data');
    Exit;
  end;
  { The saved location and size for the wording; a failure here only loses the numbers. }
  if RunHelper('state', '') then
    ReadState
  else
    Log('uninstall: could not read state; the delete form will not show a size');
  if not AskUninstallChoice(Wanted) then
  begin
    Log('uninstall: cancelled at the first form');
    Result := False;
    Exit;
  end;
  if Wanted then
  begin
    if not ConfirmDelete then
    begin
      Log('uninstall: cancelled at the DELETE confirmation');
      Result := False;
      Exit;
    end;
    UninstallDeleteData := True;
  end;
  Log('uninstall: delete_data=' + IntToStr(Ord(UninstallDeleteData)));
end;

procedure OnUninstallLine(const S: String; const Error, FirstLine: Boolean);
var
  L, DisplayTitle: String;
begin
  L := Trim(S);
  if L = '' then
    Exit;
  OnHelperLine(S, Error, FirstLine);
  if (L[1] = '{') and (CurTitle <> '') then
  begin
    DisplayTitle := JsonField(L, 'title_display');
    if DisplayTitle = '' then
      DisplayTitle := CurTitle;
    UninstallProgressForm.StatusLabel.Caption := DisplayTitle;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Flag, Text, FailReasonText: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { Before Inno deletes app\ (the helper lives there): stop Cognita, remove the task, and
      keep or delete the data (section 7.5). }
    if UninstallDeleteData then
      Flag := '--delete-data'
    else
      Flag := '--keep-data';
    ResetRun;
    HelperFolder := ExpandConstant('{app}');
    Log('uninstall: running the helper: uninstall ' + Flag);
    Code := -1;
    try
      ExecAndLogOutput(PsExe, '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + QuoteArg(HelperPath) +
        ' uninstall ' + Flag, '', SW_HIDE, ewWaitUntilTerminated, Code, @OnUninstallLine);
    except
      Log('uninstall: exception while running the helper: ' + GetExceptionMessage);
    end;
    HelperExit := Code;
    Log('uninstall: helper exit=' + IntToStr(Code) + ' last line: ' + LastResult);
    JudgeResult;
    { A delete-data run copies the helper logs to %TEMP% (it deletes the Cognita folder) and says where. }
    UninstLogsCopy := ResultValue(LastResult, 'logs_copy');
    if LastStatus <> 'ok' then
    begin
      EnsureFailureText;
      FailReasonText := ResultValue(LastResult, 'reason');
      { Design 19.11 R5: "its data could not be deleted" is true ONLY for the failed `wsl --unregister`
        (reason=unregister-failed). Any other failure of a delete-data run (the Linux uninstall, the task,
        a delete of a known item) says something else went wrong, so it gets the "part of the cleanup did
        not finish" box below with the failure text, like a keep-data run. }
      Log('uninstall: helper did not finish ok: status=' + LastStatus + ' reason=[' + FailReasonText + '] delete_data=' +
        IntToStr(Ord(UninstallDeleteData)));
      if UninstallDeleteData and (FailReasonText = 'unregister-failed') then
      begin
        { Design 19.4 item 9: a delete-data run that failed (`wsl --unregister` did not work) has already
          saved state=uninstalled, and Inno removes Cognita's own files regardless, so Cognita IS removed;
          only its data was not deleted. The closing text comes from the result: it is shown once, at the
          end (usPostUninstall), instead of this box, so the user gets one closing message. The helper's
          own failure lines are in the log. }
        UninstallDeleteFailed := True;
        Log('uninstall: the data could not be deleted (helper status=' + LastStatus + ' exit=' + IntToStr(HelperExit) +
          ' logs_copy=[' + UninstLogsCopy + ']); the closing text will say so. ' + FailMessage);
      end
      else
        SuppressibleMsgBox(FmtMessage(CustomMessageWithLines('uninstallCleanupFailed'), [FailText, LogsDir]),
          mbError, MB_OK, IDOK);
    end;
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    RemoveBinFromPath;
    { The helper deletes %LOCALAPPDATA%\Cognita on a delete-data run, but it runs from app\ inside
      it, so that folder was still in use then; Inno has now removed app\, which leaves the parent
      empty (P2, 2026-09-29). RemoveDir only removes an EMPTY folder, so nothing else can go. }
    if UninstallDeleteData then
    begin
      if RemoveDir(ExpandConstant('{localappdata}\Cognita')) then
        Log('uninstall: removed the empty Cognita folder')
      else
        Log('uninstall: the Cognita folder was not removed (absent or not empty)');
    end;
    { Design 19.4 item 21: ONE closing box. Inno's own final message (UninstalledAll / UninstalledMost, set
      in [Messages] to "Cognita was removed. Your documents were not touched.") is the success text, so
      the box that used to say it here is gone. The one exception is a delete-data run whose data could
      not be deleted (item 9): that text is Setup's own, from the helper's result, and the user needs
      the log path in it. }
    if UninstallDeleteFailed then
    begin
      Text := UninstLogsCopy;
      if Text = '' then
        Text := LogsDir;
      Log('uninstall: closing text for a failed data delete; logs=' + Text);
      SuppressibleMsgBox(FmtMessage(CustomMessage('uninstallDataFailed'), [Text]), mbError, MB_OK, IDOK);
    end
    else
      Log('uninstall: closing text is Inno''s own (delete_data=' + IntToStr(Ord(UninstallDeleteData)) +
        ' data_dir=[' + StVhdDir + ']); no Setup box');
  end;
end;

{ The uninstaller's own log, kept the same way as Setup's (design 18.5). A keep-data uninstall leaves
  %LOCALAPPDATA%\Cognita\logs in place, so the log goes there. A delete-data uninstall has just
  removed that folder on purpose: the log goes beside the copy of the helper logs the helper made
  in %TEMP% (its logs_copy result), so the deletion leaves nothing behind in the Cognita folder. }
procedure DeinitializeUninstall;
var
  Dest: String;
begin
  if not UninstallDeleteData then
    Dest := LogsDir + '\uninstall-' + StampNow + '.log'
  else if UninstLogsCopy <> '' then
    Dest := UninstLogsCopy + '\uninstall-' + StampNow + '.log'
  else
    Dest := RemoveBackslash(GetEnv('TEMP')) + '\Cognita-uninstall-' + StampNow + '\uninstall.log';
  CopyOwnLog(Dest);
end;
