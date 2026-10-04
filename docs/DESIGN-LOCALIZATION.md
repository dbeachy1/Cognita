# Cognita operator localization design

Status: independently reviewed; console scope pending. Decision: Doug requested the
GlideSlope language set for Cognita on 2026-10-04. The selected locales are
U.S. English (`en-US`), Spanish (`es-ES`), French (`fr-FR`), German (`de-DE`),
Italian (`it-IT`), and Brazilian Portuguese (`pt-BR`). Doug confirmed on
2026-10-04 that these languages are required before Cognita's first public
release. The exact console scope is pending Doug's answer; this draft assumes
the everyday console UI is included.

## Authorized behavior and limits

An operator can use Cognita's Windows Setup, Admin login and dashboard, OAuth
sign-in and consent pages, and everyday Windows and Linux console commands in
any supported language. A visible language choice is available before sign-in
and in the Admin dashboard. Windows Setup uses its own language choice. An
upgrade says which installed version it is updating to and asks for the
current Admin password once; password changes remain the explicit `cognita
password` console command. The same functional choices, warnings, and security
meaning must be available in every supported language.

Names and values that are part of Cognita's external contracts stay stable:
CLI command names and flags, URLs, file paths, project and connector names,
configuration keys, MCP tool names and schemas, OAuth parameters, JSON keys,
machine-readable error reasons, and exit codes. Logs and diagnostic bundles
remain English so support records are comparable. This work does not translate
documentation, user documents, search results, OCR languages, or embedding
models. Multilingual document retrieval is a separate product capability.

The first release must not label a locale supported until its complete operator
journeys below pass in the built package on both relevant platforms. English is
the fallback for an unsupported locale or a missing translation. Missing keys
in a supported catalog are a build failure, so fallback is for older clients
and unexpected packaging gaps, not an acceptable release state.

## Current boundaries

- `windows/setup/Cognita.iss` owns the Windows wizard; `windows/build_setup.py`
  packages it. It currently has English custom pages and only English stock
  messages. The installed 15.2.0 Windows service is running, but this design
  must be tested against a newly built Setup rather than inferred from source.
- `src/cognita/web/index.html`, `login.html`, and `app.js` own the Admin browser
  UI. `admin_api.py::_render_page` already renders both pages per request for
  theme and version. `admin-state.js` holds route/state logic and must retain
  stable route IDs. The UI currently has no language catalog.
- `src/cognita/oauth_service/templates/` and `views.py` own the public OAuth
  pages. They use Django's template escaping but have English literal text;
  `settings.py` fixes `LANGUAGE_CODE` to English.
- `scripts/cognita_cli.py` owns Linux's operator command; the Windows launcher
  calls `windows/CognitaWin.ps1`. The helper emits structured progress, which
  Setup consumes. Keep those machine fields unchanged and localize only their
  presentation. Application and MCP service code must not acquire a global
  mutable locale.

## Language ownership and resources

Use stable message IDs, explicit named placeholders, and UTF-8 catalogs. The
English catalog defines the key and placeholder set. Keep distinct catalog
namespaces for Admin/OAuth, Windows Setup, and console messages when their text
serves different workflows; do not build a runtime translation service.

1. Browser and Python text use packaged JSON catalogs in
   `src/cognita/web/locales/<locale>.json`. The standard-library-only Linux
   host CLI loads this path relative to its source tree, while the application
   package loads it relative to `cognita`; neither depends on the other being
   installed. A small Python loader validates the locale, loads a catalog
   once, and formats named values without changing process locale. The browser
   loads its versioned catalog before `app.js`,
   translates static text and attributes by message ID, and calls `t(id,
   values)` for generated text. Dynamic values enter text nodes or existing
   escaped template fields, never HTML constructed from translation text.
   Package data explicitly includes the catalogs in the wheel, Linux source
   archive, WSL update tree, and built application image. Catalog keys and
   placeholder sets must match English; unsafe HTML and missing keys fail
   verification.
2. OAuth templates receive the selected catalog in request context. Views
   format their own variable-bearing messages and pass values to Django
   templates for normal escaping. Authentication and consent decisions do not
   depend on language. Django library errors that reach these pages get a
   reviewed user-facing mapping; raw machine errors remain available only in
   diagnostics. OAuth's current Content Security Policy forbids scripts and
   stays unchanged. A separate language form on each existing login and
   consent page submits to its existing view with CSRF protection. The view
   validates a whitelisted locale, sets the language cookie, and redirects
   back to the same validated local request without submitting credentials or
   granting/denying authorization. The consent view must preserve and
   revalidate the original authorization parameters before redirecting; an
   invalid or expired request cannot be revived by switching language.
3. Inno Setup uses its installed English, Spanish, French, German, Italian,
   and Brazilian Portuguese language files for stock wizard controls. Project
   text is stored in per-language `CustomMessages` entries and read through
   Inno's `CustomMessage`/`FmtMessage` calls. The Setup build fails when a
   custom message is missing for any of the six languages. `Cognita.iss` may
   retain non-user-facing English comments and protocol identifiers. Setup
   passes a validated locale with every `CognitaWin.ps1` invocation. The
   helper's stable progress stage, result and reason fields remain English;
   human presentation fields are rendered for that invocation locale. Setup
   displays those fields but writes English diagnostic fields to its log.
   When WSL work is invoked, the helper passes the same validated locale to
   the Linux command without putting a secret in an argument or environment.
   The RunOnce command preserves Inno's `/LANG=` choice with `/resume`, so a
   restart resumes the same language. Unknown locale input fails closed to
   English without changing a command's meaning.
4. The Linux and Windows console entry points use their platform's requested
   language to select the operator-message catalog. The Windows Setup includes
   the catalogs beside the temporary preinstall helper and copies them beside
   the installed helper; `windows/build_setup.py` verifies both package paths.
   A message-ID-aware UI method renders the chosen locale to the terminal or
   wizard and the English text to the existing log path. Linux's current `UI`
   writes the same text to terminal and log, so user-facing call sites must
   move to that method; Windows's `Write-ProgressLine` must retain English
   diagnostic fields while adding localized display fields. Do not translate
   an already assembled English sentence after the fact. The helper's
   structured result keys and progress stage IDs remain English. Do not add a
   second source of truth for release version or state.

5. The Admin browser currently presents server `detail` and `message` fields
   verbatim. Keep these fields, machine reasons and status codes unchanged for
   existing clients. Add an optional stable presentation ID and named,
   nonsensitive values to responses the Admin UI displays. The browser uses
   its catalog for known IDs, including both errors and successful
   explanations. For an unmapped response, it shows a localized generic
   action-failed or action-completed heading plus an English technical detail
   in a clearly labeled disclosure; it never guesses a translation from raw
   prose. Inventory and migrate the actually displayed endpoints, with a
   variable-bearing folder validation error and a successful explanation as
   initial contract proofs. Keep all inserted values in text nodes.

## Selection and formatting

For Admin and OAuth, a validated `cognita_lang` cookie wins, then the browser's
`Accept-Language`, then English. Match exact supported tags first. Regionless
`es`, `fr`, `de`, and `it` resolve to the supported regional locale; `pt-BR`
resolves to Brazilian Portuguese. Other Portuguese variants fall back to
English because they are not translated and reviewed. The language selector
offers the six names in their own languages. Admin's browser UI sets the
cookie and reloads the current safe page. OAuth uses the CSRF-protected
no-script form above and sets the same cookie on its own origin; a public OAuth
origin can therefore have a separate choice from local Admin. The cookie is
host-only, SameSite=Lax, and Secure over HTTPS. This permits a choice before
sign-in without changing authentication state. Do not place passwords,
tokens, OAuth codes, or return URLs in language resources or logs. On every
render, set the HTML `lang` attribute to the resolved locale.

Inno's language selector controls one Setup run. Its custom text follows that
selection, including resumed setup and uninstall. `ActiveLanguage()` maps to
the explicit allowed BCP 47 tag carried to the helper; `/LANG=` is copied to
the bounded RunOnce resume command. Linux uses `LC_ALL`, then
`LC_MESSAGES`, then `LANG`; Windows uses the current UI culture. An optional
`COGNITA_LANG` override can help operators and tests select a supported locale
without changing OS settings. Invalid values fall back to English. Command
syntax remains English and examples remain copyable.

Use browser `Intl` APIs for Admin dates, times, quantities, and plural rules.
Use explicit locale formatting in Python where a localized console or OAuth
message embeds a number or date; never call process-wide `setlocale`. Keep
stored timestamps and numeric values unchanged. The translator receives
separate values, not preassembled English fragments. Layouts must permit longer
translations, wrap safely, and retain keyboard and screen-reader labels.

## Execution sequence and earliest proofs

1. Establish a baseline: current English Admin/OAuth and CLI workflows, plus
   the compiled Windows Setup and current Windows status. Inventory every
   visible literal and every server detail surfaced verbatim in these journeys.
   Record exceptions, especially technical protocol and log text.
2. Add locale resolver/catalog validation and package data. Prove a packaged
   wheel and container can load all six catalogs, and that the standard-library
   host CLI loads them from an extracted source archive without the service
   package installed. Prove both temporary and installed Windows helpers can
   load the same catalog. Source checkout tests alone do not prove this. Add
   focused tests for selection, fallback, placeholders, escaping, and explicit
   formatting.
3. Localize Admin login, dashboard, generated dialogs, errors, and accessibility
   labels. Verify language switching before sign-in and after sign-in across
   refresh and route changes. A non-English Admin journey must include one
   variable-bearing server validation error and one successful server
   explanation. Browser tests cover the real built assets and narrow and wide
   layouts for all six languages.
4. Localize OAuth sign-in and consent using the same locale decision, including
   denial, bad credentials, missing connector, and expired/invalid request
   cases. Exercise language switching before login and on consent through the
   real OAuth proxy without granting new access or relaxing the CSP.
5. Localize Setup's stock and custom text. Keep the one-entry current-password
   upgrade behavior and explicit reset command. First prove a non-English
   selection on an English OS through one relayed warning/failure and a WSL
   restart/resumed wizard; this is the earliest integration proof for the
   helper and RunOnce boundary. Compile a real Setup package,
   check every mode's page text and confirmation, then exercise fresh install,
   same-version repair, older-version upgrade, and retained-data reinstall in
   disposable Windows test state. Do not use the personal installed Cognita
   instance as a disposable fixture.
6. Localize everyday Linux and Windows console help, status, password, start,
   stop, update/repair, rollback, remote access, and recovery messages. Run
   representative success and failure paths in each locale while confirming
   command syntax, structured output, log redaction, and exit codes are stable.
   At least one localized failure must leave an English diagnostic record.
7. Obtain independent translation review for security and destructive-action
   language, then run packaged Windows and Linux end-to-end acceptance. A
   missing or misleading translation blocks claiming that locale supported.

No database migration or new network endpoint is required. Existing English
clients and protocol contracts remain compatible. A prior release remains the
rollback path if the candidate fails; changing the browser language back to
English is a local UI recovery action. The release version changes when this
code is deployed under the project's version rule. Publishing remains a
separate privacy/security gate; the current source repository stays private
until the approved public-copy workflow completes.

## Review record

The fresh Sol High reviewer `/root/localization_design_review` reviewed this
design against source HEAD `367b17f4ad6e93df4a6aec49253605f0b2a468a6`
on 2026-10-04. The first reviewed draft hash was
`AC0AF0D65096E8A3A4A5845CC01625223DC21CFD7B5818F543D6940ADA7D9720`.
It found four blocking gaps: OAuth's no-script language selection; Setup locale
propagation through its helper, WSL, and resume; dependency-free host catalog
packaging and English diagnostic logging; and Admin presentation of
server-originated messages. The same reviewer accepted the revised design
hash `41E933FD9A45BB5BC867B4F8B24B3196DC32A3E2A01D00E2821FBE75EF618D55`
with all four findings closed. The packaging and integration proofs in the
execution sequence remain acceptance gates. Console implementation waits for
Doug's scope answer. Material boundary changes return to this reviewer;
ordinary local string and layout choices do not reopen design review.
